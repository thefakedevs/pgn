"""CI-only real Go-node regression: update returns before panel config replay."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import pgn
import pgn_maintenance as m
import PasarGuardNodeBridge as Bridge
from PasarGuardNodeBridge.common import service_pb2 as service


def run(args):
    return subprocess.run([str(x) for x in args], check=True, capture_output=True, text=True).stdout


async def exercise(root):
    key = str(uuid.uuid4())
    certs = root / 'state/node/certs'
    certs.mkdir(parents=True)
    run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-sha256',
         '-keyout', certs / 'ssl_key.pem', '-out', certs / 'ssl_cert.pem', '-subj', '/CN=localhost',
         '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'])
    (root / '.env').write_text('API_KEY=' + key + '\n')
    project = 'pgn-stdin-' + str(os.getpid())
    values = {'API_KEY': key, 'COMPOSE_PROJECT_NAME': project, 'MAINTENANCE_ENABLED': 'false'}
    (root / 'compose.yml').write_text('''name: ${COMPOSE_PROJECT_NAME}
services:
  pg-node:
    image: pasarguard/node:v0.5.7
    environment:
      API_KEY: ${API_KEY}
      NODE_HOST: "0.0.0.0"
      SERVICE_PORT: "62050"
      SERVICE_PROTOCOL: grpc
      DEBUG: "false"
      GENERATED_CONFIG_PATH: /var/lib/pg-node/generated
      SSL_CERT_FILE: /var/lib/pg-node/certs/ssl_cert.pem
      SSL_KEY_FILE: /var/lib/pg-node/certs/ssl_key.pem
    ports:
      - "127.0.0.1:62060:62050"
    volumes:
      - ./state/node:/var/lib/pg-node
''')
    compose = pgn.compose
    candidate = 'local/pgn-node:stdin-fixture-' + str(os.getpid())
    try:
        compose(root, values, 'up', '-d')
        ident = compose(root, values, 'ps', '-q', 'pg-node', capture=True).strip()
        m.wait_for_node(root, values, compose, 'pasarguard/node:v0.5.7')
        def client():
            return Bridge.create_node(connection=Bridge.NodeType.grpc, address='127.0.0.1', port=62060,
                                      api_port=62061, server_ca=(certs / 'ssl_cert.pem').read_text(),
                                      api_key=key, name='stdin-regression')
        config = json.dumps({'log': {'loglevel': 'warning'}, 'inbounds': [],
                             'outbounds': [{'tag': 'direct', 'protocol': 'freedom'}]})
        node = client()
        await asyncio.wait_for(node.start(config=config, backend_type=service.BackendType.XRAY, users=[]), 30)
        run(['docker', 'exec', ident, 'pidof', 'xray'])
        assert not list((root / 'state/node/generated').rglob('*.json'))
        build = root / 'build'
        build.mkdir()
        (build / 'Dockerfile').write_text('FROM pasarguard/node:v0.5.7\nLABEL pgn.stdin-regression="candidate"\n')
        run(['docker', 'build', '--network', 'none', '-t', candidate, build])
        backup = root / 'state/maintenance/backups/fixture'
        backup.mkdir(parents=True)
        container, base = m.snapshot(root, values, compose, backup)
        # This is the exact path that previously failed for Electra's empty generated/.
        # It also recreates only this fixture's worker while no panel is replaying config.
        m.apply_candidate(root, values, compose, container, candidate,
                          {'base_image': base, 'image': candidate}, backup)
        new_ident = compose(root, values, 'ps', '-q', 'pg-node', capture=True).strip()
        assert new_ident != ident
        result = subprocess.run(['docker', 'exec', new_ident, 'pidof', 'xray'], capture_output=True)
        assert result.returncode != 0, 'fixture unexpectedly replayed Core config before update response'
        # Simulate the panel after the maintenance response releases its lifecycle lease.
        restored = client()
        await asyncio.wait_for(restored.start(config=config, backend_type=service.BackendType.XRAY, users=[]), 30)
        run(['docker', 'exec', new_ident, 'pidof', 'xray'])
        await asyncio.wait_for(restored.get_system_stats(), 10)
        assert not list((root / 'state/node/generated').rglob('*.json'))
        await restored.stop()
        print('PASS: actual Go stdin core; candidate validated; worker recreated; update returned before replay; Xray restored via gRPC')
    finally:
        compose(root, values, 'down', '--remove-orphans')
        subprocess.run(['docker', 'image', 'rm', candidate], capture_output=True)


with tempfile.TemporaryDirectory(prefix='pgn-stdin-regression-') as temp:
    asyncio.run(exercise(Path(temp)))
