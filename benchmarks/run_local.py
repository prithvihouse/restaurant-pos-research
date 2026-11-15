"""Run the exploratory profile against disposable loopback services on Linux."""

import argparse
import csv
import gzip
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from analyze import summarize
from matrix import plan
from run_trial import HERE, MODES, ROOT, TrialError, checked, file_hash, harness_hash, now, save_json
from trial_data import prepare_trial_data

SETTINGS = HERE / "settings.json"
PROFILE = "exploratory"
BINARIES = ("node", "k6", "initdb", "postgres", "psql", "pg_isready", "redis-server", "redis-cli")
INDEXES = """COPY (
    SELECT tablename, indexname, indexdef FROM pg_indexes
    WHERE schemaname = 'public' ORDER BY tablename, indexname
) TO STDOUT WITH (FORMAT csv, HEADER true);"""
PG_SETTINGS = """COPY (
    SELECT name, setting, unit FROM pg_settings
    WHERE name IN ('shared_buffers', 'max_connections', 'shared_preload_libraries',
                   'compute_query_id', 'pg_stat_statements.track', 'work_mem',
                   'effective_cache_size', 'track_io_timing') ORDER BY name
) TO STDOUT WITH (FORMAT csv, HEADER true);"""


def loopback_ports():
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(3)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]


