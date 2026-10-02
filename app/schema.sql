CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_domain TEXT NOT NULL
        CHECK (collection_domain IN ('software_dev', 'ai', 'accounting')),
    domain TEXT NOT NULL
        CHECK (domain IN ('software', 'workflow', 'research')),
    status TEXT NOT NULL
        CHECK (status IN ('draft', 'approved', 'rejected')),
    goal TEXT NOT NULL,
    improve TEXT NOT NULL,
    worsens TEXT NOT NULL,
    ideal TEXT NOT NULL,
    physical TEXT,
    constraints TEXT NOT NULL,
    resources TEXT NOT NULL,
    evidence TEXT NOT NULL,
    source_code TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_title TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    published_at TEXT,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    language TEXT NOT NULL CHECK (language IN ('ja', 'en', 'und')),
    doi TEXT,
    truncated INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0, 1)),
    reject_reason TEXT,
    approved_at TEXT,
    sebas_pushed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_url),
    UNIQUE (content_sha256)
);

CREATE INDEX idx_items_collection_domain ON items(collection_domain);
CREATE INDEX idx_items_domain ON items(domain);
CREATE INDEX idx_items_fetched_at ON items(fetched_at);
CREATE INDEX idx_items_status ON items(status);

CREATE TABLE item_bodies (
    item_id INTEGER PRIMARY KEY REFERENCES items(id),
    body TEXT NOT NULL,
    stored_at TEXT NOT NULL
);

CREATE TABLE item_duplicates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kept_item_id INTEGER NOT NULL REFERENCES items(id),
    duplicate_url TEXT NOT NULL,
    duplicate_sha256 TEXT,
    detected_at TEXT NOT NULL
);

CREATE TABLE collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'scheduled')),
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    draft_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE collection_run_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    source_code TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'skipped')),
    fetched_count INTEGER NOT NULL DEFAULT 0,
    draft_count INTEGER NOT NULL DEFAULT 0,
    finished_at TEXT,
    UNIQUE (run_id, source_code)
);

CREATE TABLE collection_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    source_code TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    http_status INTEGER,
    message TEXT NOT NULL
);

CREATE INDEX idx_errors_occurred_at ON collection_errors(occurred_at);
