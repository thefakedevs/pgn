"""CI only: download the latest Russian release and verify it with actual Xray."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import pgn
from pgn_maintenance import initial_geofiles

values = pgn.prepare(ROOT)
assert (ROOT / 'state/node/.fresh-install.json').exists()
initial_geofiles(ROOT, values, pgn.compose)
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
print('PASS: latest Russian geoip/geosite checksums, real Xray ru-blocked checks, repeat-up preservation; no node started')