def request(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(base + path, data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)


def wait_ready(process, check, label):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and process.poll() is None:
        try:
            if check():
                return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise TrialError(f"{label} did not start. Inspect its saved log.")


def hardware():
    cpu_info = Path("/proc/cpuinfo").read_text()
    brand = next((row.partition(":")[2].strip() for row in cpu_info.splitlines()
                  if row.startswith("model name")), platform.processor())
    meminfo = Path("/proc/meminfo").read_text().splitlines()[0]
    cgroup = {}
    for name in ("cpu.max", "memory.max"):
        path = Path("/sys/fs/cgroup") / name
        if path.exists():
            cgroup[name] = path.read_text().strip()
    return {
        "os": platform.platform(), "machine": platform.machine(), "cpu_model": brand,
        "os_cpu_count": os.cpu_count(), "affinity_cpu_count": len(os.sched_getaffinity(0)),
        "meminfo": meminfo, "cgroup_reported": cgroup, "python": platform.python_version(),
    }


def versions():
    commands = {
        "node": ["node", "--version"], "postgres": ["postgres", "--version"],
        "redis": ["redis-server", "--version"], "k6": ["k6", "version"],
    }
    found = {name: checked(cmd, name + " version").strip() for name, cmd in commands.items()}
    js = ("const load=require('node:module').createRequire(process.argv[1]); "
          "console.log(JSON.stringify(Object.fromEntries(['express','pg','redis']"
          ".map(k=>[k,load(k+'/package.json').version]))))")
    deps = checked(["node", "-e", js, str(ROOT / "backend/server.js")], "API dependencies")
    found["api_dependencies"] = json.loads(deps)
    return found


class Services:
    def __init__(self, stack, workspace, output, settings, mode, *, test_gate=False):
        self.stack = stack
        self.output = output
        self.pg_port, self.redis_port, api_port = loopback_ports()
        self.base = f"http://127.0.0.1:{api_port}"
        self.admin_password = secrets.token_hex(16)
        self.app_password = secrets.token_hex(16)
        password_file = workspace / "pg-password"
        password_file.write_text(self.admin_password + "\n")
        password_file.chmod(0o600)
        data_dir = workspace / "pg"
        checked(["initdb", "-D", str(data_dir), "--locale=C", "--encoding=UTF8", "-U", "pos_admin",
                 "--auth-host=scram-sha-256", "--auth-local=trust", "--pwfile", str(password_file)],
                "Initialize PostgreSQL", log=output / "setup.log", timeout=30)
        password_file.unlink()
        server = self.start("postgres", ["postgres", "-D", str(data_dir), "-p", str(self.pg_port),
                            "-h", "127.0.0.1", "-c", "unix_socket_directories=",
                            "-c", "shared_preload_libraries=pg_stat_statements", "-c", "timezone=UTC"])
        wait_ready(server, lambda: subprocess.run(
            ["pg_isready", "-q", "-h", "127.0.0.1", "-p", str(self.pg_port), "-U", "pos_admin"],
            check=False, timeout=2).returncode == 0, "PostgreSQL")
        self.sql("CREATE DATABASE pos;", database="postgres")
        self.sql((ROOT / "database/init/001-schema.sql").read_text())
        env = {
            **os.environ, "PGHOST": "127.0.0.1", "PGPORT": str(self.pg_port),
            "PGPASSWORD": self.admin_password, "POSTGRES_USER": "pos_admin",
            "POSTGRES_DB": "pos", "POS_APP_PASSWORD": self.app_password,
        }
        checked(["bash", str(ROOT / "database/init/002-app-role.sh")], "Create API role",
                env=env, log=output / "setup.log")
        seed, self.trial_data = prepare_trial_data(settings)
        (output / "seed.sql").write_text(seed)
        save_json(output / "trial_data.json", self.trial_data)
        self.sql(seed + "\nANALYZE;\n")
        cache = self.start("redis", ["redis-server", "--bind", "127.0.0.1",
                           "--port", str(self.redis_port), "--save", "", "--appendonly", "no",
                           "--dir", str(workspace)])
        wait_ready(cache, lambda: subprocess.run(
            ["redis-cli", "-p", str(self.redis_port), "PING"], capture_output=True,
            timeout=2, check=False).stdout == b"PONG\n", "Redis")
        self.api = self.start("api", ["node", str(ROOT / "backend/server.js")], env={
            **env, "PGDATABASE": "pos", "PGUSER": "pos_app", "PGPASSWORD": self.app_password,
            "PG_POOL_MAX": "10", "CACHE_MODE": MODES[mode], "CACHE_TTL_SECONDS": str(settings["cache_ttl_s"]),
            "REDIS_URL": f"redis://127.0.0.1:{self.redis_port}/0", "PORT": str(api_port),
            "BIND_HOST": "127.0.0.1",
            "POS_TEST_HOOKS": "1" if test_gate else "0",
        })
        wait_ready(self.api, lambda: request(self.base, "/healthz")[0] == 200, "API")
        status, health = request(self.base, "/healthz")
        if status != 200 or health != {"mode": MODES[mode], "ttl": settings["cache_ttl_s"], "poolMax": 10}:
            raise TrialError(f"Unexpected API configuration: {health}.")

    def start(self, name, command, env=None):
        log = self.stack.enter_context((self.output / f"{name}.log").open("w"))
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT)
        self.stack.callback(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def sql(self, sql, database="pos"):
        return checked(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1",
                        "-p", str(self.pg_port), "-U", "pos_admin", "-d", database], "PostgreSQL SQL",
                       env={**os.environ, "PGPASSWORD": self.admin_password}, data=sql, timeout=15)

    def redis(self, *args):
        return checked(["redis-cli", "--raw", "-h", "127.0.0.1", "-p", str(self.redis_port), *args],
                       "Redis CLI", timeout=5)

    def snapshot(self, suffix):
        folder = self.output
        (folder / f"pg_{suffix}.csv").write_text(self.sql((HERE / "sql/pg_snapshot.sql").read_text()))
        info = self.redis("INFO", "server", "stats", "commandstats")
        keys = int(self.redis("DBSIZE").strip())
        (folder / f"redis_{suffix}.txt").write_text(info + f"\ndb0_key_count:{keys}\n")
        save_json(folder / f"api_{suffix}.json", request(self.base, "/metrics")[1])

    def audit(self, initial_stock):
        audit = self.sql((HERE / "sql/inventory_audit.sql").read_text())
        (self.output / "inventory_audit.csv").write_text(audit)
        rows = list(csv.DictReader(audit.splitlines()))
        errors = [int(row["ingredient_id"]) for row in rows
                  if int(row["on_hand"]) != initial_stock - int(row["units_deducted"])
                  or int(row["revision"]) != int(row["expected_revision"])]
        orders = self.sql("COPY (SELECT * FROM orders ORDER BY id) TO STDOUT WITH (FORMAT csv, HEADER true);")
        with gzip.open(self.output / "orders.csv.gz", "wt", newline="") as stream:
            stream.write(orders)
        order_rows = list(csv.DictReader(orders.splitlines()))
        missing = sum(not row["stock_version_after"] or not row["sellable_units_after"] for row in order_rows)
        return {
            "valid": len(rows) == self.trial_data["counts"]["ingredients"] and not errors and missing == 0,
            "ingredient_count": len(rows), "mismatched_ingredient_ids": errors,
            "committed_orders": len(order_rows), "incomplete_receipts": missing,
        }


