# personal-intel-loop

一条个人信息管线：从几十个信息源**采集** → 存进本机 SQLite → 每天排成一份「报纸」在本地网页 / Mac App「今日」里**阅读** → 记录你的**反馈和阅读行为** → 用这些反馈**分析**、调整下一期的选稿。

它是为一个人写的，不是平台：目标不是让你读得更多，而是让你更快读到「自己还不知道、又可能改变判断」的东西。

## 为什么做这个

这是「个人操作系统」系列中的一个 App。这个系列只有一个目标：把我和外界之间每一次信息往来的动作和历史都留在自己手里——看了什么、在哪里停留了多久、点了什么、买了什么、卖了什么、看完之后做了什么。

普通人的行为受各种已知和未知的条件、环境与刺激影响，处在一种伪随机的状态里，就像别人在我们身上装了开关：这个开关一拨、那根弦一拨，我们就做出别人预期中的动作。自己感觉像随机，在别人眼里却像提线木偶。平台和机构握着我们的行为数据，比我们更了解自己。这一整套东西，就是为了改变这个状况。

背后的想法是：个人对外部世界的理解好比一滴水去理解大海，几乎不可能做到。但一滴水向内看，看清自己的每个分子怎么动，是做得到的——什么温度下我会怎么动，遇到什么潮汐、什么洋流又会怎么动。把这些搞清楚，就能为自己争取更好的生存条件。这比理解整个大海现实得多。

这滴水自己的状态也要记下来。同一个人，前一晚没睡好、当天和家人吵了架、身体不舒服的时候，重大决定出错的概率明显更高，很多人的回忆录里都写到过这样的时刻。所以除了记录动作，还要记录当时的身体、情绪和外部环境（天气、行情、日程），之后才看得出「在什么状态下，我会怎么做」。

这些 App 的后台最终要打通、互相共享信息。大型金融集团早就这样对待每一个客户：把他在存款、贷款、保险、证券上的行为合在一起看，再做交叉销售。有了 AI，个人没有理由不能对自己做同样的事——区别是这一次，数据和分析只为自己服务。目前每个 App 各用一个本机 SQLite 库，打通是下一步。

理想的最终状态是：所有能接触到我的信息，都要先经过我自己做的过滤网和记录器才能进来；我的反馈和行为，也要先经过我自己的过滤网和保护器才能发出去。

所以这个系列的共同约定是：所有行为记录写进本机数据库，不上传任何第三方；AI 分析在本机或用户自己选择的服务上运行。

## 架构

```
采集 (adapters/)          存储 (store.py, SQLite)        阅读界面                 反馈                      分析
─────────────────         ──────────────────────        ──────────────           ──────────────            ─────────────────────
RSS / 报纸专栏 /    ──►    items / claims / ratings  ──►  pil paper 出版一期  ──►  五维评分、原因码、    ──►  paper_learn: 行为复盘
官方预警 / 微博 /           behavior_events / inbox        web.py + paper_web/       批注、停留/曝光/滚动        → 微调来源/作者/主题旋钮
知乎 / 公众号导出 /         FTS5 全文索引                   macapp/「今日」(WKWebView)  行为事件                  profile_proposals: 画像修订提案
YouTube / 播客转录                                         Markdown 日报 (可选)                                 source_trust / ranking: 来源信任
```

- **采集**：`src/personal_intel_loop/adapters/` 下 21 个采集器，统一产出 `Item`，按确定性 ID 去重入库。公开源（RSS、报纸专栏、外务省海外安全、WHO 疫情通报、中国领事提醒、NOAA ENSO、气象厅/中央气象台预警、东京展演）直接抓；需要登录态的源（微博、知乎、公众号、财新）只读你自己准备好的登录态或导出产物。
- **存储**：`store.py`，SQLite + WAL，schema 由 `migrations/*.sql` 管。
- **出版与阅读**：`pil paper` 每天出一期（头条/要闻/反方/盲区/温暖/机会/结算/风险/闲与美等栏），`pil serve` 在 `127.0.0.1:8766` 提供 `/paper` 页面与 JSON API（契约见 `docs/paper_v2_contract.md`）；`macapp/` 是一个 WKWebView 外壳，常驻状态栏、轮询推送系统通知。
- **反馈**：页面上的评分、原因码（早知道 / 没看懂 / 不感兴趣 / 留着 / 想深聊）、批注，以及前端记录的阅读行为（曝光、停留、滚动深度）写进 `behavior_events` 等表。
- **分析**：出版前跑一次行为复盘（LLM 读昨天的行为表 + 批注 + 画像，给出小幅旋钮调整和画像修订提案，提案要你在页面上接受才生效）；`ranking.py` 按反馈重算来源信任；可检验断言到期后自动找证据给初判，你一键裁定。

