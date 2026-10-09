[中文](README.md) | **English**

# personal-intel-loop

A personal information pipeline: **collect** from dozens of sources → store in a local SQLite database → lay it out each day as a "newspaper" to **read** in a local web page / the Mac App "今日" (Today) → record your **feedback and reading behavior** → use that feedback to **analyze** and adjust the story selection for the next issue.

It is written for one person; it is not a platform. The goal is not to make you read more, but to get you faster to the things "you don't know yet and that might change your judgment".

## Why build this

This is one App in a "personal operating system" series. The series has a single goal: to keep in my own hands the actions and history of every exchange of information between me and the outside world — what I looked at, where I lingered and for how long, what I clicked, what I bought, what I sold, and what I did after I finished looking.

An ordinary person's behavior is shaped by all kinds of known and unknown conditions, environments and stimuli, leaving them in a pseudo-random state, as if someone had installed switches on us: flip this switch, pluck that string, and we perform the action someone else expected. To ourselves it feels random; in others' eyes we look like puppets on strings. Platforms and institutions hold our behavioral data and understand us better than we understand ourselves. This whole set of tools exists to change that.

The idea behind it: an individual trying to understand the outside world is like a drop of water trying to understand the ocean — practically impossible. But a drop of water looking inward, seeing clearly how each of its own molecules moves, is achievable — how I move at a given temperature, how I move when I meet a given tide or current. Once that is clear, you can secure better conditions for your own survival. That is far more realistic than understanding the whole ocean.

The drop's own state needs to be recorded too. The same person, after a bad night's sleep, after a fight with family that day, or when feeling unwell, is markedly more likely to get a major decision wrong — many memoirs describe moments like these. So besides recording actions, we also record the body, mood and external environment at the time (weather, markets, schedule), so that later you can see "in what state, I do what".

The back ends of these Apps are ultimately meant to be connected and share information with each other. Large financial groups have long treated every customer this way: looking at their behavior across deposits, loans, insurance and securities together, and then cross-selling. With AI, there is no reason an individual can't do the same for themselves — the difference being that this time, the data and analysis serve only you. Currently each App uses its own local SQLite database; connecting them is the next step.

The ideal end state: all information that can reach me must first pass through a filter and recorder that I built myself before it gets in; and my feedback and behavior must first pass through my own filter and protector before they go out.

So the shared convention of this series is: all behavior records are written to a local database and never uploaded to any third party; AI analysis runs locally or on a service the user chooses.

## Architecture

```
采集 (adapters/)          存储 (store.py, SQLite)        阅读界面                 反馈                      分析
─────────────────         ──────────────────────        ──────────────           ──────────────            ─────────────────────
RSS / 报纸专栏 /    ──►    items / claims / ratings  ──►  pil paper 出版一期  ──►  五维评分、原因码、    ──►  paper_learn: 行为复盘
官方预警 / 微博 /           behavior_events / inbox        web.py + paper_web/       批注、停留/曝光/滚动        → 微调来源/作者/主题旋钮
知乎 / 公众号导出 /         FTS5 全文索引                   macapp/「今日」(WKWebView)  行为事件                  profile_proposals: 画像修订提案
YouTube / 播客转录                                         Markdown 日报 (可选)                                 source_trust / ranking: 来源信任
```

