"""PIL 网页样式常量,由 web.py import。

设计约束(2026-08-26 重做规格):
- 单文件自足: 不引任何外部字体 / CDN / 图片,只用系统字体栈。
- 跟随 prefers-color-scheme 出深浅两套色,正文对比度 ≥ 7:1。
- 禁止动画 / transition / 渐变。
- 桌面主力: max-width 820px 居中; <700px 降级单列。
- 信息层级: 标题 > 来龙/去脉(正文级) > 摘要 > 元信息,四级肉眼可分。
"""

CSS = """
:root{
  --bg:#fafaf9; --surface:#ffffff; --text:#1a1a1a; --text-2:#3f3f3c;
  --muted:#6e6e6a; --faint:#767670; --border:#e3e1dc; --border-strong:#c9c7c1;
  --accent:#0b5cad; --accent-soft:#e8f0f9; --accent-ink:#ffffff;
  --warn:#9a5b00; --warn-bg:#fdf3e2; --warn-border:#e8d5b0;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#171716; --surface:#201f1e; --text:#ebebe8; --text-2:#cfcfcb;
    --muted:#a8a8a3; --faint:#8b8b86; --border:#36352f; --border-strong:#565450;
    --accent:#7db3e8; --accent-soft:#24344a; --accent-ink:#10151c;
    --warn:#e0a44f; --warn-bg:#33270f; --warn-border:#5a4620;
  }
}
*{box-sizing:border-box}
body{
  background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","PingFang SC","Hiragino Sans","Noto Sans CJK SC",Arial,sans-serif;
  font-size:16.5px; line-height:1.7; margin:0; padding:28px 20px 64px;
}
main{max-width:820px; margin:0 auto}
a{color:var(--accent)}
.muted{color:var(--muted)}
h1{font-size:1.55rem; line-height:1.3; margin:6px 0 4px}
h2{font-size:1.05rem; color:var(--muted); font-weight:600; margin:34px 0 14px; letter-spacing:.01em}
.topnav{font-size:14px; margin-bottom:10px}
.topnav a{margin-right:14px}
.pageline{color:var(--muted); font-size:14px; margin:0 0 8px}
.notice{font-size:14px; color:var(--muted); margin:4px 0 0}

/* 卡片: 靠留白分隔, 极浅描边 */
.card,.claim,.proposal{
  background:var(--surface); border:1px solid var(--border); border-radius:10px;
  padding:20px 22px; margin:0 0 26px;
}
.card h3{font-size:19px; line-height:1.45; margin:0 0 4px; font-weight:650}
.card h3 a{color:var(--text); text-decoration:none; border-bottom:1px solid var(--border-strong)}
.card h3 a:hover{color:var(--accent); border-bottom-color:var(--accent)}

/* 第二层: 来龙 / 去脉 —— 正文字号, 不灰, 带锚标签 */
.ctx{font-size:16.5px; margin:12px 0 0; color:var(--text-2)}
.ctx .tag{
  display:inline-block; font-size:12.5px; font-weight:700; line-height:1;
  color:var(--accent); background:var(--accent-soft);
  border-radius:4px; padding:4px 7px; margin-right:8px; vertical-align:2px;
}
.ctx.so{color:var(--text); font-weight:500}
.ctx.missing{
  color:var(--warn); background:var(--warn-bg); border:1px solid var(--warn-border);
  border-radius:6px; padding:8px 12px; font-size:15px;
}

/* 第三层: 摘要 */
.summary{font-size:15px; color:var(--text-2); white-space:pre-wrap; margin:12px 0 0}
details{margin:10px 0 0; font-size:14px}
details summary{color:var(--accent); cursor:pointer}
details .summary{margin-top:8px}

/* 第四层: 元信息 */
.meta{font-size:13px; color:var(--faint); margin:10px 0 0; line-height:1.6}
.meta .src{color:var(--muted)}

/* 信任度回显: 行内小提示, 不打断阅读 */
.delta{font-size:13px; color:var(--muted); margin:10px 0 0; min-height:1em}

/* 五个原因码: 一行, 同一视觉重量 */
.actions{display:flex; flex-wrap:nowrap; gap:8px; margin-top:14px}
.actions button{
  flex:1 1 0; font:inherit; font-size:14px; height:34px; padding:0 10px;
  border:1px solid var(--border-strong); border-radius:8px;
  background:transparent; color:var(--text-2); cursor:pointer; white-space:nowrap;
}
.actions button:hover{background:var(--accent-soft)}
.actions button.pressed{background:var(--accent); border-color:var(--accent); color:var(--accent-ink)}
.actions button:disabled{cursor:default}
.actions button.pressed:disabled{opacity:.92}

/* profile 提案卡 */
.proposal{padding:16px 18px}
.proposal .plabel{font-size:12.5px; font-weight:700; color:var(--muted); margin-right:6px}
.proposal .pline{margin:4px 0; font-size:15px}
.proposal .pactions{display:flex; justify-content:flex-end; gap:8px; margin-top:10px}
.proposal .pactions button,.savebtn{
  font:inherit; font-size:14px; height:34px; padding:0 16px;
  border:1px solid var(--border-strong); border-radius:8px;
  background:transparent; color:var(--text-2); cursor:pointer;
}
.proposal .pactions button:hover,.savebtn:hover{background:var(--accent-soft)}
.proposal .pactions button.pressed,.savebtn.pressed{background:var(--accent); border-color:var(--accent); color:var(--accent-ink)}

textarea{
  width:100%; min-height:55vh; padding:12px; margin-top:10px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:13.5px; line-height:1.6;
  background:var(--surface); color:var(--text); border:1px solid var(--border-strong); border-radius:8px;
}

/* 断言卡 */
.claim .ctext{font-size:15.5px}
.claim .cdue{font-size:13px; color:var(--faint); margin-top:6px}

@media (max-width:700px){
  body{padding:18px 12px 48px; font-size:16px}
  .card,.claim,.proposal{padding:16px; margin-bottom:20px}
  .actions{flex-wrap:wrap}
  .actions button{flex:1 1 44%; height:36px}
}
"""
