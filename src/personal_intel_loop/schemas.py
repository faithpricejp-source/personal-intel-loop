from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

TRACKING_QUERY_KEYS = {"fbclid", "gclid", "igshid", "ref", "ref_src", "spm"}
FEEDBACK_EVENT_WEIGHTS: dict[str, float] = {
    # 原因码 digest checkbox (2026-08-26 起): 反馈带原因, 不再是三档打分。
    # 无原因的差评学不出东西——同一个"不喜欢"混着
    # "早知道"(我脑子里已有)与"没看懂"(文章没写清)两种完全不同的事。
    "already_known": -0.5,
    "unclear": -0.5,
    "keep": 1.0,
    "deep_discuss": 1.5,
    "not_interested": -0.5,   # 域外/与我无关——不是新颖性也不是清晰度问题
    # paper 多维评分 (paper_feedback.rate, origin='paper', 2026-10-04 起)
    "rate_overall_up": 0.5,
    "rate_overall_down": -0.5,
    "rate_quality_up": 0.25,
    "rate_quality_down": -0.25,
    # legacy 3-tier checkbox (2026-04 ~ 2026-08), 保留以便历史事件仍能校验
    "useful": 1.5,
    "light": 0.3,
    "less_like_this": -1.0,
    # manual CLI actions
    "promote_to_src": 1.0,
    "promote_to_evd": 1.5,
    "more_like_this": 0.0,
    "spam_or_false": -1.5,
    "later": 0.0,
    # auto-detected vault promotion events (weights per DESIGN.md target formula).
    # These fire from vault_scanner.py when a canonical vault object (SRC/EVD/JDG/...)
    # cites a pil item's URL. No user checkbox needed — the promotion action itself
    # is the strongest feedback signal available.
    "detected_src_reference": 1.5,
    "detected_evd_reference": 2.0,
    "detected_jdg_reference": 2.0,
    "detected_cas_link": 2.5,
    "detected_dec_link": 2.5,
    "detected_mon_link": 2.0,
    "detected_mec_enrich": 2.0,
    "detected_dia_enrich": 2.0,
    "detected_heu_enrich": 2.0,
}


def sha1_hex(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def weight_for(action: str) -> float:
    try:
        return FEEDBACK_EVENT_WEIGHTS[action]
    except KeyError as exc:
        raise ValueError(f"unsupported feedback action: {action}") from exc


def canonicalize_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("url cannot be empty")

    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"url missing host: {url}")

    port = parts.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    filtered_params: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        filtered_params.append((key, value))
    filtered_params.sort(key=lambda pair: (pair[0], pair[1]))

    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    query = urlencode(filtered_params, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _coerce_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("naive datetime is not allowed")
    return dt.astimezone(timezone.utc)


def normalize_dt_to_utc_z(value: str | datetime) -> str:
    return _coerce_datetime(value).isoformat().replace("+00:00", "Z")


def _normalize_hash_text(value: str | None) -> str:
    text = str(value or "").strip()
    return re.sub(r"\s+", " ", text)


def compute_item_id(
    source: str,
    *,
    url: str | None = None,
    guid: str | None = None,
    uid: str | None = None,
    post_id: str | None = None,
    note_id: str | None = None,
) -> str:
    if source.startswith("rss_briefing:"):
        identity = (guid or "").strip() or canonicalize_url(url or "")
        return f"rss:{sha1_hex(identity)}"
    if source.startswith("weibo_timeline:") and uid and post_id:
        return f"weibo:{uid}:{post_id}"
    if source.startswith("xhs:") and note_id:
        return f"xhs:{note_id}"
    prefix = source.split(":", 1)[0]
    if url:
        return f"{prefix}:{sha1_hex(canonicalize_url(url))}"
    raise ValueError("cannot compute item id without a source-specific identity")


def compute_content_hash(item: "Item") -> str:
    payload = "\n".join(
        [
            item.source,
            canonicalize_url(item.url),
            _normalize_hash_text(item.title),
            _normalize_hash_text(item.body),
            _normalize_hash_text(item.transcript),
        ]
    )
    return sha256_hex(payload)


def compute_feedback_event_id(
    *,
    origin: str,
    item_id: str,
    action: str,
    digest_stem: str | None = None,
    event_ts_utc_iso: str | None = None,
) -> str:
    if origin == "digest_checkbox":
        if not digest_stem:
            raise ValueError("digest_stem is required for digest checkbox events")
        return sha1_hex(f"digest_checkbox|{digest_stem}|{item_id}|{action}")
    if origin == "web":
        if not digest_stem:
            raise ValueError("digest_stem is required for web events")
        return sha1_hex(f"web|{digest_stem}|{item_id}|{action}")
    if origin == "cli":
        if not event_ts_utc_iso:
            raise ValueError("event_ts_utc_iso is required for cli events")
        return sha1_hex(f"cli|{item_id}|{action}|{event_ts_utc_iso}")
    raise ValueError(f"unsupported origin: {origin}")


def compute_digest_inclusion_id(*, digest_kind: str, digest_date: str, item_id: str) -> str:
    return sha1_hex(f"{digest_kind}|{digest_date}|{item_id}")


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(default="", max_length=100000)
    author: str | None = Field(default=None, max_length=256)
    ts: datetime
    lang: str = Field(min_length=2, max_length=16)
    embedding: list[float] | None = None
    transcript: str | None = Field(default=None, max_length=500000)
    summary: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list)

    @field_validator("ts", mode="before")
    @classmethod
    def validate_ts(cls, value: Any) -> datetime:
        return _coerce_datetime(value)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return canonicalize_url(value)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        if len(value) > 64:
            raise ValueError("too many tags")
        cleaned: list[str] = []
        for tag in value:
            raw = str(tag).strip()
            if not raw:
                raise ValueError("empty tag is not allowed")
            if len(raw) > 128:
                raise ValueError("tag too long")
            cleaned.append(raw)
        return cleaned


class FeedbackEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=64)
    item_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=64)
    origin: str = Field(min_length=1, max_length=64)
    digest_path: str | None = None
    vault_target: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=2000)
    event_ts: datetime

    @field_validator("event_ts", mode="before")
    @classmethod
    def validate_event_ts(cls, value: Any) -> datetime:
        return _coerce_datetime(value)

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: str) -> str:
        if value not in FEEDBACK_EVENT_WEIGHTS:
            raise ValueError(f"unsupported feedback action: {value}")
        return value
