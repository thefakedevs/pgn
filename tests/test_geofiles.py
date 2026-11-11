"""Geofiles pair integrity, scheduling, recovery and Compose integration."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

PROJECT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


geo = module("geofiles", PROJECT / "scripts/geofiles.py")
pgn = module("pgn_geo", PROJECT / "scripts/pgn.py")
MOSCOW = timezone(timedelta(hours=3))


class Upstream:
    def __init__(self, revision=b"one"):
        self.data = {name: name.encode() + revision for name in geo.FILES}
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url.endswith("/commits/release"):
            return json.dumps({"sha": "a" * 40}).encode()
        if "/" + "a" * 40 + "/" not in url:
            raise AssertionError("Asset URL was not pinned to a commit")
        name = url.rsplit("/", 1)[1]
        if name.endswith(".sha256sum"):
            name = name.removesuffix(".sha256sum")
            return (hashlib.sha256(self.data[name]).hexdigest() + "  " + name + "\n").encode()
        return self.data[name]


class GeofilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Windows requires administrator rights for symlinks. Exercise the same state
        # transitions with link storage substituted there; Linux CI uses real links.
        if os.name == "nt":
            self.links = {}
            for name, replacement in (
                ("point", lambda root, name, version: self.links.update({name: version})),
                ("version_at", lambda root, name: self.links.get(name)),
                ("ready", lambda root: "current" in self.links),
            ):
                replacement_patch = patch.object(geo, name, side_effect=replacement)
                replacement_patch.start()
                self.addCleanup(replacement_patch.stop)

    def download(self, revision=b"one"):
        upstream = Upstream(revision)
        version = geo.download(self.root, upstream)
        return version, upstream

    def now(self, hour, minute=0, day=11):
        return datetime(2026, 11, day, hour, minute, tzinfo=MOSCOW)

    def test_initial_download_pins_pair_and_records_provenance(self):
        version, upstream = self.download()
        self.assertEqual(geo.version_at(self.root, "current"), version)
        for name, data in upstream.data.items():
            self.assertEqual((self.root / "sets" / version / name).read_bytes(), data)
        metadata = json.loads((self.root / "sets" / version / "source.json").read_text())
        self.assertEqual(metadata["commit"], "a" * 40)
        self.assertEqual(len(upstream.calls), 5)

    def test_bad_second_file_never_publishes_half_a_pair(self):
        old, _ = self.download()
        upstream = Upstream(b"two")
        original = upstream.__call__

        def corrupt(url):
            return b"corrupt" if url.endswith("/geosite.dat") else original(url)

        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            geo.download(self.root, corrupt)
        self.assertEqual(geo.version_at(self.root, "current"), old)
        self.assertEqual(geo.version_at(self.root, "pending"), old)
        self.assertEqual([p.name for p in (self.root / "sets").iterdir()], [old])

    def test_network_failure_retains_cache(self):
        old, _ = self.download()
        with self.assertRaises(OSError):
            geo.download(self.root, Mock(side_effect=OSError("offline")))
        self.assertEqual(geo.version_at(self.root, "current"), old)

    def test_changed_pair_waits_for_0200_moscow_and_restarts_once(self):
        old, _ = self.download()
        new, _ = self.download(b"two")
        restart = Mock()
        self.assertFalse(geo.apply_pending(self.root, self.now(1, 59), "02:00", restart))
        self.assertEqual(geo.version_at(self.root, "current"), old)
        self.assertTrue(geo.apply_pending(self.root, self.now(2), "02:00", restart))
        self.assertEqual(geo.version_at(self.root, "current"), new)
        self.assertEqual(geo.version_at(self.root, "previous"), old)
        self.assertFalse(geo.apply_pending(self.root, self.now(2), "02:00", restart))
        restart.assert_called_once_with()

    def test_no_changes_never_restart_and_same_data_deduplicates(self):
        old, _ = self.download()
        same, _ = self.download()
        restart = Mock()
        geo.apply_pending(self.root, self.now(2), "02:00", restart)
        restart.assert_not_called()
        self.assertEqual(old, same)
        self.assertEqual(len(list((self.root / "sets").iterdir())), 1)

    def test_first_daytime_start_waits_until_next_night(self):
        old, _ = self.download()
        self.download(b"two")
        restart = Mock()
        self.assertFalse(geo.apply_pending(self.root, self.now(14), "02:00", restart))
        self.assertFalse(geo.apply_pending(self.root, self.now(23), "02:00", restart))
        self.assertEqual(geo.version_at(self.root, "current"), old)
        self.assertTrue(geo.apply_pending(self.root, self.now(2, day=12), "02:00", restart))

    def test_missed_window_catches_up_after_slow_download_or_downtime(self):
        self.download()
        self.download(b"two")
        restart = Mock()
        geo.apply_pending(self.root, self.now(1, 59), "02:00", restart)
        self.assertTrue(geo.apply_pending(self.root, self.now(2, 4), "02:00", restart))
        restart.assert_called_once_with()

    def test_failed_restart_is_retried_with_persisted_intent(self):
        self.download()
        new, _ = self.download(b"two")
        with self.assertRaises(OSError):
            geo.apply_pending(self.root, self.now(2), "02:00", Mock(side_effect=OSError("Docker unavailable")))
        self.assertTrue((self.root / "reload-required").exists())
        restart = Mock()
        self.assertTrue(geo.apply_pending(self.root, self.now(3), "02:00", restart))
        self.assertEqual(geo.version_at(self.root, "current"), new)
        self.assertFalse((self.root / "reload-required").exists())
        restart.assert_called_once_with()

    def test_cleanup_keeps_current_pending_and_previous(self):
        old, _ = self.download()
        new, _ = self.download(b"two")
        geo.apply_pending(self.root, self.now(2), "02:00", Mock())
        discarded, _ = self.download(b"three")
        pending, _ = self.download(b"four")
        self.assertEqual({p.name for p in (self.root / "sets").iterdir()}, {old, new, pending})
        self.assertFalse((self.root / "sets" / discarded).exists())


class DockerTests(unittest.TestCase):
    def test_restart_targets_only_running_node_in_selected_project(self):
        docker = geo.Docker("custom-pgn")
        with patch.object(docker, "request", side_effect=[{"ApiVersion": "1.47"}, [{"Id": "a" * 64}], None]) as request:
            docker.restart_node()
        query = request.call_args_list[1].args[1]
        filters = json.loads(parse_qs(urlsplit(query).query)["filters"][0])
        self.assertEqual(filters["label"], ["com.docker.compose.project=custom-pgn",
                                           "com.docker.compose.service=pg-node", "com.docker.compose.oneoff=False"])
        self.assertNotIn("all=", query)
        self.assertEqual(request.call_args_list[2].args, ("POST", "/v1.47/containers/" + "a" * 64 + "/restart?t=20"))

    def test_missing_or_ambiguous_node_is_never_started_or_restarted(self):
        for containers in ([], [{"Id": "a" * 64}, {"Id": "b" * 64}]):
            docker = geo.Docker("pgn")
            with patch.object(docker, "request", side_effect=[{"ApiVersion": "1.47"}, containers]) as request:
                with self.assertRaises(RuntimeError):
                    docker.restart_node()
                self.assertEqual(request.call_count, 2)


class GeofilesConfigTests(unittest.TestCase):
    def setUp(self):
        self.base = dict(NODE_DOMAIN="vpn.example.net", API_DOMAIN="pg.example.net", XHTTP_PATH="/assets/test/")

    def test_existing_env_gets_moscow_schedule(self):
        values = pgn.validate(self.base)
        self.assertEqual({name: values[name] for name in pgn.GEOFILES_DEFAULTS}, pgn.GEOFILES_DEFAULTS)

    def test_invalid_schedule_is_rejected(self):
        for key, value in [("GEOFILES_UPDATE_INTERVAL", "0"), ("GEOFILES_UPDATE_INTERVAL", "1e4"),
                           ("GEOFILES_UPDATE_INTERVAL", "604801"), ("GEOFILES_RELOAD_AT", "24:00"),
                           ("GEOFILES_RELOAD_AT", "2:00"), ("GEOFILES_TIMEZONE", "../etc/passwd")]:
            with self.subTest(key=key, value=value), self.assertRaises(pgn.ConfigError):
                pgn.validate(dict(self.base, **{key: value}))

    def test_check_builds_updater_and_validates_without_downloading(self):
        with patch.object(pgn, "compose") as compose:
            pgn.check_stack(PROJECT, pgn.validate(self.base))
        self.assertIn("--build", compose.call_args_list[1].args)
        self.assertEqual(compose.call_args_list[1].args[-2:], ("geofiles", "--check"))


if __name__ == "__main__":
    unittest.main()
