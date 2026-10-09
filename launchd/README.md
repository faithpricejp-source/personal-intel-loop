# launchd 示例

这里的 plist 只是**占位示例**：路径写成 `__PROJECT_ROOT__`（仓库根目录）和 `__PIL_HOME__`（数据目录，
缺省是 `~/Library/Application Support/personal-intel-loop`），Label 用 `com.example.pil.*`。时间点也只是示例。

安装（以 paper 为例）：

```bash
sed -e "s|__PROJECT_ROOT__|$HOME/src/personal-intel-loop|g" \
    -e "s|__PIL_HOME__|$HOME/Library/Application Support/personal-intel-loop|g" \
    launchd/com.example.pil.paper.plist > ~/Library/LaunchAgents/com.example.pil.paper.plist
mkdir -p "$HOME/Library/Application Support/personal-intel-loop/logs"
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.pil.paper.plist
```

LLM、推送等配置项是环境变量（见仓库 README「配置项」），需要的话加进 plist 的 `EnvironmentVariables`。
不要把密钥直接写进 plist，用 `*_API_KEY_FILE` 指向一个只有自己可读的文件。

## 可以排的任务

| 示例文件 | 命令 | 建议频率 |
|---|---|---|
| `com.example.pil.ingest.plist` | `pil ingest-first-ring --adapter ...` | 每天一次（出版之前） |
| `com.example.pil.paper.plist` | `pil paper` | 每天一次（抓取之后） |
| `com.example.pil.alerts.plist` | `pil alerts-notify` | 每 30 分钟 |
| `com.example.pil.serve.plist` | `pil serve --host 127.0.0.1 --port 8766` | 常驻（Mac App 也能自己拉起服务） |

其它可选：`pil scan-feedback --propose`（读日报 markdown 里勾选的反馈，高频）、`pil digest ...`（markdown 日报）、
`pil detect-promotions`（扫笔记库里引用了条目的对象，每天一次）、`pil settle-backlog --execute --limit 20`（断言积压初判，每周一次）、
`pil ingest --adapter weibo_home`（需要登录态的源可以单独排）。
