#!/usr/bin/env python3
"""Exercise the real updater image, atomic links and Docker restart selection."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

IMAGE = "pgn-geofiles-test"


def run(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def inside():
    from datetime import datetime
    from unittest.mock import Mock
    from zoneinfo import ZoneInfo
    sys.path.insert(0, "/app")
    import geofiles as geo

    root = Path("/data")

    def upstream(revision):
        def fetch(url):
            if url.endswith("/commits/release"):
                return json.dumps({"sha": "a" * 40}).encode()
            name = url.rsplit("/", 1)[1]
            if name.endswith(".sha256sum"):
                data = (name.removesuffix(".sha256sum") + revision).encode()
                return (hashlib.sha256(data).hexdigest() + "\n").encode()
            return (name + revision).encode()
        return fetch

    old = geo.download(root, upstream("one"))
    assert geo.ready(root)
    new = geo.download(root, upstream("two"))
    assert geo.version_at(root, "current") == old
    docker = geo.Docker(__import__("os").environ["COMPOSE_PROJECT_NAME"])
    zone = ZoneInfo("Europe/Moscow")
    assert not geo.apply_pending(root, datetime(2026, 11, 11, 1, 59, tzinfo=zone), "02:00", docker.restart_node)
    assert geo.apply_pending(root, datetime(2026, 11, 11, 2, tzinfo=zone), "02:00", docker.restart_node)
    assert geo.version_at(root, "current") == new
    assert geo.version_at(root, "previous") == old
    assert (root / "current/geoip.dat").read_bytes() == b"geoip.dattwo"
    assert (root / "current/geosite.dat").read_bytes() == b"geosite.dattwo"
    restart = Mock()
    assert not geo.apply_pending(root, datetime(2026, 11, 11, 2, tzinfo=zone), "02:00", restart)
    restart.assert_not_called()
    print("Real atomic pair switch and Docker API restart passed")


def main():
    project_dir = Path(__file__).resolve().parents[2]
    run("build", "-f", str(project_dir / "docker/geofiles.Dockerfile"), "-t", IMAGE, str(project_dir))
    project = "pgn-geofiles-test-" + uuid.uuid4().hex[:10]
    containers = []
    with tempfile.TemporaryDirectory() as directory:
        try:
            for label_project, oneoff in ((project, "False"), (project + "-other", "False"), (project, "True")):
                containers.append(run("run", "-d", "--label", f"com.docker.compose.project={label_project}",
                                      "--label", "com.docker.compose.service=pg-node", "--label",
                                      f"com.docker.compose.oneoff={oneoff}", "--entrypoint", "python", IMAGE,
                                      "-c", "import time; time.sleep(600)"))
            before = [run("inspect", "--format", "{{.State.StartedAt}}", cid) for cid in containers]
            identity = f"{os.getuid()}:{os.getgid()}"
            socket_group = str(Path("/var/run/docker.sock").stat().st_gid)
            run("run", "--rm", "--read-only", "--cap-drop", "ALL", "--user", identity,
                "--group-add", socket_group,
                "--security-opt", "no-new-privileges:true",
                "-e", f"COMPOSE_PROJECT_NAME={project}", "-v", f"{directory}:/data",
                "-v", "/var/run/docker.sock:/var/run/docker.sock:ro",
                "-v", f"{Path(__file__).resolve()}:/test.py:ro", "--entrypoint", "python", IMAGE, "/test.py", "--inside")
            after = [run("inspect", "--format", "{{.State.StartedAt}}", cid) for cid in containers]
            assert before[0] != after[0], "Selected node was not restarted"
            assert before[1:] == after[1:], "An unrelated node or one-off container was restarted"
            run("run", "--rm", "--read-only", "--cap-drop", "ALL", "--user", identity,
                "-v", f"{directory}:/data", IMAGE, "--healthcheck")
            run("run", "--rm", "--read-only", IMAGE, "--check")
            print("Geofiles Docker integration passed")
        finally:
            for cid in containers:
                run("rm", "-f", cid)


if __name__ == "__main__":
    inside() if "--inside" in sys.argv else main()
