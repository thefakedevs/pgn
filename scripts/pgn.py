#!/usr/bin/env python3
"""Deploy the Altair-style PGN stack; Python standard library only."""

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
KEYS = {"NODE_DOMAIN", "API_DOMAIN", "XHTTP_PATH", "API_KEY", "COMPOSE_PROJECT_NAME"}


class ConfigError(Exception):
    pass


def load_env(root):
    path = root / ".env"
    if not path.is_file():
        raise ConfigError("Нет .env. Выполните: cp .env.example .env — и заполните домены и XHTTP_PATH.")
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, raw = line.partition("=")
        key = key.strip()
        if not sep or key not in KEYS:
            raise ConfigError(f".env:{number}: неизвестный параметр или неверный формат.")
        if key in values:
            raise ConfigError(f".env:{number}: параметр {key} указан повторно.")
        try:
            parts = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            raise ConfigError(f".env:{number}: неверные кавычки.") from None
        if len(parts) > 1:
            raise ConfigError(f".env:{number}: значение должно быть одной строкой без пробелов.")
        values[key] = parts[0] if parts else ""
    return validate(values)


def validate(values):
    values = dict(values)
    for key in ("NODE_DOMAIN", "API_DOMAIN"):
        domain = values.get(key, "").lower()
        labels = domain.split(".")
        if (len(domain) > 253 or len(labels) < 2
                or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in labels)
                or all(p.isdigit() for p in labels)):
            raise ConfigError(f"{key}: укажите DNS-имя без схемы, порта и завершающей точки.")
        if domain in {"vpn.example.com", "pg.vpn.example.com"}:
            raise ConfigError(f"{key}: замените пример своим доменом.")
        values[key] = domain
    if values["NODE_DOMAIN"] == values["API_DOMAIN"]:
        raise ConfigError("NODE_DOMAIN и API_DOMAIN должны различаться: HAProxy разделяет их по SNI.")
    path = values.get("XHTTP_PATH", "")
    if (not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)+", path)
            or "change-me" in path or len(path) > 512):
        raise ConfigError("XHTTP_PATH: скопируйте путь TLS XHTTP из панели, например /assets/abc123/. Нужен / в конце.")
    values["XHTTP_PATH"] = path
    key = values.get("API_KEY", "")
    if key:
        try:
            parsed = uuid.UUID(key)
            if str(parsed) != key.lower():
                raise ValueError
        except ValueError:
            raise ConfigError("API_KEY должен быть UUID в формате xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.") from None
    values["API_KEY"] = key
    project = values.get("COMPOSE_PROJECT_NAME") or "pgn"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise ConfigError("COMPOSE_PROJECT_NAME: до 63 символов a-z, 0-9, _ и -; первый символ — буква или цифра.")
    values["COMPOSE_PROJECT_NAME"] = project
    return values


def atomic_write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                     dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            os.chmod(temp, mode)
            stream.write(text)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def save_api_key(root, key):
    path = root / ".env"
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    found = False
    for i, line in enumerate(lines):
        if re.match(r"^\s*API_KEY\s*=", line):
            lines[i] = "API_KEY=" + key
            found = True
    if not found:
        lines.append("API_KEY=" + key)
    atomic_write(path, "\n".join(lines) + "\n")


def run(args, *, capture=False, env=None):
    try:
        result = subprocess.run(args, text=True, encoding="utf-8", errors="replace",
                                capture_output=capture, env=env, check=False)
    except FileNotFoundError:
        raise ConfigError(f"Не найдена команда: {args[0]}") from None
    if result.returncode:
        if capture:
            # Callers use capture only with OpenSSL and non-secret Docker diagnostics.
            details = (result.stderr or result.stdout or "").strip()
            raise ConfigError(f"Команда {Path(args[0]).name} завершилась с ошибкой: {details}")
        raise ConfigError(f"Команда {Path(args[0]).name} завершилась с кодом {result.returncode}.")
    return result.stdout if capture else ""


def openssl(*args):
    return run(["openssl", *map(str, args)], capture=True)


