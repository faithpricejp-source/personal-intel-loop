-- 004: 「报纸」阅读面 (paper v2)。契约: docs/paper_v2_contract.md
-- item_ai: 每条新闻一份 AI 预处理结果(lede/one_liner/byline/lane...), JSON 存 payload_json
CREATE TABLE item_ai (item_id TEXT PRIMARY KEY REFERENCES items(item_id), payload_json TEXT NOT NULL, model TEXT, error TEXT, created_at TEXT NOT NULL);

-- item_fulltext: 抓回来的正文; status: ok / failed / skipped
CREATE TABLE item_fulltext (item_id TEXT PRIMARY KEY REFERENCES items(item_id), text TEXT, status TEXT NOT NULL, fetched_at TEXT NOT NULL);

-- editions: 一天一期; section ∈ lead/top/briefs/blind, (edition_date, item_id) 唯一, 同日重跑先删后写
CREATE TABLE editions (edition_date TEXT NOT NULL, item_id TEXT NOT NULL REFERENCES items(item_id), section TEXT NOT NULL CHECK(section IN ('lead','top','briefs','blind')), rank INTEGER NOT NULL, blind_reason TEXT, built_at TEXT NOT NULL, PRIMARY KEY(edition_date, item_id));

-- item_ratings: 用户对单条的五维评分, 同 (item_id, dim) 只留一条; value=0 = 撤销后删除
CREATE TABLE item_ratings (item_id TEXT NOT NULL REFERENCES items(item_id), dim TEXT NOT NULL CHECK(dim IN ('overall','quality','author','style','topic')), value INTEGER NOT NULL CHECK(value IN (1,-1)), author_key TEXT, ts TEXT NOT NULL, distilled_at TEXT, PRIMARY KEY(item_id, dim));

-- author_trust: 作者级信任旋钮, 分数 = (2+n_up)/(4+n_up+n_down)
CREATE TABLE author_trust (author_key TEXT PRIMARY KEY, label TEXT NOT NULL, n_up INTEGER NOT NULL DEFAULT 0, n_down INTEGER NOT NULL DEFAULT 0, muted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);

-- source_overrides: 手动静音/加权源, 出版时生效
CREATE TABLE source_overrides (source TEXT PRIMARY KEY, muted INTEGER NOT NULL DEFAULT 0, boosted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);

-- read_events: 只记录不入排序 (v1)
CREATE TABLE read_events (item_id TEXT NOT NULL, dwell_ms INTEGER NOT NULL, ts TEXT NOT NULL);

CREATE INDEX IF NOT EXISTS idx_editions_date_rank ON editions(edition_date, section, rank);
CREATE INDEX IF NOT EXISTS idx_item_ratings_distilled ON item_ratings(distilled_at);
CREATE INDEX IF NOT EXISTS idx_read_events_ts ON read_events(ts);
