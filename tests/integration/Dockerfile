FROM postgres:16-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-16-partman openssl \
    && rm -rf /var/lib/apt/lists/*
COPY server.ext /tmp/server.ext
RUN mkdir /certs && openssl req -x509 -newkey rsa:2048 -nodes -days 2 \
    -subj /CN=PGN-CI-Root -addext basicConstraints=critical,CA:TRUE \
    -keyout /certs/ca.key -out /certs/ca.crt \
    && openssl req -new -newkey rsa:2048 -nodes -subj /CN=localhost \
    -keyout /certs/server.key -out /certs/server.csr \
    && openssl x509 -req -in /certs/server.csr -CA /certs/ca.crt -CAkey /certs/ca.key \
    -CAcreateserial -days 2 -sha256 -extfile /tmp/server.ext -out /certs/server.crt \
    && chown -R postgres:postgres /certs && chmod 600 /certs/server.key
CMD ["postgres", "-c", "ssl=on", "-c", "ssl_cert_file=/certs/server.crt", "-c", "ssl_key_file=/certs/server.key", "-c", "shared_preload_libraries=pg_partman_bgw", "-c", "pg_partman_bgw.dbname=pgn", "-c", "pg_partman_bgw.interval=3600", "-c", "timezone=UTC"]