def check_certificate(cert, key, domain):
    if not cert.is_file() or not key.is_file():
        raise ConfigError("Неполная пара сертификата API. Восстановите ssl_cert.pem и ssl_key.pem из одной копии.")
    openssl("x509", "-in", cert, "-noout", "-checkend", "0")
    result = openssl("x509", "-in", cert, "-noout", "-checkhost", domain)
    # Some OpenSSL versions return 0 even when -checkhost reports a mismatch.
    if "does match certificate" not in result:
        raise ConfigError("Сертификат API не соответствует API_DOMAIN. При смене домена замените пару сертификат/ключ и обновите сертификат в панели.")
    cert_public = openssl("x509", "-in", cert, "-pubkey", "-noout").strip()
    key_public = openssl("pkey", "-in", key, "-pubout", "-passin", "pass:").strip()
    if cert_public != key_public:
        raise ConfigError("Сертификат API и приватный ключ не соответствуют друг другу.")


def ensure_certificate(root, domain):
    cert_dir = root / "state/node/certs"
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert = cert_dir / "ssl_cert.pem"
    key = cert_dir / "ssl_key.pem"
    if cert.exists() or key.exists():
        check_certificate(cert, key, domain)
    else:
        with tempfile.TemporaryDirectory(dir=cert_dir) as directory:
            temp = Path(directory)
            openssl("req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes",
                    "-days", "3650", "-keyout", temp / "key.pem", "-out", temp / "cert.pem",
                    "-subj", f"/CN={domain}", "-addext", f"subjectAltName=DNS:{domain}")
            check_certificate(temp / "cert.pem", temp / "key.pem", domain)
            os.chmod(temp / "key.pem", 0o600)
            (temp / "key.pem").replace(key)
            (temp / "cert.pem").replace(cert)
    os.chmod(key, 0o600)
    os.chmod(cert, 0o644)


def render(root, values):
    replacements = {"API_DOMAIN": values["API_DOMAIN"], "NODE_DOMAIN": values["NODE_DOMAIN"],
                    "XHTTP_PATH": values["XHTTP_PATH"], "XHTTP_BASE": values["XHTTP_PATH"].rstrip("/")}
    for name in ("haproxy.cfg", "Caddyfile"):
        text = (root / "templates" / (name + ".tpl")).read_text(encoding="utf-8")
        for key, value in replacements.items():
            text = text.replace("@@" + key + "@@", value)
        if "@@" in text:
            raise ConfigError(f"Неизвестный маркер в шаблоне {name}.")
        atomic_write(root / "config" / name, text, 0o644)


def prepare(root):
    values = load_env(root)
    if not shutil.which("openssl"):
        raise ConfigError("Установите OpenSSL: apt-get install openssl")
    # Validate an existing identity before changing anything else.
    cert_dir = root / "state/node/certs"
    if any((cert_dir / n).exists() for n in ("ssl_cert.pem", "ssl_key.pem")):
        check_certificate(cert_dir / "ssl_cert.pem", cert_dir / "ssl_key.pem", values["API_DOMAIN"])
    if not values["API_KEY"]:
        # Do not silently replace an existing node's authentication credential.
        if (cert_dir / "ssl_cert.pem").exists():
            raise ConfigError("API_KEY пуст, но сертификат API уже существует. Восстановите API_KEY из прежнего .env или из панели.")
        values["API_KEY"] = str(uuid.uuid4())
        save_api_key(root, values["API_KEY"])
    else:
        os.chmod(root / ".env", 0o600)
    ensure_certificate(root, values["API_DOMAIN"])
    render(root, values)
    for directory in ("state/node/generated", "state/caddy/data", "state/caddy/config"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    return values


def compose(root, values, *args, capture=False):
    env = os.environ.copy()
    env.update(values)
    digest = hashlib.sha256()
    for name in ("haproxy.cfg", "Caddyfile"):
        path = root / "config" / name
        if path.exists():
            digest.update(name.encode() + b"\0" + path.read_bytes())
    env["PGN_CONFIG_REVISION"] = digest.hexdigest()
    return run(["docker", "compose", "--project-directory", str(root),
                "--env-file", str(root / ".env"), "-f", str(root / "compose.yml"), *args],
               env=env, capture=capture)


def require_docker():
    if sys.platform != "linux":
        raise ConfigError("Запуск контейнеров поддерживается на Linux-сервере (network_mode: host).")
    run(["docker", "compose", "version"], capture=True)
    server_os = run(["docker", "info", "--format", "{{.OSType}}"], capture=True).strip()
    if server_os != "linux":
        raise ConfigError("Нужен Docker Engine для Linux.")
    # Bind mounts and loopback must refer to the same server as this checkout.
    endpoint = os.environ.get("DOCKER_HOST", "")
    if not endpoint:
        endpoint = run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], capture=True).strip()
    if not endpoint.startswith("unix://"):
        raise ConfigError("Запускайте ./pgn на самом сервере с локальным Unix-сокетом Docker.")


