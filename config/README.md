# config/

这里的文件都是**示例配置**，用来让仓库开箱能跑测试、让你看清格式。真实使用时建议把整个目录
复制到别处（例如 `~/Library/Application Support/personal-intel-loop/config/`），改成你自己的内容，
再用环境变量 `PIL_CONFIG_DIR` 指过去。

| 文件 | 用途 |
|---|---|
| `rss_feeds.json` | `rss_briefing` 采集器的 RSS 清单（示例：几个公开媒体/机构源） |
| `html_columns.json` | `html_columns` 采集器：没有 RSS 的报纸专栏，按正则抓列表页（示例：几家报纸的公开人物专栏） |
| `paper_layout.toml` | 报纸各栏条数上限、温暖栏兜底来源 |
| `place_aliases.json` | 行程风险栏用的国家/城市多语别名表 |
| `disaster_alerts.example.json` | 灾害预警盯点示例；复制成 `disaster_alerts.json` 才生效（该文件已加入 .gitignore） |
| `profile_template/` | **虚构**的示例阅读画像；复制到 `PIL_PROFILE_DIR` 后改成你自己的 |

可选文件（不存在时相应功能为空）：`leisure_authors.txt`（休闲栏关注的作者，一行一个）、
`podcast_subscriptions_accepted.json`、`source_adapters_todo.json`（「信源提案」接受后写入）。
