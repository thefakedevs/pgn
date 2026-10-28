{
    auto_https disable_redirects
    admin off
    servers {
        protocols h1 h2
    }
}

@@NODE_DOMAIN@@:8443 {
    bind 127.0.0.1
    tls {
        issuer acme {
            disable_tlsalpn_challenge
        }
    }

    @xhttp path @@XHTTP_BASE@@ @@XHTTP_PATH@@*
    handle @xhttp {
        reverse_proxy h2c://127.0.0.1:10444 {
            flush_interval -1
        }
    }
    handle {
        root * /srv
        encode zstd gzip
        file_server
    }
}

http://@@NODE_DOMAIN@@ {
    redir https://{host}{uri} 308
}
