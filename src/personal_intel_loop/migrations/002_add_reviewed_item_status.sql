PRAGMA foreign_keys=OFF;

CREATE TABLE items_new (
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
    CHECK(item_status IN ('new', 'digested', 'reviewed', 'promoted', 'rejected', 'deferred'))
);

INSERT INTO items_new (
  item_id,
  source,
  url,
  title,
  body,
  author,
  ts,
  lang,
  embedding,
  transcript,
  summary,
  tags_json,
  source_payload_json,
  media_manifest_relpath,
  content_hash,
  adapter_name,
  first_ingested_at,
  last_seen_at,
  item_status
)
SELECT
  item_id,
  source,
  url,
  title,
  body,
  author,
  ts,
  lang,
  embedding,
  transcript,
  summary,
  tags_json,
  source_payload_json,
  media_manifest_relpath,
  content_hash,
  adapter_name,
  first_ingested_at,
  last_seen_at,
  item_status
FROM items;

DROP TABLE items;

ALTER TABLE items_new RENAME TO items;

CREATE INDEX idx_items_source_ts
ON items(source, ts DESC);

CREATE INDEX idx_items_status_ts
ON items(item_status, ts DESC);

PRAGMA foreign_keys=ON;
