-- First acquisition timestamp, in Unix milliseconds (same unit as other platforms).
-- Existing rows with no acquisition time use the migration time, not publish_time.
-- Idempotent: valid existing acquisition timestamps are retained.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE tieba_note ADD COLUMN IF NOT EXISTS add_ts bigint;
ALTER TABLE tieba_note ALTER COLUMN add_ts
    SET DEFAULT ((extract(epoch FROM clock_timestamp()) * 1000)::bigint);
UPDATE tieba_note
    SET add_ts = (extract(epoch FROM transaction_timestamp()) * 1000)::bigint
    WHERE add_ts IS NULL OR add_ts <= 0;
ALTER TABLE tieba_note ALTER COLUMN add_ts SET NOT NULL;
COMMENT ON COLUMN tieba_note.add_ts IS '首次采集时间戳（Unix毫秒）；2026-10-15之前缺失值按迁移时间回填';
CREATE INDEX IF NOT EXISTS tieba_note_add_ts_idx ON tieba_note(add_ts);
COMMIT;
