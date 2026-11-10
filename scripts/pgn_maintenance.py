"""Host commands for node-serviced; preserve PGN's Compose identity and mounts."""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

# These are the same region sources used by PasarGuard/scripts pg-node.sh.
GEO_REPOS = {
    "iran": "Chocolate4U/Iran-v2ray-rules",
    "russia": "runetfreedom/russia-v2ray-rules-dat",
    "china": "Loyalsoldier/v2ray-rules-dat",
}
VERSION = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+$")


class MaintenanceError(Exception):
    pass


def command(args):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True)
    if result.returncode:
        # Never echo env, docker inspect, generated user configs or API keys.
        raise MaintenanceError(f"{Path(str(args[0])).name} failed (exit {result.returncode})")
    return result.stdout


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(obj, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def download(url, limit=160 * 1024 * 1024):
    if not url.startswith("https://"):
        raise MaintenanceError("Only HTTPS downloads are allowed")
    req = urllib.request.Request(url, headers={"User-Agent": "pgn-maintenance", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            if not response.url.startswith("https://"):
                raise MaintenanceError("Download redirected away from HTTPS")
            payload = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError):
        raise MaintenanceError("Release download failed") from None
    if not payload or len(payload) > limit:
        raise MaintenanceError("Empty or oversized release asset")
    return payload


def release(repo, version="latest"):
    if version != "latest" and not VERSION.fullmatch(version):
        raise MaintenanceError("Version must be latest or vX.Y.Z")
    suffix = "latest" if version == "latest" else "tags/v" + version.lstrip("v")
    info = json.loads(download(f"https://api.github.com/repos/{repo}/releases/{suffix}", 2 * 1024 * 1024))
    if info.get("draft") or info.get("prerelease"):
        raise MaintenanceError("Only stable releases are supported")
    return info


def asset(info, name):
    matches = [a for a in info.get("assets", []) if a["name"] == name]
    if len(matches) != 1:
        raise MaintenanceError(f"Missing release asset: {name}")
    return download(matches[0]["browser_download_url"])


def xray_binary(info):
    arch = platform.machine()
    names = {"x86_64": "Xray-linux-64.zip", "aarch64": "Xray-linux-arm64-v8a.zip"}
    if arch not in names:
        raise MaintenanceError("Supported Xray platforms: Linux x86_64 and aarch64")
    name = names[arch]
    archive = asset(info, name)
    digest = asset(info, name + ".dgst").decode("ascii")
    # Xray publishes SHA256=<hex> / SHA2-256 (...)=<hex> in .dgst.
    expected = re.search(r"(?im)^.*SHA(?:2-)?256[^\r\n=]*=\s*([a-f0-9]{64})\s*$", digest)
    if not expected or hashlib.sha256(archive).hexdigest() != expected[1].lower():
        raise MaintenanceError("Xray SHA256 checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        binary = bundle.read("xray")
    if not binary.startswith(b"\x7fELF"):
        raise MaintenanceError("Xray release does not contain a Linux binary")
    return binary


def snapshot(root, values, compose, directory):
    ids = compose(root, values, "ps", "-q", "pg-node", capture=True).split()
    if len(ids) != 1:
        raise MaintenanceError("Expected one running pg-node in this Compose project")
    container = json.loads(command(["docker", "inspect", ids[0]]))[0]
    if not container["State"]["Running"]:
        raise MaintenanceError("The existing node is not running")
    image = container["Config"]["Image"]
    runtime = root / "config/node-runtime.json"
    if runtime.exists():
        base = json.loads(runtime.read_text())["base_image"]
    elif (root / "node-image/source.json").exists():
        base = json.loads((root / "node-image/source.json").read_text())["base_image"]
    else:
        base = image
    if not re.fullmatch(r"pasarguard/node:v[0-9]+\.[0-9]+\.[0-9]+", base):
        raise MaintenanceError("Cannot identify the official node version; refusing to guess")
    for name, source in [("xray", "/usr/local/bin/xray"),
                         ("geoip.dat", "/usr/local/share/xray/geoip.dat"),
                         ("geosite.dat", "/usr/local/share/xray/geosite.dat")]:
        command(["docker", "cp", ids[0] + ":" + source, directory / name])
    # Keep the working image and original Compose override available for rollback.
    tag = "local/pgn-node:rollback-" + directory.name
    command(["docker", "tag", container["Image"], tag])
    write_json(directory / "before.json", {"image": image, "image_id": container["Image"],
               "base_image": base, "rollback_tag": tag})
    return container, base


def test_candidate(container, image):
    mounts = [m for m in container.get("Mounts", []) if m["Destination"] == "/var/lib/pg-node"]
    if len(mounts) != 1 or mounts[0]["Type"] != "bind":
        raise MaintenanceError("Expected a persistent bind mount at /var/lib/pg-node")
    state = Path(mounts[0]["Source"])
    configs = list((state / "generated").glob("*.json"))
    if not configs:
        raise MaintenanceError("No generated Xray config to validate; connect the panel first")
    command(["docker", "run", "--rm", "--network", "none", "--entrypoint", "/usr/local/bin/xray",
             "--mount", f"type=bind,src={state},dst=/var/lib/pg-node,readonly", image,
             "run", "-test", "-confdir", "/var/lib/pg-node/generated"])


def wait_for_node(root, values, compose, expected_image):
    # The panel supplies config again after reconnect. Verify both the worker
    # process and the actual Xray child, not just an open TCP socket.
    deadline = time.monotonic() + 90
    stable = 0
    while time.monotonic() < deadline:
        ids = compose(root, values, "ps", "-q", "pg-node", capture=True).split()
        if len(ids) == 1:
            c = json.loads(command(["docker", "inspect", ids[0]]))[0]
            if c["State"].get("OOMKilled"):
                raise MaintenanceError("Updated node was OOM-killed")
            if c["State"]["Running"] and c["Config"]["Image"] == expected_image:
                result = subprocess.run(["docker", "exec", ids[0], "pidof", "xray"], capture_output=True)
                stable = stable + 1 if result.returncode == 0 else 0
                if stable >= 3:
                    return
        time.sleep(3)
    raise MaintenanceError("Updated node did not recover its Xray process")


def apply_candidate(root, values, compose, container, candidate, record, backup):
    test_candidate(container, candidate)
    override = root / "config/node-runtime.compose.json"
    previous = override.read_bytes() if override.exists() else None
    if previous is not None:
        (backup / "previous-override.json").write_bytes(previous)
    rollback = {"services": {"pg-node": {"image": container["Config"]["Image"]}}}
    write_json(backup / "rollback.compose.json", rollback)
    write_json(override, {"services": {"pg-node": {"image": candidate}}})
    try:
        compose(root, values, "config", "--quiet")
        compose(root, values, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "pg-node")
        wait_for_node(root, values, compose, candidate)
    except BaseException:
        # Pin the old image ID while recovering, even if an upstream tag moved.
        old = json.loads((backup / "before.json").read_text())["rollback_tag"]
        write_json(override, {"services": {"pg-node": {"image": old}}})
        try:
            compose(root, values, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "pg-node")
            wait_for_node(root, values, compose, old)
        except BaseException:
            # Leave the explicit rollback reference in place for a safe retry.
            raise MaintenanceError("Update and rollback both failed; rollback override retained") from None
        if previous is None:
            override.unlink()
        else:
            override.write_bytes(previous)
        raise
    write_json(root / "config/node-runtime.json", record)
    write_json(backup / "after.json", record)


def update(root, args, values, compose):
    if args.command == "geofiles" and not args.region:
        raise MaintenanceError("Choose --iran, --russia or --china")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ").lower()
    backup = root / "state/maintenance/backups" / timestamp
    backup.mkdir(parents=True)
    container, base = snapshot(root, values, compose, backup)
    record = {"base_image": base, "operation": args.command, "at": timestamp}
    if args.command == "update":
        info = release("PasarGuard/node", args.version)
        version = info["tag_name"]
        if not VERSION.fullmatch(version):
            raise MaintenanceError("Unexpected node release version")
        base = "pasarguard/node:v" + version.lstrip("v")
        command(["docker", "pull", base])
        record["base_image"] = base
    elif args.command == "core-update":
        info = release("XTLS/Xray-core", args.version)
        (backup / "xray").write_bytes(xray_binary(info))
        record["xray_release"] = info["tag_name"]
    elif args.command == "geofiles":
        info = release(GEO_REPOS[args.region])
        for name in ("geoip.dat", "geosite.dat"):
            content = asset(info, name)
            # Catch error pages and empty/truncated files before the canary test.
            if len(content) < 1024 or content.lstrip().startswith((b"<", b"{")):
                raise MaintenanceError("Invalid geofile download")
            (backup / name).write_bytes(content)
        record.update(geofiles_region=args.region, geofiles_release=info["tag_name"])
    candidate = "local/pgn-node:" + timestamp
    (backup / "Dockerfile").write_text(
        f"FROM {base}\n"
        "COPY --chmod=0755 xray /usr/local/bin/xray\n"
        "COPY --chmod=0644 geoip.dat geosite.dat /usr/local/share/xray/\n")
    (backup / ".dockerignore").write_text("*\n!Dockerfile\n!xray\n!geoip.dat\n!geosite.dat\n")
    command(["docker", "build", "--network", "none", "--tag", candidate, str(backup)])
    version = command(["docker", "run", "--rm", "--network", "none", "--entrypoint", "/usr/local/bin/xray", candidate, "version"])
    match = re.search(r"Xray\s+([0-9]+\.[0-9]+\.[0-9]+)", version)
    if not match:
        raise MaintenanceError("Candidate Xray version check failed")
    record.update(image=candidate, xray_version=match[1], rollback_image=json.loads((backup / "before.json").read_text())["rollback_tag"],
                  checksums={name: hashlib.sha256((backup / name).read_bytes()).hexdigest()
                             for name in ("xray", "geoip.dat", "geosite.dat")})
    apply_candidate(root, values, compose, container, candidate, record, backup)
    # Images and manifests are sufficient for rollback; do not accumulate build payloads.
    for name in ("xray", "geoip.dat", "geosite.dat", "Dockerfile", ".dockerignore"):
        (backup / name).unlink(missing_ok=True)
    print(f"{args.command}: completed; node and Xray are running. Rollback: {backup}")


def unit_name(values):
    return "pgn-" + values["COMPOSE_PROJECT_NAME"] + "-maintenance.service"


def install(root, values, compose):
    if os.geteuid() != 0:
        raise MaintenanceError("maintenance-install requires root")
    # Avoid systemd specifier and dotenv interpolation in generated native units.
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(root)):
        raise MaintenanceError("Use a project path with letters, digits, /, _, . and -")
    run_dir = root / "state/maintenance/run"
    if len(str(run_dir / "maintenance.sock").encode()) >= 104:
        raise MaintenanceError("Project path is too long for a Unix socket")
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o755)
    binary_dir = root / "state/maintenance/bin"
    binary_dir.mkdir(parents=True, exist_ok=True)
    source = root / "maintenance/node-serviced"
    builder = "golang:1.25.5-bookworm"
    if shutil.which("go"):
        env = dict(os.environ, CGO_ENABLED="0")
        subprocess.run(["go", "test", "./..."], cwd=source, env=env, check=True)
        subprocess.run(["go", "build", "-trimpath", "-o", str(binary_dir / "node-serviced.new"), "."], cwd=source, env=env, check=True)
    else:
        command(["docker", "run", "--rm", "--mount", f"type=bind,src={source},dst=/src,readonly",
                 "--mount", f"type=bind,src={binary_dir},dst=/out", "-w", "/src", "-e", "CGO_ENABLED=0", builder,
                 "sh", "-ec", "go test ./...; go build -trimpath -o /out/node-serviced.new ."])
    (binary_dir / "node-serviced.new").chmod(0o755)
    (binary_dir / "node-serviced.new").replace(binary_dir / "node-serviced")
    gid = compose(root, values, "run", "--rm", "--no-deps", "-T", "--entrypoint", "id", "haproxy", "-g", "haproxy", capture=True).strip()
    if not gid.isdigit():
        raise MaintenanceError("Cannot determine HAProxy socket group")
    env_path = root / "state/maintenance/service.env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(f"API_KEY={values['API_KEY']}\nAPP_NAME={root}/pgn\nSOCKET_PATH={run_dir}/maintenance.sock\nSOCKET_GID={gid}\n")
    env_path.chmod(0o600)
    unit = f"""[Unit]
Description=PGN maintenance API over Unix socket
After=docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory={root}
Environment=ENV_FILE={env_path}
ExecStart={binary_dir}/node-serviced
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths={root}

[Install]
WantedBy=multi-user.target
"""
    name = unit_name(values)
    unit_path = Path("/etc/systemd/system") / name
    unit_path.write_text(unit)
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "enable", "--now", name])
    command(["systemctl", "restart", name])
    command(["systemctl", "is-active", "--quiet", name])
    print("Unix maintenance API installed. Set MAINTENANCE_ENABLED=true, run ./pgn up, then set API port=443 in the panel.")


def execute(root, args, values, compose):
    try:
        if not values["API_KEY"]:
            raise MaintenanceError("Run ./pgn prepare first")
        if args.command == "maintenance-install":
            install(root, values, compose)
        elif args.command == "maintenance-status":
            print(command(["systemctl", "status", "--no-pager", unit_name(values)]))
        elif args.command == "restart":
            compose(root, values, "restart", "pg-node")
        else:
            update(root, args, values, compose)
    except (MaintenanceError, ValueError, KeyError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        raise MaintenanceError(str(exc)) from None
