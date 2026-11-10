# Source and changes

Vendored from [PasarGuard/node-serviced v0.0.4](https://github.com/PasarGuard/node-serviced/tree/v0.0.4).
The upstream GPL-3.0 license is included in LICENSE and applies to these files.

PGN changes:

- Unix socket transport only; TCP, TLS and API_PORT listeners are removed.
  Existing HAProxy terminates TLS with the node's unchanged certificate.
- SOCKET_PATH is required. Socket permissions are 0660; SOCKET_GID grants only
  the HAProxy container's group access. APP_NAME is an absolute path to PGN.
- Existing active sockets and ordinary files are never unlinked on startup.
- Constant-time API key comparison, exact health route, serialized operations.
- SIGTERM closes the socket. Update commands retain upstream request-independent
  contexts, so closing a browser does not interrupt an update.

The upstream endpoints and command-line contract remain unchanged. Host-side
commands are implemented in ../../scripts/pgn_maintenance.py, not the upstream
installer: they preserve PGN's Compose project, node identity and routing.

Tests use Unix sockets and fake commands. They never call a production node.
