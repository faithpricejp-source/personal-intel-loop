#!/usr/bin/env node
/* 10-05 验收修复 · 前端测试（只用 Node 内置模块）。
 * 用 vm 在沙箱里执行整个 app.js（原生浏览器脚本，IIFE 包体），直接调用文件里的真实函数；
 * 假 DOM / 假 fetch 驱动渲染与路由。每条验收一个用例：成功 exit 0，失败 exit 1。
 * 用法：node project/tests/frontend/fix_1005.js [用例id前缀]
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.resolve(__dirname, '../../src/personal_intel_loop/paper_web/app.js');

/* ---------------- 假 DOM ---------------- */

function parseCompound(sel) {
  // 支持 tag / #id / .class / [attr] / [attr="v"] 的组合
  const m = /^([a-zA-Z][\w-]*)?((?:[#.][\w-]+|\[[^\]]+\])*)$/.exec(sel.trim());
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
      this.scrollHeight = 0;
      this.clientHeight = 0;
      this.offsetHeight = 100;
      this.scrollTop = 0;
      this.offsetTop = 0;
      const self = this;
      this.classList = {
        add(c) { const s = self._className.split(' ').filter(Boolean); if (s.indexOf(c) < 0) s.push(c); self._className = s.join(' '); },
        remove(c) { self._className = self._className.split(' ').filter((x) => x && x !== c).join(' '); },
        toggle(c, force) { const has = self._className.split(' ').indexOf(c) >= 0; const want = force === undefined ? !has : !!force; if (want) this.add(c); else this.remove(c); },
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
  const visibilityListeners = [];
  const document = {
    readyState: 'loading',
    visibilityState: 'visible',
    activeElement: null,
    listeners: {},
    createElement: (tag) => new FakeNode(tag),
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); if (type === 'visibilitychange') visibilityListeners.push(fn); },
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
  return { FakeNode, document, visibilityListeners };
}

/* ---------------- 沙箱装配 ---------------- */

function bootSandbox() {
  const { FakeNode, document, visibilityListeners } = makeFakeDom();
  const fetchQueue = [];
  const beaconCalls = [];
  const timers = [];

  class FakeIntersectionObserver {
    constructor(cb, opts) { this.cb = cb; this.opts = opts; this.observeCalls = []; this.unobserveCalls = []; }
    observe(el) { this.observeCalls.push(el); }
    unobserve(el) { this.unobserveCalls.push(el); }
    disconnect() {}
  }

  const sandbox = {
    console: { log() {}, debug() {}, warn() {}, error() {} },
    location: { search: '', hash: '#/', reload() {} },
    history: { scrollRestoration: 'auto', pushState() {}, replaceState() {} },
    navigator: { sendBeacon(url, blob) { beaconCalls.push({ url, blob }); return true; } },
    Blob: function (parts, opts) { this.parts = parts; this.type = (opts && opts.type) || ''; },
    document,
    IntersectionObserver: FakeIntersectionObserver,
    fetch: (url, opts) => new Promise((resolve, reject) => { fetchQueue.push({ url: String(url), opts, resolve, reject, done: false }); }),
    setTimeout(fn, ms) { timers.push(fn); return timers.length; },
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
    visibilityListeners,
    fetchQueue,
    beaconCalls,
    pendingUrls() { return fetchQueue.filter((f) => !f.done).map((f) => f.url); },
    countFetch(urlLike) { return fetchQueue.filter((f) => f.url.indexOf(urlLike) >= 0).length; },
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
  };
  const tick = () => new Promise((r) => setImmediate(r));
  return { sb: sandbox, h: helpers, tick };
}

/* ---------------- 公共数据 ---------------- */

function itemJSON(id, extra) {
  return Object.assign({
    item_id: id, title: '标题-' + id, url: 'https://example.com/' + id,
    source: 'rss_briefing:src1', source_label: '源一', edition_date: '2026-10-05',
    lede: '导语', fulltext: '正文正文。\n\n第二段。',
  }, extra || {});
}

function editionJSON(date, leadId) {
  const ed = { date, built_at: date + 'T07:12:00+09:00', stats: { pool: 10, picked: 1, ai_done: 1 },
    sections: { lead: [], top: [], briefs: [], blind: [] } };
  if (leadId) ed.sections.lead = [itemJSON(leadId)];
  return ed;
}

function enterItemPage(ctx, id) {
  ctx.sb.location.hash = '#/item/' + id;
  ctx.sb.showItemPage(id, 'paper');
}
function goHome(ctx) {  // 模拟 route() 回首页：先结算单篇停留，再回
  ctx.sb.leaveItemPage();
  ctx.sb.location.hash = '#/';
  ctx.sb.showHome();
}

function lastOf(trackLog, kind) {
  for (let i = trackLog.length - 1; i >= 0; i--) if (trackLog[i].kind === kind) return trackLog[i];
  return null;
}
function countOf(trackLog, kind) {
  return trackLog.filter((e) => e.kind === kind).length;
}

/* ---------------- 用例 ---------------- */

const CASES = [
  {
    id: 'H103_registerItem_keeps_note', desc: 'H103：registerItem 重建 my 保留 note；反馈条显示「✎ 已批注」、批注框有内容',
    async run(ctx) {
      const sb = ctx.sb;
      const item = itemJSON('x1');
      item.my = { overall: 1, note: '我上次写的批注' };
      sb.registerItem(item, '2026-10-05');
      const my = sb.itemIndex['x1'].my;
      if (my.note !== '我上次写的批注') throw new Error('registerItem 丢了 my.note：' + JSON.stringify(my));
      const html = sb.fbHTML(sb.itemIndex['x1'], false);
      if (html.indexOf('✎ 已批注') < 0) throw new Error('反馈条没显示「✎ 已批注」');
      const panel = sb.fbPanelHTML(sb.itemIndex['x1'], false);
      if (panel.indexOf('我上次写的批注') < 0) throw new Error('批注框为空（空框保存会删掉后端批注）');
    },
  },
  {
    id: 'H201_late_item_response_does_not_overwrite', desc: 'H201：开 A→回首页→开 B（B 先回，A 晚到），A 的回调不得覆盖 B',
    async run(ctx) {
      const sb = ctx.sb;
      sb.cacheDom();
      sb.dom.home = sb.document.createElement('div');
      sb.dom.home.className = 'home';
      sb.dom.view.appendChild(sb.dom.home);
      enterItemPage(ctx, 'A');
      goHome(ctx);
      enterItemPage(ctx, 'B');
      ctx.h.respond('/api/paper/item/B', itemJSON('B'));
      await ctx.tick();
      if (sb.dom.itemPage.getAttribute('data-item-id') !== 'B') throw new Error('前置条件坏了：当前页应已是 B');
      ctx.h.respond('/api/paper/item/A', itemJSON('A'));
      await ctx.tick();
      if (sb.dom.itemPage.getAttribute('data-item-id') !== 'B') throw new Error('A 的晚到响应把 data-item-id 覆盖回了 A');
      const html = sb.dom.itemPage.innerHTML;
      if (html.indexOf('标题-A') >= 0) throw new Error('A 的晚到响应覆盖了 B 的页面内容');
      if (html.indexOf('标题-B') < 0) throw new Error('B 的内容不见了');
    },
  },
  {
    id: 'H201_late_item_response_no_wrong_dwell', desc: 'H201：A 晚到覆盖后离开页面，item_dwell 必须记给 B 而不是 A',
    async run(ctx) {
      const sb = ctx.sb;
      sb.cacheDom();
      sb.dom.home = sb.document.createElement('div');
      sb.dom.view.appendChild(sb.dom.home);
      sb.trackInit();
      enterItemPage(ctx, 'A');
      goHome(ctx);
      enterItemPage(ctx, 'B');
      ctx.h.respond('/api/paper/item/B', itemJSON('B'));
      await ctx.tick();
      ctx.h.respond('/api/paper/item/A', itemJSON('A'));
      await ctx.tick();
      sb.location.hash = '#/';
      sb.trackItemLeave();
      const dwells = sb.trackLog.filter((e) => e.kind === 'item_dwell');
      const last = dwells[dwells.length - 1];
      if (!last) throw new Error('没有发出 item_dwell');
      if (last.item_id === 'A') throw new Error('B 的停留记到了 A 名下（晚到响应改了 data-item-id）');
      if (last.item_id !== 'B') throw new Error('最后一条 item_dwell 不是 B：' + last.item_id);
    },
  },
  {
    id: 'LB1_direct_jump_sets_id_before_response', desc: '漏报-1：A→B 直跳，进页即更新 data-item-id（不等响应）',
    async run(ctx) {
      const sb = ctx.sb;
      sb.cacheDom();
      sb.dom.home = sb.document.createElement('div');
      sb.dom.view.appendChild(sb.dom.home);
      enterItemPage(ctx, 'A');
      ctx.h.respond('/api/paper/item/A', itemJSON('A'));
      await ctx.tick();
      enterItemPage(ctx, 'B');  // B 请求在途、不回
      if (sb.dom.itemPage.getAttribute('data-item-id') !== 'B') throw new Error('直跳 B 后 data-item-id 仍滞留 A');
    },
  },
  {
    id: 'LB1_direct_jump_dwell_goes_to_new_item', desc: '漏报-1：直跳后 B 未加载完就离开，停留记给 B 而不是上一篇 A',
    async run(ctx) {
      const sb = ctx.sb;
      sb.cacheDom();
      sb.dom.home = sb.document.createElement('div');
      sb.dom.view.appendChild(sb.dom.home);
      sb.trackInit();
      enterItemPage(ctx, 'A');
      ctx.h.respond('/api/paper/item/A', itemJSON('A'));
      await ctx.tick();
      enterItemPage(ctx, 'B');
      sb.location.hash = '#/';
      sb.trackItemLeave();
      const dwells = sb.trackLog.filter((e) => e.kind === 'item_dwell');
      const last = dwells[dwells.length - 1];
      if (!last) throw new Error('没有发出 item_dwell');
      if (last.item_id !== 'B') throw new Error('直跳期间的停留记成了 ' + last.item_id + '，应为 B');
    },
  },
  {
    id: 'LB2_visible_resumes_dwell_session_impressions', desc: '漏报-2：页面 hidden 一次再 visible：单篇停留计时恢复、重发会话开始、曝光观察重挂',
    async run(ctx) {
      const sb = ctx.sb;
      sb.cacheDom();
      sb.trackInit();
      // 首页挂一条可曝光条目
      sb.location.hash = '#/';
      sb.dom.home = sb.document.createElement('div');
      const card = sb.document.createElement('article');
      card.setAttribute('data-item-id', 'i9');
      card.setAttribute('data-sec', 'top');
      sb.dom.home.appendChild(card);
      sb.dom.view.appendChild(sb.dom.home);
      sb.trackObserve(sb.dom.home);
      const io = sb.trackImpObserver;
      if (!io) throw new Error('前置条件坏了：trackObserve 没建 IntersectionObserver');
      io.cb([{ isIntersecting: true, intersectionRatio: 1, intersectionRect: { height: 400 }, target: card }], io);
      // 进单篇页 i1
      enterItemPage(ctx, 'i1');
      ctx.h.respond('/api/paper/item/i1', itemJSON('i1'));
      await ctx.tick();
      card.__imp.start = sb.trackFgNow() - 900;
      card.__imp.ok = true;
      // 隐藏一次
      const vis = sb.__docHandlersVis();
      sb.document.visibilityState = 'hidden';
      vis();
      const dwellsBefore = countOf(sb.trackLog, 'item_dwell');
      if (lastOf(sb.trackLog, 'session_end') == null) throw new Error('前置条件坏了：hidden 时应收会话尾');
      // 恢复可见
      sb.document.visibilityState = 'visible';
      vis();
      if (sb.state.itemDwellSent !== false) throw new Error('恢复可见后 itemDwellSent 没复位：同一篇后续停留不再记录');
      const ss = lastOf(sb.trackLog, 'session_start');
      const se = lastOf(sb.trackLog, 'session_end');
      if (!ss || sb.trackLog.indexOf(ss) < sb.trackLog.indexOf(se)) throw new Error('恢复可见后没有重新发 session_start');
      const obsAfter = io.observeCalls.filter((el) => el === card).length;
      if (obsAfter < 2) throw new Error('恢复可见后在视口条目的曝光观察没有重挂（observe 调用数 ' + obsAfter + '）');
      // 再离开单篇页：应补记第二条停留
      sb.location.hash = '#/';
      sb.trackItemLeave();
      const dwellsAfter = countOf(sb.trackLog, 'item_dwell');
      if (dwellsAfter <= dwellsBefore) throw new Error('恢复可见后同一篇的后续停留没有记录');
    },
  },
  {
    id: 'H106_loadFirstPage_reentry_guard', desc: 'H106=H202：loadFirstPage 在途时再调用不重复发请求（防 editions 重复/版面丢失）',
    async run(ctx) {
      const sb = ctx.sb;
      sb.cacheDom();
      sb.loadFirstPage();
      sb.loadFirstPage();
      if (ctx.h.countFetch('/api/paper/editions') !== 1) throw new Error('并发调用发了 ' + ctx.h.countFetch('/api/paper/editions') + ' 次 editions 请求（应为 1 次，需防重入）');
      ctx.h.respond('/api/paper/editions', { editions: [editionJSON('2026-10-05', 'L1'), editionJSON('2026-10-04', 'L2')], next_before: null });
      await ctx.tick();
      if (sb.state.editions.length !== 2) throw new Error('state.editions 长度 ' + sb.state.editions.length + '，应为 2');
    },
  },
  {
    id: 'H106_push_dedup_by_date', desc: 'H106=H202：回调 push 前按 date 去重',
    async run(ctx) {
      const sb = ctx.sb;
      sb.cacheDom();
      sb.loadFirstPage();
      ctx.h.respond('/api/paper/editions', { editions: [editionJSON('2026-10-05', 'L1'), editionJSON('2026-10-05', 'L1')], next_before: null });
      await ctx.tick();
      if (sb.dom.view.querySelectorAll('.err-page').length !== 0) throw new Error('渲染出错（非本用例断言）：' + sb.dom.view.querySelectorAll('.err-page').map((n) => n.innerHTML).join('|'));
      if (sb.state.editions.length !== 1) throw new Error('同日期两期没有去重，state.editions 长度 ' + sb.state.editions.length);
    },
  },
  {
    id: 'LB3_item_page_no_orig_link_without_url', desc: '漏报-3：条目无原文链接时，「阅读原文」不渲染成 href="#" 的链接',
    async run(ctx) {
      const sb = ctx.sb;
      sb.cacheDom();
      sb.dom.home = sb.document.createElement('div');
      sb.dom.view.appendChild(sb.dom.home);
      enterItemPage(ctx, 'nourl');
      ctx.h.respond('/api/paper/item/nourl', itemJSON('nourl', { url: null }));
      await ctx.tick();
      const html = sb.dom.itemPage.innerHTML;
      if (html.indexOf('btn orig') >= 0 && html.indexOf('href="#"') >= 0) throw new Error('无 url 仍渲染了 href="#" 的「阅读原文」（点了会打开报纸自身并记 open_original）');
      if (/<a[^>]*btn orig/.test(html)) throw new Error('无 url 仍渲染了 <a> 形态的「阅读原文」');
      // 对照：有 url 时链接照常
      enterItemPage(ctx, 'withurl');
      ctx.h.respond('/api/paper/item/withurl', itemJSON('withurl'));
      await ctx.tick();
      if (!/<a[^>]*btn orig[^>]*href="https:\/\/example\.com\/withurl"/.test(sb.dom.itemPage.innerHTML)) throw new Error('有 url 的条目「阅读原文」链接被误删');
    },
  },
  {
    id: 'LB3_follow_evi_no_link_without_url', desc: '漏报-3：follow-evi 证据没有 url 时不渲染链接',
    async run(ctx) {
      const sb = ctx.sb;
      const sg = { id: 'f1', platform: 'weibo', label: '某号', url: 'https://home.example', reason: '理由', status: 'new',
        evidence: [{ title: '没有链接的证据' }, { title: '有链接', url: 'https://evi.example' }] };
      const html = sb.followRowHTML(sg, false);
      if (html.indexOf('href="#"') >= 0) throw new Error('follow-evi 仍渲染 href="#"');
      const n = (html.match(/<a class="follow-evi"/g) || []).length;
      if (n !== 1) throw new Error('应只有 1 个证据链接，实际 ' + n);
      if (html.indexOf('没有链接的证据') < 0) throw new Error('无 url 的证据标题丢了');
    },
  },
  {
    id: 'LB4_retry_success_clears_err_page', desc: '漏报-4：「再试一次」成功后错误提示条移除',
    async run(ctx) {
      const sb = ctx.sb;
      sb.cacheDom();
      sb.loadFirstPage();
      ctx.h.failFetch('/api/paper/editions', 500, 'boom');
      await ctx.tick();
      const errBefore = sb.dom.view.querySelectorAll('.err-page').length;
      if (errBefore !== 1) throw new Error('前置条件坏了：失败后应有 1 条错误提示，实际 ' + errBefore);
      sb.loadFirstPage();  // 「再试一次」走的就是 loadFirstPage
      ctx.h.respond('/api/paper/editions', { editions: [editionJSON('2026-10-05', 'L1')], next_before: null });
      await ctx.tick();
      if (sb.dom.view.querySelectorAll('.err-page').length !== 0) throw new Error('重试成功后错误提示条没移除');
      if (sb.state.editions.length !== 1) throw new Error('重试成功但版面没铺出来');
    },
  },
  {
    id: 'beacon_json_blob', desc: '跨站防护（10-09）：收尾 beacon 的 Blob 必须是 application/json，text/plain 会被服务端 403 静默丢',
    async run(ctx) {
      const sb = ctx.sb;
      sb.trackQueue.push({ ts: '2026-10-09T00:00:00Z', kind: 'focus', ms: null });
      sb.trackBeacon('test');
      const calls = ctx.h.beaconCalls;
      if (calls.length !== 1) throw new Error('应发 1 次 beacon，实际 ' + calls.length);
      if (calls[0].blob.type !== 'application/json') throw new Error('beacon Blob 类型是 ' + calls[0].blob.type);
      if (sb.trackQueue.length !== 0) throw new Error('beacon 成功后队列没清空');
    },
  },
  {
    id: 'beacon_json_fetch_fallback', desc: '跨站防护（10-09）：sendBeacon 不收 JSON Blob（返回 false）时退到 fetch keepalive，带 JSON 头；失败重排队',
    async run(ctx) {
      const sb = ctx.sb;
      sb.navigator.sendBeacon = function () { return false; };
      sb.trackQueue.push({ ts: '2026-10-09T00:00:00Z', kind: 'focus', ms: null });
      sb.trackBeacon('test');
      const f = ctx.h.fetchQueue.filter((e) => e.url.indexOf('/api/paper/events') >= 0);
      if (f.length !== 1) throw new Error('应退到 1 次 fetch，实际 ' + f.length);
      const o = f[0].opts || {};
      if (o.method !== 'POST' || o.keepalive !== true) throw new Error('fetch 没带 POST + keepalive');
      if (!o.headers || o.headers['Content-Type'] !== 'application/json') throw new Error('fetch 没带 application/json 头');
      ctx.h.failFetch('/api/paper/events', 403, 'content-type must be application/json');
      await ctx.tick();
      if (sb.trackQueue.length !== 1) throw new Error('fetch 失败后事件没重排队，队列长 ' + sb.trackQueue.length);
    },
  },
];

/* 给 LB2 用例补一个取 visibilitychange 处理器的辅助（挂在沙箱全局，测试专用） */
function attachVisHelper(ctx) {
  const doc = ctx.sb.document;
  ctx.sb.__docHandlersVis = function () {
    const list = doc.listeners['visibilitychange'] || [];
    if (!list.length) throw new Error('app.js 没注册 visibilitychange 处理器');
    return () => list.forEach((fn) => fn({ type: 'visibilitychange' }));
  };
}

/* ---------------- 跑测 ---------------- */

(async function main() {
  const filter = process.argv[2];
  const cases = CASES.filter((c) => !filter || c.id.startsWith(filter) || c.id.indexOf(filter) >= 0);
  if (!cases.length) { console.error('没有匹配的用例：' + filter); process.exit(1); }
  let pass = 0; let fail = 0;
  for (const c of cases) {
    try {
      const ctx = bootSandbox();
      attachVisHelper(ctx);
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
