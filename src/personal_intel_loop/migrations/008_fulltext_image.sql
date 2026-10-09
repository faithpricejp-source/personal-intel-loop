-- WSJ 式图文版面：抓全文时顺带记下文章主图（og:image / twitter:image）
ALTER TABLE item_fulltext ADD COLUMN image_url TEXT;
