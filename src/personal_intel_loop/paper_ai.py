"""AI 预处理(paper 版): 一条新闻 → 版面用的结构化 JSON。

一个提示词输出: lede / one_liner / backstory / so_what / claim / byline / style_tags /
topic / profile_hit / lane, 外加 TASK4 契约 7.1/8/10.1 的 novelty(新知判定, 参照注入的
vault_lookup 检索结果)、lane_tags(含 warmth/opportunity 时分别要求 verification /
opportunity 字段)。byline 只认文中署名, 不许拿媒体名充当(提示词里明说)。
llm_call / vault_lookup 可注入; llm_call 缺省走 llm_backend 的主力档/兜底档(见 _call_item_preprocess)。vault_lookup 缺省 = 不取 vault 对象(纯离线安全); 生产链路
(CLI pil paper / build_edition 显式传参)用 default_vault_lookup 走 vault_corpus 检索前 3 条。
"""
from __future__ import annotations

import json
from pathlib import Path
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from personal_intel_loop.schemas import normalize_dt_to_utc_z

logger = logging.getLogger(__name__)
PAPER_AI_BODY_LIMIT = 6000
CLOUD_TIMEOUT_SECONDS = 120

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

NOVELTY_KINDS = ("new_fact", "new_mechanism", "counter", "known", "confirming")
VERIFICATION_LEVELS = ("primary", "secondary", "unverified")
LANE_TAG_LIMIT = 3

PAPER_AI_PROMPT = """你在为一份个人信息日报「报纸」做预处理。读一条新闻, 输出严格的 JSON(不要 markdown 代码块, 不要解释)。

## 读者画像(判 profile_hit / lane 的唯一依据; 不含未接受的提案)
{profile}

## 用户 vault 里最相关的判断对象(判 novelty 的参照; 没有就当 null 处理)
{vault}

## 校准样例(过去你判了「新知」但用户说「早知道」的例子; 判 novelty 时引以为鉴)
{calibration}

## 这条内容
- 标题: {title}
- 来源: {source}
- 正文: {body}

## 事实纪律（10-05 摘要核对：393 句里 6 句与原文矛盾、30 句原文没有，集中在下面几类）
- 摘要里的人名、数字、年份、机构、引语**只能来自上面的正文**，不用你自己的背景知识补。正文只是网页页脚、导航、版权声明或报错页（没有实际内容）时，lede 只写「正文未抓到，内容不明」，one_liner 同义，backstory/so_what/claim 一律 null。
- 数字保留原文的口径与基准：「相对 9 月计划 +3.1%」不能写成「10 月再升 3.1%」；「到 2027 年 6 月前」不能写成「2027 年 6 月起」；A 超过 B 不能改写成 A 与 C 合计超过 B。
- 第一人称的个人体验写成「作者称/发帖人称」，不升格为普遍结论；原文没给年份的不要补年份；作者身份只取正文署名，不从 URL 或账号名推断。

## 输出字段
- lede: 两三句中文导语——发生了什么、之前是什么状态、往下会改变什么; 推不出的部分不要编。**若来源标注「音视频，正文是转录稿」：lede 改为 4-6 句概括这期节目的主要观点与最有信息量的论据/数字（谁说的、说了什么），不要写成「这是一期节目」式的元描述。**
- one_liner: 一句话中文概括(≤60 字)。
- backstory: 来龙——这件事之前是什么状态; 正文里找不到就 null, 不要编。
- so_what: 去脉——往下改变什么、谁受影响、接下来看什么; 推不出就 null。
- claim: 这条里最值得日后核验的一句可证伪断言, 形如 {{"text": "断言原文(带量与时间窗)", "check_after": "YYYY-MM-DD"}}; 没有可证伪断言就 null。
- byline: 文中署名的作者名(人名/笔名, 逐字取原文); 找不到就 null。**不许拿媒体名/来源名/机构名充当署名**。
- style_tags: ≤3 个中文短词描述文风(如 ["数据密集", "第一人称"]); 没有就 []。
- topic: 这条的主题门类, 3-10 个字(如 "资源/大宗"、"AI 产业")。
- profile_hit: 逐字引用上面画像里被这条命中的那一句(≤60 字); 没有就 null。
- lane: 按画像里的 lane 划分判这条属于哪条(material/surprise/track/warmth/none 或画像自定的 lane 名); 判不了就 "none"。
- novelty: 新知判定, {{"kind": "new_fact|new_mechanism|counter|known|confirming", "why": "一句话: 具体是哪件事/哪个数/哪条机制是新的, 或它反驳了你的哪条判断", "against": "被挑战的 vault 对象名或画像那一句, 没有则 null"}}。new_fact=新事实/新数字, new_mechanism=没见过的因果链或跨域连接, counter=与用户已有判断相反且有具体证据, known=用户大概率已知, confirming=主要在印证用户已有看法。
- lane_tags: ≤3 个内容标签短词; 词表含 "warmth"(人间温暖: 亲情/爱情/家国情怀/对陌生人的善意/跨越多年的回报, 须有具名的人和事, 只有口号宣传套话的不标)与 "opportunity"(尚无共识、信号混乱、但可能是大变化开端), 其余自由短词; 没有就 []。
- verification: 仅当 lane_tags 含 "warmth" 时必填, {{"level": "primary|secondary|unverified", "named": ["具名人物/地点/时间/金额"], "note": "依据"}}。primary=有一手来源(当事人/当地媒体原报道/官方记录); 同一故事多年反复转载的要找最早出处, 找不到在 note 里写「出处不明」; 非温暖条目给 null。
- opportunity: 仅当 lane_tags 含 "opportunity" 时必填, {{"if_true": "若为真用户能做什么(具体动作)", "kill_signal": "出现什么就说明它错了", "horizon": "大致时间尺度"}}; 只列可选动作与证伪条件, 不给投资建议口吻; 非机会条目给 null。

## 输出
只返回一个 JSON 对象:
{{"lede": "...", "one_liner": "...", "backstory": null, "so_what": null, "claim": null, "byline": null, "style_tags": [], "topic": "...", "profile_hit": null, "lane": "none", "novelty": {{"kind": "known", "why": null, "against": null}}, "lane_tags": [], "verification": null, "opportunity": null}}
"""


