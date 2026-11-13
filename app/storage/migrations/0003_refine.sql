-- 0003_refine.sql — a follow-up turn must survive a restart.
--
-- Refining an app queues a job whose text is the user's follow-up instruction.
-- The queue is the only durable place for it: a job claimed seconds before the
-- process died has to be re-runnable after startup, so the instruction lives on
-- the job row rather than in the caller's memory.

ALTER TABLE jobs ADD COLUMN instruction TEXT NOT NULL DEFAULT '';
