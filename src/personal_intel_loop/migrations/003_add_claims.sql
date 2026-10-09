-- 可检验断言: 摘要器从每条候选里抽出的一句可证伪断言/预测, 靠时间回填 outcome。
-- 这是 RSS lane 的价值单位(断言, 不是文章), 见 docs/phase2/日报规格.md B 段。
CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  source TEXT NOT NULL,
  claim TEXT NOT NULL,
  check_after TEXT,
  extracted_at TEXT NOT NULL,
  outcome TEXT CHECK(outcome IS NULL OR outcome IN ('true', 'false', 'unresolvable')),
  outcome_note TEXT,
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_claims_item ON claims(item_id);
CREATE INDEX IF NOT EXISTS idx_claims_open ON claims(outcome, check_after);
