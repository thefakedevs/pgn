# Keep component IDs stable: they identify persistent checkpoints and buffers.
data_dir: /var/lib/vector
timezone: UTC
sources:
  xray:
    type: file
    include: [/var/log/xray/access.log, /var/log/xray/error.log]
    read_from: beginning
    max_line_bytes: 1048576
    acknowledgements:
      enabled: true
transforms:
  access_candidates:
    type: filter
    inputs: [xray]
    condition: 'match(string!(.message), r"\b(?:accepted|rejected)\b")'
  parse_access:
    type: remap
    inputs: [access_candidates]
    drop_on_error: true
    drop_on_abort: true
    reroute_dropped: true
    source: |
      p = parse_regex!(string!(.message), r'^(?P<time>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?) (?:from )?\S+ (?P<status>accepted|rejected) (?P<protocol>tcp|udp):(?P<host>\[[^\]]+\]|[^\s:]+)(?::(?P<port>\d+))?(?: \[(?P<route>[^\]]+)\])?(?: (?P<tail>.*))?$')
      ts = parse_timestamp!(p.time, format: "%Y/%m/%d %H:%M:%S%.f", timezone: "UTC")
      if to_unix_timestamp(ts) > to_unix_timestamp(now()) + 300 { abort }
      port = null
      if exists(p.port) && p.port != null {
        port = to_int!(p.port)
        if port < 1 || port > 65535 { abort }
      }
      email = null
      if exists(p.tail) && p.tail != null {
        e, err = parse_regex(p.tail, r'(?:^| )email: (?P<email>.+)$')
        if err == null { email = e.email }
      }
      . = {
        "event_time": ts,
        "email": email,
        "destination_host": replace(replace(p.host, r'^\[', ""), r'\]$', ""),
        "destination_port": port,
        "protocol": p.protocol,
        "status": p.status,
        "route": p.route,
        "node_id": get_env_var!("ACCESS_LOG_NODE_ID")
      }
  retained:
    type: filter
    inputs: [parse_access]
    condition: 'to_unix_timestamp(timestamp!(.event_time)) >= to_unix_timestamp(now()) - 2592000'
  diagnostic:
    type: remap
    inputs: [parse_access.dropped]
    source: |
      . = {"kind": "invalid_xray_access", "message": truncate(string!(.message), limit: 2048)}
sinks:
  postgres:
    type: postgres
    inputs: [retained]
    endpoint: ${ACCESS_LOG_POSTGRES_DSN}
    table: pgn.access_ingest
    pool_size: 2
    acknowledgements:
      enabled: true
    batch:
      max_events: 500
      timeout_secs: 1
    buffer:
      type: disk
      max_size: @@ACCESS_LOG_BUFFER_BYTES@@
      when_full: block
    request:
      concurrency: 1
      retry_initial_backoff_secs: 1
      retry_max_duration_secs: 30
  # Docker's json-file log is a separate bounded diagnostic file (10 MiB x 3).
  invalid_access:
    type: console
    inputs: [diagnostic]
    target: stderr
    encoding:
      codec: json
tests:
  - name: accepted_domain
    inputs:
      - insert_at: parse_access
        type: log
        log_fields:
          message: '2020/11/10 12:34:56.123456 from 1.2.3.4:1234 accepted tcp:example.org:443 [in -> direct] email: user@example.org'
    outputs:
      - extract_from: parse_access
        conditions:
          - type: vrl
            source: 'assert_eq!(.destination_host, "example.org"); assert_eq!(.destination_port, 443); assert_eq!(.email, "user@example.org"); assert_eq!(.route, "in -> direct"); assert_eq!(.status, "accepted")'
  - name: rejected_ipv6_without_email
    inputs:
      - insert_at: parse_access
        type: log
        log_fields:
          message: '2020/11/10 12:34:56 from [::1]:1234 rejected udp:[2001:db8::1]:53 blocked by policy'
    outputs:
      - extract_from: parse_access
        conditions:
          - type: vrl
            source: 'assert_eq!(.destination_host, "2001:db8::1"); assert_eq!(.destination_port, 53); assert_eq!(.email, null); assert_eq!(.route, null); assert_eq!(.status, "rejected"); assert_eq!(.protocol, "udp")'
  - name: ipv4_without_port_route_or_from
    inputs:
      - insert_at: parse_access
        type: log
        log_fields:
          message: '2020/11/10 12:34:56 127.0.0.1:1234 accepted tcp:192.0.2.1'
    outputs:
      - extract_from: parse_access
        conditions:
          - type: vrl
            source: 'assert_eq!(.destination_host, "192.0.2.1"); assert_eq!(.destination_port, null); assert_eq!(.email, null); assert_eq!(format_timestamp!(timestamp!(.event_time), format: "%Y"), "2020")'
  - name: malformed_access_diagnostic
    inputs:
      - insert_at: parse_access
        type: log
        log_fields:
          message: 'broken accepted tcp:example.org:443'
    outputs:
      - extract_from: diagnostic
        conditions:
          - type: vrl
            source: 'assert_eq!(.kind, "invalid_xray_access")'
  - name: invalid_port_diagnostic
    inputs:
      - insert_at: parse_access
        type: log
        log_fields:
          message: '2020/11/10 12:34:56 from 127.0.0.1:123 rejected tcp:example.org:99999'
    outputs:
      - extract_from: diagnostic
        conditions:
          - type: vrl
            source: 'assert_eq!(.kind, "invalid_xray_access")'
  - name: future_time_diagnostic
    inputs:
      - insert_at: parse_access
        type: log
        log_fields:
          message: '2099/11/10 12:34:56 from 127.0.0.1:123 accepted tcp:example.org:443'
    outputs:
      - extract_from: diagnostic
        conditions:
          - type: vrl
            source: 'assert_eq!(.kind, "invalid_xray_access")'
  - name: ignore_ordinary_errors
    inputs:
      - insert_at: access_candidates
        type: log
        log_fields:
          message: '2020/11/10 12:34:56 [Warning] failed to dial destination'
    no_outputs_from: [access_candidates]
  - name: expired_event_filtered
    inputs:
      - insert_at: parse_access
        type: log
        log_fields:
          message: '2020/11/10 12:34:56 from 127.0.0.1:123 accepted tcp:example.org:443'
    no_outputs_from: [retained]
