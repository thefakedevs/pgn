-- psql -v batch_size=500 -f sql/process_batch.sql DATABASE
-- Example transaction for ONE consumer group. Replace the marked section with
-- database work in this transaction. Never commit before that work succeeds.
BEGIN;
CREATE TEMP TABLE pgn_batch ON COMMIT DROP AS
SELECT event_time, id FROM pgn.access_logs
WHERE processed_at IS NULL
ORDER BY event_time, id
LIMIT :batch_size
FOR UPDATE SKIP LOCKED;

-- Business action goes here. The selected rows remain locked until COMMIT.
SELECT logs.* FROM pgn.access_logs logs
JOIN pgn_batch batch USING (event_time, id);

UPDATE pgn.access_logs logs SET processed_at = clock_timestamp()
FROM pgn_batch batch
WHERE logs.event_time = batch.event_time AND logs.id = batch.id;
COMMIT;