## 依赖

- macOS（Mac App 与部分功能依赖 macOS；Python 部分在其它系统上大体可用，但没有测试过）
- Python ≥ 3.11
- 核心：`requirements.txt`（pydantic、feedparser、requests、PyYAML、numpy、qdrant-client；测试另需 pytest、cryptography）
- 可选（`requirements-optional.txt`，缺了相应功能降级或跳过）：
  - `sentence-transformers`：本地嵌入。没有它时 v2 排序、盲区版、笔记库近邻检索不可用，digest 回落到 v1 排序。
  - `trafilatura`：网页全文抽取。
  - `playwright`：需要浏览器渲染的报纸专栏。
  - `curl_cffi` / `youtube-transcript-api` / `yt-dlp`：YouTube 频道解析、字幕、无字幕视频转写。
  - `pdftotext`（poppler）：东京都感染症周报解析。
  - `pandoc`：`pil digest --epub` 导出 EPUB。
  - Qdrant + 一个外部的笔记库索引器：v2 排序与「笔记库近邻」需要（见下）。

## 安装与运行

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

Mac App：`macapp/build.sh "$(pwd)"` 编译出 `build/今日.app`（传入的路径写进 Info.plist，App 用它拉起本地服务）。

定时任务：见 `launchd/README.md`（只有占位示例）。

## 配置项

所有配置都是环境变量或 `config/` 下的文件，没有任何内置账号或密钥。

| 变量 | 作用 | 缺省 |
|---|---|---|
| `PIL_HOME` | 数据根目录 | `~/Library/Application Support/personal-intel-loop` |
| `PIL_DATA_DIR` / `PIL_DB_PATH` | 数据目录 / SQLite 路径 | `$PIL_HOME/data` / `$PIL_DATA_DIR/intel_loop.sqlite` |
| `PIL_CONFIG_DIR` | 配置目录（信息源清单、版面、地名别名、预警盯点） | 仓库自带的 `config/`（示例） |
| `PIL_PROFILE_DIR` | 阅读画像目录（`reading_profile.md` + `blindspots.md`，模板见 `config/profile_template/`） | `$PIL_HOME/profile` |
| `PIL_STAGING_DIR` | Markdown 日报 / 讨论包输出目录（可指向 Obsidian vault 里的子目录） | `$PIL_HOME/staging` |
| `PIL_VAULT_DIR` | 可选：你的笔记库根目录（用于「当前活跃判断」参照和「笔记引用了哪条」自动检测） | 不设 = 关闭 |
| `PIL_QDRANT_URL` / `PIL_QDRANT_COLLECTION_MARKER` | 笔记库向量索引（Qdrant 地址；一个文本文件，内容是当前 collection 名） | `http://localhost:6333` / `$PIL_HOME/qdrant_collection.txt` |
| `PIL_TZ` / `PIL_HOME_PLACE` | 本地时区 / 常驻地（「城市,国家」，需能在 `place_aliases.json` 查到） | `Asia/Tokyo` / `東京,日本` |
| `PIL_LOCATION_FILE` | 临时所在地（灾害预警追加盯点） | `$PIL_HOME/location.json` |
| `PIL_LLM_BASE_URL` / `PIL_LLM_MODEL` / `PIL_LLM_API_KEY(_FILE)` | 主力 LLM（任何 OpenAI 兼容端点） | 不设 = 无 LLM，相关步骤跳过 |
| `PIL_LLM_FALLBACK_*` | 兜底 LLM | 不设 |
| `PIL_LOCAL_LLM_BASE_URL` / `PIL_LOCAL_LLM_MODEL` | 本机模型服务（llama.cpp server、LM Studio、Ollama 等的 OpenAI 兼容端点） | 不设 |
| `PIL_OFFPEAK_GATE=deepseek` | 主力档是 DeepSeek 时，批量初判只在其非高峰时段调用 | 关 |
| `PIL_NOTIFY_CMD` | `--push command`：推送命令，正文走 stdin | 不设 |
| `PIL_SMTP_*` / `PIL_NOTIFY_EMAIL_FROM` / `PIL_NOTIFY_EMAIL_TO` | `--push email` 与预警邮件 | 不设 |
| `PIL_EPUB_DIR` | `pil digest --epub` 的输出目录 | 不设 |
| `PIL_TRANSCRIPT_ROOTS` | 本机转录/文章 markdown 根目录，JSON：`{"podcast": "/path", ...}` | `$PIL_HOME/transcripts/podcast` |
| `PIL_PODCAST_ROOTS` | `podcast_new` 读的转录目录（冒号分隔） | 同上 |
| `PIL_WEIBO_STORAGE_STATE` | 微博登录态（Playwright storage_state） | `$PIL_HOME/auth/weibo_storage_state.json` |
| `PIL_WEIBO_TIMELINES_DIR` | 外部微博时间线导出目录 | `$PIL_HOME/weibo_timelines` |
| `PIL_ZHIHU_COOKIES` | 知乎 cookie JSON | `~/.zhihu-cli/cookies.json` |
| `PIL_WEREAD_ARTICLES_DIR` | 公众号文章导出目录（`<账号>/<标题>.md + .json`） | `$PIL_HOME/weread_articles` |
| `PIL_ASR_ENABLE=1` / `PIL_ASR_MODEL` / `PIL_YTDLP` | 无字幕 YouTube 视频本机转写 | 关 |
| `HOME_CINEMA_DB` | 可选：本地影视库 SQLite，用于「闲与美」栏 | 不设则跳过 |