def default_vault_lookup(text: str) -> list[dict]:
    """缺省 vault 检索(生产链路): embeddings 查询向量 → vault_corpus.query_top_k 前 3 条。

    返回 [{"name","summary"}]。vault 未运行/模型不可用等一切失败都返回 []——
    vault 缺席不阻塞预处理(测试因此不受影响: 缺省链路只在显式传入时启用)。
    """
    try:
        from personal_intel_loop import embeddings, vault_corpus

        vault_corpus.assert_fresh()
        vector = embeddings.embed_queries([text])[0]
        hits = vault_corpus.query_top_k(vector, k=3)
        return [{"name": hit.title or hit.note_id, "summary": (hit.text or "")[:200]} for hit in hits]
    except Exception as exc:  # noqa: BLE001 — vault 缺席不阻塞预处理
        # 降级留痕, 否则 vault 持续故障时所有 novelty 判定悄悄失去参照
        logger.warning("vault lookup failed, proceeding without vault references: %s", exc)
        return []


def build_paper_prompt(
    *,
    profile: str,
    title: str,
    source: str,
    body: str,
    vault_objects: list[dict] | None = None,
    calibration_samples: list[dict] | None = None,
) -> str:
    vault_lines = []
    for obj in vault_objects or []:
        name = str(obj.get("name") or "").strip()
        summary = str(obj.get("summary") or "").strip()
        if name or summary:
            vault_lines.append(f"- {name or '(未命名)'}: {summary}")
    calibration_lines = []
    for sample in calibration_samples or []:
        line = str(sample.get("line") or "").strip()
        if line:
            calibration_lines.append(f"- {line}")
    return PAPER_AI_PROMPT.format(
        profile=(profile or "").strip() or "(无画像)",
        vault="\n".join(vault_lines) if vault_lines else "(无)",
        calibration="\n".join(calibration_lines) if calibration_lines else "(无)",
        title=(title or "").strip() or "(无标题)",
        source=source or "unknown",
        body=(body or "").strip()[:PAPER_AI_BODY_LIMIT] or "(空)",
    )


