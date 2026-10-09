#!/usr/bin/env node
/* 10-05 验收 C1 · 前端「采集健康」栏测试（只用 Node 内置模块）。
 * 用 vm 在沙箱里执行整个 app.js（剥掉 IIFE 外壳，原生浏览器脚本），直接调文件里的真实渲染函数
 * healthPanelHTML / healthRowHTML；不复制、不改写被测代码。全部通过 exit 0，任一失败 exit 1。
 * 用法：node project/tests/frontend/health_panel.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.resolve(__dirname, '../../src/personal_intel_loop/paper_web/app.js');

function makeDocument() {
  const doc = {
    readyState: 'loading',          // 别让 app.js 末尾的 boot() 跑起来
    listeners: {},
    addEventListener(type, fn) { (doc.listeners[type] = doc.listeners[type] || []).push(fn); },
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getElementById() { return null; },
    createElement() {
      return { className: '', innerHTML: '', hidden: false, style: {}, childNodes: [],
               appendChild() {}, setAttribute() {}, getAttribute() { return null; } };
    },
    documentElement: { style: {} },
    body: { appendChild() {}, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } } },
  };
  return doc;
}

function loadApp() {
  const src = fs.readFileSync(APP, 'utf8');
  const head = '(function () {';
  const start = src.indexOf(head);
  const end = src.lastIndexOf('})();');
  if (start < 0 || end < 0) throw new Error('app.js 结构不符合预期');
  const body = "'use strict';\n" + src.slice(start + head.length, end);
  const sandbox = {
    console: { log() {}, debug() {}, warn() {}, error() {} },
    location: { search: '', hash: '#/', reload() {} },
    history: { scrollRestoration: 'auto', pushState() {}, replaceState() {} },
    navigator: { sendBeacon() { return true; } },
    document: makeDocument(),
    fetch() { return Promise.reject(new Error('测试不该发请求')); },
    setTimeout() { return 0; },
    clearTimeout() {},
    setInterval() { return 0; },
    clearInterval() {},
    requestAnimationFrame() { return 0; },
    scrollTo() {},
    addEventListener() {},
    removeEventListener() {},
    Intl: Intl,
    Date: Date,
    JSON: JSON,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(body, sandbox, { filename: 'app.js' });
  sandbox.__src = src;
  return sandbox;
}

const sb = loadApp();

function row(extra) {
  return Object.assign({
    adapter: 'aihot', status: 'ok', count: 120, errors: [],
    run_at_utc: '2026-10-05T10:48:44.825825Z', zero_streak: 0, stale: false,
  }, extra || {});
}

function assert(cond, msg) { if (!cond) throw new Error(msg); }
function has(html, needle, label) {
  assert(html.indexOf(needle) >= 0, (label || '') + ' 缺少 ' + JSON.stringify(needle) + '；实际：' + html);
}
function lacks(html, needle, label) {
  assert(html.indexOf(needle) < 0, (label || '') + ' 不该出现 ' + JSON.stringify(needle) + '；实际：' + html);
}

const CASES = [
  {
    id: 'FE1_empty', desc: 'health 为空列表 → 一行「所有采集器最近一轮正常」，标题仍是「采集健康」',
    run() {
      assert(typeof sb.healthPanelHTML === 'function', 'app.js 没有 healthPanelHTML 渲染函数');
      const html = sb.healthPanelHTML([]);
      has(html, '采集健康');
      has(html, '所有采集器最近一轮正常');
      lacks(html, 'health-row');
      const html2 = sb.healthPanelHTML(undefined);   // 后端没给 health 键（老接口）也不能崩
      has(html2, '所有采集器最近一轮正常');
    },
  },
  {
    id: 'FE2_degraded', desc: '降级：一行里有名字、状态「降级」、东京时间的最近运行时间',
    run() {
      const html = sb.healthPanelHTML([row({ status: 'degraded', count: 0 })]);
      has(html, '采集健康');
      has(html, 'aihot');
      has(html, '降级');
      has(html, '2026-10-05 19:48');   // UTC 10:48 → 东京 19:48
      lacks(html, '所有采集器最近一轮正常');
    },
  },
  {
    id: 'FE3_partial_failures', desc: 'status=ok 但有 errors → 状态写「有失败」并列出原因',
    run() {
      const html = sb.healthPanelHTML([row({ errors: ['feed failed: Reuters Top News (403)'] })]);
      has(html, '有失败');
      has(html, 'feed failed: Reuters Top News (403)');
    },
  },
  {
    id: 'FE4_zero_streak', desc: '连续 0 条 → 状态写「连续 N 轮 0 条」',
    run() {
      const html = sb.healthPanelHTML([row({ count: 0, zero_streak: 4 })]);
      has(html, '连续 4 轮 0 条');
      has(html, 'aihot');
    },
  },
  {
    id: 'FE5_stale', desc: '超过 48 小时没跑 → 状态写「超过 48 小时没跑」',
    run() {
      const html = sb.healthPanelHTML([row({ stale: true })]);
      has(html, '超过 48 小时没跑');
    },
  },
  {
    id: 'FE6_errors_capped', desc: '失败原因最多 3 条，超出显示「等 N 条」',
    run() {
      const errors = ['e1-原因', 'e2-原因', 'e3-原因', 'e4-原因', 'e5-原因'];
      const html = sb.healthPanelHTML([row({ status: 'degraded', errors })]);
      has(html, 'e1-原因'); has(html, 'e2-原因'); has(html, 'e3-原因');
      lacks(html, 'e4-原因'); lacks(html, 'e5-原因');
      has(html, '等 2 条');
      const one = sb.healthPanelHTML([row({ status: 'degraded', errors: ['only'] })]);
      lacks(one, '等 ');   // 不超出就不该有「等 N 条」
    },
  },
  {
    id: 'FE7_escaping', desc: '采集器名与失败原因全部经 esc() 转义，不产生可执行标签',
    run() {
      const html = sb.healthPanelHTML([row({
        adapter: '<img src=x onerror=alert(1)>',
        status: 'degraded',
        errors: ['<script>alert("x & y")</script>', "it's <b>bad</b>"],
      })]);
      lacks(html, '<img');
      lacks(html, '<script');
      lacks(html, '<b>');
      has(html, '&lt;img');
      has(html, '&lt;script');
      has(html, '&quot;');
      has(html, '&#39;');
    },
  },
  {
    id: 'FE8_one_row_per_adapter', desc: '每个采集器一行；多种状态同时出现时各自成行',
    run() {
      const html = sb.healthPanelHTML([
        row({ adapter: 'aihot', status: 'degraded', errors: ['non-200: 404'] }),
        row({ adapter: 'mofa_anzen', zero_streak: 7, count: 0 }),
        row({ adapter: 'who_don', stale: true, run_at_utc: '2026-10-03T01:00:00.000000Z' }),
      ]);
      const rows = html.split('class="health-row"').length - 1;
      assert(rows === 3, '每个采集器一行：应有 3 个 health-row，实际 ' + rows + ' 个；' + html);
      ['aihot', 'mofa_anzen', 'who_don'].forEach((a) => has(html, a, '行 ' + a + '：'));
      has(html, '降级'); has(html, '连续 7 轮 0 条'); has(html, '超过 48 小时没跑');
    },
  },
  {
    id: 'FE9_missing_time', desc: '没有 run_at_utc 时时间位显示「—」，不能显示 undefined',
    run() {
      const html = sb.healthPanelHTML([row({ run_at_utc: '' })]);
      has(html, '—');
      lacks(html, 'undefined');
    },
  },
  {
    id: 'FE10_wired_into_pipeline_page', desc: '管道页渲染处调用采集健康栏（顶部一栏）',
    run() {
      const src = sb.__src;
      const start = src.indexOf('function showPipeline()');
      assert(start >= 0, '找不到 showPipeline');
      const body = src.slice(start, start + 4000);
      has(body, 'healthPanelHTML', 'showPipeline 顶部未渲染采集健康栏：');
      const at = body.indexOf('healthPanelHTML');
      const dir = body.indexOf('pipe-dir');
      assert(at >= 0 && (dir < 0 || at < dir), '「采集健康」应排在管道页顶部（画像目录之前）');
    },
  },
];

let failed = 0;
for (const c of CASES) {
  try {
    c.run();
    console.log('PASS ' + c.id + ' — ' + c.desc);
  } catch (e) {
    failed += 1;
    console.log('FAIL ' + c.id + ' — ' + c.desc + '\n     ' + e.message);
  }
}
console.log(failed ? failed + ' failed, ' + (CASES.length - failed) + ' passed' : '0 failed, ' + CASES.length + ' passed');
process.exit(failed ? 1 : 0);
