-- Lab 4: task processing queue in PostgreSQL and its results.
-- Applied by the same migrate Job as 001.

CREATE TABLE tasks (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id           BIGINT       NOT NULL
        REFERENCES trial_participants (id) ON DELETE CASCADE,
    status              VARCHAR(16)  NOT NULL DEFAULT 'queued',
    seed                BIGINT       NOT NULL,
    attempts            INTEGER      NOT NULL DEFAULT 0,
    max_attempts        INTEGER      NOT NULL DEFAULT 3,
    token               BIGINT       NOT NULL DEFAULT 0,      -- fencing token
    lease_until         TIMESTAMPTZ  NULL,
    worker_id           VARCHAR(128) NULL,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    started_at          TIMESTAMPTZ  NULL,
    finished_at         TIMESTAMPTZ  NULL,
    -- D6 (variant 25): task cancellation
    cancel_requested    BOOLEAN      NOT NULL DEFAULT false,
    cancel_requested_at TIMESTAMPTZ  NULL,

    CONSTRAINT ck_tasks_status
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    CONSTRAINT ck_tasks_seed CHECK (seed >= 0 AND seed < 2147483648)
);

-- claiming the oldest queued task and counting by status
CREATE INDEX ix_tasks_status_created ON tasks (status, created_at);

CREATE TABLE task_results (
    task_id     BIGINT       PRIMARY KEY REFERENCES tasks (id) ON DELETE CASCADE,
    token       BIGINT       NOT NULL,
    worker_id   VARCHAR(128) NOT NULL,
    result      JSONB        NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);
