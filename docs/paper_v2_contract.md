# 报纸 v2 接口契约

后端（`web.py` / `paper_api.py`）、前端（`paper_web/`）、Mac 壳（`macapp/`）共用本契约。字段增改先改这里。

## 0. 总体
- 服务：PIL 现有 `web.py`（标准库 `http.server.ThreadingHTTPServer`，**不是 FastAPI**），监听 `127.0.0.1:8766`。新增路由都加在 `_Handler.do_GET / do_POST` 分支里，处理函数写成可单测的纯函数 `handle_xxx(conn, ...) -> dict`（同现有 `handle_feedback`）。
- 静态页：`GET /paper` 和 `GET /paper/` 返回 `src/personal_intel_loop/paper_web/index.html`；`GET /paper/<文件名>` 返回同目录下的 `app.js` / `app.css`（只允许这个目录里的文件，拒绝 `..`）。
- 所有 JSON 用 UTF-8、`ensure_ascii=False`。错误：HTTP 400 + `{"error": "<说明>"}`；未知 item 404 + `{"error": ...}`。
- 时间一律 ISO 8601。`date` 是本地日期 `YYYY-MM-DD`（时区见环境变量 `PIL_TZ`，缺省 Asia/Tokyo）。

## 1. 数据对象

### Item（一条新闻在版面上的样子）
```json
{
  "item_id": "rss_briefing:abc123",
  "title": "标题原文",
  "url": "https://example.com/a",
  "source": "rss_briefing:example_feed",
  "source_label": "Example Feed",
  "author_key": "example_feed",           // 作者归一键：抽到署名用署名，抽不到用 source_label
  "author_label": "张三",                  // 展示用；抽不到署名时 = source_label
  "author_is_byline": true,               // true=真署名；false=只是来源名
  "published_at": "2026-10-04T06:12:00+09:00",
  "lang": "zh",                           // zh / ja / en / unknown
  "image_url": "https://example.com/a.jpg", // 可为 null
  "lede": "两三句中文导语。",               // AI 预处理；未处理时 null
  "one_liner": "一句话。",                 // 未处理时用标题
  "backstory": "来龙：……",                 // 可为 null
  "so_what": "去脉：……",                   // 可为 null
  "claim": {"text": "……", "check_after": "2027-01-01"},  // 可为 null
  "topic": "资源/大宗",
  "style_tags": ["数据密集", "第一人称"],   // ≤3 个，可为空数组
  "why_here": {"profile_hit": "profile 中命中的那一句", "lane": "1", "blind_reason": null},
  "my": {"overall": 0, "quality": 0, "author": 0, "style": 0, "topic": 0, "reason_code": null, "note": null},
  "trust": {"source": 0.42, "author": 0.55}   // author 无记录时 null
}
```
- `my.*` 取值 `1 / -1 / 0`；`reason_code` 取 `already_known / unclear / not_interested / keep / deep_discuss` 或 null（沿用现有原因码）。
- 盲区版条目：`why_here.blind_reason` 写命中的盲区方向那一句，`profile_hit` 可为 null。

### Edition（一期报纸）
```json
{
  "date": "2026-10-04",
  "built_at": "2026-10-04T09:12:00+09:00",
  "sections": {
    "lead":   [Item],        // 1 条
    "top":    [Item],        // 4–6 条
    "briefs": [Item],        // 其余主线
    "blind":  [Item]         // 盲区版，约 15%
  },
  "letters": [Proposal],     // 只有最新一期带；其余期为 []
  "stats": {"pool": 268, "picked": 60, "ai_done": 58}
}
```

### Proposal（编辑部来信 = profile 待接受修订）
```json
{"line": "<profile 待接受修订区那一行去掉开头 '- ' 的原文>", "date": "2026-08-27", "basis": "依据……", "target": "……", "suggestion": "……"}
```
`line` 是接受/拒绝时回传的唯一键；`date/basis/target/suggestion` 解析失败可为 null。

## 2. 接口

