-- Схема сутності «Учасник клінічного дослідження» (варіант 25).
-- Накочується разовим контейнером migrate (python -m app.migrate),
-- а НЕ через /docker-entrypoint-initdb.d.

CREATE TABLE trial_participants (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    participant_code    VARCHAR(32)  NOT NULL,
    protocol            VARCHAR(64)  NOT NULL,
    randomization_group VARCHAR(16)  NOT NULL,
    enrollment_date     DATE         NOT NULL,
    status              VARCHAR(16)  NOT NULL,
    completion_date     DATE         NULL,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_trial_participants_code
        UNIQUE (participant_code),
    CONSTRAINT ck_trial_participants_group
        CHECK (randomization_group IN ('A', 'B', 'контроль')),
    CONSTRAINT ck_trial_participants_status
        CHECK (status IN ('screening', 'active', 'completed', 'withdrawn')),
    CONSTRAINT ck_trial_participants_dates
        CHECK (completion_date IS NULL OR completion_date >= enrollment_date)
);

-- Фільтр переліку за групою + стабільна пагінація ORDER BY id
CREATE INDEX ix_trial_participants_group_id
    ON trial_participants (randomization_group, id);
