"""纯 Markdown digest 解析器,供局域网阅读页面和测试使用。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from personal_intel_loop.feedback import DIGEST_RE, ITEM_BLOCK_RE


@dataclass
class DigestView:
    date: str
    ranking_version: str
    claims: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    profile_status: str = ""


_ITEM_TITLE_RE = re.compile(r"^## \[(?P<item_id>[^]]+)\] (?P<title>.*)$", re.MULTILINE)
_LINK_RE = re.compile(r"^- \[原链\]\((?P<url>[^)]+)\)\s*$", re.MULTILINE)
_CLAIM_RE = re.compile(r"^- \[(?P<label>[^]]+)\] (?P<text>.*?) \(核验起点: (?P<check_after>.*?)\) <!-- pil_claim=(?P<claim_id>[^ ]*) -->$", re.MULTILINE)
_TIER_RE = re.compile(r"^## (?P<header>⚠️ 预警|📖 Longform|🔁 Pulse|🎧 Transcript Backlog)", re.MULTILINE)


def _tier_for(text: str, offset: int) -> str | None:
    found = list(_TIER_RE.finditer(text[:offset]))
    if not found:
        return None
    header = found[-1].group("header")
    return {"⚠️ 预警": "alert", "📖 Longform": "longform", "🔁 Pulse": "pulse",
            "🎧 Transcript Backlog": "transcript_backlog"}[header]


def _line_value(body: str, prefix: str) -> str | None:
    for line in body.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def _summary(body: str, origin_start: int) -> str:
    lines = body.splitlines()
    metadata_seen = False
    metadata_done = False
    out: list[str] = []
    for line in lines:
        if line.startswith("- [原链]("):
            break
        if not metadata_done:
            if line.startswith("## ["):
                continue
            if line.startswith("-"):
                metadata_seen = True
                continue
            if not line.strip():
                continue
            if metadata_seen:
                metadata_done = True
            else:
                continue
        if metadata_done and not line.startswith("![](") and not line.startswith("- "):
            out.append(line)
    return "\n".join(out).strip()


def _translation(body: str) -> str | None:
    marker = "**中文译文**"
    if marker not in body:
        return None
    text = body.split(marker, 1)[1]
    text = text.split("\n- [本地", 1)[0]
    text = text.split("\n- 可检验断言:", 1)[0]
    text = text.split("\n### Feedback", 1)[0]
    return text.strip() or None


def parse_digest(md_text: str) -> DigestView:
    header = DIGEST_RE.search(md_text)
    if not header:
        raise ValueError("not an Intel Loop digest")
    view = DigestView(date=header.group("date"), ranking_version=header.group("ranking"))
    view.profile_status = _line_value(md_text[header.end():md_text.find("## 可检验断言") if "## 可检验断言" in md_text else len(md_text)], "- profile:") or ""
    claims_start = md_text.find("## 可检验断言")
    if claims_start >= 0:
        claims_end = md_text.find("\n## ", claims_start + 3)
        claims_text = md_text[claims_start:claims_end if claims_end >= 0 else len(md_text)]
        for match in _CLAIM_RE.finditer(claims_text):
            view.claims.append({"claim_id": match.group("claim_id"), "source_label": match.group("label"), "text": match.group("text"), "check_after": match.group("check_after")})

    for match in ITEM_BLOCK_RE.finditer(md_text):
        body = match.group("body")
        title_match = _ITEM_TITLE_RE.search(body)
        if not title_match:
            continue
        url_match = _LINK_RE.search(body)
        view.items.append({
            "item_id": title_match.group("item_id"),
            "source": match.group("source"),
            "title": title_match.group("title").strip(),
            "tier": _tier_for(md_text, match.start()),
            "url": url_match.group("url") if url_match else None,
            "llm_why": _line_value(body, "- llm_why:"),
            "profile_line": _line_value(body, "- profile:"),
            "backstory": _line_value(body, "- 来龙:"),
            "so_what": _line_value(body, "- 去脉:"),
            "context_missing": _line_value(body, "- 来龙去脉:") is not None,
            "claim": _line_value(body, "- 可检验断言:"),
            "summary": _summary(body, url_match.start() if url_match else len(body)),
            "translation": _translation(body),
            "local_media_dir": (re.search(r"^- \[本地媒体目录\]\(([^)]+)\)", body, re.MULTILINE).group(1) if re.search(r"^- \[本地媒体目录\]\(([^)]+)\)", body, re.MULTILINE) else None),
        })
    return view
