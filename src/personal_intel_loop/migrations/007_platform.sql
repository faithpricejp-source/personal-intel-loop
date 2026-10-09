-- 007: 全平台接入 (paper v2 契约第 6、8、9、10 节; TASK4)
-- auth_status: 登录态探针结果 (6.1); ok 1/0, since_failing=由真转假的时刻, 恢复后清 NULL
CREATE TABLE auth_status (key TEXT PRIMARY KEY, ok INTEGER NOT NULL, checked_at TEXT NOT NULL, detail TEXT, since_failing TEXT);

-- follow_suggestions: 推荐关注 (6.3); status: new → followed(系统自动判定) / dismissed(用户拒绝)
CREATE TABLE follow_suggestions (id TEXT PRIMARY KEY, platform TEXT NOT NULL, account_id TEXT NOT NULL, label TEXT NOT NULL, url TEXT, reason TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','followed','dismissed')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX idx_follow_suggestions_status ON follow_suggestions(status);

-- digests: 合成条目(讨论综述 digest:* / 行前风险 risk:* / 闲与美 leisure:*)的完整 Item payload;
-- 上版位置照常写 editions(item_id 用各自前缀, 见下方 editions 重建——去掉 items 外键)
CREATE TABLE digests (digest_id TEXT PRIMARY KEY, edition_date TEXT NOT NULL, payload_json TEXT NOT NULL, member_ids_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL);
CREATE INDEX idx_digests_edition_date ON digests(edition_date);

-- trips: 行程(9 节风险提示栏的个性化输入)
CREATE TABLE trips (trip_id TEXT PRIMARY KEY, place TEXT NOT NULL, country TEXT, start_date TEXT NOT NULL, end_date TEXT, note TEXT, created_at TEXT NOT NULL);

-- claim_prechecks: 结算栏的 AI 初判缓存 (10.2); 同一 claim 只初判一次
CREATE TABLE claim_prechecks (claim_id TEXT PRIMARY KEY REFERENCES claims(claim_id), verdict TEXT NOT NULL CHECK(verdict IN ('likely_true','likely_false','unclear')), basis TEXT NOT NULL DEFAULT '', links_json TEXT NOT NULL DEFAULT '[]', model TEXT, created_at TEXT NOT NULL);

-- items_fts: 全部来源检索 (6.2) — items 的 title+body; ingest 写入同步更新(失败不影响写入), pil archive-reindex 可重建
CREATE VIRTUAL TABLE items_fts USING fts5(item_id UNINDEXED, title, body);

-- editions 重建: 004 的 CHECK 只认 lead/top/briefs/blind, 契约 7.2/8/9/10 新增
-- counter/warmth/risk/opportunity/settle/leisure 分区; 综述/风险/闲与美条目不在 items 表
-- 里, 同步去掉 item_id 的外键(位置信息仍在 (edition_date, item_id) 主键下)。
CREATE TABLE editions_new (edition_date TEXT NOT NULL, item_id TEXT NOT NULL, section TEXT NOT NULL CHECK(section IN ('lead','top','briefs','blind','counter','warmth','risk','opportunity','settle','leisure')), rank INTEGER NOT NULL, blind_reason TEXT, built_at TEXT NOT NULL, PRIMARY KEY(edition_date, item_id));
INSERT INTO editions_new (edition_date, item_id, section, rank, blind_reason, built_at) SELECT edition_date, item_id, section, rank, blind_reason, built_at FROM editions;
DROP TABLE editions;
ALTER TABLE editions_new RENAME TO editions;
CREATE INDEX IF NOT EXISTS idx_editions_date_rank ON editions(edition_date, section, rank);
