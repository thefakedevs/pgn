"""Linux/Docker end-to-end checks. Uses an isolated temporary Compose project."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import psycopg

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("pgn", ROOT / "scripts/pgn.py")
pgn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pgn)
# Only this isolated fixture uses public test credentials. Preserve diagnostics
# here; production pgn deliberately suppresses connection strings in failures.
original_run = pgn.run
def fixture_run(*args, **kwargs):
    try:
        return original_run(*args, **kwargs)
    except pgn.ConfigError as exc:
        print(str(exc), flush=True)
        raise
pgn.run = fixture_run
DB = "pgn-access-test-db"
ADMIN = "postgres://postgres:test-password@localhost:55432/pgn"


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def wait_for(check, description, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except psycopg.OperationalError:
            pass
        time.sleep(1)
    raise AssertionError("Timed out: " + description)


def sql_checks(admin):
    # Global parent identity, routing to different time partitions, defaults.
    now = datetime.now(timezone.utc)
    with admin.cursor() as cur:
        cur.execute("INSERT INTO pgn.access_ingest (event_time,destination_host,protocol,status,node_id) VALUES (%s,'sql.example','tcp','accepted','sql'),(%s,'sql.example','udp','rejected','sql')",
                    (now, now - timedelta(days=1)))
        cur.execute("SELECT count(DISTINCT id), count(DISTINCT tableoid), bool_and(processed_at IS NULL) FROM pgn.access_logs")
        assert cur.fetchone() == (2, 2, True)
        cur.execute("SELECT is_identity, identity_generation FROM information_schema.columns WHERE table_schema='pgn' AND table_name='access_logs' AND column_name='id'")
        assert cur.fetchone() == ('YES', 'ALWAYS')
    # Run the documented example itself, including its CTAS row-lock query.
    admin.execute((ROOT / "sql/process_batch.sql").read_text().replace(":batch_size", "500"))
    assert admin.execute("SELECT count(*) FROM pgn.access_logs WHERE processed_at IS NULL").fetchone()[0] == 0

    # Out-of-order commits: allocate smaller ID but keep it invisible.
    a = psycopg.connect(ADMIN)
    b = psycopg.connect(ADMIN)
    claim = "SELECT event_time,id FROM pgn.access_logs WHERE processed_at IS NULL ORDER BY event_time,id LIMIT 1 FOR UPDATE SKIP LOCKED"
    try:
        admin.execute("UPDATE pgn.access_logs SET processed_at=now()")
        low = a.execute("INSERT INTO pgn.access_logs(event_time,destination_host,protocol,status,node_id) VALUES(now(),'low','tcp','accepted','sql') RETURNING id").fetchone()[0]
        high = b.execute("INSERT INTO pgn.access_logs(event_time,destination_host,protocol,status,node_id) VALUES(now(),'high','tcp','accepted','sql') RETURNING id").fetchone()[0]
        b.commit()
        assert low < high
        chosen = b.execute(claim).fetchone()
        assert chosen[1] == high
        # Concurrent consumer cannot obtain the locked higher ID.
        assert admin.execute(claim).fetchone() is None
        b.rollback()
        assert admin.execute(claim).fetchone()[1] == high
        b.execute("UPDATE pgn.access_logs SET processed_at=now() WHERE id=%s", (high,))
        b.commit()
        a.commit()
        assert b.execute(claim).fetchone()[1] == low
        b.rollback()
        # Rollback releases claims and both consumers obtain disjoint batches.
        admin.execute("UPDATE pgn.access_logs SET processed_at=NULL")
        first = a.execute(claim).fetchone()
        second = b.execute(claim).fetchone()
        assert first and second and first != second
        a.rollback()
        b.rollback()
    finally:
        a.close()
        b.close()

    # Expired backlog is accepted by the view but never stored.
    before = admin.execute("SELECT count(*) FROM pgn.access_logs").fetchone()[0]
    admin.execute("INSERT INTO pgn.access_ingest(event_time,destination_host,protocol,status,node_id) VALUES(now()-interval '32 days','expired','tcp','accepted','sql')")
    assert admin.execute("SELECT count(*) FROM pgn.access_logs").fetchone()[0] == before
    # Seed an old partition directly, then verify real partman retention and premake.
    old = (now - timedelta(days=35)).replace(hour=0, minute=0, second=0, microsecond=0)
    admin.execute("SELECT partman.create_partition_time('pgn.access_logs', ARRAY[%s::timestamptz])", (old,))
    partition = sorted(admin.execute("SELECT * FROM partman.show_partitions('pgn.access_logs')").fetchall())[0][1]
    admin.execute("SELECT partman.run_maintenance('pgn.access_logs')")
    assert admin.execute("SELECT to_regclass(%s)", ('pgn.' + partition,)).fetchone()[0] is None
    assert admin.execute("SELECT count(*) FROM partman.show_partitions('pgn.access_logs')").fetchone()[0] >= 8
    admin.execute("TRUNCATE pgn.access_logs")


def main():
    with tempfile.TemporaryDirectory(prefix="pgn-vector-") as directory:
        root = Path(directory)
        shutil.copytree(ROOT / "templates", root / "templates")
        shutil.copy(ROOT / "compose.yml", root / "compose.yml")
        try:
            run("docker", "run", "-d", "--name", DB, "-p", "127.0.0.1:55432:5432",
                "-e", "POSTGRES_PASSWORD=test-password", "-e", "POSTGRES_DB=pgn", "pgn-postgres-test")
            def ready():
                with psycopg.connect(ADMIN) as conn:
                    return conn.execute("SELECT 1").fetchone()[0] == 1
            wait_for(ready, "Postgres startup")
            run("docker", "cp", DB + ":/certs/server.crt", str(root / "ca.pem"))
            with psycopg.connect(ADMIN, autocommit=True) as admin:
                admin.execute("CREATE SCHEMA partman; CREATE EXTENSION pg_partman SCHEMA partman")
                for migration in sorted((ROOT / "sql").glob("00*.sql")):
                    admin.execute(migration.read_text())
                admin.execute("CREATE ROLE pgn_ingest LOGIN PASSWORD 'test-ingest'; GRANT USAGE ON SCHEMA pgn TO pgn_ingest; GRANT INSERT ON pgn.access_ingest TO pgn_ingest")
                sql_checks(admin)

            env = dict(NODE_DOMAIN="vpn.example.net", API_DOMAIN="pg.example.net", XHTTP_PATH="/assets/test/",
                       COMPOSE_PROJECT_NAME="pgn-access-ci", ACCESS_LOG_ENABLED="true", ACCESS_LOG_NODE_ID="ci-node",
                       ACCESS_LOG_BUFFER_BYTES="268435488", ACCESS_LOG_POSTGRES_CA_FILE="ca.pem",
                       ACCESS_LOG_POSTGRES_DSN="postgres://pgn_ingest:test-ingest@localhost:55432/pgn?sslmode=verify-full")
            (root / ".env").write_text("\n".join(f"{k}='{v}'" for k, v in env.items()) + "\n")
            values = pgn.prepare(root)
            pgn.compose(root, values, "config", "--quiet")
            pgn.compose(root, dict(values, ACCESS_LOG_ENABLED="false"), "config", "--quiet")
            for args in (("validate", "--no-environment"), ("test",)):
                pgn.compose(root, values, "run", "--rm", "--no-deps", "-T", "vector", *args, "/etc/vector/vector.yaml")

            # TLS tests exercise both certificate trust and hostname rejection.
            def health(dsn, ca_file):
                candidate = dict(values, ACCESS_LOG_POSTGRES_DSN=dsn, ACCESS_LOG_POSTGRES_CA_FILE=ca_file)
                return pgn.compose(root, candidate, "run", "--rm", "--no-deps", "-T", "vector", "validate", "/etc/vector/vector.yaml", capture=True)
            health(values["ACCESS_LOG_POSTGRES_DSN"], "ca.pem")
            for dsn, ca_file in [(values["ACCESS_LOG_POSTGRES_DSN"], ""),
                                 (values["ACCESS_LOG_POSTGRES_DSN"].replace("@localhost:", "@127.0.0.1:"), "ca.pem")]:
                try:
                    health(dsn, ca_file)
                except pgn.ConfigError:
                    pass
                else:
                    raise AssertionError("TLS verification should fail")

            pgn.compose(root, values, "up", "-d", "vector")
            def emit(number):
                stamp = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M:%S.%f")
                for name, status in (("access.log", "accepted"), ("error.log", "rejected")):
                    with (root / "state/node/logs" / name).open("a") as file:
                        for i in range(number):
                            file.write(f"{stamp} from 127.0.0.1:1000 {status} tcp:example.org:443 [in -> direct] email: ci-{i}@example.org\n")
            def count():
                with psycopg.connect(ADMIN) as conn:
                    return conn.execute("SELECT count(*) FROM pgn.access_logs WHERE node_id='ci-node'").fetchone()[0]
            emit(10)
            wait_for(lambda: count() == 20, "first batch")
            run("docker", "stop", DB)
            emit(25)
            time.sleep(5)
            # Crash/recreate Vector while Postgres is down: only disk state survives.
            pgn.compose(root, values, "kill", "-s", "SIGKILL", "vector")
            pgn.compose(root, values, "up", "-d", "vector")
            run("docker", "start", DB)
            wait_for(lambda: count() >= 70, "buffer replay after database and Vector restart")
            time.sleep(3)
            assert count() == 70, "Unexpected duplicates in a cleanly interrupted batch"
            pgn.compose(root, dict(values, ACCESS_LOG_ENABLED="false"), "stop", "vector")
            assert any((root / "state/vector").iterdir()), "Persistent state was removed"
            print("PostgreSQL, TLS, retention, SKIP LOCKED, batching and disk recovery: OK")
        finally:
            # Runtime logs can include the test DSN, which contains test-only credentials.
            subprocess.run(["docker", "compose", "--project-directory", str(root), "--profile", "access-logs", "down"], capture_output=True)
            subprocess.run(["docker", "rm", "-f", DB], capture_output=True)


if __name__ == "__main__":
    main()
