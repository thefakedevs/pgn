global
    user haproxy
    group haproxy
    log stdout format raw local0
    maxconn 10000

defaults
    log global
    mode tcp
    option tcplog
    timeout connect 5s
    timeout client 2h
    timeout server 2h

frontend shared_443
    bind 0.0.0.0:443
    tcp-request inspect-delay 5s
    acl is_panel req.ssl_sni -i @@API_DOMAIN@@
    tcp-request content accept if { req.ssl_hello_type 1 }
    use_backend management if is_panel
    default_backend reality

backend management
    server node 127.0.0.1:62050

backend reality
    server xray 127.0.0.1:10443 send-proxy-v2