| 方法 路径 | 入参 | 返回 |
|---|---|---|
| `GET /api/paper/editions?before=YYYY-MM-DD&limit=N` | `before` 缺省 = 明天（即从最新一期开始）；`limit` 缺省 1，最大 3 | `{"editions": [Edition…按日期倒序], "next_before": "YYYY-MM-DD" 或 null}`。`next_before` = 本次最旧一期的日期，没有更早的期就 null |
| `GET /api/paper/item/<item_id>` | item_id 需 URL 编码 | `Item` 加 `"fulltext": "正文（≤20000 字）或 null"`、`"fulltext_source": "fetched|feed|null"`（10-07 增：原文抓不到时回落 items.body，标 feed）、`"edition_date": "…"` |
| `POST /api/paper/rate` | `{"item_id", "dim": "overall|quality|author|style|topic", "value": 1|-1|0}`（0=撤销） | `{"ok": true, "item_id", "dim", "value", "effect": Effect}` |
| `POST /api/feedback` | **已有**，不改。前端传 `{"item_id", "action": <原因码>, "digest_date": <该期 date>}` | 已有结构（含 `trust_before/trust_after`） |
| `POST /api/proposal` | **已有**，不改。`{"line", "decision": "accept|reject"}` | `{"ok", "pending_count"}` |
| `GET /api/paper/pipeline` | — | `Pipeline`（见下） |
| `POST /api/paper/pipeline` | `{"kind": "source|author", "key": "<source 或 author_key>", "action": "mute|unmute|boost|unboost|reset"}`（`unboost` 仅 source，10-05 验收 H204：只撤销加权、不动屏蔽） | `{"ok": true, "kind", "key", "action", "before": 0.4, "after": 0.6, "muted": false}` |
| `POST /api/paper/read` | `{"item_id", "dwell_ms": 12345}` | `{"ok": true}`（v1 只记录不入排序） |

### Effect（按下按钮后当场回显，**每个按钮必须有**）
```json
{"kind": "source_trust|author_trust|queued", "label": "此源信任 0.31→0.36", "before": 0.31, "after": 0.36}
```
- `overall`、`quality` → `source_trust`；`author` → `author_trust`；`style`、`topic` → `queued`，label 固定为「已记，明早编辑部来信里给出修订提案」，before/after 为 null。

### Pipeline（管道页）
```json
{
  "sources": [{"source": "...", "label": "...", "trust": 0.42, "items_30d": 120, "on_paper_30d": 14, "muted": false, "boosted": false}],
  "authors": [{"author_key": "...", "label": "...", "trust": 0.61, "n_up": 3, "n_down": 0, "muted": false}],
  "recent": [{"ts": "...", "item_id": "...", "title": "...", "dim": "author", "value": 1}],
  "profile_dir": "/path/to/profile",    // 当前生效的画像目录
  "health": [{"adapter": "aihot", "status": "degraded", "count": 0, "errors": ["aihot non-200: 404"], "run_at_utc": "2026-10-05T10:48:44.825825Z", "zero_streak": 7, "stale": false}]
}
```
`sources` 按 trust 降序，`authors` 按 n_up+n_down 降序，`recent` 最近 50 条。
`health` 是「采集健康」：后端读 `data/runs/ingest_*.jsonl`（最多最近 500 个文件）取每个采集器**最近一次**运行记录（按 `run_at_utc`；`ingest_first_ring_*.jsonl` 里的逐适配器小结也算），`count` 为该轮采集条数，`zero_streak` 取自 `data/runs/adapter_zero_streaks.json`（文件不存在按 0），旧记录没有 `status`/`errors` 键时视为 `ok`/`[]`。只列需要注意的那些：`status != "ok"`、`errors` 非空、`zero_streak >= 3`，或最近一次运行早于 48 小时（此时 `stale: true`）；全部正常时 `health` 为 `[]`。前端在管道页顶部渲染：空列表显示「所有采集器最近一轮正常」，否则每个采集器一行（名字、状态「降级 / 有失败 / 连续 N 轮 0 条 / 超过 48 小时没跑」、最近运行时间（东京时间）、失败原因最多 3 条，超出显示「等 N 条」）。

## 3. 画像接口（让别人也能用：驱动版面的是使用者自己的画像）
- 画像目录由环境变量 `PIL_PROFILE_DIR` 指定，缺省 `<项目>/config`。目录里两个文件：
  - `reading_profile.md`：现有格式（含「## 待接受的修订」「## 已接受的修订」两节）。
  - `blindspots.md`：每行 `- ` 开头一条盲区方向（一句话）。文件缺失 = 盲区版为空，版面照常出。
