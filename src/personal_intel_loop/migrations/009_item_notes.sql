-- 反馈里的批注（更详细的反馈）：一条一份，可改；空文本即删除
CREATE TABLE IF NOT EXISTS item_notes (item_id TEXT PRIMARY KEY REFERENCES items(item_id), text TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_item_notes_updated ON item_notes(updated_at);
