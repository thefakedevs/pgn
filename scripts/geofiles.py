#!/usr/bin/env python3
"""Download Runet Freedom assets and apply changed pairs in a daily window."""

import argparse
from datetime import datetime, timedelta
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import tempfile
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

REPOSITORY = "runetfreedom/russia-v2ray-rules-dat"
FILES = ("geoip.dat", "geosite.dat")
MAX_FILE_BYTES = 128 * 1024 * 1024
LOG = logging.getLogger("geofiles")


def fetch(url, limit=MAX_FILE_BYTES):
    request = Request(url, headers={"User-Agent": "pgn-geofiles/1"})
    with urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError("Empty or oversized upstream response")
    return data


def atomic_text(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="ascii", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(value)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def point(root, name, version):
    """One rename exposes both files together; readers mount the parent directory."""
    temporary = root / ("." + name + ".tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to("sets/" + version, target_is_directory=True)
    temporary.replace(root / name)


def version_at(root, name):
    link = root / name
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    if not re.fullmatch(r"sets/[a-f0-9]{64}", target):
        raise ValueError("Invalid geofiles version link")
    return target.split("/")[1]


def ready(root):
    return all((root / "current" / name).is_file() and (root / "current" / name).stat().st_size
               for name in FILES)


def download(root, fetcher=fetch):
    """Pin all four downloads to one commit, verify both, then stage the pair."""
    release = json.loads(fetcher(f"https://api.github.com/repos/{REPOSITORY}/commits/release"))
    commit = release["sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Invalid upstream commit")
    base = f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}"
    sets = root / "sets"
    sets.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=sets) as directory:
        temporary = Path(directory)
        hashes = []
        for name in FILES:
            checksum = fetcher(f"{base}/{name}.sha256sum").decode("ascii").split()
            if not checksum or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum[0]):
                raise ValueError(f"Invalid SHA-256 for {name}")
            data = fetcher(f"{base}/{name}")
            digest = hashlib.sha256(data).hexdigest()
            if not data or digest != checksum[0].lower():
                raise ValueError(f"SHA-256 mismatch for {name}")
            (temporary / name).write_bytes(data)
            hashes.append(digest)
        version = hashlib.sha256("\n".join(hashes).encode("ascii")).hexdigest()
        (temporary / "source.json").write_text(json.dumps({"repository": REPOSITORY, "commit": commit,
                                                          "sha256": dict(zip(FILES, hashes))}) + "\n")
        destination = sets / version
        if not destination.exists():
            temporary.rename(destination)
        # TemporaryDirectory also works when its directory has been renamed.
    point(root, "pending", version)
    if not ready(root):
        point(root, "current", version)
        LOG.info("Initial geofiles installed: %s", version)
    elif version != version_at(root, "current"):
        LOG.info("New geofiles staged for the daily reload: %s", version)
    cleanup(root)
    return version


def cleanup(root):
    keep = {version_at(root, name) for name in ("current", "pending", "previous")}
    for directory in (root / "sets").iterdir():
        if ((re.fullmatch(r"[a-f0-9]{64}", directory.name) and directory.name not in keep)
                or directory.name.startswith(".download-")):
            shutil.rmtree(directory)


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=45)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


class Docker:
    def __init__(self, project):
        self.project = project
        self.api = None

    def request(self, method, path):
        connection = DockerConnection()
        try:
            connection.request(method, path)
            response = connection.getresponse()
            data = response.read()
            if response.status >= 300:
                raise RuntimeError(f"Docker API returned HTTP {response.status}")
            return json.loads(data) if data else None
        finally:
            connection.close()

    def restart_node(self):
        if self.api is None:
            version = self.request("GET", "/version")["ApiVersion"]
            if not re.fullmatch(r"1\.[0-9]+", version):
                raise ValueError("Invalid Docker API version")
            self.api = "/v" + version
        filters = {"label": [f"com.docker.compose.project={self.project}",
                             "com.docker.compose.service=pg-node", "com.docker.compose.oneoff=False"]}
        containers = self.request("GET", self.api + "/containers/json?" + urlencode({"filters": json.dumps(filters)}))
        if len(containers) != 1 or not re.fullmatch(r"[a-f0-9]{64}", containers[0]["Id"]):
            raise RuntimeError("Expected exactly one running pg-node in this Compose project")
        self.request("POST", self.api + f"/containers/{containers[0]['Id']}/restart?t=20")


