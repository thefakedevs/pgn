-- Apply once after 001/002, as the schema owner, before updating Vector.
BEGIN;
SET LOCAL TIME ZONE 'UTC';
SET LOCAL lock_timeout = '5s';

ALTER TABLE pgn.access_logs
    ADD COLUMN source_ip inet,
    ADD COLUMN outbound_tag text,
    ADD COLUMN reason text;

-- Append columns to preserve the view, its trigger, and existing grants.
-- Old producers omit these JSON keys and continue inserting NULL values.
CREATE OR REPLACE VIEW pgn.access_ingest AS
SELECT event_time, email, destination_host, destination_port, protocol, status, route, node_id,
       source_ip, outbound_tag, reason
FROM pgn.access_logs;

CREATE OR REPLACE FUNCTION pgn.insert_access_log() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NEW.event_time < statement_timestamp() - interval '30 days' THEN
        RETURN NULL;
    END IF;
    IF NEW.event_time > statement_timestamp() + interval '5 minutes' THEN
        RAISE EXCEPTION 'access event timestamp is in the future';
    END IF;
    INSERT INTO pgn.access_logs
        (event_time, email, destination_host, destination_port, protocol, status, route, node_id,
         source_ip, outbound_tag, reason)
    VALUES
        (NEW.event_time, NEW.email, NEW.destination_host, NEW.destination_port,
         NEW.protocol, NEW.status, NEW.route, NEW.node_id,
         NEW.source_ip, NEW.outbound_tag, NEW.reason);
    RETURN NEW;
END;
$$;
COMMIT;
