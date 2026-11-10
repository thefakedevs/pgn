"""Regression tests for DEBUG=false stdin cores and worker-before-panel recovery."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('maintenance', PROJECT / 'scripts/pgn_maintenance.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class StdinCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        (self.state / 'generated').mkdir()
        self.container = {'Id': 'fixture-node', 'Mounts': [
            {'Type': 'bind', 'Source': str(self.state), 'Destination': '/var/lib/pg-node'},
            {'Type': 'bind', 'Source': '/fixture/renewed-tls', 'Destination': '/var/lib/pg-node/tls'}]}

    def test_stdin_node_is_checked_without_a_generated_file_or_reading_its_pipe(self):
        def command(args):
            self.assertFalse(any('/fd/0' in a for a in args))
            if args[-2:] == ['pidof', 'xray']:
                return '42\n'
            if args[-1] == '/proc/42/cmdline':
                return '/usr/local/bin/xray\0-c\0stdin:\0'
            self.assertIn('--network', args)
            self.assertIn('none', args)
            self.assertIn('type=bind,src=/fixture/renewed-tls,dst=/var/lib/pg-node/tls,readonly', args)
            temporary = next(a for a in args if 'dst=/tmp/pgn-check.json' in a)
            source = Path(temporary.split('src=', 1)[1].split(',dst=')[0])
            config = json.loads(source.read_text())
            self.assertEqual(config['routing']['rules'][0]['ip'], ['geoip:private'])
            self.assertEqual(source.stat().st_mode & 0o777, 0o600)
            return ''
        with patch.object(m, 'command', side_effect=command):
            m.test_candidate(self.container, 'candidate')
        self.assertEqual(list(self.state.glob('.pgn-check-*')), [])

    def test_failed_stdin_check_removes_temporary_files(self):
        with patch.object(m, 'command', side_effect=['42', 'xray\0-c\0stdin:\0', m.MaintenanceError('invalid assets')]):
            with self.assertRaisesRegex(m.MaintenanceError, 'invalid assets'):
                m.test_candidate(self.container, 'candidate')
        self.assertEqual(list(self.state.glob('.pgn-check-*')), [])

    def test_saved_config_still_gets_full_validation_with_nested_tls_mount(self):
        config = self.state / 'generated/xray/xray.json'
        config.parent.mkdir()
        config.write_text('{}')
        with patch.object(m, 'command') as run:
            m.test_candidate(self.container, 'candidate')
        args = run.call_args.args[0]
        self.assertEqual(args[-2:], ['-c', '/var/lib/pg-node/generated/xray/xray.json'])
        self.assertIn('type=bind,src=/fixture/renewed-tls,dst=/var/lib/pg-node/tls,readonly', args)
        self.assertNotIn('exec', args)

    def test_unknown_runtime_does_not_silently_bypass_validation(self):
        with patch.object(m, 'command', side_effect=['42', 'xray\0-c\0/unknown/config.json\0']):
            with self.assertRaisesRegex(m.MaintenanceError, 'runtime does not use stdin'):
                m.test_candidate(self.container, 'candidate')

    def test_worker_recovery_does_not_wait_for_a_core_before_panel_replay(self):
        container = {'State': {'Running': True}, 'Config': {'Image': 'candidate', 'Env': ['SERVICE_PORT=62050']}}
        def command(args):
            self.assertNotIn('pidof', args)
            if args[1] == 'inspect':
                return json.dumps([container])
            return '  0: 0100007F:' + f'{62050:04X}' + ' 00000000:0000 0A 0000\n'
        with patch.object(m, 'command', side_effect=command), patch.object(m.time, 'sleep'):
            m.wait_for_node(Path('/fixture'), {}, Mock(return_value='fixture-node'), 'candidate')


if __name__ == '__main__':
    unittest.main()