- 代码里不得出现任何具体个人画像内容；仓库附 `config/profile_template/`（虚构示例）。

## 4. 行为监测（核心功能）
App 自动记录「怎么读」，作为推荐的主要反馈来源；显式按钮是辅助。数据只存本机库。

### 前端上报 `POST /api/paper/events`
请求体 `{"session_id": "<页面加载时生成的随机串>", "events": [Event…]}`，返回 `{"ok": true, "stored": N}`。前端攒批：每 10 秒或攒满 50 条发一次；页面隐藏/关闭时用 `navigator.sendBeacon` 发剩余的（Blob 类型 `application/json`；浏览器不收这种 Blob 时退到 `fetch` keepalive）。2026-10-09 起所有 POST 过跨站防护：只收 `application/json`、Host 为本机或 `*.ts.net`、带 Origin 时须同源，`text/plain` 一律 403。
```json
{"ts": "2026-10-04T08:01:02.345+09:00", "kind": "<见下表>", "item_id": "…或 null", "edition_date": "2026-10-04", "ms": 1234, "meta": {}}
```
| kind | 何时发 | ms | meta |
|---|---|---|---|
| `impression` | 条目在视口内可见 ≥50% 面积并持续 ≥800ms；离开视口时发一次，记总可见时长 | 可见毫秒 | `{"section": "lead|top|briefs|blind", "rank": n}` |
| `open_item` | 点标题进单篇页 | null | `{"from": "paper|keyboard"}` |
| `open_original` | 点 ↗ 或「阅读原文」 | null | `{"from": "paper|item"}` |
| `item_dwell` | 离开单篇页 | 停留毫秒（只计页面可见且 App 在前台的时间） | `{"max_scroll_pct": 0–100, "fulltext_chars": n}` |
| `expand_feedback` | 展开「更多」面板 | null | `{}` |
| `focus` | j/k 把焦点移到某条 | null | `{}` |
| `session_start` / `session_end` | 页面加载 / 隐藏或关闭 | session_end 记本次前台总毫秒 | `{"editions_seen": n}` |
| `app_active` / `app_inactive` | Mac App 切到前台 / 后台（由 App 调 `window.__todayAppActive(true|false)` 触发） | null | `{}` |
「可见」一律以 `document.visibilityState=='visible'` 且 App 在前台为前提；后台时间不计入任何 ms。

### 后端用途（两层）
1. **自动小调（数字旋钮，有上限、可撤销、全留痕）**：每天出版前，AI 读昨天的行为汇总（每条：所在分区、曝光时长、是否点开、停留时长相对正文长度、滚动深度、是否开原文、显式评分）+ 当前画像，输出对 source / author / topic 的调整建议；单个旋钮单日调整幅度 ≤ ±0.05、绝对值夹在 [0.05, 0.95]，自动生效，写 `ai_adjustments` 表。管道页显示「AI 调整记录」，每条可一键撤销。
2. **画像修订（文字，必须用户批准）**：同一步里 AI 若认为画像文本该改（新兴趣、该降的域、文风偏好），写成「编辑部来信」提案，用户接受才进画像。
- 另出一段「昨天你是怎么读的」（≤150 字），放在当期报头下面，让用户看得见系统怎么理解他。

### 新增接口
| 方法 路径 | 返回 |
|---|---|
| `POST /api/paper/events` | 见上 |
| `GET /api/paper/adjustments?limit=50` | `{"adjustments": [{"id", "ts", "kind": "source|author|topic", "key", "label", "before", "after", "reason", "reverted": false}]}` |
| `POST /api/paper/adjustments/revert` `{"id"}` | `{"ok": true, "key", "before", "after"}` |
Edition 对象新增字段 `"reading_note": "昨天你是怎么读的……" 或 null`。

## 5. 本机投递箱 + App 推送（新闻 App 式推送，不依赖邮件）
本机各项目原来发邮件的通知，改投到报纸里；紧急的由「今日」App 弹 macOS 系统通知，点通知直接跳到报纸里那一条。邮件通道保留代码但默认关闭。

