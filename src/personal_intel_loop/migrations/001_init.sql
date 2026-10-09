CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  author TEXT,
  ts TEXT NOT NULL,
  lang TEXT NOT NULL DEFAULT 'unknown',
  embedding BLOB,
  transcript TEXT,
  summary TEXT,
  tags_json TEXT NOT NULL DEFAULT '[]',
  source_payload_json TEXT NOT NULL DEFAULT '{}',
  media_manifest_relpath TEXT,
  content_hash TEXT NOT NULL,
  adapter_name TEXT NOT NULL,
  first_ingested_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  item_status TEXT NOT NULL DEFAULT 'new'
    CHECK(item_status IN ('new', 'digested', 'promoted', 'rejected', 'deferred'))
);

CREATE INDEX IF NOT EXISTS idx_items_source_ts
ON items(source, ts DESC);

CREATE INDEX IF NOT EXISTS idx_items_status_ts
ON items(item_status, ts DESC);

CREATE TABLE IF NOT EXISTS source_trust (
  source TEXT PRIMARY KEY,
  adapter_name TEXT NOT NULL,
  trust_score REAL NOT NULL DEFAULT 0.35,
  prior_score REAL NOT NULL DEFAULT 0.35,
  prior_weight REAL NOT NULL DEFAULT 12.0,
  ingested_items_seen_90d INTEGER NOT NULL DEFAULT 0,
  digested_items_seen_90d INTEGER NOT NULL DEFAULT 0,
  promote_src_90d INTEGER NOT NULL DEFAULT 0,
  promote_evd_90d INTEGER NOT NULL DEFAULT 0,
  cited_jdg_90d INTEGER NOT NULL DEFAULT 0,
  linked_cas_90d INTEGER NOT NULL DEFAULT 0,
  linked_dec_90d INTEGER NOT NULL DEFAULT 0,
  linked_mon_90d INTEGER NOT NULL DEFAULT 0,
  last_event_ts TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digest_inclusions (
  inclusion_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  source TEXT NOT NULL,
  digest_kind TEXT NOT NULL DEFAULT 'daily',
  digest_date TEXT NOT NULL,
  digest_path TEXT NOT NULL,
  included_at_utc TEXT NOT NULL,
  FOREIGN KEY(item_id) REFERENCES items(item_id),
  UNIQUE(item_id, digest_kind, digest_date)
);

CREATE INDEX IF NOT EXISTS idx_digest_inclusions_source_ts
ON digest_inclusions(source, included_at_utc DESC);

CREATE INDEX IF NOT EXISTS idx_digest_inclusions_digest
ON digest_inclusions(digest_kind, digest_date);

CREATE TABLE IF NOT EXISTS promotion_events (
  event_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  source TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_weight REAL NOT NULL,
  origin TEXT NOT NULL,
  digest_path TEXT,
  vault_object_type TEXT,
  vault_object_id TEXT,
  note TEXT,
  event_ts TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(item_id) REFERENCES items(item_id)
);

CREATE INDEX IF NOT EXISTS idx_promotion_events_item_ts
ON promotion_events(item_id, event_ts DESC);

CREATE INDEX IF NOT EXISTS idx_promotion_events_source_ts
ON promotion_events(source, event_ts DESC);