MECHANICAL_FIELDS = ("lede", "one_liner", "backstory", "so_what", "claim", "byline", "style_tags", "topic")
JUDGMENT_FIELDS = ("profile_hit", "lane", "novelty", "lane_tags", "verification", "opportunity")
# 两段式预处理：机械字段走主力档关思考（省 token，避免推理把输出撑到上限导致 JSON 截断），
# 判定字段走主力档高推理；主力档不可用时回落 fallback 档。档位配置见 llm_backend。


def _subset_prompt(prompt: str, fields: tuple[str, ...]) -> str:
    return prompt + "\n\n## 本次只输出这些字段\n只返回包含 " + ", ".join(fields) + " 这几个键的 JSON 对象，其余字段省略。\n"


def _call_primary(prompt: str, *, thinking: bool = False) -> tuple[str | None, str | None]:
    """主力档。thinking=False 给机械字段（短输出、不要推理）；thinking=True 给判定字段（高推理）。
    未配置/失败/输出被截断都返回 (None, None) 让上层回落。"""
    from personal_intel_loop import llm_backend

    if not llm_backend.is_configured("primary"):
        return None, None
    try:
        if thinking:
            r = llm_backend.chat(prompt, tier="primary", max_tokens=8000, timeout=CLOUD_TIMEOUT_SECONDS,
                                 reasoning_effort="high")
        else:
            r = llm_backend.chat(prompt, tier="primary", max_tokens=1500, timeout=CLOUD_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("primary llm call failed: %s", exc)
        return None, None
    if r.get("finish_reason") == "length":
        logger.warning("primary llm output truncated (thinking=%s)", thinking)
        return None, None
    return (r.get("text") or None), (r.get("model") or "primary") + ("-think" if thinking else "")


def _call_judge(prompt: str) -> tuple[str | None, str | None]:
    """判定类调用：主力档开思考，失败回落 fallback 档高推理。"""
    raw, model = _call_primary(prompt, thinking=True)
    if raw:
        return raw, model
    return _call_fallback_high(prompt)


def _call_fallback_high(prompt: str) -> tuple[str | None, str | None]:
    """兜底档、高推理强度。未配置或失败返回 (None, None)。"""
    from personal_intel_loop import llm_backend

    if not llm_backend.is_configured("fallback"):
        return None, None
    try:
        r = llm_backend.chat(prompt, tier="fallback", max_tokens=2500, timeout=CLOUD_TIMEOUT_SECONDS,
                             reasoning_effort="high")
    except Exception as exc:  # noqa: BLE001
        logger.warning("fallback llm judgment call failed: %s", exc)
        return None, None
    return (r.get("text") or None), r.get("model")


def _call_cloud(prompt: str) -> tuple[str | None, str | None]:
    """通用单次调用（learn / settle / follow / risk / social / distill 都复用它）：走判定通道。
    注意不要改成逐条预处理的两段式，否则别的模块复用时整份提示词会被拆成字段子集。"""
    return _call_judge(prompt)


def _call_item_preprocess(prompt: str) -> tuple[str | None, str | None]:
    """逐条预处理专用，两段：机械字段主力档关思考（不可用回落 fallback 档），判定字段走 _call_judge；合并成一个 JSON 文本。"""
    mech_raw, mech_model = _call_primary(_subset_prompt(prompt, MECHANICAL_FIELDS))
    mech = _extract_json(mech_raw) if mech_raw else None
    if mech is None:
        mech_raw, mech_model = _call_fallback_high(_subset_prompt(prompt, MECHANICAL_FIELDS))
        mech = _extract_json(mech_raw) if mech_raw else None
    judg_raw, judg_model = _call_judge(_subset_prompt(prompt, JUDGMENT_FIELDS))
    judg = _extract_json(judg_raw) if judg_raw else None
    if mech is None and judg is None:
        return None, None
    merged = {**(mech or {}), **{k: v for k, v in (judg or {}).items() if k in JUDGMENT_FIELDS}}
    return json.dumps(merged, ensure_ascii=False), f"{mech_model or '-'}+{judg_model or '-'}"


def _normalize_novelty(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if kind not in NOVELTY_KINDS:
        return None
    why = value.get("why")
    against = value.get("against")
    return {
        "kind": kind,
        "why": (str(why).strip()[:300] or None) if isinstance(why, str) else None,
        "against": (str(against).strip()[:120] or None) if isinstance(against, str) else None,
    }


def _normalize_verification(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    level = value.get("level")
    if level not in VERIFICATION_LEVELS:
        return None
    named = value.get("named")
    names = [str(entry).strip()[:80] for entry in named if isinstance(entry, str) and entry.strip()] if isinstance(named, list) else []
    note = value.get("note")
    return {
        "level": level,
        "named": names[:10],
        "note": (str(note).strip()[:300] or None) if isinstance(note, str) else None,
    }


def _normalize_opportunity(value) -> dict | None:
    if not isinstance(value, dict):
        return None

    def _text(key: str, limit: int) -> str | None:
        raw = value.get(key)
        return (str(raw).strip()[:limit] or None) if isinstance(raw, str) else None

    opportunity = {"if_true": _text("if_true", 300), "kill_signal": _text("kill_signal", 300), "horizon": _text("horizon", 60)}
    return opportunity if any(opportunity.values()) else None


def _normalize_payload(payload: dict) -> dict:
    """收敛模型输出: 字段缺失/类型不对都归到契约形态, 不让脏值进库。"""

    def _text(value, limit: int) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned[:limit] or None

    claim = payload.get("claim")
    claim_out = None
    if isinstance(claim, dict):
        claim_text = _text(claim.get("text"), 300)
        check_after = _text(claim.get("check_after"), 10)
        if check_after and not _DATE_RE.fullmatch(check_after):
            check_after = None
        if claim_text:
            claim_out = {"text": claim_text, "check_after": check_after}

    style_tags = payload.get("style_tags")
    tags: list[str] = []
    if isinstance(style_tags, list):
        for tag in style_tags:
            if isinstance(tag, str) and tag.strip():
                tags.append(tag.strip())
            if len(tags) >= 3:
                break

    lane_tags = payload.get("lane_tags")
    content_tags: list[str] = []
    if isinstance(lane_tags, list):
        for tag in lane_tags:
            if isinstance(tag, str) and tag.strip():
                content_tags.append(tag.strip()[:40])
            if len(content_tags) >= LANE_TAG_LIMIT:
                break
    is_warmth = "warmth" in content_tags
    is_opportunity = "opportunity" in content_tags

    return {
        "lede": _text(payload.get("lede"), 800),
        "one_liner": _text(payload.get("one_liner"), 200) or "",
        "backstory": _text(payload.get("backstory"), 300),
        "so_what": _text(payload.get("so_what"), 300),
        "claim": claim_out,
        "byline": _text(payload.get("byline"), 120),
        "style_tags": tags,
        "topic": _text(payload.get("topic"), 40),
        "profile_hit": _text(payload.get("profile_hit"), 120),
        "lane": _text(payload.get("lane"), 40) or "none",
        "novelty": _normalize_novelty(payload.get("novelty")),
        "lane_tags": content_tags,
        "verification": _normalize_verification(payload.get("verification")) if is_warmth else None,
        "opportunity": _normalize_opportunity(payload.get("opportunity")) if is_opportunity else None,
    }


def _extract_json(text: str | None) -> dict | None:
    from personal_intel_loop.summarizer import _extract_json as summarizer_extract_json

    parsed = summarizer_extract_json(text or "")
    return parsed if isinstance(parsed, dict) else None


def _write_error(conn: sqlite3.Connection, item_id: str, message: str, model: str | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES (?, '{}', ?, ?, ?)",
        (item_id, model, message[:500], normalize_dt_to_utc_z(datetime.now(timezone.utc))),
    )
    conn.commit()


MEDIA_BODY_LIMIT = 24000  # 音视频转录稿给到 2.4 万字，否则只能概括前十几分钟
_MEDIA_URL_RE = re.compile(r"(youtube\.com/|youtu\.be/|xiaoyuzhoufm\.com|podcasts\.apple\.com|spotify\.com/episode)", re.I)
_MEDIA_ADAPTERS = ("youtube_followed:", "podcast_new:", "local_transcripts:")


ASR_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "youtube_asr.py"


def _asr_youtube(video_id: str, title: str) -> str:
    """没有字幕：下音频本机转文字（需 PIL_ASR_ENABLE=1，见 scripts/youtube_asr.py）。子进程跑，失败/超长返回空串。"""
    import subprocess

    import os
    import sys

    if os.environ.get("PIL_ASR_ENABLE", "").strip() != "1":
        return ""
    lang = "zh" if re.search(r"[\u4e00-\u9fff]", title) else "en"
    try:
        r = subprocess.run([sys.executable, str(ASR_SCRIPT), video_id, "--lang", lang],
                           capture_output=True, text=True, timeout=3600)
    except Exception as exc:  # noqa: BLE001
        logger.warning("youtube asr failed %s: %s", video_id, exc)
        return ""
    if r.returncode != 0:
        logger.warning("youtube asr rc=%s %s: %s", r.returncode, video_id, r.stderr[-200:])
        return ""
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]).get("text") or ""
    except (ValueError, IndexError, AttributeError):
        # 合法 JSON 但非对象(123/[1,2]/"done")时 .get 抛 AttributeError, 也要兜住
        return ""


def _pick_body(conn, item_id: str, row, fulltext: str | None) -> tuple[str, bool]:
    """选给模型读的正文（YouTube 网页全文只有页脚模板，看不出讲了什么，所以音视频优先用转录）。
    音视频：转录稿 > 条目正文（follow_builders 把转录放在 body）；网页抓的全文是 YouTube 页脚，忽略；
    YouTube 条目手上没有转录就现抓一次（yt-dlp 字幕）。其它：全文与条目正文取更长的。"""
    url = str(row["url"] or "")
    is_media = bool(_MEDIA_URL_RE.search(url)) or str(row["source"]).startswith(_MEDIA_ADAPTERS)
    if is_media:
        text = max([row["transcript"] or "", row["body"] or ""], key=len)
        if len(text) < 500 and ("youtube.com/watch" in url or "youtu.be/" in url):
            try:
                from urllib.parse import parse_qs, urlparse
                from personal_intel_loop.adapters.youtube_followed import fetch_transcript

                vid = parse_qs(urlparse(url).query).get("v", [""])[0] or url.rstrip("/").rsplit("/", 1)[-1]
                fetched = fetch_transcript(vid) if vid else ""
                if not fetched and vid:
                    fetched = _asr_youtube(vid, row["title"] or "")
                if fetched and len(fetched) > len(text):
                    text = fetched
                    conn.execute("UPDATE items SET transcript=? WHERE item_id=?", (fetched[:500000], item_id))
                    conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("transcript fetch failed for %s: %s", item_id, exc)
        return text[:MEDIA_BODY_LIMIT], True
    text = max([fulltext or "", row["body"] or ""], key=len)
    return text[:PAPER_AI_BODY_LIMIT], False


def preprocess(
    conn: sqlite3.Connection,
    item_ids,
    *,
    llm_call: Callable[[str], str | None] | None = None,
    vault_lookup: Callable[[str], list[dict]] | None = None,
) -> int:
    """对一批 item 跑 AI 预处理。已有 item_ai 记录的跳过(可续跑); 每条处理完立即提交;
    JSON 解析失败/模型异常写 error 字段不抛。返回本轮成功写入的条数。

    vault_lookup=None 时提示词不带 vault 对象(离线安全); 生产链路传
    default_vault_lookup(或注入假实现)。校准样例(契约 7.3)自动取最近 10 条分歧。
    """
    from personal_intel_loop.paper_calibration import disagreement_samples
    from personal_intel_loop.profile import profile_body_for_prompt, resolve_profile_path

    profile_text = profile_body_for_prompt(resolve_profile_path())
    calibration = disagreement_samples(conn, limit=10)
    processed = 0
    for item_id in list(item_ids):
        # 只跳过成功行：error 行（LLM 故障、判定段缺失的半份结果）下次重试（10-05 审计 B1/B2）
        existing = conn.execute("SELECT 1 FROM item_ai WHERE item_id=? AND error IS NULL", (item_id,)).fetchone()
        if existing is not None:
            continue
        row = conn.execute("SELECT title, source, body, url, transcript FROM items WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            continue
        fulltext_row = conn.execute("SELECT text FROM item_fulltext WHERE item_id=? AND status='ok'", (item_id,)).fetchone()
        body, is_media = _pick_body(conn, item_id, row, fulltext_row["text"] if fulltext_row else None)
        vault_objects = list(vault_lookup(f"{row['title'] or ''}\n{body}")) if vault_lookup is not None else []
        prompt = build_paper_prompt(
            profile=profile_text,
            title=row["title"],
            source=row["source"] + ("（音视频，正文是转录稿）" if is_media else ""),
            body=body,
            vault_objects=vault_objects,
            calibration_samples=calibration,
        )

        model = None
        # 判定段缺失的半份结果：机械字段已有就只补判定，不重跑机械段（10-06：省下重复的机械调用）
        half = None
        if llm_call is None:
            half = conn.execute(
                "SELECT payload_json, model FROM item_ai WHERE item_id=? AND error='judgment missing'", (item_id,)
            ).fetchone()
        if half is not None:
            judg_raw, judg_model = _call_judge(_subset_prompt(prompt, JUDGMENT_FIELDS))
            judg = _extract_json(judg_raw) if judg_raw else None
            if judg is None:
                continue
            mech = {k: v for k, v in json.loads(half["payload_json"]).items() if k in MECHANICAL_FIELDS}
            merged = {**mech, **{k: v for k, v in judg.items() if k in JUDGMENT_FIELDS}}
            raw, model = json.dumps(merged, ensure_ascii=False), f"{(half['model'] or '-').split('+')[0]}+{judg_model}"
        else:
            try:
                if llm_call is not None:
                    raw = llm_call(prompt)
                else:
                    raw, model = _call_item_preprocess(prompt)
            except Exception as exc:
                _write_error(conn, item_id, f"llm failed: {exc}", None)
                continue

        parsed = _extract_json(raw)
        if parsed is None:
            _write_error(conn, item_id, f"invalid json: {(raw or '')[:200]}", model)
            continue

        payload = _normalize_payload(parsed)
        # 两段式里判定段失败（模型标记以 "+-" 结尾）：机械字段照常上版，但记 error 让下次重试补判定
        # 对称处理机械段失败（标记以 "-+" 开头）：判定字段照常入库但记 error，下次重试整条，
        # 否则机械字段永久为空仍上版且被 error IS NULL 的跳过检查挡住永不重试
        if model and model.endswith("+-"):
            error = "judgment missing"
        elif model and model.startswith("-+"):
            error = "mechanical missing"
        else:
            error = None
        conn.execute(
            "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES (?, ?, ?, ?, ?)",
            (item_id, json.dumps(payload, ensure_ascii=False), model, error, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
        )
        conn.commit()
        processed += 1
    return processed
