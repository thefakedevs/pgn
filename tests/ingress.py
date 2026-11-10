"""CI-only fixture: real upstream binary and HAProxy; fake update commands/worker."""
import atexit
import http.client
import importlib.util
import os
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import RequestReceived, DataReceived, StreamEnded, ResponseReceived, TrailersReceived

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pgn', ROOT / 'scripts/pgn.py')
pgn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pgn)
values = pgn.load_env(ROOT)
DOMAIN = values['API_DOMAIN']
KEY = values['API_KEY']
CERT = ROOT / 'state/node/certs/ssl_cert.pem'
PRIVATE = CERT.with_name('ssl_key.pem')
containers = []


def cleanup():
    for name in containers:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


atexit.register(cleanup)


def docker(name, *args):
    containers.append(name)
    subprocess.run(['docker', 'run', '-d', '--rm', '--name', name, *map(str, args)], check=True, stdout=subprocess.DEVNULL)


def context(h2=False):
    ctx = ssl.create_default_context(cafile=str(CERT))
    ctx.set_alpn_protocols(['h2'] if h2 else ['http/1.1'])
    return ctx


def listen(port, handler):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', port))
    sock.listen()
    def loop():
        while True:
            c, _ = sock.accept()
            threading.Thread(target=handler, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return sock


server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
server_ctx.load_cert_chain(str(CERT), str(PRIVATE))
server_ctx.set_alpn_protocols(['h2'])


def grpc(raw):
    with server_ctx.wrap_socket(raw, server_side=True) as s:
        h = H2Connection(config=H2Configuration(client_side=False, header_encoding='utf-8'))
        h.initiate_connection()
        s.sendall(h.data_to_send())
        requests = {}
        while True:
            data = s.recv(65535)
            if not data:
                return
            for event in h.receive_data(data):
                if isinstance(event, RequestReceived):
                    requests[event.stream_id] = dict(event.headers)
                elif isinstance(event, DataReceived):
                    h.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, StreamEnded):
                    headers = requests[event.stream_id]
                    assert headers['x-api-key'] == KEY
                    assert headers['content-type'] == 'application/grpc'
                    h.send_headers(event.stream_id, [(':status', '200'), ('content-type', 'application/grpc')])
                    h.send_data(event.stream_id, b'\0' * 5)
                    h.send_headers(event.stream_id, [('grpc-status', '0')], end_stream=True)
            s.sendall(h.data_to_send())


site_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
site_ctx.load_cert_chain(str(CERT), str(PRIVATE))


def exact(s, size):
    data = b''
    while len(data) < size:
        more = s.recv(size - len(data))
        if not more:
            raise EOFError()
        data += more
    return data


def site(raw):
    header = exact(raw, 16)
    assert header[:12] == b'\r\n\r\n\0\r\nQUIT\n'
    exact(raw, int.from_bytes(header[14:16], 'big'))
    with site_ctx.wrap_socket(raw, server_side=True) as s:
        s.recv(8192)
        s.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 13\r\nConnection: close\r\n\r\nexisting-site')


worker = listen(62050, grpc)
website = listen(10443, site)
fake = ROOT / 'state/maintenance/fake-pgn'
fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> /fixture/commands\n')
fake.chmod(0o755)
name = 'pgn-maintenance-fixture-' + str(os.getpid())
docker(name, '--publish', '127.0.0.1:62051:3000',
       '--mount', f'type=bind,src={fake.parent},dst=/fixture',
       '--mount', f'type=bind,src={CERT.parent},dst=/certs,readonly',
       '-e', f'API_KEY={KEY}', '-e', 'API_PORT=3000', '-e', 'APP_NAME=/fixture/fake-pgn',
       '-e', 'SSL_CERT_FILE=/certs/ssl_cert.pem', '-e', 'SSL_KEY_FILE=/certs/ssl_key.pem',
       'local/pgn-maintenance:0.0.4')
# Neither fixture container receives the host Docker socket.
docker(name + '-proxy', '--network', 'host', '--user', '0:0',
       '--mount', f'type=bind,src={ROOT / "config"},dst=/usr/local/etc/haproxy,readonly',
       '--mount', f'type=bind,src={ROOT / "state/maintenance/tls"},dst=/etc/pgn/tls,readonly',
       '--mount', f'type=bind,src={ROOT / "state/maintenance/run"},dst=/run/pgn',
       'haproxy:3.2.24-alpine', 'haproxy', '-db', '-f', '/usr/local/etc/haproxy/haproxy.cfg')


def request(path, key=KEY, body=None, domain=DOMAIN):
    ctx = context()
    # Site fixture reuses the API cert; only the API route asserts host identity.
    if domain != DOMAIN:
        ctx.check_hostname = False
    conn = http.client.HTTPSConnection(domain, timeout=5, context=ctx)
    conn.sock = ctx.wrap_socket(socket.create_connection(('127.0.0.1', 443), timeout=5), server_hostname=domain)
    headers = {'x-api-key': key, 'Content-Type': 'application/json'}
    conn.request('POST' if body is not None else 'GET', path, body=body, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


for _ in range(100):
    try:
        if request('/')[0] == 200:
            break
    except (OSError, http.client.HTTPException):
        pass
    time.sleep(.2)
else:
    for c in containers:
        subprocess.run(['docker', 'logs', c])
    raise AssertionError('Fixture startup failed')

for path in ['/node/update', '/node/core_update', '/node/geofiles']:
    for bad_key in ['', 'wrong']:
        assert request(path, bad_key, '{}')[0] == 401
assert not (fake.parent / 'commands').exists()
for path, body in [('/node/update', '{}'), ('/node/core_update', '{"core_version":"26.11.9"}'),
                   ('/node/geofiles', '{"region":"russia"}')]:
    assert request(path, body=body)[0] == 200
assert (fake.parent / 'commands').read_text().splitlines() == [
    'update --no-update-service', 'core-update --version v26.11.9', 'geofiles --russia']
assert request('/unknown')[0] == 404
with context(True).wrap_socket(socket.create_connection(('127.0.0.1', 443)), server_hostname=DOMAIN) as s:
    assert s.selected_alpn_protocol() == 'h2'
    s.settimeout(5)
    h = H2Connection(config=H2Configuration(client_side=True, header_encoding='utf-8'))
    h.initiate_connection()
    h.send_headers(1, [(':method', 'POST'), (':scheme', 'https'), (':authority', DOMAIN),
                      (':path', '/service.Service/GetSystemStats'), ('content-type', 'application/grpc'),
                      ('te', 'trailers'), ('x-api-key', KEY)])
    h.send_data(1, b'\0' * 5, end_stream=True)
    s.sendall(h.data_to_send())
    status = None
    trailer = None
    payload = b''
    done = False
    while not done:
        data = s.recv(65535)
        assert data
        for event in h.receive_data(data):
            if isinstance(event, ResponseReceived):
                status = dict(event.headers)[':status']
            elif isinstance(event, DataReceived):
                payload += event.data
                h.acknowledge_received_data(event.flow_controlled_length, 1)
            elif isinstance(event, TrailersReceived):
                trailer = dict(event.headers)['grpc-status']
            elif isinstance(event, StreamEnded):
                done = True
        s.sendall(h.data_to_send())
    assert (status, trailer, payload) == ('200', '0', b'\0' * 5)
assert request('/', domain=values['NODE_DOMAIN']) == (200, b'existing-site')
print('PASS: official binary, API auth, three command contracts, gRPC HTTP/2, original SNI/PROXY route')