`config/` 里的文件全部是示例，说明见 `config/README.md`。

## 数据存在哪、会发给谁

- **本机**：所有条目、评分、批注、阅读行为事件、画像、运行记录都在 `$PIL_HOME` 下的 SQLite 和文件里。前端只和 `127.0.0.1` 上的本地服务通信；Mac App 只加载本地页面。
- **信息源**：采集时会访问各信息源的网站/API（这是采集本身）。
- **LLM**：摘要、翻译、新知判定、行为复盘、画像修订提案、断言初判、行程风险简报、推荐关注会调用你配置的 LLM 档位。提示词里可能包含：条目正文、**阅读画像**、**你写的批注**、**昨天逐条的阅读行为标签**、行程地点与日期。
  - 只配 `PIL_LOCAL_LLM_*`（或把主力档指向本机端点）时，这些数据不离开本机；`propose-profile-edits` 本来就只走本机档。
  - 主力/兜底档指向云服务时，上述内容会发给该服务。是否这样配置由你决定。
- **推送**：只在你配置了 `--push` / SMTP / `PIL_NOTIFY_CMD` 时发送，内容是日报摘要或预警标题。

## 局限

- 在作者本机环境（macOS、Apple Silicon、东京时区）下开发和使用，很多默认值（时区、常驻地、东京展演与东京都健康预警采集器、中日文媒体）带着这个环境的痕迹。
- 每个采集源都需要你自己配置：公开源有示例清单，需要登录态的源（微博、知乎、公众号、财新、日经中文）要你自己准备登录态或导出工具的产物，本仓库不包含任何登录、抓取账号的工具。
- 「笔记库」相关功能（v2 排序、活跃判断参照、自动检测笔记引用）假设你有一个 Obsidian 风格的 markdown 笔记库和一个把它写进 Qdrant 的索引器；目录约定（如 `07 判断与决策/当前活跃判断与决策索引.md`）写在 `active_corpus.py` / `vault_scanner.py` 里，需要按自己的库修改。
- 东京都感染症周报解析的测试需要三份公开 PDF，它们不随仓库分发，相应用例默认跳过（见 `tests/test_adapter_home_alerts.py` 说明）。
- 没有用户认证：`pil serve` 只应绑定 `127.0.0.1`。

## English summary

A single-user, local-first information pipeline: 21 adapters collect from RSS feeds, newspaper columns, official
alert/advisory feeds and (with your own credentials or exports) social platforms; items go into a local SQLite
database; `pil paper` lays out a daily "newspaper" served on `127.0.0.1` and wrapped by a small macOS app; your
ratings, notes and reading behavior are stored locally and fed back into ranking and an LLM-based daily review.
All endpoints, models and credentials are configured through environment variables (see the table above); nothing
is hard-coded. If you point the LLM tiers at a cloud provider, prompts include your reading profile, notes and
behavior summaries — configure only the local tier to keep everything on your machine. Licensed under GPL-3.0.