def phase(services, folder, label, workload, metadata, settings):
    duration = metadata["warmup_s" if label == "warmup" else "measure_s"]
    env = {
        **os.environ, "POS_BASE_URL": services.base, "POS_SETTINGS": str(folder / "settings.json"),
        "POS_TRIAL_DATA": str(folder / "trial_data.json"), "POS_VUS": str(metadata["vus"]),
        "POS_PHASE": label, "POS_WORKLOAD": workload, "POS_WORKLOAD_SEED": str(metadata["workload_seed"]),
        "POS_DURATION_S": str(duration), "K6_NO_USAGE_REPORT": "true",
    }
    script = "freshness.js" if workload == "freshness" else "performance.js"
    checked(["k6", "run", "--quiet", "--summary-mode=disabled",
             "--out", f"json={folder / (label + '.ndjson.gz')}", str(HERE / "k6" / script)],
            "k6 " + label, env=env, log=folder / (label + ".log"),
            timeout=duration + settings["graceful_stop_s"] + 20)


def run_trial(output, row, settings, environment):
    run_id, workload, vus, repeat, mode = row
    folder = output / run_id
    folder.mkdir()
    shutil.copyfile(SETTINGS, folder / "settings.json")
    profile = settings["profiles"][PROFILE]
    metadata = {
        "run_id": run_id, "execution": "local_process", "status": "starting", "started_at": now(),
        "profile": PROFILE, "workload": workload, "vus": vus, "repeat": repeat, "mode": mode,
        "cache_mode": MODES[mode], "cache_ttl_s": settings["cache_ttl_s"],
        "workload_seed": settings["workload_seed"] + repeat - 1,
        "warmup_s": profile["warmup_s"], "measure_s": profile["freshness_s" if workload == "freshness" else "measure_s"],
        "settings_sha256": file_hash(SETTINGS), "harness_sha256": harness_hash(),
        "api_sha256": file_hash(ROOT / "backend/server.js"), "app_pool_max_configured": 10,
        "api_package_lock_sha256": file_hash(ROOT / "backend/package-lock.json"), **environment,
    }
    save_json(folder / "metadata.json", metadata)
    print(f"Loading trial data for {run_id}", flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix="pos-trial-") as workspace, ExitStack() as stack:
            services = Services(stack, Path(workspace), folder, settings, mode)
            metadata["dataset_sha256"] = services.trial_data["sql_sha256"]
            (folder / "pg_indexes.csv").write_text(services.sql(INDEXES))
            (folder / "pg_settings.csv").write_text(services.sql(PG_SETTINGS))
            phase(services, folder, "warmup", "read", metadata, settings)
            services.snapshot("before")
            metadata["measured_command_started_at"] = now()
            phase(services, folder, "measured", workload, metadata, settings)
            metadata["measured_command_finished_at"] = now()
            services.snapshot("after")
            metadata["inventory_audit"] = services.audit(settings["stock_per_ingredient"])
        result = summarize(folder, metadata, settings)
        save_json(folder / "summary.json", result)
        if not result["requests"][0]["started"] or not metadata["inventory_audit"]["valid"]:
            raise TrialError("No starts or failed inventory audit.")
        metadata["status"] = "complete"
    except (Exception, KeyboardInterrupt) as exc:
        metadata["status"] = "failed"
        metadata["error"] = str(exc) or type(exc).__name__
        raise
    finally:
        metadata["finished_at"] = now()
        save_json(folder / "metadata.json", metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory for the measured trials")
    parser.add_argument("--one", choices=("A", "B", "C"), help="Only run one 10-VU mixed trial")
    args = parser.parse_args()
    if platform.system() != "Linux" or os.getuid() == 0:
        parser.error("Run as an unprivileged Linux user.")
    missing = [name for name in BINARIES if not shutil.which(name)]
    if missing:
        parser.error("Missing programs: " + ", ".join(missing))
    settings = json.loads(SETTINGS.read_text())
    output = args.output.resolve()
    environment = {"hardware": hardware(), "versions": versions()}
    output.mkdir(parents=True, exist_ok=False)
    rows = list(plan(settings, profile=PROFILE))
    if args.one:
        rows = [row for row in rows if row[1] == "mixed" and row[3:] == (1, args.one)]
    with (output / "plan.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("run_id", "workload", "vus", "repeat", "mode"))
        writer.writerows(rows)
    for row in rows:
        run_trial(output, row, settings, environment)


if __name__ == "__main__":
    main()