def check_stack(root, values):
    compose(root, values, "config", "--quiet")
    compose(root, values, "run", "--rm", "--no-deps", "-T", "--entrypoint", "haproxy",
            "haproxy", "-c", "-f", "/usr/local/etc/haproxy/haproxy.cfg")
    compose(root, values, "run", "--rm", "--no-deps", "-T", "--entrypoint", "caddy",
            "caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")


def show_info(root, values):
    if not values["API_KEY"]:
        raise ConfigError("Сначала выполните ./pgn prepare или ./pgn up.")
    cert = root / "state/node/certs/ssl_cert.pem"
    if not cert.is_file():
        raise ConfigError("Нет сертификата API. Сначала выполните ./pgn prepare.")
    print(f"""
Данные для панели PasarGuard
  Адрес:       {values['API_DOMAIN']}
  Порт:        443
  Протокол:    gRPC
  TLS:         включён
  API key:     {values['API_KEY']}

Публичный сертификат API — вставьте в панель:
{cert.read_text(encoding='ascii').strip()}

Параметры Xray, которые задаются в панели
  Reality inbound: listen 127.0.0.1, port 10443
  acceptProxyProtocol: true
  Reality target: 127.0.0.1:8443
  Reality serverNames: {values['NODE_DOMAIN']}
  TLS XHTTP inbound: listen 127.0.0.1, port 10444, security none
  TLS XHTTP path: {values['XHTTP_PATH']}

Ключ Reality, shortIds, путь Reality XHTTP и пользователей задаёт панель.
PGN ожидает конфигурацию и пользователей от панели.
Сайт: https://{values['NODE_DOMAIN']}
""".strip())


@contextmanager
def project_lock(root):
    # flock is released by the OS even after a failed/interrupted installation.
    if sys.platform == "linux":
        import fcntl
        with (root / ".pgn.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConfigError("Другой процесс ./pgn уже работает с этим каталогом.") from None
            yield
    else:
        yield


def main():
    parser = argparse.ArgumentParser(description="PasarGuard Node + HAProxy + Caddy, один публичный порт 443")
    parser.add_argument("command", choices=("prepare", "check", "up", "info", "status", "logs", "down"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        with project_lock(ROOT):
            if args.command == "info":
                show_info(ROOT, load_env(ROOT))
                return 0
            if args.command == "prepare":
                show_info(ROOT, prepare(ROOT))
                return 0
            require_docker()
            if args.command in {"up", "check"}:
                values = prepare(ROOT)
                check_stack(ROOT, values)
                if args.command == "check":
                    print("Compose, HAProxy и Caddy: конфигурация корректна. VPN-клиент не проверялся.")
                    return 0
                compose(ROOT, values, "up", "-d")
                compose(ROOT, values, "ps")
                print("Контейнеры запущены. Дождитесь сертификата Caddy и подключения панели.\n")
                show_info(ROOT, values)
            else:
                values = load_env(ROOT)
                if not values["API_KEY"]:
                    raise ConfigError("Нет API_KEY. Сначала выполните ./pgn prepare.")
                commands = {"status": ("ps",), "logs": ("logs", "--tail", "100"), "down": ("down",)}
                compose(ROOT, values, *commands[args.command])
        return 0
    except (ConfigError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
