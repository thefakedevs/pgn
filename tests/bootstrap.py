"""CI only: download the latest Russian release and verify it with actual Xray."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import pgn
from pgn_maintenance import ensure_service, initial_geofiles
from unittest.mock import patch
import subprocess

values = pgn.prepare(ROOT)
assert values['MAINTENANCE_ENABLED'] == 'true'
assert (ROOT / 'state/node/.fresh-install.json').exists()
real_compose = pgn.compose
launches = []
def fixture_compose(root, values, *args, **kwargs):
    if args == ('up', '-d'):
        # Exercise actual up preparation while explicitly blocking service launch.
        subprocess.run(['docker', 'image', 'inspect', 'local/pgn-maintenance:0.0.4'],
                       check=True, stdout=subprocess.DEVNULL)
        assert (ROOT / 'config/node-runtime.compose.json').exists()
        launches.append(True)
        return ''
    return real_compose(root, values, *args, **kwargs)
with patch.object(sys, 'argv', ['pgn', 'up']), patch.object(pgn, 'compose', side_effect=fixture_compose), \
        patch.object(pgn, 'show_info'):
    assert pgn.main() == 0
assert launches == [True]
# Cached image reuse must not fetch the release or rebuild.
with patch('pgn_maintenance.install', side_effect=AssertionError('cached image rebuilt')):
    ensure_service(ROOT, values, real_compose)
record = json.loads((ROOT / 'config/node-runtime.json').read_text())
assert record['operation'] == 'fresh_install'
assert record['geofiles_region'] == 'russia'
assert set(record['checksums']) == {'geoip.dat', 'geosite.dat'}
assert not (ROOT / 'state/node/.fresh-install.json').exists()
# A repeated first-up check does not resolve a new release or change the image.
before = (ROOT / 'config/node-runtime.compose.json').read_bytes()
initial_geofiles(ROOT, values, pgn.compose)
assert (ROOT / 'config/node-runtime.compose.json').read_bytes() == before
assert not pgn.compose(ROOT, values, 'ps', '-a', '-q', 'pg-node', capture=True).strip()
print('PASS: plain up automatically prepares API and Russian databases; real Xray checks and cached image reuse; service launch intercepted')
