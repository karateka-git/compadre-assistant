-- Обращения модели принадлежат возможности Ask и не привязаны к профилю чата.
CREATE TABLE ask_usage (
    call_id TEXT PRIMARY KEY NOT NULL CHECK (length(call_id) = 36),
    started_at_us INTEGER NOT NULL CHECK (typeof(started_at_us) = 'integer'),
    provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
    actual_model TEXT CHECK (actual_model IS NULL OR length(trim(actual_model)) > 0),
    tokens_json TEXT,
    tariff_json TEXT,
    cost_nanorubles INTEGER,
    unknown_reason TEXT,
    CHECK (
        (cost_nanorubles IS NULL AND unknown_reason IS NOT NULL AND length(trim(unknown_reason)) > 0)
        OR (typeof(cost_nanorubles) = 'integer' AND cost_nanorubles >= 0
            AND tariff_json IS NOT NULL AND unknown_reason IS NULL)
    )
);

CREATE INDEX idx_ask_usage_started_at ON ask_usage (started_at_us);
