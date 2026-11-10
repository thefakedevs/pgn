"""Optional logging configuration and command wiring (no Docker required)."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("pgn_access", Path(__file__).resolve().parents[1] / "scripts/pgn.py")
pgn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pgn)


class AccessLogTests(unittest.TestCase):
    def setUp(self):
        self.base = dict(NODE_DOMAIN="vpn.example.net", API_DOMAIN="pg.example.net",
                         XHTTP_PATH="/assets/test/", API_KEY="")
        self.enabled = dict(self.base, ACCESS_LOG_ENABLED="true", ACCESS_LOG_NODE_ID="eu-1",
                            ACCESS_LOG_POSTGRES_DSN="postgres://ingest:secret@db.example.net/logs?sslmode=verify-full")

    def test_old_env_defaults_to_disabled(self):
        values = pgn.validate(self.base)
        self.assertEqual(values["ACCESS_LOG_ENABLED"], "false")
        self.assertEqual(values["ACCESS_LOG_BUFFER_BYTES"], "1073741824")

    def test_enabled_validation(self):
        self.assertEqual(pgn.validate(self.enabled)["ACCESS_LOG_NODE_ID"], "eu-1")
        for name, bad in [("ACCESS_LOG_ENABLED", "yes"), ("ACCESS_LOG_NODE_ID", ""),
                          ("ACCESS_LOG_NODE_ID", "${SECRET}"), ("ACCESS_LOG_BUFFER_BYTES", "268435487"),
                          ("ACCESS_LOG_BUFFER_BYTES", "1e9"), ("ACCESS_LOG_POSTGRES_DSN", "postgres://secret@db/logs"),
                          ("ACCESS_LOG_POSTGRES_DSN", "postgres://db/logs?sslmode=require"),
                          ("ACCESS_LOG_POSTGRES_DSN", "postgres://db/logs?sslmode=verify-full&sslmode=disable"),
                          ("ACCESS_LOG_POSTGRES_DSN", "postgres://db/logs?sslmode=verify-full&ssl-mode=disable"),
                          ("ACCESS_LOG_POSTGRES_DSN", "postgres://db:bad/logs?sslmode=verify-full")]:
            with self.subTest(name=name, value=bad), self.assertRaises(pgn.ConfigError) as error:
                pgn.validate(dict(self.enabled, **{name: bad}))
            self.assertNotIn("secret@", str(error.exception))

    def test_profile_is_independent_of_shell_and_management_sees_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for enabled, command, expected in [(False, "up", False), (True, "up", True),
                                               (False, "stop", True), (False, "ps", True),
                                               (False, "logs", True), (False, "down", True)]:
                values = pgn.validate(self.enabled if enabled else self.base)
                with patch.dict(pgn.os.environ, {"COMPOSE_PROFILES": "unexpected"}), patch.object(pgn, "run", return_value="") as run:
                    pgn.compose(root, values, command)
                    self.assertEqual("--profile" in run.call_args.args[0], expected)
                    self.assertEqual(run.call_args.kwargs["env"]["COMPOSE_PROFILES"], "")

    def test_ca_and_dsn_never_appear_in_command_or_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = pgn.validate(dict(self.enabled, ACCESS_LOG_POSTGRES_CA_FILE="ca.pem"))
            with patch.object(pgn, "run", return_value=values["ACCESS_LOG_POSTGRES_DSN"]), patch("builtins.print") as output:
                pgn.compose(root, values, "logs")
                self.assertNotIn("secret", str(output.call_args))
            with patch.object(pgn, "run", side_effect=pgn.ConfigError(values["ACCESS_LOG_POSTGRES_DSN"])):
                with self.assertRaises(pgn.ConfigError) as error:
                    pgn.compose(root, values, "run")
                self.assertNotIn("secret", str(error.exception))

    def test_vector_checks_only_when_enabled(self):
        with patch.object(pgn, "compose") as compose:
            pgn.check_stack(Path("."), pgn.validate(self.base))
            self.assertEqual(compose.call_count, 3)
            compose.reset_mock()
            pgn.check_stack(Path("."), pgn.validate(self.enabled))
            calls = str(compose.call_args_list)
            self.assertIn("--no-environment", calls)
            self.assertIn("'test'", calls)


if __name__ == "__main__":
    unittest.main()
