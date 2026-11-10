-- Apply once, in order, as the dedicated schema owner. PostgreSQL 16+.
BEGIN;
SET LOCAL TIME ZONE 'UTC';
CREATE SCHEMA pgn;
CREATE TABLE pgn.access_logs (
    id bigint GENERATED ALWAYS AS IDENTITY,
    event_time timestamptz NOT NULL,
    email text,
    destination_host text NOT NULL,
    destination_port integer CHECK (destination_port BETWEEN 1 AND 65535),
    protocol text NOT NULL CHECK (protocol IN ('tcp', 'udp')),
    status text NOT NULL CHECK (status IN ('accepted', 'rejected')),
    route text,
    node_id text NOT NULL,
    processed_at timestamptz,
    PRIMARY KEY (event_time, id)
) PARTITION BY RANGE (event_time);
CREATE INDEX access_logs_pending ON pgn.access_logs (event_time, id)
    WHERE processed_at IS NULL;

-- Vector uses INSERT ... SELECT * FROM jsonb_populate_recordset(NULL::view,...).
-- Exclude server-generated columns from its row type. Always insert via parent.
CREATE VIEW pgn.access_ingest AS
SELECT event_time, email, destination_host, destination_port, protocol, status, route, node_id
FROM pgn.access_logs;

CREATE FUNCTION pgn.insert_access_log() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    -- Recheck age after a potentially long-lived Vector disk-buffer backlog.
    IF NEW.event_time < statement_timestamp() - interval '30 days' THEN
        RETURN NULL;
    END IF;
    IF NEW.event_time > statement_timestamp() + interval '5 minutes' THEN
        RAISE EXCEPTION 'access event timestamp is in the future';
    END IF;
    INSERT INTO pgn.access_logs
        (event_time, email, destination_host, destination_port, protocol, status, route, node_id)
    VALUES
        (NEW.event_time, NEW.email, NEW.destination_host, NEW.destination_port,
         NEW.protocol, NEW.status, NEW.route, NEW.node_id);
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION pgn.insert_access_log() FROM PUBLIC;
CREATE TRIGGER insert_access_log INSTEAD OF INSERT ON pgn.access_ingest
    FOR EACH ROW EXECUTE FUNCTION pgn.insert_access_log();
REVOKE ALL ON SCHEMA pgn FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA pgn FROM PUBLIC;
COMMIT;
