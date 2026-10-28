"""Regression tests for persistent credentials, validation and proxy wiring."""

import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pgn", PROJECT / "scripts/pgn.py")
pgn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pgn)


class PrepareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("openssl"):
            raise unittest.SkipTest("OpenSSL is required")
        cls.fixture_temp = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.fixture_temp.name)
        pgn.ensure_certificate(cls.fixture, "pg.node.example.net")

    @classmethod
    def tearDownClass(cls):
        cls.fixture_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(PROJECT / "templates", self.root / "templates")
        shutil.copy(PROJECT / "compose.yml", self.root)
        self.values = {
            "NODE_DOMAIN": "node.example.net", "API_DOMAIN": "pg.node.example.net",
            "XHTTP_PATH": "/assets/abc123/", "API_KEY": "", "COMPOSE_PROJECT_NAME": "pgn-test",
        }
        self.write_env()

    def write_env(self):
        (self.root / ".env").write_text("\n".join(f"{k}={v}" for k, v in self.values.items()) + "\n", encoding="utf-8")

    def use_existing_identity(self):
        shutil.copytree(self.fixture / "state", self.root / "state")
        self.values["API_KEY"] = str(uuid.uuid4())
        self.write_env()

    def test_first_prepare_and_repeated_prepare_preserve_identity(self):
        first = pgn.prepare(self.root)
        self.assertEqual(str(uuid.UUID(first["API_KEY"])), first["API_KEY"])
        files = [self.root / ".env", self.root / "state/node/certs/ssl_cert.pem",
                 self.root / "state/node/certs/ssl_key.pem"]
        before = [p.read_bytes() for p in files]
        second = pgn.prepare(self.root)
        self.assertEqual(first, second)
        self.assertEqual(before, [p.read_bytes() for p in files])
        if os.name != "nt":
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(files[2].stat().st_mode & 0o777, 0o600)

    def test_migration_preserves_existing_key_and_certificate(self):
        self.use_existing_identity()
        cert = self.root / "state/node/certs/ssl_cert.pem"
        before = cert.read_bytes()
        values = pgn.prepare(self.root)
        self.assertEqual(values["API_KEY"], self.values["API_KEY"])
        self.assertEqual(cert.read_bytes(), before)

    def test_lost_api_key_is_not_silently_replaced(self):
        self.use_existing_identity()
        self.values["API_KEY"] = ""
        self.write_env()
        before = (self.root / ".env").read_bytes()
        with self.assertRaisesRegex(pgn.ConfigError, "API_KEY пуст"):
            pgn.prepare(self.root)
        self.assertEqual((self.root / ".env").read_bytes(), before)

    def test_partial_certificate_is_not_regenerated(self):
        self.use_existing_identity()
        cert = self.root / "state/node/certs/ssl_cert.pem"
        before = cert.read_bytes()
        (cert.parent / "ssl_key.pem").unlink()
        with self.assertRaisesRegex(pgn.ConfigError, "Неполная пара"):
            pgn.prepare(self.root)
        self.assertEqual(cert.read_bytes(), before)

    def test_changed_api_domain_requires_deliberate_certificate_replacement(self):
        self.use_existing_identity()
        self.values["API_DOMAIN"] = "pg.other.example.net"
        self.write_env()
        with self.assertRaises(pgn.ConfigError):
            pgn.prepare(self.root)

    def test_corrupt_key_fails_without_overwrite(self):
        self.use_existing_identity()
        key = self.root / "state/node/certs/ssl_key.pem"
        key.write_text("not a key", encoding="ascii")
        with self.assertRaises(pgn.ConfigError):
            pgn.prepare(self.root)
        self.assertEqual(key.read_text(), "not a key")

    def test_proxy_wiring_and_exact_path_are_rendered(self):
        self.use_existing_identity()
        pgn.prepare(self.root)
        haproxy = (self.root / "config/haproxy.cfg").read_text()
        caddy = (self.root / "config/Caddyfile").read_text()
        self.assertIn("req.ssl_sni -i pg.node.example.net", haproxy)
        self.assertIn("127.0.0.1:10443 send-proxy-v2", haproxy)
        self.assertIn("127.0.0.1:62050", haproxy)
        self.assertIn("@xhttp path /assets/abc123 /assets/abc123/*", caddy)
        self.assertIn("h2c://127.0.0.1:10444", caddy)
        self.assertNotIn(self.values["API_KEY"], caddy + haproxy)

    def test_invalid_values_are_rejected_before_generation(self):
        cases = [
            ("NODE_DOMAIN", "vpn.example.com"),
            ("NODE_DOMAIN", "https://node.example.net"),
            ("API_DOMAIN", "node.example.net"),
            ("API_KEY", "not-a-uuid"),
            ("XHTTP_PATH", "/assets/change-me/"),
            ("XHTTP_PATH", "/assets/no-trailing-slash"),
            ("XHTTP_PATH", "/assets/../"),
            ("XHTTP_PATH", "/assets/*/"),
            ("XHTTP_PATH", "/assets/abc/ { malicious }"),
            ("NODE_DOMAIN", "$(touch /tmp/pgn-test)"),
            ("COMPOSE_PROJECT_NAME", "invalid:project"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                invalid = dict(self.values, **{key: value})
                with self.assertRaises(pgn.ConfigError):
                    pgn.validate(invalid)
        self.assertFalse((self.root / "state").exists())

    def test_duplicate_or_unknown_env_values_fail(self):
        for suffix in ("API_KEY=\n", "APY_KEY=typo\n"):
            self.write_env()
            with (self.root / ".env").open("a") as stream:
                stream.write(suffix)
            with self.assertRaises(pgn.ConfigError):
                pgn.load_env(self.root)

    def test_quoted_values_comments_and_crlf_are_supported(self):
        (self.root / ".env").write_bytes(
            b'NODE_DOMAIN="NODE.EXAMPLE.NET" # comment\r\n'
            b"API_DOMAIN='pg.node.example.net'\r\nXHTTP_PATH=/assets/abc123/\r\nAPI_KEY=\r\n")
        values = pgn.load_env(self.root)
        self.assertEqual(values["NODE_DOMAIN"], "node.example.net")
        self.assertEqual(values["COMPOSE_PROJECT_NAME"], "pgn")

    def test_config_revision_changes_only_with_proxy_config(self):
        self.use_existing_identity()
        values = pgn.prepare(self.root)
        with patch.object(pgn, "run", return_value="") as run:
            pgn.compose(self.root, values, "up", "-d")
            initial = run.call_args.kwargs["env"]["PGN_CONFIG_REVISION"]
            self.assertNotIn(values["API_KEY"], run.call_args.args[0])
        pgn.prepare(self.root)
        with patch.object(pgn, "run", return_value="") as run:
            pgn.compose(self.root, values, "up", "-d")
            self.assertEqual(initial, run.call_args.kwargs["env"]["PGN_CONFIG_REVISION"])
        self.values["XHTTP_PATH"] = "/assets/changed/"
        self.write_env()
        values = pgn.prepare(self.root)
        with patch.object(pgn, "run", return_value="") as run:
            pgn.compose(self.root, values, "up", "-d")
            self.assertNotEqual(initial, run.call_args.kwargs["env"]["PGN_CONFIG_REVISION"])

    def test_info_outputs_public_certificate_but_not_private_key(self):
        self.use_existing_identity()
        values = pgn.prepare(self.root)
        with patch("builtins.print") as output:
            pgn.show_info(self.root, values)
        text = output.call_args.args[0]
        self.assertIn("BEGIN CERTIFICATE", text)
        self.assertIn(values["API_KEY"], text)
        self.assertNotIn("PRIVATE KEY", text)


if __name__ == "__main__":
    unittest.main()
