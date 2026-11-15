COPY (
    SELECT 'info'::text AS row_type, NULL::text AS queryid, NULL::bigint AS calls,
           NULL::bigint AS rows, NULL::double precision AS total_exec_ms,
           NULL::text AS query, stats_reset::text, dealloc
    FROM pg_stat_statements_info
    UNION ALL
    SELECT 'statement', queryid::text, calls, rows, total_exec_time, query, NULL, NULL
    FROM pg_stat_statements
    WHERE toplevel
      AND userid = (SELECT oid FROM pg_roles WHERE rolname = 'pos_app')
      AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    ORDER BY row_type, queryid
) TO STDOUT WITH (FORMAT csv, HEADER true);