### 投递 `POST /api/paper/inbox`
```json
{"source": "pil.alerts", "title": "东京都 大雨警報", "body": "正文，可多行 markdown 纯文本", "url": "https://… 或 null", "priority": "urgent|normal|low", "dedup_key": "可选，同 key 24 小时内只收一次"}
```
返回 `{"ok": true, "inbox_id": 123, "deduped": false}`。`source` 用 `<项目>.<用途>` 命名。投递方在服务不可达时写本机 spool（`~/Library/Application Support/TodayPaper/spool/*.json`），服务启动后与每次出版前自动补收。

### 版面
- `urgent`：报头上方红色「快讯」横条（当天 + 未读），点开展开正文；同时触发 App 通知。
- `normal`：当期新增分区 `"inbox"`（「本机简报」），按 source 分组，每组折叠成一行标题 + 条数，点开平铺全部条目。
- `low`：只进「本机简报」，不提醒。
- Edition 对象 `sections` 新增 `"inbox": [InboxItem]`，顶层新增 `"flash": [InboxItem]`（urgent 且未读）。
```json
InboxItem = {"inbox_id": 123, "source": "pil.alerts", "source_label": "灾害预警", "title": "…", "body": "…", "url": null, "priority": "urgent", "created_at": "…", "read": false}
```
- `POST /api/paper/inbox/read` `{"inbox_id"}` → `{"ok": true}`（点开即标已读；也算一次行为事件 `open_inbox`，ms=null）。

### App 推送 `GET /api/paper/notifications?since=<ISO 时间>`
返回 `{"now": "<服务端时间>", "notifications": [{"id": "edition:2026-10-05" 或 "inbox:123", "title": "…", "body": "…", "open_path": "/paper/#/" 或 "/paper/#/inbox/123"}]}`。两类：新一期出版（每期一条，标题「今日 · 第 N 期」，正文 = 头条标题）、`urgent` 投递。App 每 60 秒拉一次，用上次的 `now` 作 `since`。

## 6. 全平台接入：登录态、全部来源、推荐关注、讨论综述
用户目标：不再逐个打开平台（微博、知乎、公众号、YouTube、播客/小宇宙、财新……），只看「今日」。任何媒体格式都收；音视频以转录文本进入。关注由用户自己去平台点，系统负责发现与推荐、并自动读到用户的关注流。

### 6.1 登录态健康 `auth_health`
- 每个需要登录的通道登记一个探针：`{"key": "weibo", "label": "微博", "probe": <函数>, "relogin": {"kind": "command|url", "value": "..."}}`。探针每小时跑一次（随 scan-feedback 那个定时任务即可），结果写表 `auth_status(key PK, ok INTEGER, checked_at, detail, since_failing)`。
- `GET /api/paper/auth` → `{"channels": [{"key", "label", "ok": true|false, "checked_at", "detail", "since_failing", "relogin_hint": "人话说明怎么重登"}]}`。
- 任一 `ok=false`：Edition 顶层新增 `"auth_alerts": [同上结构]`，前端在报头上方显示黄色条「微博登录已失效 · 重新登录」；通知接口额外推一条（同一通道失效后只推一次，恢复后再失效才再推）。
- `POST /api/paper/auth/relogin {"key"}`：仅当 `relogin.kind=="command"` 时在后台启动登记好的登录脚本（白名单，前端传不进任意命令），返回 `{"ok": true, "started": true}`；`kind=="url"` 时返回 `{"ok": true, "open_url": "..."}` 由前端/App 打开。
- 已实现的通道：微博（Playwright storage_state）、知乎（cookie JSON）、微信读书（外部导出工具的产物目录）、财新（浏览器 profile/cookies）。各自路径见 README「配置项」。

### 6.2 全部来源 `archive`
- `GET /api/paper/archive?source=&date=&q=&before=&limit=50` → `{"items": [ArchiveItem], "next_before": "..."}`；`ArchiveItem` = Item 的子集（`item_id,title,url,source,source_label,author_label,published_at,one_liner,on_paper: "2026-10-04|null"`）。`q` 走 SQLite FTS5（标题+正文）。
- `GET /api/paper/archive/sources` → 每个来源近 7 天条数与最近一条时间，用于来源列表。
- 用途：「看原文和当天前后有什么」——单篇页上加「同一来源当天的其它内容」链接，跳到 `archive?source=…&date=…`。

