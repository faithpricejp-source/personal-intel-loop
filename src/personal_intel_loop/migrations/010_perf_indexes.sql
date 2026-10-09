-- 010 · 热路径索引（依 SQL 性能审计的 EXPLAIN QUERY PLAN 实测）
-- 起因：同日 _sync_fts 按 UNINDEXED 列删除导致抓取 2 小时持锁；审计找同类全表扫。
CREATE INDEX IF NOT EXISTS idx_items_ts_id ON items(ts, item_id);                              -- P01 归档浏览排序
CREATE INDEX IF NOT EXISTS idx_editions_item_date ON editions(item_id, edition_date);           -- P02 按条目查上版记录
CREATE INDEX IF NOT EXISTS idx_ui_events_item_ts ON ui_events(item_id, ts);                     -- P04 校准统计
CREATE INDEX IF NOT EXISTS idx_items_first_ingested_ts ON items(first_ingested_at, ts);         -- P05 出版/风险/盲区/温暖窗口
CREATE INDEX IF NOT EXISTS idx_items_source_fing_ts ON items(source, first_ingested_at, ts);     -- P07/P11 来源列表与信任重算
CREATE INDEX IF NOT EXISTS idx_items_url ON items(url);                                         -- P08 vault 引文回退
CREATE INDEX IF NOT EXISTS idx_promotion_events_type_ts ON promotion_events(event_type, event_ts); -- P10
ANALYZE;
