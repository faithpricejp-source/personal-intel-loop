-- 006: 本机投递箱 (paper v2 契约第 5 节)。本机各项目的通知改投报纸, 紧急的弹 macOS 系统通知。
-- inbox: 投递进来的条目; source 用 <项目>.<用途> 命名; read_at 非空 = 已读(点开即标)
CREATE TABLE inbox (inbox_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', url TEXT, priority TEXT NOT NULL CHECK(priority IN ('urgent','normal','low')), dedup_key TEXT, created_at TEXT NOT NULL, read_at TEXT);

-- 同 dedup_key 24 小时内只收一次, 查重走 created_at 范围扫描
CREATE INDEX idx_inbox_dedup_key ON inbox(dedup_key);
CREATE INDEX idx_inbox_created_at ON inbox(created_at);

-- notified 表(契约 5 节标注可选)不建: 推送去重由 since 水位 + 每期/每条 id 天然幂等保证
