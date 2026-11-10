-- pg_partman 5.x must be installed in schema partman by a database administrator.
BEGIN;
SET LOCAL TIME ZONE 'UTC';
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
                   WHERE e.extname='pg_partman' AND e.extversion LIKE '5.%' AND n.nspname='partman') THEN
        RAISE EXCEPTION 'Install pg_partman 5.x in schema partman before this migration';
    END IF;
END;
$$;
SELECT partman.create_parent(
    p_parent_table := 'pgn.access_logs',
    p_control := 'event_time',
    p_interval := '1 day',
    p_premake := 7,
    p_start_partition := (date_trunc('day', CURRENT_TIMESTAMP) - interval '30 days')::text,
    p_default_table := false
);
UPDATE partman.part_config
SET retention = '30 days', retention_keep_table = false,
    retention_keep_index = false, infinite_time_partitions = true,
    automatic_maintenance = 'on'
WHERE parent_table = 'pgn.access_logs';
COMMIT;