### 6.3 推荐关注 `follow_suggestions`
- 被动发现，零额外抓取：从已抓内容里统计「被关注者转发/引用/点赞但用户没关注」的账号（微博 `retweeted_status.user`、知乎 moments 里的被赞作者、公众号文章里的引用号等），每周由 AI 结合画像与已有反馈挑 ≤5 个，写理由（引用具体内容）。
- `GET /api/paper/follow_suggestions` → `{"suggestions": [{"id", "platform", "account_id", "label", "url", "reason", "evidence": [{"title","url"}], "status": "new|followed|dismissed"}]}`；`POST /api/paper/follow_suggestions/decide {"id","decision":"dismiss"}`。`followed` 由系统自动判定：之后该账号出现在用户的关注流里即视为已关注。
- 版面：每周一期带「推荐关注」小栏，每条「去关注 ↗」直接开该账号主页（用户自己点关注）。

### 6.4 讨论综述（社交与讨论区不给原帖流）
- 微博、知乎这类条目不逐帖上版。出版时把当天（及前一天未上版的）社交条目按语义聚类成话题，每个话题由 AI 写一条「综述 Item」：`kind:"digest"`，`title` = 话题一句话，`lede` = 发生了什么、各方说法与分歧，`quotes`: 2–3 条最有信息量的原话 `{text, author_label, url}`，`members`: 组内全部原帖 item_id。综述 Item 与普通 Item 同版面排序；点进单篇页看全部原帖。
- Item 对象新增可选字段 `kind`（`"article"` 缺省 / `"digest"` / `"media"`）、`quotes`、`members`、`media`（`{"type":"video|audio","duration_s","transcript_chars"}`）。

## 7. 「告诉我不知道的」是一等目标
只验证、强化用户已有偏见和判断规律的报纸算失败。行为学习天然会把人推回舒适区（人更愿意读同意自己的东西），所以这一节的约束**优先于第 4 节的行为学习**。

### 7.1 每条一个「新知判定」（AI 预处理里做）
预处理时额外给模型：与该条最相关的 3 个 vault 判断对象（用 PIL 现有的 vault 检索取标题+摘要）+ 画像。模型输出：
```json
"novelty": {"kind": "new_fact|new_mechanism|counter|known|confirming", "why": "一句话：具体是哪件事/哪个数/哪条机制是新的，或它反驳了你的哪条判断", "against": "被挑战的 vault 对象名或画像那一句，没有则 null"}
```
- `new_fact` 新事实/新数字；`new_mechanism` 没见过的因果链或跨域连接；`counter` 与你已有判断相反且有具体证据；`known` 你大概率已知；`confirming` 主要是在印证你已有的看法。
- Item 新增字段 `novelty`。前端在条目上用小标签显示：「新知」「反方」（counter）、「已知」「印证」用灰色小字。

### 7.2 版面配额（代码硬约束）
- 主线（不含盲区版）里 `new_fact|new_mechanism|counter` 合计 ≥40%；其中 `counter` 至少 3 条（当天候选里不够就有多少放多少，并在 stats 里记缺口）。头条与要闻（lead+top）里 `known|confirming` 最多 1 条。
- 新增分区 `"counter"`：「反方」——当天所有 `counter` 条目里挑最有证据的 ≤5 条单独成栏，每条显示 `novelty.against`（「这条在挑战你的：<判断名>」）。
- Edition `stats` 新增 `novelty_mix: {"new_fact": n, "new_mechanism": n, "counter": n, "known": n, "confirming": n}`，报头下显示一行「今日新知 x% · 反方 n 条」。

### 7.3 行为学习的护栏（修订第 4 节）
- `counter` 与盲区版条目被跳过、读得短，**不得**产生任何负向旋钮调整（只有显式 👎/写得差/没见识 才算负反馈）。
- AI 学习提示词明写：目标是「让用户知道更多他不知道的」，不是「让用户读得更多」；不得因为用户常读某类印证性内容就加权该类。
- 每周统计「用户点开/读完的条目里 known+confirming 的占比」，连续两周上升则在编辑部来信里自动写一封「你最近在读越来越多印证自己的东西」并附数字。
- 「早知道」原因码与 `novelty.kind=known` 的不一致（AI 判新、用户说早知道）记下来，每周喂回预处理提示词作为校准样例。

