"""Run one local POS benchmark in a fresh Docker Compose project."""

import argparse
import csv
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from analyze import summarize
from trial_data import prepare_trial_data


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MODES = {"A": "postgres", "B": "ttl", "C": "ttl-invalidate", "D": "ttl-fenced"}
SERVICES = ("postgres", "redis", "api", "bench-k6")
DOCKER = ["docker"]
BASE = DOCKER + ["compose", "-f", "compose.yaml"]
PSQL = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", "pos_admin", "-d", "pos"]
HEALTH = (
    "fetch('http://127.0.0.1:3000/healthz', {signal: AbortSignal.timeout(1500)})"
    ".then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"
)


class TrialError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def command(args, *, env=None, data=None, timeout=120):
    try:
        input_options = {"input": data} if data is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True,
                              timeout=timeout, check=False, **input_options)
    except FileNotFoundError as exc:
        raise TrialError(f"Command unavailable: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TrialError(f"{args[0]} command exceeded {timeout} seconds") from exc


def checked(args, label, *, env=None, data=None, log=None, timeout=120):
    result = command(args, env=env, data=data, timeout=timeout)
    if log:
        with log.open("a", encoding="utf-8") as stream:
            stream.write(result.stdout + result.stderr)
    if result.returncode:
        if log:
            raise TrialError(f"{label} failed (exit {result.returncode}). See {log.name}.")
        details = result.stderr.strip().splitlines()
        raise TrialError(f"{label} failed: {details[-1] if details else result.returncode}")
    return result.stdout


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def harness_hash():
    digest = hashlib.sha256()
    for path in sorted(HERE.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".js", ".yaml", ".sql"}:
            digest.update(str(path.relative_to(HERE)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def local_hardware():
    keys = ("hw.model", "hw.memsize", "hw.ncpu", "machdep.cpu.brand_string")
    info = {}
    if platform.system() == "Darwin":
        for key in keys:
            result = command(["sysctl", "-n", key])
            if result.returncode == 0:
                info[key] = result.stdout.strip()
    return {
        "os": platform.platform(), "machine": platform.machine(),
        "logical_cpus": os.cpu_count(), "python": platform.python_version(),
        **info,
    }


def check_isolation(model, project):
    services = model.get("services", {})
    for category in ("networks", "volumes"):
        for logical_name, resource in (model.get(category) or {}).items():
            definition = resource or {}
            if definition.get("external") or not definition.get("name", "").startswith(project + "_"):
                raise TrialError(f"{category[:-1]} {logical_name} is outside the trial project.")
            if category == "volumes" and (definition.get("driver") not in {None, "local"}
                                         or definition.get("driver_opts")):
                raise TrialError(f"Volume {logical_name} uses a custom storage driver or options.")

    for name in SERVICES:
        service = services[name]
        if service.get("network_mode") or service.get("container_name"):
            raise TrialError(f"Use project-scoped networking and container names for {name}.")
        if name in {"postgres", "redis"}:
            for mount in service.get("volumes", []):
                if mount["type"] == "bind" and not mount.get("read_only"):
                    raise TrialError(f"{name} has a writable host bind mount. Use a trial-local volume.")
    environment = services["api"].get("environment") or {}
    for name in ("DATABASE_URL", "POSTGRES_URL"):
        value = environment.get(name)
        if value:
            url = urlsplit(value)
            if (url.hostname, url.path, url.username) != ("postgres", "/pos", "pos_app"):
                raise TrialError(f"Configure {name} to use pos_app on postgres/pos inside the trial.")


def docker_setup(settings, project, mode):
    services = checked(BASE + ["config", "--services"], "Base Compose configuration").split()
    missing = {"postgres", "redis", "api"} - set(services)
    if missing:
        raise TrialError("compose.yaml needs these backend services: " + ", ".join(sorted(missing))
                         + ". Run from the root of the research package.")
    checked(DOCKER + ["info", "--format", "{{.ServerVersion}}"], "Docker engine")
    env = {
        **os.environ,
        "POSBENCH_UID": str(os.getuid()), "POSBENCH_GID": str(os.getgid()),
        "POSBENCH_API_IMAGE": settings["api_image"],
        "POSBENCH_MODE": MODES[mode], "POSBENCH_TTL_SECONDS": str(settings["cache_ttl_s"]),
    }
    cli = BASE + ["-f", "benchmarks/compose.bench.yaml", "-p", project, "--profile", "benchmark"]
    model = json.loads(checked(cli + ["config", "--format", "json"], "Benchmark Compose configuration", env=env))
    check_isolation(model, project)
    checked(DOCKER + ["image", "inspect", "--format", "{{.Id}}", settings["api_image"]],
            "Prebuilt API image. Build it with the command in benchmarks/README.md")
    return cli, env, model


def sample_cpu(folder, ids, stop):
    path = folder / "cpu.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("sample_started_at", "sample_finished_at", "service", "container_id",
                         "cpu_percent", "memory_usage"))
        while not stop.wait(2):
            started = now()
            try:
                result = command(DOCKER + ["stats", "--no-stream", "--no-trunc", "--format", "{{json .}}",
                                           *ids.values()], timeout=8)
                if result.returncode:
                    raise TrialError(result.stderr.strip())
                ended = now()
                for line in result.stdout.splitlines():
                    row = json.loads(line)
                    container_id = row["ID"]
                    service = next((name for name, cid in ids.items() if cid.startswith(container_id)
                                    or container_id.startswith(cid)), "")
                    writer.writerow((started, ended, service, container_id, row["CPUPerc"], row["MemUsage"]))
                stream.flush()
            except (TrialError, KeyError, ValueError) as exc:
                with (folder / "cpu-errors.log").open("a", encoding="utf-8") as errors:
                    errors.write(f"{now()} {exc}\n")


class Trial:
    def __init__(self, args, settings, settings_path, cli, env, model, project):
        self.args = args
        self.settings = settings
        self.cli = cli
        self.env = env
        self.model = model
        self.project = project
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.run_id = args.id or f"{args.profile}-{args.workload}-{args.vus}vu-r{args.repeat}-{args.mode}-{stamp}"
        self.folder = ROOT / "results" / "runs" / self.run_id
        self.folder.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(settings_path, self.folder / "settings.json")
        profile = settings["profiles"][args.profile]
        self.warmup = 0 if args.workload == "cold" else profile["warmup_s"]
        time_key = {"cold": "cold_s", "freshness": "freshness_s"}.get(args.workload, "measure_s")
        self.duration = profile[time_key]
        self.seed = settings["workload_seed"] + args.repeat - 1
        self.metadata = {
            "run_id": self.run_id, "started_at": now(), "status": "starting",
            "project": project, "execution": "docker_compose", "profile": args.profile, "workload": args.workload,
            "mode": args.mode, "cache_mode": MODES[args.mode], "cache_ttl_s": settings["cache_ttl_s"],
            "vus": args.vus, "repeat": args.repeat, "workload_seed": self.seed,
            "warmup_s": self.warmup, "measure_s": self.duration, "cpu_sampling": args.cpu,
            "hardware": local_hardware(), "settings_sha256": file_hash(settings_path),
            "compose_sha256": file_hash(ROOT / "compose.yaml"), "harness_sha256": harness_hash(),
            "app_pool_max_configured": model["services"]["api"]["environment"].get("PG_POOL_MAX"),
            "free_bytes_at_start": shutil.disk_usage(self.folder).free,
        }
        self.ids = {}
        self.started_services = False
        self.write_metadata()

    def write_metadata(self):
        save_json(self.folder / "metadata.json", self.metadata)

    def compose(self, args, label, *, data=None, log=None, timeout=120):
        return checked(self.cli + args, label, env=self.env, data=data, log=log, timeout=timeout)

    def execute(self, service, args, label, *, data=None):
        return self.compose(["exec", "-T", service, *args], label, data=data)

    def sql(self, sql):
        return self.execute("postgres", PSQL, "PostgreSQL query", data=sql)

    def wait_for_api(self):
        deadline = time.monotonic() + 20
        args = self.cli + ["exec", "-T", "api", "node", "-e", HEALTH]
        while time.monotonic() < deadline:
            if command(args, env=self.env, timeout=5).returncode == 0:
                health = self.api_json("/healthz")
                expected = {"mode": MODES[self.args.mode], "ttl": self.settings["cache_ttl_s"],
                            "poolMax": 10}
                if health != expected:
                    raise TrialError(f"API configuration differs from the trial: {health}.")
                return
            time.sleep(1)
        raise TrialError("The API did not return a successful GET /healthz on port 3000.")

    def api_json(self, path):
        script = ("fetch('http://127.0.0.1:3000'+process.argv[1])"
                  ".then(r => {if(!r.ok) throw Error(r.status); return r.json();})"
                  ".then(data => console.log(JSON.stringify(data)))")
        return json.loads(self.execute("api", ["node", "-e", script, path], "API snapshot"))

    def snapshot(self, suffix):
        folder = self.folder
        sql = (HERE / "sql" / "pg_snapshot.sql").read_text()
        (folder / f"pg_{suffix}.csv").write_text(self.sql(sql), encoding="utf-8")
        info = self.execute("redis", ["redis-cli", "--raw", "INFO", "server", "stats", "commandstats"],
                            "Redis statistics")
        keys = self.execute("redis", ["redis-cli", "--raw", "DBSIZE"], "Redis DB 0 size").strip()
        int(keys)
        (folder / f"redis_{suffix}.txt").write_text(info + f"\ndb0_key_count:{keys}\n", encoding="utf-8")
        save_json(folder / f"api_{suffix}.json", self.api_json("/metrics"))
        return int(keys)

    def capture_environment(self):
        self.metadata["docker_versions"] = checked(DOCKER + ["version", "--format", "{{json .}}"],
                                                   "Docker versions").strip()
        self.metadata["compose_version"] = checked(DOCKER + ["compose", "version", "--short"],
                                                   "Compose version").strip()
        self.metadata["docker_resources"] = checked(
            DOCKER + ["info", "--format", "{{.NCPU}} CPUs | {{.MemTotal}} bytes | {{.Architecture}}"],
            "Docker VM resources").strip()
        self.metadata["k6_version"] = self.execute("bench-k6", ["k6", "version"], "k6 version").strip()
        self.metadata["postgres_version"] = self.execute(
            "postgres", PSQL + ["-Atc", "SHOW server_version"], "PostgreSQL version").strip()
        self.metadata["redis_version"] = self.execute(
            "redis", ["redis-server", "--version"], "Redis version").strip()
        self.metadata["node_version"] = self.execute("api", ["node", "--version"], "Node version").strip()
        try:
            deps = command(self.cli + ["exec", "-T", "api", "npm", "ls", "--depth=0", "--json"],
                           env=self.env, timeout=20)
            versions = json.loads(deps.stdout).get("dependencies", {})
        except (TrialError, ValueError):
            versions = {}
        self.metadata["api_package_versions"] = {
            key: versions.get(key, {}).get("version") for key in ("express", "pg", "redis")
        }
        lockfile = ROOT / "backend" / "package-lock.json"
        self.metadata["api_package_lock_sha256"] = file_hash(lockfile) if lockfile.is_file() else None
        images = {}
        for name in SERVICES:
            container_id = self.compose(["ps", "-q", name], f"{name} container ID").strip()
            if not container_id or "\n" in container_id:
                raise TrialError(f"Expected one container for {name}.")
            self.ids[name] = container_id
            image = checked(DOCKER + ["inspect", "--format", "{{.Image}}", container_id],
                            f"{name} image ID").strip()
            details = checked(DOCKER + ["image", "inspect", "--format",
                                        "{{.Os}}/{{.Architecture}} {{json .RepoDigests}}", image],
                              f"{name} image details").strip()
            images[name] = {"id": image, "platform_and_repo_digests": details}
        self.metadata["images"] = images
        git = shutil.which("git")
        if git:
            commit = command([git, "rev-parse", "HEAD"])
            if commit.returncode == 0:
                self.metadata["git_head"] = commit.stdout.strip()
                self.metadata["git_dirty"] = bool(command([git, "status", "--porcelain"]).stdout.strip())
        indexes = """COPY (
            SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname = 'public'
            ORDER BY tablename, indexname
        ) TO STDOUT WITH (FORMAT csv, HEADER true);"""
        (self.folder / "pg_indexes.csv").write_text(self.sql(indexes), encoding="utf-8")
        pg_settings = """COPY (
            SELECT name, setting, unit FROM pg_settings
            WHERE name IN ('shared_buffers', 'max_connections', 'shared_preload_libraries',
                           'compute_query_id', 'pg_stat_statements.track', 'work_mem',
                           'effective_cache_size', 'track_io_timing')
            ORDER BY name
        ) TO STDOUT WITH (FORMAT csv, HEADER true);"""
        (self.folder / "pg_settings.csv").write_text(self.sql(pg_settings), encoding="utf-8")

    def start(self):
        self.started_services = True
        self.compose(["up", "-d", "--no-build", "--no-deps", "--wait", "--wait-timeout", "90",
                      "postgres", "redis", "bench-k6"], "Start data services and k6",
                     log=self.folder / "startup.log", timeout=300)
        print(f"Loading trial data for {self.run_id}", flush=True)
        seed_sql, trial_data = prepare_trial_data(self.settings)
        (self.folder / "seed.sql").write_text(seed_sql, encoding="utf-8")
        save_json(self.folder / "trial_data.json", trial_data)
        self.metadata["dataset_sha256"] = trial_data["sql_sha256"]
        self.compose(["exec", "-T", "postgres", *PSQL], "Loading trial data",
                     data=seed_sql + "\nANALYZE;\n", log=self.folder / "startup.log")
        totals = "SELECT " + ", ".join(f"(SELECT count(*) FROM {name})" for name in trial_data["counts"])
        observed = self.execute("postgres", PSQL + ["-Atc", totals], "Check trial dataset table counts").strip()
        if observed != "|".join(str(n) for n in trial_data["counts"].values()):
            raise TrialError("The trial database did not start with the expected actual trial dataset.")
        connect = ('PGPASSWORD="$POS_APP_PASSWORD" exec psql -X -h 127.0.0.1 -U pos_app '
                   '-d pos -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM menu_items"')
        self.execute("postgres", ["sh", "-c", connect], "Check PostgreSQL pos_app login")
        self.compose(["up", "-d", "--no-build", "--no-deps", "--wait", "--wait-timeout", "90", "api"],
                     "Start the prebuilt API", log=self.folder / "startup.log", timeout=120)
        self.wait_for_api()
        self.capture_environment()
        self.write_metadata()

    def phase(self, phase, seconds, workload, cpu=False):
        entry = "freshness.js" if workload == "freshness" else "performance.js"
        root = f"/results/{self.run_id}"
        variables = {
            "POS_SETTINGS": f"{root}/settings.json", "POS_TRIAL_DATA": f"{root}/trial_data.json",
            "POS_VUS": str(self.args.vus), "POS_PHASE": phase, "POS_WORKLOAD": workload,
            "POS_WORKLOAD_SEED": str(self.seed), "POS_DURATION_S": str(seconds),
        }
        args = [*self.cli, "exec", "-T"]
        for key, value in variables.items():
            args.extend(["-e", f"{key}={value}"])
        args.extend(["bench-k6", "k6", "run", "--quiet", "--summary-mode=disabled",
                     "--out", f"json={root}/{phase}.ndjson.gz", f"/benchmarks/k6/{entry}"])
        print(f"{self.run_id} | {phase} | {seconds}s", flush=True)
        stop = threading.Event()
        sampler = threading.Thread(target=sample_cpu, args=(self.folder, self.ids, stop)) if cpu else None
        with (self.folder / f"{phase}.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(args, cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT)
            if sampler:
                sampler.start()
            try:
                code = process.wait(timeout=seconds + self.settings["graceful_stop_s"] + 45)
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                process.kill()
                process.wait()
                raise
            finally:
                stop.set()
                if sampler:
                    sampler.join()
        if code:
            raise TrialError(f"k6 {phase} failed (exit {code}). See {phase}.log.")

    def audit(self):
        sql = (HERE / "sql" / "inventory_audit.sql").read_text()
        audit = self.sql(sql)
        (self.folder / "inventory_audit.csv").write_text(audit, encoding="utf-8")
        rows = list(csv.DictReader(audit.splitlines()))
        stock = self.settings["stock_per_ingredient"]
        mismatches = [int(row["ingredient_id"]) for row in rows
                      if int(row["on_hand"]) != stock - int(row["units_deducted"])
                      or int(row["revision"]) != int(row["expected_revision"])]
        orders = self.sql("COPY (SELECT * FROM orders ORDER BY id) TO STDOUT WITH (FORMAT csv, HEADER true);")
        with gzip.open(self.folder / "orders.csv.gz", "wt", encoding="utf-8", newline="") as stream:
            stream.write(orders)
        total = self.execute("postgres", PSQL + ["-Atc",
            "SELECT count(*), count(*) FILTER (WHERE stock_version_after IS NULL "
            "OR sellable_units_after IS NULL) FROM orders"], "Order receipt audit").strip()
        committed, incomplete = map(int, total.split("|"))
        return {
            "valid": len(rows) == 150 and not mismatches and incomplete == 0,
            "ingredient_count": len(rows), "mismatched_ingredient_ids": mismatches,
            "committed_orders": committed, "incomplete_receipts": incomplete,
        }

    def run(self):
        try:
            self.start()
            if self.warmup:
                self.phase("warmup", self.warmup, "read")
            keys = self.snapshot("before")
            if self.args.workload == "cold" and keys:
                raise TrialError("The empty-Redis trial began with keys already in DB 0.")
            self.metadata["measured_command_started_at"] = now()
            self.phase("measured", self.duration, self.args.workload, self.args.cpu)
            self.metadata["measured_command_finished_at"] = now()
            self.snapshot("after")
            self.metadata["inventory_audit"] = self.audit()
            result = summarize(self.folder, self.metadata, self.settings)
            save_json(self.folder / "summary.json", result)
            if result["requests"][0]["started"] == 0:
                raise TrialError("The measured phase recorded zero request starts.")
            self.metadata["status"] = "complete"
            print(f"Read {self.folder.relative_to(ROOT)}/summary.json", flush=True)
        except (Exception, KeyboardInterrupt) as exc:
            self.metadata["status"] = "failed"
            self.metadata["error"] = str(exc) or type(exc).__name__
            raise
        finally:
            try:
                self.cleanup()
            finally:
                self.metadata["finished_at"] = now()
                self.write_metadata()

    def cleanup(self):
        if not self.started_services:
            return
        try:
            logs = command(self.cli + ["logs", "--no-color", "api", "postgres", "redis"], env=self.env, timeout=20)
            (self.folder / "services.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        except TrialError as exc:
            (self.folder / "services.log").write_text(str(exc) + "\n", encoding="utf-8")
        try:
            down = command(self.cli + ["down", "-v"], env=self.env, timeout=45)
            self.metadata["cleanup_exit"] = down.returncode
            (self.folder / "cleanup.log").write_text(down.stdout + down.stderr, encoding="utf-8")
        except TrialError as exc:
            self.metadata["cleanup_exit"] = None
            (self.folder / "cleanup.log").write_text(str(exc) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate the Compose model and prebuilt image.")
    parser.add_argument("--profile", choices=("pilot", "formal"), default="pilot")
    parser.add_argument("--mode", choices=MODES, default="A")
    parser.add_argument("--workload", choices=("read", "mixed", "freshness", "cold"), default="mixed")
    parser.add_argument("--vus", type=int, default=2)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--settings", type=Path, default=HERE / "settings.json")
    parser.add_argument("--id", help="Result directory name. Defaults to a unique timestamped name.")
    parser.add_argument("--cpu", action="store_true", help="Collect optional container CPU samples.")
    args = parser.parse_args()
    if args.mode == "D":
        parser.error("D uses the revised R2 protocol. See benchmarks/README.md.")
    if args.id and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", args.id)):
        parser.error("--id must start with a letter or digit and use only letters, digits, dots, _ or -")

    try:
        settings_path = args.settings.resolve()
        settings = json.loads(settings_path.read_text())
        profile = settings["profiles"][args.profile]
        if args.vus not in profile["vus"] and not args.check:
            parser.error(f"{args.profile} VUs are {profile['vus']}")
        if not 1 <= args.repeat <= settings["repetitions"]:
            parser.error("--repeat is outside the configured repetition range")
        for key in ("menu_select_regex", "extra_read_regex"):
            if settings[key]:
                re.compile(settings[key])
        if min(settings["think_ms"], settings["timeout_ms"], settings["probe_period_ms"]) <= 0:
            parser.error("Think time, request timeout, and probe period must be positive.")
        if settings["graceful_stop_s"] * 1000 < 3 * settings["timeout_ms"]:
            parser.error("Grace time must accommodate the three-request freshness probe.")
        project = "posbench-" + uuid.uuid4().hex[:12]
        cli, env, model = docker_setup(settings, project, args.mode)
        if args.check:
            print("Compose model, isolated resources, Docker engine, and API image checks passed.")
            return 0
        trial = Trial(args, settings, settings_path, cli, env, model, project)
        trial.run()
    except (TrialError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Benchmark stopped. {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Benchmark interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
