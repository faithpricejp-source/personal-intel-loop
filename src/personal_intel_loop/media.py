from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from personal_intel_loop.schemas import Item


def safe_item_dirname(item_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", item_id).strip("_") or "item"


def ensure_staging_media_symlink(staging_dir: Path, media_root: Path) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    link_path = staging_dir / "pil_media"
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_symlink() and link_path.resolve() == media_root.resolve():
            return link_path
        raise RuntimeError(f"existing pil_media path is not the expected symlink: {link_path}")
    link_path.symlink_to(media_root)
    return link_path


def _choose_filename(source_url: str, content_type: str | None, index: int) -> str:
    parsed = urlparse(source_url)
    base = Path(parsed.path).name
    if base:
        return base
    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
    return f"asset_{index:03d}{ext}"


def localize_item_media(item: Item, media_urls: list[str], *, media_root: Path) -> str | None:
    unique_urls = [url for idx, url in enumerate(media_urls) if url and url not in media_urls[:idx]]
    if not unique_urls:
        return None

    dirname = safe_item_dirname(item.id)
    item_dir = media_root / dirname
    item_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = item_dir / "manifest.json"

    # 已有 manifest 只补下缺失的 URL: 上次部分超时的图要能重试, 不能因 manifest 存在就永久跳过
    assets: list[dict] = []
    if manifest_path.exists():
        assets = json.loads(manifest_path.read_text("utf-8")).get("assets", [])
    done_urls = {a.get("source_url") for a in assets}
    if done_urls and all(url in done_urls for url in unique_urls):
        return f"data/media/{dirname}/manifest.json"

    used_names: set[str] = {Path(a.get("local_relpath", "")).name for a in assets}
    added = 0
    for index, source_url in enumerate(unique_urls, start=1):
        if source_url in done_urls:
            continue
        try:
            response = requests.get(source_url, timeout=20)
            response.raise_for_status()
        except Exception:
            continue
        filename = _choose_filename(source_url, response.headers.get("Content-Type"), index)
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        candidate = filename
        counter = 1
        while candidate in used_names:
            candidate = f"{stem}_{counter}{suffix}"
            counter += 1
        used_names.add(candidate)
        target_path = item_dir / candidate
        target_path.write_bytes(response.content)
        assets.append(
            {
                "kind": "image",
                "source_url": source_url,
                "local_relpath": f"data/media/{dirname}/{candidate}",
            }
        )
        added += 1

    if not assets:
        return None

    if added:
        manifest = {"item_id": item.id, "assets": assets}
        tmp_path = manifest_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
        tmp_path.replace(manifest_path)
    return f"data/media/{dirname}/manifest.json"