- **Collection**: 21 adapters under `src/personal_intel_loop/adapters/`, all producing a uniform `Item`, deduplicated by deterministic ID on ingest. Public sources (RSS, newspaper columns, Japan MOFA overseas safety, WHO disease outbreak news, Chinese consular advisories, NOAA ENSO, JMA / China National Meteorological Center warnings, Tokyo performances and exhibitions) are fetched directly; sources that require a logged-in session (Weibo, Zhihu, WeChat Official Accounts, Caixin) only read login state or exports that you have prepared yourself.
- **Storage**: `store.py`, SQLite + WAL, schema managed by `migrations/*.sql`.
- **Publishing and reading**: `pil paper` publishes one issue a day (sections such as headline / top stories / counterpoint / blind spots / warmth / opportunities / settlement / risks / leisure & beauty); `pil serve` serves the `/paper` page and a JSON API on `127.0.0.1:8766` (contract in `docs/paper_v2_contract.md`); `macapp/` is a WKWebView shell that lives in the menu bar and polls to push system notifications.
- **Feedback**: ratings on the page, reason codes (knew it already / didn't understand / not interested / keep / want to discuss in depth), annotations, and reading behavior recorded by the front end (impressions, dwell time, scroll depth) are written to `behavior_events` and other tables.
- **Analysis**: before publishing, a behavior review runs once (an LLM reads yesterday's behavior table + annotations + profile and proposes small knob adjustments and profile revision proposals; proposals only take effect once you accept them on the page); `ranking.py` recomputes source trust from feedback; when a verifiable claim comes due, evidence is gathered automatically for a preliminary verdict, which you settle with one click.

## Dependencies

- macOS (the Mac App and some features depend on macOS; the Python part should mostly work on other systems, but has not been tested)
- Python ≥ 3.11
- Core: `requirements.txt` (pydantic, feedparser, requests, PyYAML, numpy, qdrant-client; tests additionally need pytest, cryptography)
- Optional (`requirements-optional.txt`; when missing, the corresponding feature degrades or is skipped):
  - `sentence-transformers`: local embeddings. Without it, v2 ranking, the blind-spots section and note-vault nearest-neighbor search are unavailable, and digest falls back to v1 ranking.
  - `trafilatura`: full-text extraction from web pages.
  - `playwright`: newspaper columns that require browser rendering.
  - `curl_cffi` / `youtube-transcript-api` / `yt-dlp`: YouTube channel resolution, subtitles, and transcription of videos without subtitles.
  - `pdftotext` (poppler): parsing the Tokyo Metropolitan infectious disease weekly report.
  - `pandoc`: EPUB export via `pil digest --epub`.
  - Qdrant + an external note-vault indexer: required for v2 ranking and "note-vault neighbors" (see below).

## Installation and running

```bash
git clone <this repo> personal-intel-loop && cd personal-intel-loop
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -e .
.venv/bin/python -m pytest -q          # 全部离线

# 抓几个公开源、出一期报纸、起本地服务
.venv/bin/pil ingest --adapter rss_briefing --limit 20
.venv/bin/pil paper --dry-run            # 只打印入选清单，不写库、不调模型
.venv/bin/pil paper
.venv/bin/pil serve                      # 打开 http://127.0.0.1:8766/paper/
```

Mac App: `macapp/build.sh "$(pwd)"` builds `build/今日.app` (the path you pass in is written into Info.plist, and the App uses it to launch the local service).

Scheduled jobs: see `launchd/README.md` (placeholder examples only).

## Configuration

All configuration is via environment variables or files under `config/`; there are no built-in accounts or keys.

| Variable | Purpose | Default |
|---|---|---|
| `PIL_HOME` | Data root directory | `~/Library/Application Support/personal-intel-loop` |
| `PIL_DATA_DIR` / `PIL_DB_PATH` | Data directory / SQLite path | `$PIL_HOME/data` / `$PIL_DATA_DIR/intel_loop.sqlite` |
| `PIL_CONFIG_DIR` | Config directory (source lists, layout, place-name aliases, alert watch points) | The repo's bundled `config/` (examples) |
| `PIL_PROFILE_DIR` | Reading profile directory (`reading_profile.md` + `blindspots.md`; templates in `config/profile_template/`) | `$PIL_HOME/profile` |
| `PIL_STAGING_DIR` | Output directory for Markdown daily digests / discussion packs (can point to a subdirectory in an Obsidian vault) | `$PIL_HOME/staging` |
| `PIL_VAULT_DIR` | Optional: your note vault root (used for the "currently active judgments" reference and automatic detection of "which item a note cites") | Unset = disabled |
| `PIL_QDRANT_URL` / `PIL_QDRANT_COLLECTION_MARKER` | Note-vault vector index (Qdrant address; a text file whose content is the current collection name) | `http://localhost:6333` / `$PIL_HOME/qdrant_collection.txt` |
| `PIL_TZ` / `PIL_HOME_PLACE` | Local time zone / home location ("city,country"; must be resolvable in `place_aliases.json`) | `Asia/Tokyo` / `東京,日本` |
| `PIL_LOCATION_FILE` | Temporary current location (adds extra watch points for disaster alerts) | `$PIL_HOME/location.json` |
| `PIL_LLM_BASE_URL` / `PIL_LLM_MODEL` / `PIL_LLM_API_KEY(_FILE)` | Primary LLM (any OpenAI-compatible endpoint) | Unset = no LLM; related steps are skipped |
| `PIL_LLM_FALLBACK_*` | Fallback LLM | Unset |
| `PIL_LOCAL_LLM_BASE_URL` / `PIL_LOCAL_LLM_MODEL` | Local model server (OpenAI-compatible endpoint of llama.cpp server, LM Studio, Ollama, etc.) | Unset |
| `PIL_OFFPEAK_GATE=deepseek` | When the primary tier is DeepSeek, batch preliminary verdicts are only called during its off-peak hours | Off |
| `PIL_NOTIFY_CMD` | `--push command`: push command; the message body goes via stdin | Unset |
| `PIL_SMTP_*` / `PIL_NOTIFY_EMAIL_FROM` / `PIL_NOTIFY_EMAIL_TO` | `--push email` and alert emails | Unset |
| `PIL_EPUB_DIR` | Output directory for `pil digest --epub` | Unset |
| `PIL_TRANSCRIPT_ROOTS` | Local transcript/article markdown root directories, JSON: `{"podcast": "/path", ...}` | `$PIL_HOME/transcripts/podcast` |
| `PIL_PODCAST_ROOTS` | Transcript directories read by `podcast_new` (colon-separated) | Same as above |
| `PIL_WEIBO_STORAGE_STATE` | Weibo login state (Playwright storage_state) | `$PIL_HOME/auth/weibo_storage_state.json` |
| `PIL_WEIBO_TIMELINES_DIR` | Directory of externally exported Weibo timelines | `$PIL_HOME/weibo_timelines` |
| `PIL_ZHIHU_COOKIES` | Zhihu cookie JSON | `~/.zhihu-cli/cookies.json` |
| `PIL_WEREAD_ARTICLES_DIR` | WeChat Official Account article export directory (`<账号>/<标题>.md + .json`) | `$PIL_HOME/weread_articles` |
| `PIL_ASR_ENABLE=1` / `PIL_ASR_MODEL` / `PIL_YTDLP` | Local transcription of YouTube videos without subtitles | Off |
| `HOME_CINEMA_DB` | Optional: local film/TV library SQLite, used for the "leisure & beauty" section | Skipped if unset |

All files in `config/` are examples; see `config/README.md` for details.

## Where data is stored and who it is sent to

- **Local machine**: all items, ratings, annotations, reading behavior events, profiles and run records live in SQLite and files under `$PIL_HOME`. The front end only talks to the local service on `127.0.0.1`; the Mac App only loads local pages.
- **Information sources**: collection accesses each source's website/API (that is what collection is).
- **LLM**: summaries, translation, novelty judgment, behavior review, profile revision proposals, preliminary claim verdicts, trip risk briefings and follow recommendations call the LLM tiers you have configured. Prompts may include: item text, **your reading profile**, **annotations you wrote**, **yesterday's per-item reading behavior labels**, and trip locations and dates.
  - If you only configure `PIL_LOCAL_LLM_*` (or point the primary tier at a local endpoint), this data never leaves your machine; `propose-profile-edits` only ever uses the local tier.
  - If the primary/fallback tiers point at a cloud service, the content above is sent to that service. Whether to configure it that way is your decision.
- **Push notifications**: only sent if you have configured `--push` / SMTP / `PIL_NOTIFY_CMD`; the content is the daily digest summary or alert headlines.

## Limitations

- Developed and used in the author's own environment (macOS, Apple Silicon, Tokyo time zone); many defaults (time zone, home location, the Tokyo performances/exhibitions and Tokyo Metropolitan health alert adapters, Chinese and Japanese media) bear traces of that environment.
- Every source needs to be configured by you: public sources come with example lists; sources that require a logged-in session (Weibo, Zhihu, WeChat Official Accounts, Caixin, Nikkei Chinese) need you to prepare the login state or the output of an export tool yourself. This repository contains no tools for logging in or for scraping accounts.
- The "note vault" features (v2 ranking, the active-judgments reference, automatic detection of note citations) assume you have an Obsidian-style markdown note vault and an indexer that writes it into Qdrant; directory conventions (such as `07 判断与决策/当前活跃判断与决策索引.md`) are written in `active_corpus.py` / `vault_scanner.py` and need to be adapted to your own vault.
- Tests for the Tokyo Metropolitan infectious disease weekly report parser need three public PDFs that are not distributed with the repository; the corresponding test cases are skipped by default (see the notes in `tests/test_adapter_home_alerts.py`).
- There is no user authentication: `pil serve` should only be bound to `127.0.0.1`.

## English summary

A single-user, local-first information pipeline: 21 adapters collect from RSS feeds, newspaper columns, official
alert/advisory feeds and (with your own credentials or exports) social platforms; items go into a local SQLite
database; `pil paper` lays out a daily "newspaper" served on `127.0.0.1` and wrapped by a small macOS app; your
ratings, notes and reading behavior are stored locally and fed back into ranking and an LLM-based daily review.
All endpoints, models and credentials are configured through environment variables (see the table above); nothing
is hard-coded. If you point the LLM tiers at a cloud provider, prompts include your reading profile, notes and
behavior summaries — configure only the local tier to keep everything on your machine. Licensed under GPL-3.0.
