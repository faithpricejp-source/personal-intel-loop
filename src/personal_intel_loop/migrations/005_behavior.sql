-- 005: 行为监测 → AI 学习 → 调推荐 (paper v2 契约第 4 节 + 7.3 节护栏)
-- ui_events: 前端上报的阅读行为事件; ts 为前端 ISO 8601 原文(带时区), meta_json 存 meta 对象
CREATE TABLE ui_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL, item_id TEXT, edition_date TEXT, ms INTEGER, meta_json TEXT NOT NULL DEFAULT '{}', received_at TEXT NOT NULL);
CREATE INDEX idx_ui_events_ts ON ui_events(ts);

-- knob_offsets: AI 对三类旋钮的累计偏移; 有效值 = 基础值(trust_score / author_score / 0.5) + offset, 夹在 [0.05, 0.95]
CREATE TABLE knob_offsets (kind TEXT NOT NULL CHECK(kind IN ('source','author','topic')), key TEXT NOT NULL, offset REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY(kind, key));

-- ai_adjustments: 每次 AI 调整的留痕(before/after 为有效值), reverted=1 表示已撤销(delta 已从 offset 减回)
CREATE TABLE ai_adjustments (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, for_date TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, label TEXT NOT NULL, delta REAL NOT NULL, before REAL NOT NULL, after REAL NOT NULL, reason TEXT NOT NULL, reverted INTEGER NOT NULL DEFAULT 0, model TEXT);
CREATE INDEX idx_ai_adjustments_for_date ON ai_adjustments(for_date);

-- edition_notes: 「昨天你是怎么读的」, edition_date = 行为日期 + 1(即下一期的日期)
CREATE TABLE edition_notes (edition_date TEXT PRIMARY KEY, reading_note TEXT, model TEXT, created_at TEXT NOT NULL);
