CREATE TABLE IF NOT EXISTS synthetic_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at_utc TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS synthetic_streams (
    source_instance_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    last_sequence BIGINT NOT NULL CHECK (last_sequence >= 1),
    head_hash CHAR(64) NOT NULL,
    last_received_at_utc TIMESTAMPTZ NOT NULL,
    stream_state TEXT NOT NULL CHECK (stream_state IN ('FRESH', 'CORRUPT')),
    handshake_ok BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS synthetic_snapshots (
    source_instance_id TEXT NOT NULL REFERENCES synthetic_streams(source_instance_id),
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    snapshot_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    received_at_utc TIMESTAMPTZ NOT NULL,
    previous_hash CHAR(64),
    snapshot_hash CHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (source_instance_id, sequence),
    UNIQUE (source_instance_id, snapshot_hash)
);

CREATE INDEX IF NOT EXISTS synthetic_snapshots_received_idx
    ON synthetic_snapshots (source_instance_id, received_at_utc DESC);
