#!/usr/bin/env node
/* 10-06 复核 Kimi 审查发现（G 路）· 前端测试（只用 Node 内置模块）。
 * 与 tests/frontend/fix_1005.js 同构：用 vm 在沙箱里执行整个 app.js（剥掉 IIFE 壳，
 * 让文件里的真实函数成为沙箱全局），用假 DOM / 假 fetch 驱动真实事件链。
 * 本文件不复制、不改写 app.js 的任何被测函数。
 * 用法：node project/tests/frontend/kimi_review_g.js [用例id前缀]
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.resolve(__dirname, '../../src/personal_intel_loop/paper_web/app.js');

/* ---------------- 假 DOM ---------------- */

function parseCompound(sel) {
  const m = /^([a-zA-Z][\w-]*)?((?:[#.][\w-]+|\[[^\]]+\])*)$/.exec(String(sel).trim());
  if (!m) return null;
  const out = { tag: m[1] ? m[1].toLowerCase() : null, id: null, classes: [], attrs: [] };
  const rest = m[2] || '';
  const re = /([#.][\w-]+)|\[([^\]=]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]/g;
  let t;
  while ((t = re.exec(rest)) !== null) {
    if (t[1]) {
      if (t[1][0] === '#') out.id = t[1].slice(1);
      else out.classes.push(t[1].slice(1));
    } else {
      const v = t[3] !== undefined ? t[3] : (t[4] !== undefined ? t[4] : t[5]);
      out.attrs.push([t[2], v === undefined ? null : v]);
    }
  }
  return out;
}

function makeFakeDom() {
  class FakeNode {
    constructor(tagName) {
      this.tagName = String(tagName || 'div').toUpperCase();
      this.childNodes = [];
      this.parentNode = null;
      this.attributes = {};
      this.style = {};
      this.listeners = {};
      this._className = '';
      this.textContent = '';
      this._innerHTML = '';
      this.hidden = false;
      this.disabled = false;
      this.value = '';
      this.complete = false;
      this.naturalWidth = 0;
      const self = this;
      this.classList = {
        add(c) { const s = self._className.split(' ').filter(Boolean); if (s.indexOf(c) < 0) s.push(c); self._className = s.join(' '); },
        remove(c) { self._className = self._className.split(' ').filter((x) => x && x !== c).join(' '); },
        contains(c) { return self._className.split(' ').indexOf(c) >= 0; },
      };
    }
    get className() { return this._className; }
    set className(v) { this._className = String(v || ''); }
    get innerHTML() { return this._innerHTML; }
    set innerHTML(v) { this._innerHTML = String(v); }
    insertAdjacentHTML(pos, html) {
      if (pos === 'beforeend') this._innerHTML += String(html);
      else if (pos === 'afterbegin') this._innerHTML = String(html) + this._innerHTML;
      else throw new Error('假 DOM 不支持 insertAdjacentHTML 位置: ' + pos);
    }
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k === 'class') this._className = String(v);
    }
    appendChild(c) {
      if (c.parentNode) c.parentNode.removeChild(c);
      this.childNodes.push(c);
      c.parentNode = this;
      return c;
    }
    insertBefore(n, ref) {
      if (!ref) return this.appendChild(n);
      if (n.parentNode) n.parentNode.removeChild(n);
      const i = this.childNodes.indexOf(ref);
      if (i < 0) return this.appendChild(n);
      this.childNodes.splice(i, 0, n);
      n.parentNode = this;
      return n;
    }
    removeChild(c) {
      const i = this.childNodes.indexOf(c);
      if (i >= 0) this.childNodes.splice(i, 1);
      c.parentNode = null;
      return c;
    }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    removeEventListener() {}
    getBoundingClientRect() { return { top: 0, left: 0, right: 100, bottom: 100, width: 100, height: 100 }; }
    closest() { return null; }
    contains(n) {
      for (const c of this.childNodes) if (c === n || c.contains(n)) return true;
      return false;
    }
    _desc(out) { for (const c of this.childNodes) { out.push(c); if (c._desc) c._desc(out); } return out; }
    querySelectorAll(sel) {
      const alts = String(sel).split(',').map(parseCompound).filter(Boolean);
      return this._desc([]).filter((n) => alts.some((a) => nodeMatches(n, a)));
    }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  }

  function nodeMatches(n, c) {
    if (!(n instanceof FakeNode)) return false;
    if (c.tag && n.tagName.toLowerCase() !== c.tag) return false;
    if (c.id && n.attributes.id !== c.id) return false;
    for (const cls of c.classes) if (!n.classList.contains(cls)) return false;
    for (const [k, v] of c.attrs) {
      if (!Object.prototype.hasOwnProperty.call(n.attributes, k)) return false;
      if (v !== null && v !== undefined && n.attributes[k] !== v) return false;
    }
    return true;
  }

  const docById = {};
  const document = {
    readyState: 'loading',
    visibilityState: 'visible',
    activeElement: null,
    listeners: {},
    createElement: (tag) => new FakeNode(tag),
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    removeEventListener() {},
    querySelector(sel) {
      if (/^#[\w-]+$/.test(sel)) {
        const id = sel.slice(1);
        if (!docById[id]) { docById[id] = new FakeNode('div'); docById[id].setAttribute('id', id); }
        return docById[id];
      }
      return null;
    },
    querySelectorAll: () => [],
    contains: () => false,
  };
  return { FakeNode, document };
}

/* ---------------- 沙箱装配 ---------------- */

function bootSandbox() {
  const { FakeNode, document } = makeFakeDom();
  const fetchQueue = [];
  const timers = [];

  class FakeIntersectionObserver {
    constructor(cb, opts) { this.cb = cb; this.opts = opts; this.observeCalls = []; this.unobserveCalls = []; }
    observe(el) { this.observeCalls.push(el); }
    unobserve(el) { this.unobserveCalls.push(el); }
    disconnect() {}
  }

  const sandbox = {
    console: { log() {}, debug() {}, warn() {}, error() {} },
    location: { search: '', hash: '#/archive', reload() {} },
    history: { scrollRestoration: 'auto', pushState() {}, replaceState() {} },
    navigator: { sendBeacon() { return true; } },
    Blob: function (parts) { this.parts = parts; },
    document,
    IntersectionObserver: FakeIntersectionObserver,
    fetch: (url, opts) => new Promise((resolve, reject) => { fetchQueue.push({ url: String(url), opts, resolve, reject, done: false }); }),
    setTimeout(fn) { timers.push(fn); return timers.length; },
    clearTimeout() {},
    setInterval() { return 0; },
    clearInterval() {},
    requestAnimationFrame() { return 0; },
    scrollTo() {},
    scrollY: 0,
    pageYOffset: 0,
    innerHeight: 800,
    open() {},
    addEventListener(type, fn) { (sandbox.__winListeners[type] = sandbox.__winListeners[type] || []).push(fn); },
    removeEventListener() {},
    __winListeners: {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);

  const src = fs.readFileSync(APP, 'utf8');
  const head = '(function () {';
  const start = src.indexOf(head);
  const end = src.lastIndexOf('})();');
  if (start < 0 || end < 0) throw new Error('app.js 结构不符合预期');
  const body = "'use strict';\n" + src.slice(start + head.length, end);
  vm.runInContext(body, sandbox, { filename: 'app.js' });

  const helpers = {
    FakeNode,
    document,
    fetchQueue,
    pendingUrls() { return fetchQueue.filter((f) => !f.done).map((f) => f.url); },
    respond(urlLike, body) {
      const e = fetchQueue.find((f) => !f.done && f.url.indexOf(urlLike) >= 0);
      if (!e) throw new Error('没有待响应的 fetch: ' + urlLike);
      e.done = true;
      e.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) });
    },
    failFetch(urlLike, status, errMsg) {
      const e = fetchQueue.find((f) => !f.done && f.url.indexOf(urlLike) >= 0);
      if (!e) throw new Error('没有待响应的 fetch: ' + urlLike);
      e.done = true;
      e.resolve({ ok: false, status, text: () => Promise.resolve(JSON.stringify({ error: errMsg })) });
    },
    rejectFetch(urlLike, errMsg) {
      const e = fetchQueue.find((f) => !f.done && f.url.indexOf(urlLike) >= 0);
      if (!e) throw new Error('没有待响应的 fetch: ' + urlLike);
      e.done = true;
      e.reject(new Error(errMsg));
    },
  };
  const tick = () => new Promise((r) => setImmediate(r));
  return { sb: sandbox, h: helpers, tick, FakeNode };
}

/* ---------------- 公共数据与小工具 ---------------- */

function arcItemJSON(id) {
  return {
    item_id: 'arc-' + id, title: '标题-' + id, url: 'https://example.com/' + id,
    source: 'rss_briefing:src1', source_label: '源一', published_at: '2026-10-0' + (id % 9 || 1) + 'T01:00:00Z',
    one_liner: '一句话-' + id,
  };
}

/** 手工搭一个 archive 页（showArchive 的 innerHTML 串假 DOM 不解析，这里直接建节点） */
function buildArchivePage(sb, FakeNode) {
  const page = sb.document.createElement('section');
  page.className = 'archive';
  const status = sb.document.createElement('div');
  status.setAttribute('data-role', 'status');
  status.textContent = '正在取条目…';
  const box = sb.document.createElement('div');
  box.setAttribute('data-role', 'items');
  const more = sb.document.createElement('button');
  more.setAttribute('data-act', 'arc-more');
  more.hidden = true;
  const wrap = new FakeNode('div');
  wrap.className = 'arc-more-wrap';
  wrap.appendChild(more);
  page.appendChild(status);
  page.appendChild(box);
  page.appendChild(wrap);
  return page;
}

function resetArchiveState(sb, page) {
  sb.state.archive = {
    source: '', date: '', q: '', nextBefore: undefined, exhausted: false,
    loading: false, count: 0, sources: [],
  };
  sb.dom.archivePage = page;
  if (!page.parentNode) sb.dom.view.appendChild(page);
}

/** 搭一个行程页（showTrips 同构：表 + 表单 + flash） */
function buildTripsSection(sb) {
  const sec = sb.document.createElement('section');
  sec.className = 'trips';
  const tb = sb.document.createElement('tbody');
  const tr = sb.document.createElement('tr');
  const td = sb.document.createElement('td');
  td.setAttribute('colspan', '4');
  td.className = 'dimname';
  td.textContent = '正在读行程…';
  tr.appendChild(td);
  tb.appendChild(tr);
  sec.appendChild(tb);
  const fields = { place: '', start: '', end: '', note: '', tripflash: '' };
  Object.keys(fields).forEach((role) => {
    const el = sb.document.createElement('input');
    el.setAttribute('data-role', role);
    el.value = fields[role];
    sec.appendChild(el);
  });
  const btn = sb.document.createElement('button');
  btn.setAttribute('data-act', 'trip-add');
  sec.appendChild(btn);
  return sec;
}

/** 可控前台时钟：把沙箱里的 trackFgNow 换成读 now 的版本（app.js 内部调用即走它） */
function fakeClock(sb) {
  const clock = { now: 1000 };
  sb.trackFgNow = () => clock.now;
  return clock;
}

function impressions(sb) {
  return (sb.trackLog || []).filter((e) => e.kind === 'impression');
}

/* ---------------- 用例 ---------------- */

const CASES = [
  {
    id: 'G1_impression_survives_threshold_cross',
    desc: 'G-1：仍在视口内、只是跨过另一个 threshold 时，曝光计时不得被清零，离开时要能凑满 800ms 发一条 impression',
    async run(ctx) {
      const sb = ctx.sb;
      sb.cacheDom();
      const clock = fakeClock(sb);
      const root = sb.document.createElement('div');
      const card = sb.document.createElement('article');
      card.setAttribute('data-item-id', 'g1');
      card.setAttribute('data-sec', 'top');
      card.setAttribute('data-rank', '3');
      root.appendChild(card);
      sb.trackObserve(root);
      const io = sb.trackImpObserver;
      if (!io) throw new Error('前置条件坏了：trackObserve 没建 IntersectionObserver');

      // 0ms：可见比例 0.6 进入视口
      io.cb([{ isIntersecting: true, intersectionRatio: 0.6, intersectionRect: { height: 480 }, target: card }], io);
      const start0 = card.__imp.start;
      if (start0 === null || start0 === undefined) throw new Error('前置条件坏了：进入后没开始计时');

      // 500ms：仍在视口内，只是从 0.6 滚到 0.8，跨过已注册的 0.75 → 浏览器重发 entry
      clock.now = 1500;
      io.cb([{ isIntersecting: true, intersectionRatio: 0.8, intersectionRect: { height: 640 }, target: card }], io);
      if (card.__imp.start !== start0) {
        throw new Error('threshold 交叉重发 entry 把已积累的可见时长清零了：start ' + start0 + ' → ' + card.__imp.start);
      }

      // 900ms：扫描器判定够 800ms
      clock.now = 1900;
      sb.trackImpScan();
      if (card.__imp.ok !== true) throw new Error('前置条件坏了：扫描后 ok 应为 true（实际 ' + card.__imp.ok + '）');

      // 1000ms：真正离开视口
      clock.now = 2000;
      io.cb([{ isIntersecting: false, intersectionRatio: 0, intersectionRect: { height: 0 }, target: card }], io);
      const list = impressions(sb);
      if (list.length !== 1) throw new Error('慢滚读完一条应有 1 条 impression，实际 ' + list.length + ' 条：' + JSON.stringify(list));
      if (list[0].item_id !== 'g1') throw new Error('impression 记错了条目：' + list[0].item_id);
      if (list[0].ms < 800) throw new Error('曝光时长被缩水：ms=' + list[0].ms + '（应为累计可见 1000）');

      // 真离开后再进来看：必须重新起表（修复不能变成「永不重置」）
      clock.now = 3000;
      io.cb([{ isIntersecting: true, intersectionRatio: 1, intersectionRect: { height: 800 }, target: card }], io);
      if (card.__imp.start !== 3000) throw new Error('第二次进入视口没有重新起表：start=' + card.__imp.start);
    },
  },
  {
    id: 'G3_page2_failure_keeps_loaded_items',
    desc: 'G-3：第 2 页失败后已加载条目仍在、末尾追加提示行、游标与计数不动；重试取同一页',
    async run(ctx) {
      const sb = ctx.sb;
      const { FakeNode } = ctx;
      sb.cacheDom();
      const page = buildArchivePage(sb, FakeNode);
      resetArchiveState(sb, page);

      sb.loadArchivePage(null, true);
      ctx.h.respond('/api/paper/archive', { items: [arcItemJSON('1'), arcItemJSON('2'), arcItemJSON('3')], next_before: 'NB2' });
      await ctx.tick();
      const box = sb.dom.archivePage.querySelector('[data-role="items"]');
      if (box.innerHTML.indexOf('arc-1') < 0 || box.innerHTML.indexOf('arc-3') < 0) throw new Error('前置条件坏了：第 1 页 3 条没铺出来');
      if (sb.state.archive.count !== 3) throw new Error('前置条件坏了：st.count 应为 3，实际 ' + sb.state.archive.count);

      sb.loadArchivePage('NB2', false);
      ctx.h.failFetch('/api/paper/archive', 503, '翻页挂了');
      await ctx.tick();

      const html = box.innerHTML;
      const st = sb.state.archive;
      if (html.indexOf('arc-1') < 0 || html.indexOf('arc-3') < 0) throw new Error('第 2 页失败把已加载条目抹掉了');
      if (html.indexOf('arc-more-err') < 0 || html.indexOf('翻页挂了') < 0) throw new Error('没有追加失败提示行：' + html.slice(-200));
      if (html.indexOf('arc-more-err') < html.indexOf('arc-3')) throw new Error('提示行应在已加载条目之后');
      if (st.count !== 3 || st.nextBefore !== 'NB2' || st.exhausted) throw new Error('游标/计数被动了：' + JSON.stringify(st));
      const wrapBtn = sb.dom.archivePage.querySelector('.arc-more-wrap').querySelector('[data-act="arc-more"]');
      if (!wrapBtn.hidden) throw new Error('底部自动翻页按钮应先收起');

      // 「再试一次」→ 用同一游标取第 2 页，序号接着 3 往下
      sb.archiveMore();
      const calls = ctx.h.fetchQueue.filter((f) => f.url.indexOf('/api/paper/archive') >= 0);
      if (calls[calls.length - 1].url.indexOf('NB2') < 0) throw new Error('重试没用原游标：' + calls[calls.length - 1].url);
      ctx.h.respond('/api/paper/archive', { items: [arcItemJSON('4')], next_before: null });
      await ctx.tick();
      if (box.innerHTML.indexOf('arc-1') < 0 || box.innerHTML.indexOf('arc-4') < 0) throw new Error('重试成功后条目不全');
      if (st.count !== 4) throw new Error('重试成功后 count 应为 4，实际 ' + st.count);
    },
  },
  {
    id: 'G3_first_page_failure_retry_refetches_first_page',
    desc: 'G-3 顺带：第一页失败时「再试一次」原先是死按钮（无 next_before），现在重取第一页',
    async run(ctx) {
      const sb = ctx.sb;
      const { FakeNode } = ctx;
      sb.cacheDom();
      const page = buildArchivePage(sb, FakeNode);
      resetArchiveState(sb, page);
      sb.loadArchivePage(null, true);
      ctx.h.failFetch('/api/paper/archive', 503, '首屏挂了');
      await ctx.tick();
      const box = sb.dom.archivePage.querySelector('[data-role="items"]');
      if (box.innerHTML.indexOf('首屏挂了') < 0) throw new Error('首屏失败没显示错误条');
      const before = ctx.h.countFetchCall('/api/paper/archive');
      sb.archiveMore();
      if (ctx.h.countFetchCall('/api/paper/archive') !== before + 1) throw new Error('「再试一次」没发出请求（死按钮）');
      ctx.h.respond('/api/paper/archive', { items: [arcItemJSON('1')], next_before: null });
      await ctx.tick();
      if (box.innerHTML.indexOf('arc-1') < 0 || box.innerHTML.indexOf('首屏挂了') >= 0) throw new Error('重取第一页后应整块重画：' + box.innerHTML);
    },
  },
  {
    id: 'G5_addtrip_refetch_is_caught',
    desc: 'G-5：addTrip 内层 api.trips() 失败时——验证它被 return 链到外层 catch，不构成 unhandled rejection',
    async run(ctx) {
      const sb = ctx.sb;
      sb.cacheDom();
      const unhandled = [];
      const onUnhandled = (reason) => unhandled.push(String(reason && reason.message ? reason.message : reason));
      process.on('unhandledRejection', onUnhandled);
      try {
        const sec = buildTripsSection(sb);
        sec.querySelector('[data-role="place"]').value = '京都';
        sb.state.tripBusy = false;
        sb.addTrip(sec);
        if (ctx.h.countFetchCall('/api/paper/trips') !== 1) throw new Error('前置条件坏了：没发出 tripAdd 请求');
        ctx.h.respond('/api/paper/trips', { ok: true, trip: { trip_id: 't1', place: '京都' } });
        await ctx.tick();
        // 刷新列表的 GET 失败
        ctx.h.failFetch('/api/paper/trips', 503, '刷新挂了');
        await ctx.tick();
        await ctx.tick();

        if (unhandled.length) throw new Error('确实出现了 unhandled rejection：' + JSON.stringify(unhandled));
        const flash = sec.querySelector('[data-role="tripflash"]');
        const tb = sec.querySelector('tbody');
        const cellText = tb.childNodes.map((tr) => tr.childNodes.map((td) => td.textContent).join('')).join('');
        console.log('  观察：flash=「' + flash.textContent + '」 表格单元格=「' + cellText + '」');
        if (flash.textContent.indexOf('刷新挂了') < 0) {
          throw new Error('内层请求的失败没被任何 catch 处理（flash 仍是「' + flash.textContent + '」）→ Kimi 的「无 catch」成立');
        }
        if (flash.textContent.indexOf('没加上') !== 0) {
          throw new Error('外层 catch 文案与预期不同：' + flash.textContent);
        }
      } finally {
        process.removeListener('unhandledRejection', onUnhandled);
      }
    },
  },
  {
    id: 'G4_external_links_only_http_https',
    desc: 'G-4：危险协议当作没有链接，http(s) 照常',
    async run(ctx) {
      const sb = ctx.sb;
      for (const bad of ['javascript:alert(1)', ' JavaScript:alert(1)', 'javascript://evil.example.com/%0Aalert(1)', 'data:text/html,x', 'mailto:a@b.c']) {
        const html = sb.outLinkHTML({ url: bad });
        if (html !== '') throw new Error('危险协议没被拦：' + bad + ' → ' + html);
        if (sb.inboxOutHTML({ url: bad }) !== '') throw new Error('投递箱外链没被拦：' + bad);
      }
      const ok = sb.outLinkHTML({ url: 'https://example.com/a?b=1&c=2' });
      if (ok.indexOf('href="https://example.com/a?b=1&amp;c=2"') < 0) throw new Error('https 链接被误伤：' + ok);
      if (sb.outLinkHTML({ url: 'http://x.jp/' }).indexOf('href="http://x.jp/"') < 0) throw new Error('http 链接被误伤');
    },
  },
];

/* ---------------- 跑测 ---------------- */

(async function main() {
  const filter = process.argv[2];
  // helpers 里补一个按 url 计数的小工具
  const cases = CASES.filter((c) => !filter || c.id.startsWith(filter) || c.id.indexOf(filter) >= 0);
  if (!cases.length) { console.error('没有匹配的用例：' + filter); process.exit(1); }
  let pass = 0; let fail = 0;
  for (const c of cases) {
    try {
      const ctx = bootSandbox();
      ctx.h.countFetchCall = (urlLike) => ctx.h.fetchQueue.filter((f) => f.url.indexOf(urlLike) >= 0).length;
      await c.run(ctx);
      console.log('PASS ' + c.id);
      pass++;
    } catch (e) {
      console.log('FAIL ' + c.id + ' :: ' + (e && e.message ? e.message : e));
      if (e && e.stack) console.error(e.stack.split('\n').slice(0, 4).join('\n'));
      fail++;
    }
  }
  console.log(pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
