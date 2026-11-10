# The existing SNI route reaches this listener; maintenance is loopback-only.
frontend management_http
    mode http
    option httplog
    bind /run/pgn/management.sock user haproxy group haproxy mode 600 ssl crt /etc/pgn/tls/management.pem alpn h2,http/1.1
    timeout http-request 15s
    acl maintenance_path path -m str /node/update /node/core_update /node/geofiles /node/hard_reset
    acl health_path path -m str /
    acl grpc_request req.hdr(content-type) -m beg application/grpc
    http-request deny deny_status 404 if !maintenance_path !health_path !grpc_request
    use_backend maintenance_api if maintenance_path
    use_backend maintenance_api if health_path
    use_backend management_grpc if grpc_request

backend maintenance_api
    mode http
    timeout server 330s
    server serviced 127.0.0.1:62051 ssl verify required ca-file /etc/pgn/tls/node-ca.pem verifyhost @@API_DOMAIN@@ sni str(@@API_DOMAIN@@)

backend management_grpc
    mode http
    server node 127.0.0.1:62050 ssl alpn h2 verify required ca-file /etc/pgn/tls/node-ca.pem verifyhost @@API_DOMAIN@@ sni str(@@API_DOMAIN@@)