def reload_due(root, now, reload_at):
    """Persist the next window, including catch-up after downtime or slow downloads."""
    path = root / "schedule.json"
    identity = {"time": reload_at, "zone": str(now.tzinfo)}
    schedule = json.loads(path.read_text()) if path.exists() else {}
    if any(schedule.get(key) != value for key, value in identity.items()):
        hour, minute = map(int, reload_at.split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now.strftime("%H:%M") > reload_at:
            target += timedelta(days=1)
        schedule = dict(identity, next=target.isoformat())
        atomic_text(path, json.dumps(schedule))
    return now.timestamp() >= datetime.fromisoformat(schedule["next"]).timestamp()


def advance_schedule(root, now, reload_at):
    hour, minute = map(int, reload_at.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=1)
    atomic_text(root / "schedule.json", json.dumps({"time": reload_at, "zone": str(now.tzinfo),
                                                  "next": target.isoformat()}))


def apply_pending(root, now, reload_at, restart):
    """Publish at the daily window; persist restart intent across updater failures."""
    intent = root / "reload-required"
    if not intent.exists():
        current, pending = version_at(root, "current"), version_at(root, "pending")
        if not reload_due(root, now, reload_at):
            return False
        if not current or not pending or current == pending:
            advance_schedule(root, now, reload_at)
            return False
        point(root, "previous", current)
        # Record intent before the atomic switch so crashes can be recovered.
        atomic_text(intent, json.dumps({"version": pending, "day": now.date().isoformat()}))
        advance_schedule(root, now, reload_at)
    operation = json.loads(intent.read_text())
    if not re.fullmatch(r"[a-f0-9]{64}", operation["version"]):
        raise ValueError("Invalid reload intent")
    point(root, "current", operation["version"])
    restart()
    atomic_text(root / "last-reload", operation["day"])
    intent.unlink()
    advance_schedule(root, now, reload_at)
    LOG.info("pg-node restarted with geofiles %s", operation["version"])
    cleanup(root)
    return True


def configuration():
    interval = os.environ.get("GEOFILES_UPDATE_INTERVAL", "21600")
    reload_at = os.environ.get("GEOFILES_RELOAD_AT", "02:00")
    if not interval.isdecimal() or not 300 <= int(interval) <= 604800:
        raise ValueError("GEOFILES_UPDATE_INTERVAL must be 300..604800 seconds")
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", reload_at):
        raise ValueError("GEOFILES_RELOAD_AT must be HH:MM")
    zone = ZoneInfo(os.environ.get("GEOFILES_TIMEZONE", "Europe/Moscow"))
    project = os.environ.get("COMPOSE_PROJECT_NAME", "pgn")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise ValueError("Invalid COMPOSE_PROJECT_NAME")
    return int(interval), reload_at, zone, project


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate settings without downloads or Docker access")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    root = Path(os.environ.get("GEOFILES_DIRECTORY", "/data"))
    if args.healthcheck:
        return 0 if ready(root) else 1
    interval, reload_at, zone, project = configuration()
    if args.check:
        print(f"Runet Freedom geofiles: check every {interval}s, reload at {reload_at} {zone}")
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root.mkdir(parents=True, exist_ok=True)
    # A one-shot Compose check must never compete with the daemon for data.
    import fcntl
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        docker = Docker(project)
        next_download = 0
        LOG.info("Checking every %ss; applying at %s %s", interval, reload_at, zone)
        while not stop.is_set():
            # Apply first, so a slow or failed network request at 02:00 cannot skip the window.
            try:
                apply_pending(root, datetime.now(zone), reload_at, docker.restart_node)
            except Exception:
                LOG.exception("Could not reload pg-node; retrying in 15 seconds")
            if time.monotonic() >= next_download:
                try:
                    download(root)
                    next_download = time.monotonic() + interval
                    LOG.info("Upstream check completed")
                except Exception:
                    LOG.exception("Download failed; keeping cached geofiles and retrying in 60 seconds")
                    next_download = time.monotonic() + 60
            stop.wait(15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
