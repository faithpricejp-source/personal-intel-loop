"""2026-10-02 复核 PIL n1-MISS01: 部分图片超时后 manifest 落盘, 之后早退, 失败的图永不重试。"""
import json

import personal_intel_loop.media as media
from tests.conftest import make_item


class _Resp:
    def __init__(self, content=b"img"):
        self.content = content
        self.headers = {"Content-Type": "image/png"}

    def raise_for_status(self):
        pass


def test_failed_image_is_retried_on_next_call(tmp_path, monkeypatch):
    urls = ["https://x/a.png", "https://x/b.png"]
    down = {"https://x/b.png"}

    def fake_get(url, timeout):
        if url in down:
            raise TimeoutError(url)
        return _Resp()

    monkeypatch.setattr(media.requests, "get", fake_get)
    item = make_item(item_id="rss:media")
    rel = media.localize_item_media(item, urls, media_root=tmp_path)
    manifest = tmp_path / rel.split("data/media/", 1)[1]
    assert [a["source_url"] for a in json.loads(manifest.read_text())["assets"]] == ["https://x/a.png"]

    down.clear()
    media.localize_item_media(item, urls, media_root=tmp_path)
    got = [a["source_url"] for a in json.loads(manifest.read_text())["assets"]]
    assert got == ["https://x/a.png", "https://x/b.png"]