## 8. 人间温暖栏
提醒「人性温暖」：亲情、爱情、家国情怀、对陌生人的善意、跨越多年的回报。画像里原有 lane 5「人间温暖」，此前没有产出这类内容的源。
- 新增分区 `"warmth"`：每期 3–5 条，不走兴趣排序、不受行为学习影响（跳过不算负反馈），新颖性对它无意义。
- **核实优先**（沿用画像 lane 5 规则）：温暖故事是网上被编造最多的体裁，假的比没有更糟。每条 AI 预处理额外输出 `verification`：`{"level": "primary|secondary|unverified", "named": ["具名人物/地点/时间/金额"], "note": "依据"}`；`primary` = 有一手来源（当事人、当地媒体原报道、官方记录），`unverified` 照常显示但标「未核实」。同一故事多年反复转载的（「30 年前捐 5000 美元」类）要找到最早出处，找不到标「出处不明」。
- 家国情怀类同样要求具名的人和事；只有口号、宣传套话、没有具体个人经历的不收（叙事中立规则：不替任何一方宣传）。
- Item 字段 `verification` 可选；前端在温暖栏条目上显示「已核实 · 一手」/「二手」/「未核实」小标签。

## 9. 风险提示栏
提示用户本人可能遇到的危险和风险，按「他在哪、要去哪」个性化。
- **常驻地**：环境变量 `PIL_HOME_PLACE`（缺省东京；已有灾害预警 lane，日本只收 警報/特別警報，不降门槛）。
- **行程**：新表 `trips(trip_id, place, country, start_date, end_date, note, created_at)`；管道页新增「行程」小表单（地点、起止日期、备注），`GET/POST /api/paper/trips`、`POST /api/paper/trips/delete {"trip_id"}`。出发前 30 天起，每期为该行程生成一条「行前风险简报」Item（`kind:"risk"`）；`end_date` 可留空，留空=从开始日起持续监测 14 天（开始日算第 1 天，开始日早于今天满 14 天即停止生成简报、也不再算行程地命中；2026-10-07 用户定，H-6），表单「结束」栏须标明这一点。简报内容：气候与季节性灾害（含 ENSO 等大尺度气候状态对当地的影响，须引用官方机构当期说法）、传染病、治安（**具体到区域与时段**，如「某区某街某时段后避免步行」，须引用当地警方或官方犯罪数据/本国驻外机构提醒）、交通与诈骗常见手法、当地法规雷区。每条结论附来源与日期；来源不足时明说「没查到官方说法」，不得编。
- **信息源**（加进 RSS 与定时抓取）：日本外务省海外安全信息、美国国务院 Travel Advisories、中国外交部/驻外使领馆领事提醒、WHO 疾病暴发通报（DON）、各国气象机构的 ENSO 状态、行程目的地城市的官方犯罪开放数据（如有）。
- 新增分区 `"risk"`：当期所有 `kind:"risk"` 条目 + 与常驻地/行程地相关的 urgent 投递；没有就不显示。行程临近（≤3 天）且有新的高等级警示时走 App 推送（`priority=urgent` 投递到 inbox）。

## 10. 机会、结算、闲与美；版面预算
整体框架：成长（主线/新知/反方/盲区）、防护（风险/快讯）、提醒自己是人（温暖/闲与美），外加机会与结算两块。

### 10.1 机会栏 `"opportunity"`（每期 ≤3 条）
依据：人对框架外的机会普遍识别滞后。预处理新增 `lane_tags` 值 `"opportunity"`：尚无共识、信号混乱、但可能是大变化开端的条目。入栏条目额外输出 `opportunity: {"if_true": "若为真你能做什么（具体动作）", "kill_signal": "出现什么就说明它错了", "horizon": "大致时间尺度"}`。不提供个性化投资建议的口吻，只列可选动作与证伪条件。

### 10.2 结算栏 `"settle"`
来源：现有 `claims` 表（文章里的可检验断言，带 `check_after`）中到期未结的；Item 关联的作者 track record（同作者已结算断言的对错数）。每期列「今天到期的」≤5 条：断言原文、谁说的、当时出处、现在的证据（AI 当晚先核，给初判 `likely_true|likely_false|unclear` + 依据链接）、用户可一键裁定（复用 `POST /api/claims/resolve`）。作者对错统计并入 `author_trust` 的显示（不自动改分）。
- **名额只给近期到期的**：`check_after` 落在 [出版日 − 7 天, 出版日] 的未结断言，新到期的排前（`RECENT_DUE_DAYS=7`）。更早到期的和没有到期日的都算**积压**，不占版面，走 `pil settle-backlog [--limit N] [--execute]` 批量初判：缺省 dry-run 只读连库，列将处理的断言与检索到的证据数，不调模型不写库；`--execute` 才逐条调模型写 `claim_prechecks`（已有有效初判的跳过；每条立即落库，中断后重跑接着做）。`--rejudge-before YYYY-MM-DD` 改为重判初判早于该日的已到期未结断言，同样缺省 dry-run。**错峰**（可选，`PIL_OFFPEAK_GATE=deepseek` 开启）：`--execute`（含重判）每次调模型前判 DeepSeek 高峰（北京时间周一至周五 9–12、14–18 点），高峰就睡到该段结束再调，日志写明等待到何时。定时示例见 `launchd/`。
- **证据检索以到期日为锚**：按条目发布时间 `items.ts`（不是入库时间：入库时间可能远晚于发布时间）取 [到期日 − 14 天, min(max(到期日, 出处发布日) + 60 天, 现在)] 内的条目，items_fts 二元组 OR 命中、bm25 排序，取前 5；断言自己的出处条目不算证据。前 14 天覆盖提前发生/提前公布，后 60 天覆盖月度统计、季报、央行纪要的发布滞后；年报类（到期后 3–4 个月）仍会落空，模型给 unclear。上界取到期日与出处发布日中较晚者，是因为相当一部分断言的到期日早于出处文章（回顾性事实，或抽取把年份写早一年），只锚到期日会全打到无关的旧条目上。无到期日的断言以出处发布日为锚，按 `items.ts` 取 [出处发布日 − 14 天, min(出处发布日 + 60 天, 现在)]（`UNDATED_BEFORE_DAYS` / `UNDATED_AFTER_DAYS`），出处条目不在库或无发布时间时才退回「现在往前 30 天」。锚定出处的意义在于积压放久之后不漂到无关的新条目上。常量在 `paper_settle.py`（`EVIDENCE_BEFORE_DAYS` / `EVIDENCE_AFTER_DAYS` / `SEARCH_DAYS`）。

### 10.3 闲与美栏 `"leisure"`（每期 ≤4 条，跳过不算负反馈）
- 影视库（可选）：读一个本地影视库 SQLite（只读；路径用环境变量 `HOME_CINEMA_DB` 配置；读不到就跳过）里导演/剧集的新作与更新。
- 书：已读账里作者的新书（读配置文件给的作者列表，缺省空列表）。
- 每条 Item `kind:"leisure"`。

### 10.4 版面预算
- 每栏条数上限（可在 `config/paper_layout.toml` 改）：lead 1 / top 5 / counter 5 / briefs 30 / blind 9 / warmth 5 / risk 不限 / opportunity 3 / settle 5 / leisure 4 / inbox 不限（折叠为按来源平铺）/ follow 5。超出的不上版，进「全部来源」。
- 每期页尾显示「今天就这些 · 预计阅读 N 分钟」（按各条导语+简讯字数估），不再往下自动加载更早一期，除非用户点「看昨天的」。（修订第 2 节「往下滚自动加载前一期」：改为页尾按钮。）
- 每周编辑部来信附「各栏实际阅读时长与点开率」（来自行为监测），连续 4 周点开率 <5% 的栏目自动写提案「建议砍掉/合并 X 栏」。


## 11. 批注
反馈面板底部一个文本框，用户可以给任何一条写批注（更详细的反馈）。`Item.my.note` 为当前批注或 null。
- `POST /api/paper/note {"item_id", "text"}` → `{"ok": true, "item_id", "note": "<文本或 null>", "saved_at", "effect": {"kind": "queued", "label": "批注已存，明早编辑部复盘时会读"}}`；空文本 = 删除；单条 ≤4000 字。
- 次日出版前的行为复盘（第 4 节 learn）把当天新写/改过的批注单独列给模型，作为优先级最高的显式反馈；有批注时即使当天没有曝光事件也照样复盘。
