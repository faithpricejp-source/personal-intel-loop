/* ============================================================
 * 今日 · 个人 AI 报纸 —— 前端单页
 * 契约：paper_v2_contract.md（冻结 2026-10-04）
 * 纯原生 JS，无外部依赖。?mock=1 走 fixtures/。
 * ============================================================ */
'use strict';

(function () {

/* ---------------------------------------------------------
 * 0. 常量与小工具
 * ------------------------------------------------------- */

var MOCK = /(^|[?&])mock=1(&|$)/.test(location.search);
var FIXTURE_BASE = 'fixtures/';
var FIXTURE_FALLBACK = '../fixtures/';   // out/ 下自成一体时回退到仓库根的 fixtures/

var ISSUE_EPOCH = Date.UTC(2026, 9, 4); // 「第 N 期」按日期算，2026-10-04 创刊为第 1 期

var DIMS = [
  { dim: 'overall', name: '喜不喜欢', pos: '👍', neg: '👎' },
  { dim: 'quality', name: '写得好吗', pos: '写得好', neg: '写得差' },
  { dim: 'author', name: '作者', pos: '有见识', neg: '没见识' },
  { dim: 'style',   name: '文风',     pos: '喜欢',  neg: '不喜欢' },
  { dim: 'topic',   name: '这类',     pos: '多来点', neg: '少来点' }
];

var REASONS = [
  { code: 'already_known',  label: '早知道' },
  { code: 'unclear',       label: '没看懂' },
  { code: 'not_interested', label: '不感兴趣' },
  { code: 'keep',           label: '留' },
  { code: 'deep_discuss',   label: '深挖' }
];

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// 外链只放行 http/https：AI 产出与投递箱的 url 不经协议校验，其它协议一律当作没有链接
function extUrl(u) {
  var s = String(u === null || u === undefined ? '' : u).trim();
  return /^https?:\/\//i.test(s) ? s : '';
}

function $(sel, root) { return (root || document).querySelector(sel); }
function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

function fmtTrust(v) {
  if (v === null || v === undefined || v === '') return '—';
  return Number(v).toFixed(2);
}

function fmtTime(iso) {
  if (!iso) return '';
  // 带 Z 或时区偏移的时间先换成东京时间再显示（10-04 首期：UTC 的出版时间被当成东京时间）
  if (/[zZ]$|[+-]\d{2}:?\d{2}$/.test(String(iso))) {
    var d = new Date(iso);
    if (!isNaN(d)) {
      var parts = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(d);
      return parts.replace('T', ' ');
    }
  }
  var m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (m) return m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5];
  return String(iso);
}

function fmtDateCN(date) {
  if (!date) return '';
  var m = String(date).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return String(date);
  return Number(m[1]) + ' 年 ' + Number(m[2]) + ' 月 ' + Number(m[3]) + ' 日';
}

function issueNo(date) {
  var m = String(date || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return 1;
  var t = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Math.floor((t - ISSUE_EPOCH) / 86400000) + 1;
}

function debounce(fn, ms) {
  var t = null;
  return function () {
    var self = this, a = arguments;
    if (t) clearTimeout(t);
    t = setTimeout(function () { fn.apply(self, a); }, ms);
  };
}

/* ---------------------------------------------------------
 * 1. 数据层：api
 * ------------------------------------------------------- */

function request(url, opts) {
  return fetch(url, opts).then(function (res) {
    return res.text().then(function (txt) {
      var data = null;
      if (txt) { try { data = JSON.parse(txt); } catch (e) { data = null; } }
      if (!res.ok) {
        var msg = (data && data.error) ? data.error : ('HTTP ' + res.status);
        var err = new Error(msg);
        err.status = res.status;
        throw err;
      }
      if (data === null) throw new Error('返回不是 JSON');
      return data;
    });
  });
}

function getJSON(url) { return request(url, { method: 'GET', headers: { 'Accept': 'application/json' } }); }

/** mock 模式的 fixture 读取：先试页面同目录 fixtures/，再试上一级 fixtures/ */
function getFixture(name) {
  return getJSON(FIXTURE_BASE + name).catch(function (err) {
    return getJSON(FIXTURE_FALLBACK + name).catch(function () { throw err; });
  });
}

function postJSON(url, body) {
  return request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
    body: JSON.stringify(body)
  });
}

var rateExamples = null;
function loadRateExamples() {
  if (rateExamples) return Promise.resolve(rateExamples);
  return getFixture('rate_examples.json').then(function (d) {
    rateExamples = Array.isArray(d) ? d : [d];
    return rateExamples;
  });
}

function mockRateFor(dim, item_id, value) {
  // overall/quality → 第 1 条；author → 第 2 条；style/topic → 第 3 条
  var idx = (dim === 'overall' || dim === 'quality') ? 0 : (dim === 'author' ? 1 : 2);
  return loadRateExamples().then(function (list) {
    var ex = list[idx] || list[list.length - 1] || {};
    var eff = ex.effect || {};
    return {
      ok: true,
      item_id: item_id,
      dim: dim,
      value: value,
      effect: {
        kind: eff.kind || 'queued',
        label: eff.label || '已记',
        before: eff.before === undefined ? null : eff.before,
        after: eff.after === undefined ? null : eff.after
      }
    };
  });
}

var api = {
  mock: MOCK,

  editions: function (before, limit) {
    if (MOCK) {
      // 第一页读 editions_page1.json，before 之后（before=2026-10-04）读 editions_page2.json
      return getFixture(before ? 'editions_page2.json' : 'editions_page1.json');
    }
    var url = '/api/paper/editions?limit=' + encodeURIComponent(limit || 1);
    if (before) url += '&before=' + encodeURIComponent(before);
    return getJSON(url);
  },

  item: function (itemId) {
    // mock：digest: 前缀返回综述详情，podcast:/youtube: 开头返回音视频详情，其它 id 返回普通详情
    if (MOCK) {
      var id = String(itemId);
      if (id.indexOf('digest:') === 0) return getFixture('item_digest.json');
      if (id.indexOf('podcast:') === 0) return getFixture('item_media_audio.json');
      if (id.indexOf('youtube:') === 0) return getFixture('item_media_video.json');
      return getFixture('item_detail.json');
    }
    return getJSON('/api/paper/item/' + encodeURIComponent(itemId));
  },

  rate: function (itemId, dim, value) {
    if (MOCK) return mockRateFor(dim, itemId, value);
    return postJSON('/api/paper/rate', { item_id: itemId, dim: dim, value: value });
  },

  note: function (itemId, text) {
    if (MOCK) return Promise.resolve({ ok: true, item_id: itemId, note: text || null, effect: { kind: 'queued', label: text ? '批注已存，明早编辑部复盘时会读' : '批注已删除' } });
    return postJSON('/api/paper/note', { item_id: itemId, text: text });
  },

  reason: function (itemId, action, digestDate) {
    if (MOCK) return Promise.resolve({ recorded: true, trust_before: 0.31, trust_after: 0.40 });
    return postJSON('/api/feedback', { item_id: itemId, action: action, digest_date: digestDate });
  },

  proposal: function (line, decision) {
    if (MOCK) return Promise.resolve({ ok: true, pending_count: 1 });
    return postJSON('/api/proposal', { line: line, decision: decision });
  },

  pipeline: function () {
    if (MOCK) return getFixture('pipeline.json');
    return getJSON('/api/paper/pipeline');
  },

  pipelinePost: function (kind, key, action) {
    if (MOCK) return Promise.resolve({ ok: true, kind: kind, key: key, action: action, before: 0.4, after: 0.5, muted: false });
    return postJSON('/api/paper/pipeline', { kind: kind, key: key, action: action });
  },

  adjustments: function (limit) {
    if (MOCK) return getFixture('adjustments.json');
    return getJSON('/api/paper/adjustments?limit=' + encodeURIComponent(limit || 50));
  },

  revertAdjustment: function (id) {
    if (MOCK) return Promise.resolve({ ok: true, id: id, key: null, before: 0.40, after: 0.35 });
    return postJSON('/api/paper/adjustments/revert', { id: id });
  },

  events: function (sessionId, events) {
    if (MOCK) return Promise.resolve({ ok: true, stored: (events || []).length, mock: true });
    return postJSON('/api/paper/events', { session_id: sessionId, events: events });
  },

  /* 契约 v3 第 5 节：点开即标已读 */
  inboxRead: function (inboxId) {
    if (MOCK) return Promise.resolve({ ok: true });
    return postJSON('/api/paper/inbox/read', { inbox_id: inboxId });
  },

  /* ---- 契约 v4 第 6 节：全平台接入 ---- */

  /** 登录态健康：{channels:[{key,label,ok,checked_at,detail,since_failing,relogin_hint}]} */
  auth: function () {
    if (MOCK) return getFixture('auth.json');
    return getJSON('/api/paper/auth');
  },

  /** 重新登录：kind=command 返回 {ok,started}；kind=url 返回 {ok,open_url} */
  authRelogin: function (key) {
    if (MOCK) {
      // mock：weibo 走 url（前端开新窗口），其余走 command（后台起脚本）
      if (key === 'weibo') return Promise.resolve({ ok: true, open_url: 'https://weibo.com/login.php' });
      return Promise.resolve({ ok: true, started: true });
    }
    return postJSON('/api/paper/auth/relogin', { key: key });
  },

  /** 全部来源条目：{source,date,q,before,limit} -> {items:[ArchiveItem],next_before} */
  archive: function (p) {
    p = p || {};
    if (MOCK) {
      // mock：before 有值读第二页，否则第一页；source/date/q 在 mock 里做本地过滤
      return getFixture(p.before ? 'archive_page2.json' : 'archive_page1.json').then(function (d) {
        var list = (d.items || []).slice();
        if (p.source) list = list.filter(function (it) { return it.source === p.source; });
        if (p.date) list = list.filter(function (it) { return String(it.published_at || '').slice(0, 10) === p.date; });
        if (p.q) {
          var q = String(p.q).toLowerCase();
          list = list.filter(function (it) {
            return (String(it.title || '') + ' ' + String(it.one_liner || '')).toLowerCase().indexOf(q) >= 0;
          });
        }
        return { items: list, next_before: d.next_before || null, mock_paged: !!p.before };
      });
    }
    var url = '/api/paper/archive?limit=' + encodeURIComponent(p.limit || 50);
    if (p.source) url += '&source=' + encodeURIComponent(p.source);
    if (p.date) url += '&date=' + encodeURIComponent(p.date);
    if (p.q) url += '&q=' + encodeURIComponent(p.q);
    if (p.before) url += '&before=' + encodeURIComponent(p.before);
    return getJSON(url);
  },

  /** 全部来源的来源列表：近 7 天条数 + 最近一条时间 */
  archiveSources: function () {
    if (MOCK) return getFixture('archive_sources.json');
    return getJSON('/api/paper/archive/sources');
  },

  /** 推荐关注：{suggestions:[{id,platform,account_id,label,url,reason,evidence,status}]} */
  followSuggestions: function () {
    if (MOCK) return getFixture('follow_suggestions.json');
    return getJSON('/api/paper/follow_suggestions');
  },

  followDecide: function (id, decision) {
    if (MOCK) return Promise.resolve({ ok: true, id: id, status: decision === 'dismiss' ? 'dismissed' : 'followed' });
    return postJSON('/api/paper/follow_suggestions/decide', { id: id, decision: decision });
  },

  /* ---- 契约 v4 第 9 节：行程 ---- */

  trips: function () {
    if (MOCK) return getFixture('trips.json');
    return getJSON('/api/paper/trips');
  },

  tripAdd: function (place, startDate, endDate, note) {
    if (MOCK) return Promise.resolve({ ok: true, trip: { trip_id: 'trip_' + Date.now(), place: place, start_date: startDate, end_date: endDate, note: note || '' } });
    return postJSON('/api/paper/trips', { place: place, start_date: startDate, end_date: endDate, note: note || '' });
  },

  tripDelete: function (tripId) {
    if (MOCK) return Promise.resolve({ ok: true, trip_id: tripId });
    return postJSON('/api/paper/trips/delete', { trip_id: tripId });
  },

  /* ---- 契约 v4 第 10 节：到期结算 ---- */

  claimResolve: function (claimId, outcome) {
    if (MOCK) return Promise.resolve({ ok: true, claim_id: claimId, outcome: outcome });
    return postJSON('/api/claims/resolve', { claim_id: claimId, outcome: outcome });
  }
};

/* ---------------------------------------------------------
 * 2. 全局状态
 * ------------------------------------------------------- */

var state = {
  editions: [],       // 已加载的期（按日期倒序）
  nextBefore: undefined,
  loadingMore: false,
  firstLoading: false,  // 首页铺版在途标志（10-05 验收 H106=H202：防并发重铺）
  exhausted: false,   // next_before === null
  started: false,
  scrollMemo: 0,      // 进单篇页前的滚动位置
  pendingRestore: false,
  pipeline: null,
  itemEnterFg: 0,      // 进单篇页时的前台时钟读数
  itemSeq: 0,         // 单篇页请求序号：晚到的旧响应核对不上就丢弃（10-05 验收 H201）
  itemDwellSent: false,
  itemMaxScroll: 0,   // 单篇页滚到过的最大深度（%）
  itemFulltextChars: 0,
  kbdIndex: -1,
  pendingOpenFrom: '',// 'paper' | 'keyboard'，open_item 的 meta.from
  pendingOpenSection: '', // 'archive'：从全部来源页点进来，open_item 的 meta.section
  flashAcked: {},     // inbox_id -> true：点过「知道了」的快讯，本页不再显示
  inboxHitTimer: null,// #/inbox/<id> 高亮 2 秒的定时器
  pendingInbox: null, // 铺版完成后要定位的 inbox_id
  inboxMissTried: false,
  authRelogging: {},  // channel key -> 'started'：已经点过「重新登录」等扫码
  archive: null,      // 全部来源页的当前查询与已加载的条目
  followDecided: {},  // suggestion id -> 'dismissed' | 'followed'
  tripBusy: false
};

var itemIndex = {};   // item_id -> item（含 my 状态）
var itemDigest = {};  // item_id -> 该期 date

var dom = {};

function cacheDom() {
  dom.masthead = $('#masthead');
  dom.mastEdition = $('#mastEdition');
  dom.mastSub = $('#mastSub');
  dom.flash = $('#flash');
  dom.authbar = $('#authbar');
  dom.riskbar = $('#riskbar');
  dom.view = $('#view');
  dom.moreWrap = $('#moreWrap');
  dom.moreNote = $('#moreNote');
  dom.moreBtn = $('#moreBtn');
  dom.home = null;
  dom.itemPage = null;
  dom.pipePage = null;
  dom.adjBox = null;
  dom.archivePage = null;
  dom.authPage = null;
}

/* ---------------------------------------------------------
 * 3. 反馈条（所有分区的每一条都有）
 * ------------------------------------------------------- */

function fbEffectHTML() { return '<div class="fb-effect" data-role="effect"></div>'; }

function fbPanelHTML(item, open) {
  var my = item.my || {};
  var rows = DIMS.map(function (d) {
    var cur = my[d.dim] || 0;
    var side = '';
    if (d.dim === 'author') {
      side = '<span class="d-side">' + esc(item.author_label || '—') +
        (item.author_is_byline === false ? '<span class="byline-only">（仅来源名）</span>' : '') + '</span>';
    } else if (d.dim === 'style') {
      var tags = item.style_tags || [];
      side = '<span class="d-side">' + (tags.length ? esc(tags.join(' · ')) : '—') + '</span>';
    } else if (d.dim === 'topic') {
      side = '<span class="d-side">' + esc(item.topic || '—') + '</span>';
    }
    return '<div class="fb-dim">' +
      '<span class="d-name">' + d.name + '</span>' + side +
      '<span class="d-btns">' +
        '<button type="button" class="btn mini' + (cur === 1 ? ' on' : '') + '" data-act="rate" data-dim="' + d.dim + '" data-val="1" aria-pressed="' + (cur === 1) + '">' + d.pos + '</button>' +
        '<button type="button" class="btn mini' + (cur === -1 ? ' on' : '') + '" data-act="rate" data-dim="' + d.dim + '" data-val="-1" aria-pressed="' + (cur === -1) + '">' + d.neg + '</button>' +
      '</span></div>';
  }).join('');

  var rc = REASONS.map(function (r) {
    var on = my.reason_code === r.code;
    return '<button type="button" class="btn mini' + (on ? ' on' : '') + '" data-act="reason" data-code="' + r.code + '" aria-pressed="' + on + '">' + r.label + '</button>';
  }).join('');

  return '<div class="fb-panel' + (open ? ' open' : '') + '" data-role="panel">' + rows +
    '<div class="fb-sep"></div>' +
    '<div class="fb-dim"><span class="d-name">原因</span>' +
    '<span class="d-btns fb-why" data-role="reasons">' + rc + '</span></div>' +
    '<div class="fb-sep"></div>' +
    '<div class="fb-note"><span class="d-name">批注</span>' +
      '<div class="fb-note-box"><textarea data-role="note" rows="3" placeholder="写下你对这篇的看法：哪里好、哪里不对、想多看什么……（⌘↩ 保存）">' + esc(my.note || '') + '</textarea>' +
      '<div class="fb-note-actions"><button type="button" class="btn mini" data-act="note">保存批注</button><span class="fb-note-hint">明早出版前，AI 复盘会优先读你的批注</span></div></div>' +
    '</div>' +
    '</div>';
}

/** 紧凑反馈条：👍 👎 + 更多 */
function fbHTML(item, open) {
  var my = item.my || {};
  return '<div class="fb" data-item="' + esc(item.item_id) + '">' +
    '<div class="fb-row">' +
      '<button type="button" class="btn icon' + (my.overall === 1 ? ' on' : '') + '" data-act="rate" data-dim="overall" data-val="1" aria-pressed="' + (my.overall === 1) + '" title="喜不喜欢：👍">👍</button>' +
      '<button type="button" class="btn icon' + (my.overall === -1 ? ' on' : '') + '" data-act="rate" data-dim="overall" data-val="-1" aria-pressed="' + (my.overall === -1) + '" title="喜不喜欢：👎">👎</button>' +
      '<button type="button" class="btn mini fb-more" data-act="more">' + (open ? '收起' : '更多') + '</button>' +
      (my.note ? '<span class="note-mark" title="' + esc(my.note) + '">✎ 已批注</span>' : '') +
    '</div>' +
    fbPanelHTML(item, !!open) +
    fbEffectHTML() +
  '</div>';
}

function fbSync(fbEl, item, open) {
  var my = item.my || {};
  $$('[data-act="rate"]', fbEl).forEach(function (b) {
    var d = b.getAttribute('data-dim');
    var v = Number(b.getAttribute('data-val'));
    var on = (my[d] || 0) === v;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  $$('[data-act="reason"]', fbEl).forEach(function (b) {
    var on = my.reason_code === b.getAttribute('data-code');
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  var more = $('[data-act="more"]', fbEl);
  if (more) more.textContent = open ? '收起' : '更多';
}

function fbSetBusy(fbEl, busy) {
  $$('button', fbEl).forEach(function (b) { b.disabled = !!busy; });
}

function fbShowEffect(fbEl, text, isErr) {
  var box = $('[data-role="effect"]', fbEl);
  if (!box) return;
  box.textContent = text || '';
  box.classList.remove('fade');
  box.style.color = isErr ? 'var(--danger)' : '';
  if (!text) return;
  setTimeout(function () { box.classList.add('fade'); }, 3000);
}

function fbShowError(fbEl, msg) {
  var old = $('[data-role="err"]', fbEl);
  if (old) old.remove();
  var e = document.createElement('span');
  e.className = 'err';
  e.setAttribute('data-role', 'err');
  e.textContent = '写不进去：' + msg;
  fbEl.appendChild(e);
  setTimeout(function () { if (e.parentNode) e.remove(); }, 6000);
}

function digestOf(itemId) {
  return itemDigest[itemId] || (itemIndex[itemId] && itemIndex[itemId].edition_date) || null;
}

/** 三态按钮：未选 → 选中 → 再点撤销（value=0） */
function doNote(itemId, fbEl) {
  var item = itemIndex[itemId];
  var ta = $('[data-role="note"]', fbEl);
  if (!item || !ta || fbEl.getAttribute('data-busy') === '1') return;
  var text = ta.value.trim();
  fbEl.setAttribute('data-busy', '1');
  fbSetBusy(fbEl, true);
  api.note(itemId, text).then(function (res) {
    (item.my || (item.my = {})).note = text || null;
    fbEl.removeAttribute('data-busy');
    fbSetBusy(fbEl, false);
    var mark = $('.note-mark', fbEl);
    if (text && !mark) $('[data-act="more"]', fbEl).insertAdjacentHTML('afterend', '<span class="note-mark">✎ 已批注</span>');
    if (!text && mark) mark.remove();
    fbShowEffect(fbEl, (res && res.effect && res.effect.label) || '已存', false);
  }).catch(function (err) {
    fbEl.removeAttribute('data-busy');
    fbSetBusy(fbEl, false);
    fbShowError(fbEl, err.message);
  });
}

document.addEventListener('keydown', function (ev) {
  var ta = ev.target;
  if (!ta || ta.getAttribute('data-role') !== 'note') return;
  if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) {
    ev.preventDefault();
    var fbEl = ta.closest('.fb');
    if (fbEl) doNote(fbEl.getAttribute('data-item'), fbEl);
  }
});

function doRate(itemId, dim, val, fbEl) {
  var item = itemIndex[itemId];
  if (!item) return;
  if (fbEl.getAttribute('data-busy') === '1') return;
  var my = item.my || (item.my = {});
  var cur = my[dim] || 0;
  var next = (cur === val) ? 0 : val;
  fbEl.setAttribute('data-busy', '1');
  fbSetBusy(fbEl, true);
  api.rate(itemId, dim, next).then(function (res) {
    my[dim] = next;
    fbEl.removeAttribute('data-busy');
    fbSetBusy(fbEl, false);
    fbSync(fbEl, item, fbEl.querySelector('[data-role="panel"]').classList.contains('open'));
    var eff = res && res.effect;
    fbShowEffect(fbEl, eff && eff.label ? eff.label : '已记', false);
  }).catch(function (err) {
    fbEl.removeAttribute('data-busy');
    fbSetBusy(fbEl, false);
    fbSync(fbEl, item, fbEl.querySelector('[data-role="panel"]').classList.contains('open'));
    fbShowEffect(fbEl, '');
    fbShowError(fbEl, err.message);
  });
}

function doReason(itemId, code, fbEl) {
  var item = itemIndex[itemId];
  if (!item) return;
  if (fbEl.getAttribute('data-busy') === '1') return;
  var my = item.my || (item.my = {});
  if (my.reason_code === code) return; // 单选互斥：已选中的再点不重复提交
  var d = digestOf(itemId);
  if (!d) { fbShowError(fbEl, '这一条没有期号，不知道往哪写'); return; }
  fbEl.setAttribute('data-busy', '1');
  fbSetBusy(fbEl, true);
  api.reason(itemId, code, d).then(function (res) {
    my.reason_code = code;
    fbEl.removeAttribute('data-busy');
    fbSetBusy(fbEl, false);
    fbSync(fbEl, item, fbEl.querySelector('[data-role="panel"]').classList.contains('open'));
    var b = res && res.trust_before, a = res && res.trust_after;
    fbShowEffect(fbEl, (b !== undefined && a !== undefined) ? ('此源信任 ' + fmtTrust(b) + '→' + fmtTrust(a)) : '已记', false);
  }).catch(function (err) {
    fbEl.removeAttribute('data-busy');
    fbSetBusy(fbEl, false);
    fbShowError(fbEl, err.message);
  });
}

/* 全局委托：点标题 / ↗ / 阅读原文 / 反馈条 */
document.addEventListener('click', function (ev) {
  var t = ev.target;
  if (!t.closest) return;

  // 点 ↗ 或「阅读原文」开原文（linkout 也在 .item-title 里，所以要先判）
  var outA = t.closest('a.linkout, a.orig');
  if (outA) {
    // 「去关注 ↗」走自己的事件（open_original / meta.from=follow_suggestion）
    if (outA.getAttribute('data-act') === 'follow-go') {
      trackFollowGo(outA.getAttribute('data-fs'));
      return;
    }
    var hostOut = outA.closest('[data-item-id]');
    trackOpenOriginal(parseHash().name === 'item' ? 'item' : 'paper',
      hostOut ? hostOut.getAttribute('data-item-id') : null);
    return;
  }

  // 全部来源页的条目标题：进单篇页，open_item 的 meta.section=archive
  var arcA = t.closest('.arc-title');
  if (arcA) {
    state.pendingOpenSection = 'archive';
    return;   // hash 交给 hashchange → route
  }

  // 点标题进单篇页（单篇页里的标题不再重复记）。
  // 这里只记下 from，open_item 事件统一由 showItemPage 发，避免点一次记两条。
  var titleA = t.closest('.item-title a');
  if (titleA && parseHash().name === 'home') {
    state.pendingOpenFrom = 'paper';
    return;   // hash 交给 hashchange → route
  }

  var btn = t.closest('[data-act]');
  if (!btn) return;
  var act = btn.getAttribute('data-act');
  if (act !== 'rate' && act !== 'reason' && act !== 'more' && act !== 'note') return;
  var fbEl = btn.closest('.fb');
  if (!fbEl) return;
  var itemId = fbEl.getAttribute('data-item');
  ev.preventDefault();

  if (act === 'more') {
    var panel = $('[data-role="panel"]', fbEl);
    var open = !panel.classList.contains('open');
    panel.classList.toggle('open', open);
    btn.textContent = open ? '收起' : '更多';
    var host = fbEl.closest('.item');
    if (host) host.classList.toggle('pinned', open);  // 展开的反馈面板不随鼠标移开而消失
    if (open) trackExpandFeedback(itemId);
    return;
  }
  if (act === 'note') { doNote(itemId, fbEl); return; }
  if (act === 'rate') doRate(itemId, btn.getAttribute('data-dim'), Number(btn.getAttribute('data-val')), fbEl);
  if (act === 'reason') doReason(itemId, btn.getAttribute('data-code'), fbEl);
});

/* ---------------------------------------------------------
 * 4. 条目渲染
 * ------------------------------------------------------- */

function whyHTML(item) {
  var wh = item.why_here || {};
  if (!wh.profile_hit) return '';
  return '<p class="why">为什么在这：' + esc(wh.profile_hit) + '</p>';
}

function blindReasonHTML(item) {
  var wh = item.why_here || {};
  if (!wh.blind_reason) return '';
  return '<p class="blind-reason">' + esc(wh.blind_reason) + '</p>';
}

function bylineHTML(item) {
  var a = esc(item.author_label || '—');
  if (item.author_is_byline === false) a += '<span class="byline-only">（仅来源名）</span>';
  return a;
}

function metaHTML(item) {
  return '<p class="meta">' + esc(item.source_label || item.source || '—') +
    '<span class="dot">·</span>' + bylineHTML(item) +
    '<span class="dot">·</span>' + esc(fmtTime(item.published_at)) + '</p>';
}

function outLinkHTML(item) {
  if (!extUrl(item.url)) return '';
  return '<a class="linkout" href="' + esc(extUrl(item.url)) + '" target="_blank" rel="noopener noreferrer" title="新标签打开原文">↗</a>';
}

/* ---------------------------------------------------------
 * 4a. 条目的小标签：kind（综述 / 音视频）、新知判定、核实级别
 *     契约 v4 第 6.4 / 7.1 / 8 节
 * ------------------------------------------------------- */

var NOVELTY_LABEL = {
  new_fact: '新知',
  new_mechanism: '新知',
  counter: '反方',
  known: '已知',
  confirming: '印证'
};

function fmtMinutes(sec) {
  var s = Number(sec);
  if (!isFinite(s) || s <= 0) return '';
  return Math.round(s / 60) + ' 分钟';
}

/**
 * 音视频 / 综述的条目前置小标签。
 * digest → 「讨论综述 · N 条」（N = members 数）；media → 「音频 · 52 分钟」/「视频 · 18 分钟」
 */
function kindTagHTML(item) {
  var kind = item.kind;
  if (kind === 'digest') {
    var n = (item.members && item.members.length) ? item.members.length : ((item.quotes || []).length);
    return '<span class="kindtag digest">讨论综述 · ' + n + ' 条</span>';
  }
  if (kind === 'media') {
    var m = item.media || {};
    var word = m.type === 'video' ? '视频' : '音频';
    var mins = fmtMinutes(m.duration_s);
    return '<span class="kindtag media">' + word + (mins ? ' · ' + mins : '') + '</span>';
  }
  return '';
}

/**
 * 新知判定小标签（契约 v4 第 7.1 节）。
 * new_fact / new_mechanism → 「新知」强调色描边；counter → 「反方」强调色实底白字；
 * known / confirming → 灰色小字。鼠标悬停显示 why；counter 另显示「这条在挑战你的：…」。
 */
function noveltyHTML(item, withWhy) {
  var nv = item.novelty;
  if (!nv || !nv.kind) return '';
  var label = NOVELTY_LABEL[nv.kind];
  if (!label) return '';
  var tip = [];
  if (nv.why) tip.push(nv.why);
  if (nv.kind === 'counter' && nv.against) tip.push('这条在挑战你的：' + nv.against);
  var tipAttr = tip.length ? ' title="' + esc(tip.join('　·　')) + '"' : '';
  var h = '<span class="novtag ' + esc(nv.kind) + '"' + tipAttr + '>' + esc(label) + '</span>';
  if (nv.kind === 'counter' && nv.against) {
    h += '<span class="against">这条在挑战你的：' + esc(nv.against) + '</span>';
  }
  if (withWhy && nv.why) h += '<p class="why novwhy">为什么算新：' + esc(nv.why) + '</p>';
  return h;
}

/** 温暖栏的核实标签（第 8 节）：primary → 强调色描边，secondary / unverified → 灰 */
var VERIFY_LABEL = { primary: '已核实 · 一手', secondary: '二手', unverified: '未核实' };

function verificationHTML(item) {
  var v = item.verification;
  if (!v || !v.level) return '';
  var label = VERIFY_LABEL[v.level] || v.level;
  var tip = [];
  if (v.note) tip.push(v.note);
  if (v.named && v.named.length) tip.push('具名：' + v.named.join('、'));
  var tipAttr = tip.length ? ' title="' + esc(tip.join('　·　')) + '"' : '';
  return '<span class="verifytag ' + esc(v.level) + '"' + tipAttr + '>' + esc(label) + '</span>';
}

/** 综述的引文平铺：每条引文 + 「— 作者」+ 原帖链接 ↗ */
function quotesHTML(item) {
  var qs = item.quotes || [];
  if (!qs.length) return '';
  return '<div class="quotes">' + qs.map(function (q) {
    return '<blockquote class="quote">' +
      '<span class="q-text">' + esc(q.text || '') + '</span>' +
      '<span class="q-by">— ' + esc(q.author_label || '匿名') +
      (extUrl(q.url) ? ' <a class="linkout" href="' + esc(extUrl(q.url)) + '" target="_blank" rel="noopener noreferrer">原帖 ↗</a>' : '') +
      '</span></blockquote>';
  }).join('') + '</div>';
}

function titleHTML(item, cls) {
  var href = '#/item/' + encodeURIComponent(item.item_id);
  // 微博/知乎等社交条目的「标题」是正文前 60 字，读起来像半截话 → 有 AI 一句话概括就用它当标题（10-04 头版实测）
  var social = /^(weibo|weibo_home|weibo_timeline|zhihu_moments):/.test(String(item.source || ''));
  var shown = social && item.one_liner ? item.one_liner : item.title;
  return '<h3 class="item-title ' + (cls || '') + '">' + kindTagHTML(item) +
    '<a href="' + href + '">' + esc(shown) + '</a> ' + outLinkHTML(item) + '</h3>' +
    noveltyHTML(item, false);
}

function claimHTML(item) {
  if (!item.claim || !item.claim.text) return '';
  return '<p class="claim"><span class="claim-k">断言：</span>' + esc(item.claim.text) +
    (item.claim.check_after ? '<span class="claim-when">到期核对：' + esc(item.claim.check_after) + '</span>' : '') +
    '</p>';
}

function registerItem(item, date) {
  var prev = itemIndex[item.item_id];
  var my = (prev && prev.my) || item.my || {};
  item.my = {
    overall: my.overall || 0,
    quality: my.quality || 0,
    author: my.author || 0,
    style: my.style || 0,
    topic: my.topic || 0,
    reason_code: my.reason_code || null,
    note: my.note || null // 10-05 验收 H103：漏了 note 会让批注看不见、空框保存还会删掉后端批注
  };
  itemIndex[item.item_id] = item;
  if (date) itemDigest[item.item_id] = date;
}

function imgHTML(item, cls) {
  if (!item.image_url) return '';
  return '<img class="thumb ' + (cls || '') + '" src="' + esc(item.image_url) + '" alt="" loading="' + (cls === 'tall' ? 'eager' : 'lazy') + '" referrerpolicy="no-referrer">';
}

/** 条目 DOM 上带 section / rank，供行为监测用 */
function itemAttrs(item, sec, rank) {
  return ' data-item-id="' + esc(item.item_id) + '" data-sec="' + esc(sec) + '" data-rank="' + (Number(rank) || 0) + '" tabindex="-1"';
}

/** 头条 */
function leadHTML(item, date, rank) {
  registerItem(item, date);
  return '<article class="item lead"' + itemAttrs(item, 'lead', rank) + '>' +
    titleHTML(item) +
    imgHTML(item, 'tall') +
    (item.lede ? '<p class="lede">' + esc(item.lede) + '</p>' : '') +
    quotesHTML(item) +
    (item.backstory ? '<p class="backstory">' + esc(item.backstory) + '</p>' : '') +
    (item.so_what ? '<p class="sowhat">' + esc(item.so_what) + '</p>' : '') +
    claimHTML(item) +
    metaHTML(item) + whyHTML(item) + verificationHTML(item) +
    fbHTML(item, false) +
  '</article>';
}

/** 竖栏 / 三栏条目 */
function cardHTML(item, date, withThumb, sec, rank) {
  registerItem(item, date);
  var head = withThumb && item.image_url
    ? '<div class="col-figure">' + imgHTML(item) + '<div>' + titleHTML(item) +
      (item.lede ? '<p class="lede">' + esc(item.lede) + '</p>' : '') + quotesHTML(item) + '</div></div>'
    : titleHTML(item) + imgHTML(item) +
      (item.lede ? '<p class="lede">' + esc(item.lede) + '</p>' : '') + quotesHTML(item);
  return '<article class="item"' + itemAttrs(item, sec || 'top', rank) + '>' +
    head + metaHTML(item) + verificationHTML(item) + '<div class="hover-only">' + whyHTML(item) + fbHTML(item, false) + '</div>' +
  '</article>';
}

/** 简讯 / 盲区版 / 反方 / 温暖 / 闲与美条目（同一套结构，分区名不同） */
function briefHTML(item, date, sec, rank) {
  registerItem(item, date);
  var isBlind = sec === 'blind';
  return '<article class="item brief ' + esc(sec || 'briefs') + '"' + itemAttrs(item, sec || 'briefs', rank) + '>' +
    '<div class="fb-row" style="align-items:baseline">' + titleHTML(item) + verificationHTML(item) + '</div>' +
    '<p class="one-liner">' + esc(item.one_liner || item.lede || '') + '</p>' +
    quotesHTML(item) +
    (isBlind ? blindReasonHTML(item) : '') +
    '<p class="meta">' + esc(item.source_label || '—') +
      (item.author_is_byline === false ? '' : '<span class="dot">·</span>' + esc(item.author_label || '')) + '</p>' +
    '<div class="hover-only">' + whyHTML(item) + fbHTML(item, false) + '</div>' +
  '</article>';
}

/* ---------------------------------------------------------
 * 4c. 登录态提醒（契约 v4 第 6.1 节）
 *     Edition 顶层 auth_alerts 非空 → 报头上方（快讯条之下）黄色条
 *     「<label> 登录已失效（自 <since_failing>）· [重新登录]」
 *     按钮调 POST /api/paper/auth/relogin：
 *       返回 open_url → 新窗口打开；返回 started → 按钮变「登录窗口已打开，扫码后回来刷新」
 * ------------------------------------------------------- */

function authBarRowHTML(ch) {
  var busy = state.authRelogging[ch.key] || '';
  var btnLabel = busy === 'started' ? '登录窗口已打开，扫码后回来刷新' : '重新登录';
  var note = busy === 'started' ? '<span class="auth-note">登录窗口已打开，扫码后回来刷新</span>' : '';
  return '<div class="auth-row" data-key="' + esc(ch.key) + '">' +
    '<span class="auth-label">' + esc(ch.label || ch.key) + ' 登录已失效</span>' +
    (ch.since_failing ? '<span class="auth-since">（自 ' + esc(fmtTime(ch.since_failing)) + '）</span>' : '') +
    note +
    '<button type="button" class="btn mini auth-btn" data-act="relogin" data-key="' + esc(ch.key) + '"' +
      (busy === 'started' ? ' disabled' : '') + '>' + esc(btnLabel) + '</button>' +
    '<span class="auth-flash" data-role="flash"></span>' +
  '</div>';
}

/** 报头上方的黄色条：只在报纸页、且 auth_alerts 非空时显示 */
function renderAuthBar() {
  if (!dom.authbar) return;
  var first = state.editions[0];
  var list = (first && first.auth_alerts) ? first.auth_alerts : [];
  if (!list.length || !isPaperRoute()) { dom.authbar.hidden = true; dom.authbar.innerHTML = ''; return; }
  dom.authbar.hidden = false;
  dom.authbar.innerHTML = '<div class="auth-bar">' +
    '<div class="auth-kicker">登录态</div>' +
    list.map(authBarRowHTML).join('') +
  '</div>';
}

/** 点「重新登录」 */
function doRelogin(btn) {
  var key = btn.getAttribute('data-key');
  var row = btn.closest('.auth-row');
  if (!key || !row || btn.disabled) return;
  var flash = $('[data-role="flash"]', row);
  btn.disabled = true;
  api.authRelogin(key).then(function (res) {
    if (res && res.open_url) {
      // kind == "url"：由前端开新窗口
      try { window.open(res.open_url, '_blank', 'noopener'); }
      catch (e) { try { window.open(res.open_url, '_blank'); } catch (e2) { /* 忽略 */ } }
      if (flash) { flash.textContent = '已在新窗口打开登录页，登录后回来刷新'; flash.style.color = ''; }
      btn.disabled = false;
      return;
    }
    // kind == "command"：后台已启动登录脚本
    state.authRelogging[key] = 'started';
    btn.textContent = '登录窗口已打开，扫码后回来刷新';
    if (flash) { flash.textContent = '登录脚本已在后台启动'; flash.style.color = ''; }
  }).catch(function (err) {
    btn.disabled = false;
    if (flash) { flash.textContent = '没拉起登录：' + err.message; flash.style.color = 'var(--danger)'; }
  });
}

/* ---------- 新页面 #/auth：全部通道表格 ---------- */

var AUTH_HEADERS = ['通道', '状态', '上次检查', '说明', '操作'];

function authRowHTML(ch) {
  var ok = !!ch.ok;
  var busy = state.authRelogging[ch.key] || '';
  return '<tr data-auth-key="' + esc(ch.key) + '"' + (ok ? '' : ' class="bad"') + '>' +
    '<td>' + esc(ch.label || ch.key) + '</td>' +
    '<td><span class="dot-state ' + (ok ? 'ok' : 'bad') + '"></span>' + (ok ? '正常' : '已失效') + '</td>' +
    '<td class="dimname">' + esc(fmtTime(ch.checked_at) || '—') + '</td>' +
    '<td class="auth-detail">' +
      (ch.since_failing ? '<span class="auth-since-inline">自 ' + esc(fmtTime(ch.since_failing)) + ' 起</span>' : '') +
      (ch.detail ? '<span class="auth-detail-t">' + esc(ch.detail) + '</span>' : '') +
      (ch.relogin_hint ? '<span class="auth-hint">怎么重登：' + esc(ch.relogin_hint) + '</span>' : '') +
    '</td>' +
    '<td>' + (ok
      ? '<span class="dimname">—</span>'
      : '<button type="button" class="btn mini" data-act="relogin" data-key="' + esc(ch.key) + '"' +
        (busy === 'started' ? ' disabled' : '') + '>' + (busy === 'started' ? '已打开登录窗口' : '重新登录') + '</button>') +
      '<div class="pipe-flash" data-role="flash"></div></td>' +
  '</tr>';
}

function showAuthPage() {
  leaveItemPage();
  state.pendingRestore = false;
  clearAllPages();
  dom.masthead.hidden = true;
  dom.moreWrap.hidden = true;
  renderFlash();
  renderAuthBar();

  if (!dom.authPage) {
    dom.authPage = document.createElement('section');
    dom.authPage.className = 'authpage';
  }
  dom.authPage.innerHTML = '<p class="backlink"><a href="#/">← 回到报纸</a></p>' +
    '<h2>登录状态</h2>' +
    '<div class="pipe-scroll-x"><table class="pipe-tbl auth-tbl"><thead><tr>' +
    AUTH_HEADERS.map(function (h) { return '<th>' + esc(h) + '</th>'; }).join('') +
    '</tr></thead><tbody><tr><td colspan="5" class="dimname">正在读登录态…</td></tr></tbody></table></div>' +
    '<p class="auth-page-note">某个通道失效时，这条来源就悄悄停更了。重新登录后回来刷新本页即可。</p>';
  if (!dom.authPage.parentNode) dom.view.appendChild(dom.authPage);
  window.scrollTo(0, 0);

  return api.auth().then(function (data) {
    if (!dom.authPage || !dom.authPage.parentNode) return;
    var list = (data && data.channels) ? data.channels : [];
    var tb = $('tbody', dom.authPage);
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="5" class="dimname">没有登记任何需要登录的通道。</td></tr>';
      return;
    }
    tb.innerHTML = list.map(authRowHTML).join('');
  }).catch(function (err) {
    if (!dom.authPage || !dom.authPage.parentNode) return;
    var tb2 = $('tbody', dom.authPage);
    if (tb2) tb2.innerHTML = '<tr><td colspan="5" class="err-page">登录态读不出来：' + esc(err.message) + '</td></tr>';
  });
}

/* ---------------------------------------------------------
 * 4d. 风险提示栏（契约 v4 第 9 节）
 *     位置：快讯条之下、报纸主体之前；有 kind:"risk" 条目或常驻地 urgent 投递才显示
 * ------------------------------------------------------- */

/** 一个行程一块：标题「<地点> · <起止日期> · 行前风险简报」，按 sections 平铺 heading 与 points */
function riskBlockHTML(it) {
  var trip = it.trip || {};
  var span = '';
  if (trip.start_date || trip.end_date) {
    span = (trip.start_date || '?') + ' → ' + (trip.end_date || '?');
  } else if (it.date_span) {
    span = it.date_span;
  }
  var head = '<div class="risk-head"><h3>' + esc(trip.place || it.title || '行程') +
    (span ? ' · ' + esc(span) : '') + ' · 行前风险简报</h3></div>';
  var secs = it.sections || [];
  var body = secs.map(function (s) {
    var points = (s.points || []).map(function (p) {
      var src = '';
      if (extUrl(p.source_url)) {
        src = '<a class="linkout risk-src" href="' + esc(extUrl(p.source_url)) + '" target="_blank" rel="noopener noreferrer">来源 ↗</a>';
        if (p.source_date) src += '<span class="risk-date">' + esc(p.source_date) + '</span>';
      } else if (p.source_date) {
        src = '<span class="risk-date">' + esc(p.source_date) + '</span>';
      }
      return '<li class="risk-point">' + esc(p.text || p.point || '') + src + '</li>';
    }).join('');
    return '<div class="risk-sec"><h4>' + esc(s.heading || '') + '</h4>' +
      (points ? '<ul class="risk-points">' + points + '</ul>' : '<p class="risk-none">没查到官方说法。</p>') +
      '</div>';
  }).join('');
  var lead = it.lede ? '<p class="risk-lede">' + esc(it.lede) + '</p>' : '';
  return '<article class="risk-block" data-risk-id="' + esc(it.item_id || '') + '">' +
    head + lead + body +
    (extUrl(it.url) ? '<p class="risk-orig"><a class="linkout" href="' + esc(extUrl(it.url)) + '" target="_blank" rel="noopener noreferrer">完整简报 ↗</a></p>' : '') +
  '</article>';
}

/** 风险提示条：常驻地预警条目 + 行前风险简报，平铺在同一栏 */
function renderRiskBar() {
  if (!dom.riskbar) return;
  var first = state.editions[0];
  if (!first || !isPaperRoute()) { dom.riskbar.hidden = true; dom.riskbar.innerHTML = ''; return; }
  var risk = (first.sections && first.sections.risk) ? first.sections.risk : [];
  var home_ = risk.filter(function (it) { return it.kind === 'risk' && !it.trip; });
  var trips = risk.filter(function (it) { return it.kind === 'risk' && it.trip; });
  if (!risk.length) { dom.riskbar.hidden = true; dom.riskbar.innerHTML = ''; return; }
  dom.riskbar.hidden = false;
  dom.riskbar.innerHTML = '<div class="risk-bar">' +
    '<div class="sec-head risk-head-line"><h2>风险提示</h2><span class="sec-note">' +
      (home_.length + trips.length) + ' 块 · 你在哪、你要去哪</span></div>' +
    (home_.length ? '<div class="risk-home"><div class="risk-kicker">常驻地</div>' +
      home_.map(function (it) {
        return '<div class="risk-home-row">' +
          '<span class="risk-home-title">' + esc(it.title || '') + '</span>' +
          (it.detail ? '<span class="risk-home-detail">' + esc(it.detail) + '</span>' : '') +
          (extUrl(it.url) ? '<a class="linkout" href="' + esc(extUrl(it.url)) + '" target="_blank" rel="noopener noreferrer">详情 ↗</a>' : '') +
        '</div>';
      }).join('') + '</div>' : '') +
    trips.map(riskBlockHTML).join('') +
  '</div>';
}

/* ---------------------------------------------------------
 * 4e. 反方 / 机会 / 结算 / 温暖 / 闲与美（契约 v4 第 7、8、10 节）
 * ------------------------------------------------------- */

/** 反方：当天 counter 条目，每条显示「这条在挑战你的：…」 */
function counterSectionHTML(list, date) {
  if (!list || !list.length) return '';
  return '<section class="counter-sec">' +
    '<div class="sec-head"><h2>反方</h2><span class="sec-note">今天有 ' + list.length + ' 条在挑战你</span></div>' +
    '<div class="counter-grid">' + list.map(function (it, i) {
      return briefHTML(it, date, 'counter', i + 1);
    }).join('') + '</div>' +
  '</section>';
}

/** 机会：若为真 / 证伪信号 / 时间尺度 三行 */
function opportunitySectionHTML(list, date) {
  if (!list || !list.length) return '';
  return '<section class="oppo-sec">' +
    '<div class="sec-head"><h2>机会</h2><span class="sec-note">尚无共识、信号混乱的 ' + list.length + ' 条</span></div>' +
    '<div class="oppo-grid">' + list.map(function (it, i) {
      registerItem(it, date);
      var op = it.opportunity || {};
      return '<article class="item brief oppo"' + itemAttrs(it, 'opportunity', i + 1) + '>' +
        '<div class="fb-row" style="align-items:baseline">' + titleHTML(it) + '</div>' +
        '<p class="one-liner">' + esc(it.one_liner || it.lede || '') + '</p>' +
        '<dl class="oppo-rows">' +
          '<div class="oppo-row"><dt>若为真：</dt><dd>' + esc(op.if_true || '—') + '</dd></div>' +
          '<div class="oppo-row"><dt>证伪信号：</dt><dd>' + esc(op.kill_signal || '—') + '</dd></div>' +
          '<div class="oppo-row"><dt>时间尺度：</dt><dd>' + esc(op.horizon || '—') + '</dd></div>' +
        '</dl>' +
        metaHTML(it) + whyHTML(it) + fbHTML(it, false) +
      '</article>';
    }).join('') + '</div>' +
  '</section>';
}

var OUTCOME_LABEL = { true: '对了', false: '错了', unresolvable: '说不清' };

/** 到期结算：断言原文 / 谁说的 / 出处 / AI 初判（带依据）/ 三个裁定按钮 */
function settleSectionHTML(list, date) {
  if (!list || !list.length) return '';
  return '<section class="settle-sec">' +
    '<div class="sec-head"><h2>到期结算</h2><span class="sec-note">今天到期的 ' + list.length + ' 条断言</span></div>' +
    '<div class="settle-list">' + list.map(function (it, i) {
      registerItem(it, date);
      var rec = it.author_record || {};
      var verdict = it.verdict || {};
      var vLabel = { likely_true: '大概成立', likely_false: '大概不成立', unclear: '说不清' }[verdict.kind] || '待核';
      var outBtns = ['true', 'false', 'unresolvable'].map(function (o) {
        var cur = it.my_outcome === o;
        return '<button type="button" class="btn mini' + (cur ? ' on' : '') + '" data-act="resolve" data-claim="' +
          esc(it.claim_id || '') + '" data-outcome="' + o + '"' + (it.claim_id ? '' : ' disabled') + '>' +
          OUTCOME_LABEL[o] + '</button>';
      }).join('');
      return '<article class="settle-row"' + itemAttrs(it, 'settle', i + 1) + '>' +
        '<div class="settle-claim">' + esc((it.claim && it.claim.text) || it.one_liner || it.title || '') + '</div>' +
        '<div class="settle-meta">' +
          '<span class="settle-who">' + esc(it.author_label || '—') + '</span>' +
          (typeof rec.right === 'number' || typeof rec.wrong === 'number'
            ? '<span class="settle-record">此人过往 ' + esc(rec.right || 0) + ' 对 / ' + esc(rec.wrong || 0) + ' 错</span>'
            : '') +
          (it.claim && extUrl(it.claim.source_url)
            ? '<a class="linkout" href="' + esc(extUrl(it.claim.source_url)) + '" target="_blank" rel="noopener noreferrer">当时出处 ↗</a>'
            : '') +
        '</div>' +
        '<div class="settle-verdict"><span class="settle-vk">AI 初判</span><span class="settle-vt">' + esc(vLabel) + '</span>' +
          (extUrl(verdict.basis_url)
            ? '<a class="linkout" href="' + esc(extUrl(verdict.basis_url)) + '" target="_blank" rel="noopener noreferrer">依据 ↗</a>'
            : '<span class="dimname">无依据链接</span>') +
          (verdict.note ? '<span class="settle-vn">' + esc(verdict.note) + '</span>' : '') +
        '</div>' +
        '<div class="settle-btns">' + outBtns + '<span class="settle-flash" data-role="flash"></span></div>' +
        fbHTML(it, false) +
      '</article>';
    }).join('') + '</div>' +
  '</section>';
}

/** 人间温暖：版式更松（衬线导语、留白多）+ 核实标签 */
function warmthSectionHTML(list, date) {
  if (!list || !list.length) return '';
  return '<section class="warmth-sec">' +
    '<div class="sec-head"><h2>人间温暖</h2><span class="sec-note">' + list.length + ' 条 · 核实优先</span></div>' +
    '<div class="warmth-grid">' + list.map(function (it, i) {
      return briefHTML(it, date, 'warmth', i + 1);
    }).join('') + '</div>' +
  '</section>';
}

/** 闲与美：小图 + 一句话 */
function leisureSectionHTML(list, date) {
  if (!list || !list.length) return '';
  return '<section class="leisure-sec">' +
    '<div class="sec-head"><h2>闲与美</h2><span class="sec-note">' + list.length + ' 条</span></div>' +
    '<div class="leisure-grid">' + list.map(function (it, i) {
      registerItem(it, date);
      return '<article class="item leisure"' + itemAttrs(it, 'leisure', i + 1) + '>' +
        (it.image_url ? '<img class="thumb leisure-thumb" src="' + esc(it.image_url) + '" alt="" loading="lazy">' : '') +
        '<div class="leisure-text">' + titleHTML(it) +
        '<p class="one-liner">' + esc(it.one_liner || it.lede || '') + '</p>' +
        metaHTML(it) +
        '</div>' +
      '</article>';
    }).join('') + '</div>' +
  '</section>';
}

/* ---------------------------------------------------------
 * 4f. 推荐关注（契约 v4 第 6.3 节）
 * ------------------------------------------------------- */

var PLATFORM_LABEL = {
  weibo: '微博', zhihu: '知乎', wechat: '公众号', youtube: 'YouTube',
  xiaoyuzhou: '小宇宙', podcast: '播客', douban: '豆瓣', other: '其它'
};

var FOLLOW_STATUS = { new: '待你决定', followed: '已关注', dismissed: '已忽略' };

/** 一条推荐：平台标识 + 账号名 + 理由 + 证据标题链接 + 「去关注 ↗」+「不感兴趣」 */
function followRowHTML(sg, compact) {
  var st = state.followDecided[sg.id] || sg.status || 'new';
  var evidence = (sg.evidence || []).map(function (e) {
    // 10-05 验收 漏报-3：证据没有 url 就不渲染链接（同下方 follow-go 的写法）
    return extUrl(e.url)
      ? '<a class="follow-evi" href="' + esc(extUrl(e.url)) + '" target="_blank" rel="noopener noreferrer">' +
          esc(e.title || '') + ' ↗</a>'
      : '<span class="follow-evi no-url">' + esc(e.title || '') + '</span>';
  }).join('');
  var isNew = st === 'new';
  return '<article class="follow-row" data-fs-id="' + esc(sg.id) + '" data-status="' + esc(st) + '">' +
    '<div class="follow-line">' +
      '<span class="follow-platform">' + esc(PLATFORM_LABEL[sg.platform] || sg.platform || '—') + '</span>' +
      '<span class="follow-label">' + esc(sg.label || sg.account_id || '—') + '</span>' +
      (compact ? '' : '<span class="follow-status ' + esc(st) + '">' + esc(FOLLOW_STATUS[st] || st) + '</span>') +
    '</div>' +
    '<p class="follow-reason">' + esc(sg.reason || '') + '</p>' +
    (evidence ? '<div class="follow-evidence">' + evidence + '</div>' : '') +
    '<div class="follow-actions">' +
      // 没有主页链接时不渲染成 href="#"（10-05：点了会跳回报纸本身）
      (extUrl(sg.url)
        ? '<a class="btn mini follow-go" href="' + esc(extUrl(sg.url)) + '" target="_blank" rel="noopener noreferrer"' +
            (isNew ? ' data-act="follow-go" data-fs="' + esc(sg.id) + '"' : '') + '>去关注 ↗</a>'
        : '<span class="btn mini follow-go is-disabled" title="没有拿到这个账号的主页链接">无主页链接</span>') +
      (isNew ? '<button type="button" class="btn mini follow-no" data-act="follow-dismiss" data-fs="' + esc(sg.id) + '">不感兴趣</button>' : '') +
      '<span class="follow-flash" data-role="flash"></span>' +
    '</div>' +
  '</article>';
}

/** 版面小栏：盲区版之后 */
function followSectionHTML(list) {
  if (!list || !list.length) return '';
  return '<section class="follow-sec">' +
    '<div class="sec-head"><h2>本周推荐关注</h2><span class="sec-note">系统发现 · 关注由你自己去点</span></div>' +
    '<div class="follow-list">' + list.map(function (sg) { return followRowHTML(sg, true); }).join('') + '</div>' +
  '</section>';
}

/** 点「不感兴趣」：调 decide dismiss，成功后整条淡出 */
function dismissFollow(btn) {
  var id = btn.getAttribute('data-fs');
  var row = btn.closest('.follow-row');
  if (!id || !row || btn.disabled) return;
  btn.disabled = true;
  api.followDecide(id, 'dismiss').then(function () {
    state.followDecided[id] = 'dismissed';
    row.classList.add('is-gone');
    setTimeout(function () {
      if (row.parentNode) row.parentNode.removeChild(row);
    }, 200);
  }).catch(function (err) {
    btn.disabled = false;
    var f = $('[data-role="flash"]', row);
    if (f) { f.textContent = '没记上：' + err.message; f.style.color = 'var(--danger)'; }
  });
}

/** 点「去关注 ↗」：新窗口打开该账号主页，并发 open_original（meta.from=follow_suggestion） */
function trackFollowGo(sgId) {
  if (!sgId) return;
  trackEmit('open_original', { item_id: 'follow:' + sgId, edition_date: null, ms: null, meta: { from: 'follow_suggestion' } });
}

/* ---------------------------------------------------------
 * 4b. 本机投递箱：快讯条 + 本机简报（契约 v3 第 5 节）
 *     InboxItem = {inbox_id, source, source_label, title, body,
 *                  url, priority, created_at, read}
 * ------------------------------------------------------- */

var FLASH_LINES = 6;   // 快讯正文最多 6 行
var INBOX_LINES = 8;   // 本机简报正文最多 8 行

function inboxIdOf(it) {
  return (it && it.inbox_id !== undefined && it.inbox_id !== null) ? String(it.inbox_id) : '';
}

/** 正文按空行分段，段内换行保留 */
function inboxBodyHTML(body) {
  if (body === null || body === undefined || body === '') return '';
  return String(body)
    .split(/\n\s*\n/)
    .filter(function (p) { return p.trim(); })
    .map(function (p) { return '<p>' + esc(p.trim()).replace(/\n/g, '<br>') + '</p>'; })
    .join('');
}

/**
 * 正文块：超过 maxLines 行就渐隐 + 「展开全部」。
 * 收起状态下按行高 × 行数截断（--ib-lines），渐隐交给 CSS 的 mask。
 */
function inboxBodyClampedHTML(body, maxLines, moreLabel) {
  var inner = inboxBodyHTML(body);
  if (!inner) return '';
  return '<div class="ib-body" data-role="body" data-lines="' + maxLines + '" style="--ib-lines:' + maxLines + '">' +
    '<div class="ib-body-in" data-role="bodyin">' + inner + '</div>' +
    '<button type="button" class="btn mini ib-more" data-act="ib-more">' + esc(moreLabel || '展开全部') + '</button>' +
    '</div>';
}

/** 量一下正文是不是真的超了行数：没超就不给「展开」按钮，也不加渐隐 */
function tuneInboxBodies(root) {
  if (!root) return;
  $$('.ib-body', root).forEach(function (box) {
    var inner = $('[data-role="bodyin"]', box);
    var btn = $('.ib-more', box);
    if (!inner || !btn) return;
    if (box.classList.contains('open')) return;
    var over = inner.scrollHeight - inner.clientHeight > 2;
    box.classList.toggle('clipped', over);
    btn.hidden = !over;
  });
}

/** 条目在 DOM 上的标记：data-ib-id 供路由定位，data-sec=inbox 供曝光统计 */
function inboxAttrs(it, rank) {
  return ' data-ib-id="' + esc(inboxIdOf(it)) + '" data-sec="inbox" data-rank="' + (Number(rank) || 0) + '" tabindex="-1"';
}

/** 记下这条投递箱条目属于哪一期，供行为事件的 edition_date 用 */
function registerInbox(it, date) {
  var id = inboxIdOf(it);
  if (id && date) trackInboxDates[id] = date;
}

function inboxOutHTML(it) {
  if (!extUrl(it.url)) return '';
  return '<a class="linkout ib-out" href="' + esc(extUrl(it.url)) + '" target="_blank" rel="noopener noreferrer">来源 ↗</a>';
}

/* ---------- 快讯条（报头上方红底横条） ---------- */

function flashRowHTML(it, rank, date) {
  registerInbox(it, date);
  return '<article class="flash-row"' + inboxAttrs(it, rank) + '>' +
    '<div class="flash-line">' +
      '<span class="flash-src">' + esc(it.source_label || it.source || '—') + '</span>' +
      '<span class="dot">·</span>' +
      '<span class="flash-title">' + esc(it.title || '') + '</span>' +
      '<span class="dot">·</span>' +
      '<span class="flash-time">' + esc(fmtTime(it.created_at)) + '</span>' +
      inboxOutHTML(it) +
      '<button type="button" class="btn mini flash-ack" data-act="flash-ack" data-ib="' + esc(inboxIdOf(it)) + '">知道了</button>' +
    '</div>' +
    inboxBodyClampedHTML(it.body, FLASH_LINES, '展开全部') +
  '</article>';
}

/** 快讯条：Edition 顶层 flash 非空才显示；全部读过就整条消失 */
function renderFlash() {
  if (!dom.flash) return;
  var first = state.editions[0];
  var all = (first && first.flash) ? first.flash : [];
  var list = all.filter(function (it) { return !state.flashAcked[inboxIdOf(it)]; });
  if (!list.length || !isPaperRoute()) { dom.flash.hidden = true; dom.flash.innerHTML = ''; return; }
  dom.flash.hidden = false;
  dom.flash.innerHTML =
    '<div class="flash-bar">' +
      '<div class="flash-kicker">快讯</div>' +
      list.map(function (it, i) { return flashRowHTML(it, i + 1, first.date); }).join('') +
    '</div>';
  tuneInboxBodies(dom.flash);
  trackObserve(dom.flash);
}

/** 点「知道了」：调 inbox/read，成功后这条收起；全部读完横条消失 */
function ackFlash(btn) {
  var id = btn.getAttribute('data-ib');
  var row = btn.closest('.flash-row');
  if (!id || !row || btn.disabled) return;
  btn.disabled = true;
  api.inboxRead(Number(id)).then(function () {
    trackOpenInbox(id, 'flash_ack');
    state.flashAcked[id] = true;
    if (row.parentNode) row.parentNode.removeChild(row);
    var left = $$('.flash-row', dom.flash).length;
    if (!left) { dom.flash.hidden = true; dom.flash.innerHTML = ''; }
  }).catch(function (err) {
    btn.disabled = false;
    var e = document.createElement('span');
    e.className = 'err';
    e.textContent = '没标上已读：' + err.message;
    row.appendChild(e);
    setTimeout(function () { if (e.parentNode) e.remove(); }, 6000);
  });
}

/* ---------- 本机简报（主线简讯之后、盲区版之前） ---------- */

function inboxGroupHTML(label, items, startRank) {
  return '<div class="ib-group">' +
    '<div class="ib-group-head"><h3>' + esc(label) + '</h3><span class="ib-count">' + items.length + ' 条</span></div>' +
    '<div class="ib-items">' +
      items.map(function (it, i) {
        var urgent = it.priority === 'urgent';
        return '<article class="ib-item' + (urgent ? ' urgent' : '') + '"' + inboxAttrs(it, startRank + i) + '>' +
          '<div class="ib-line">' +
            '<span class="ib-title">' + esc(it.title || '') + '</span>' +
            '<span class="dot">·</span>' +
            '<span class="ib-time">' + esc(fmtTime(it.created_at)) + '</span>' +
            inboxOutHTML(it) +
          '</div>' +
          inboxBodyClampedHTML(it.body, INBOX_LINES, '展开') +
        '</article>';
      }).join('') +
    '</div>' +
  '</div>';
}

function inboxSectionHTML(list, date) {
  if (!list || !list.length) return '';
  list.forEach(function (it) { registerInbox(it, date); });
  var order = [], groups = {};
  list.forEach(function (it) {
    var key = it.source_label || it.source || '—';
    if (!groups[key]) { groups[key] = []; order.push(key); }
    groups[key].push(it);
  });
  var rank = 0;
  var body = order.map(function (k) {
    rank += 1;                                  // rank 从 1 开始，与新闻条目的 data-rank 一致
    var html = inboxGroupHTML(k, groups[k], rank);
    rank += groups[k].length - 1;
    return html;
  }).join('');
  return '<section class="inbox-sec">' +
    '<div class="sec-head"><h2>本机简报</h2><span class="sec-note">' + list.length + ' 条 · ' + order.length + ' 个来源</span></div>' +
    body + '</section>';
}

/* ---------------------------------------------------------
 * 5. 一期的版面
 * ------------------------------------------------------- */

/** 头版（参照 WSJ / NYT / Barron's：第一屏放下十几条，三栏）
 *  左栏：要闻 3 条（标题 + 两行导语）；中栏：头条（图在上、大标题、导语）；右栏：要闻余下 + 简讯前若干条，只给标题。 */
var FRONT_RIGHT_BRIEFS = 8;
var FRONT_CENTER_BRIEFS = 6;
function frontPageHTML(lead, top, briefs, date) {
  var left = top.slice(0, 3);
  var rightTop = top.slice(3);
  var rightBriefs = briefs.slice(0, FRONT_RIGHT_BRIEFS);
  var L = left.map(function (it, i) {
    registerItem(it, date);
    return '<article class="item front-l"' + itemAttrs(it, 'top', i + 1) + '>' + titleHTML(it) +
      (it.lede ? '<p class="lede clamp3">' + esc(it.lede) + '</p>' : '') +
      metaHTML(it) + '<div class="hover-only">' + whyHTML(it) + fbHTML(it, false) + '</div></article>';
  }).join('');
  var C = '';
  if (lead.length) {
    var it = lead[0];
    registerItem(it, date);
    C = '<article class="item lead front-c"' + itemAttrs(it, 'lead', 1) + '>' + imgHTML(it, 'tall') + titleHTML(it) +
      (it.lede ? '<p class="lede">' + esc(it.lede) + '</p>' : '') +
      (it.so_what ? '<p class="sowhat">' + esc(it.so_what) + '</p>' : '') +
      metaHTML(it) + '<div class="hover-only">' + whyHTML(it) + claimHTML(it) + fbHTML(it, false) + '</div></article>';
  }
  var R = rightTop.map(function (it, i) {
    registerItem(it, date);
    return '<article class="item front-r"' + itemAttrs(it, 'top', i + 4) + '>' + imgHTML(it, 'mini') + titleHTML(it) +
      '<p class="meta">' + esc(it.source_label || '') + '</p><div class="hover-only">' + fbHTML(it, false) + '</div></article>';
  }).join('') + (rightBriefs.length ? '<div class="front-r-head">简讯</div>' : '') + rightBriefs.map(function (it, i) {
    registerItem(it, date);
    return '<article class="item front-rb"' + itemAttrs(it, 'briefs', i + 1) + '>' + titleHTML(it) +
      '<p class="meta">' + esc(it.source_label || '') + '</p><div class="hover-only">' + fbHTML(it, false) + '</div></article>';
  }).join('');
  // 中栏头条下面再铺两排小条（取简讯第 9–14 条），避免中栏只有一条、下半截空着
  var under = briefs.slice(FRONT_RIGHT_BRIEFS, FRONT_RIGHT_BRIEFS + FRONT_CENTER_BRIEFS);
  if (under.length) {
    C += '<div class="front-under">' + under.map(function (it, i) {
      registerItem(it, date);
      return '<article class="item front-u"' + itemAttrs(it, 'briefs', FRONT_RIGHT_BRIEFS + i + 1) + '>' + titleHTML(it) +
        '<p class="one-liner clamp3">' + esc(it.lede || it.one_liner || '') + '</p><p class="meta">' + esc(it.source_label || '') +
        '</p><div class="hover-only">' + fbHTML(it, false) + '</div></article>';
    }).join('') + '</div>';
  }
  return '<div class="front"><div class="front-col front-left">' + L + '</div><div class="front-col front-center">' + C +
    '</div><div class="front-col front-right">' + R + '</div></div>';
}

function editionHTML(ed, isFirst) {
  var date = ed.date;
  var s = ed.sections || {};
  var lead = s.lead || [];
  var top = s.top || [];
  var briefs = s.briefs || [];
  var blind = s.blind || [];
  var inbox = s.inbox || [];
  var counter = s.counter || [];
  var opportunity = s.opportunity || [];
  var settle = s.settle || [];
  var warmth = s.warmth || [];
  var leisure = s.leisure || [];
  var out = '';

  out += '<div class="edition-head">' +
    '<span class="edition-date">' + esc(fmtDateCN(date)) + '</span>' +
    '<span class="edition-note">第 ' + issueNo(date) + ' 期' +
      (ed.stats ? ' · 池 ' + ed.stats.pool + ' → 选 ' + ed.stats.picked + ' · AI 处理 ' + ed.stats.ai_done : '') +
    '</span></div>';

  // 「昨天你是怎么读的」：契约新增字段 reading_note，非空才显示
  if (ed.reading_note) {
    out += '<p class="reading-note">编辑部观察：' + esc(ed.reading_note) + '</p>';
  }

  out += frontPageHTML(lead, top, briefs, date);
  briefs = briefs.slice(FRONT_RIGHT_BRIEFS + FRONT_CENTER_BRIEFS);

  // 反方：要闻之后、简讯之前（契约第 7.2 节）
  if (counter.length) out += counterSectionHTML(counter, date);

  // 机会：反方之后（第 10.1 节）
  if (opportunity.length) out += opportunitySectionHTML(opportunity, date);

  // 到期结算：机会之后（第 10.2 节）
  if (settle.length) out += settleSectionHTML(settle, date);

  if (briefs.length) {
    out += '<div class="sec-head"><h2>更多简讯</h2><span class="sec-note">' + briefs.length + ' 条</span></div>';
    out += '<div class="briefs">' + briefs.map(function (it, i) { return briefHTML(it, date, 'briefs', i + 1); }).join('') + '</div>';
  }

  // 本机简报：主线简讯之后、盲区版之前，组内全部平铺，没有折叠
  if (inbox.length) out += inboxSectionHTML(inbox, date);

  if (blind.length) {
    out += '<section class="blind">' +
      '<div class="sec-head"><h2>盲区版 · 你平时不看的</h2><span class="sec-note">' + blind.length + ' 条</span></div>' +
      '<div class="blind-grid">' + blind.map(function (it, i) { return briefHTML(it, date, 'blind', i + 1); }).join('') + '</div>' +
    '</section>';
  }

  // 人间温暖：盲区版之后（第 8 节）
  if (warmth.length) out += warmthSectionHTML(warmth, date);

  // 闲与美：温暖之后（第 10.3 节）
  if (leisure.length) out += leisureSectionHTML(leisure, date);

  // 推荐关注：盲区版之后（第 6.3 节）
  if (ed.follow_suggestions && ed.follow_suggestions.length) out += followSectionHTML(ed.follow_suggestions);

  if (isFirst && ed.letters && ed.letters.length) out += lettersHTML(ed.letters);

  return '<section class="edition" data-date="' + esc(date) + '">' + out + '</section>';
}

function lettersHTML(letters) {
  var body = letters.map(function (p, i) {
    // 平时只显示 依据/目标/建议 三行；解析为 null（basis/target/suggestion 全空）才退回显示原始 line
    var parsed = p.basis || p.target || p.suggestion;
    var main = parsed
      ? '<div class="letter-row"><span class="k">依据</span><span class="v">' + esc(p.basis || '—') + '</span></div>' +
        '<div class="letter-row"><span class="k">目标</span><span class="v">' + esc(p.target || '—') + '</span></div>' +
        '<div class="letter-row"><span class="k">建议</span><span class="v">' + esc(p.suggestion || '—') + '</span></div>'
      : '<p class="letter-line">' + esc(p.line) + '</p>';
    return '<article class="letter" data-line="' + esc(p.line) + '" data-idx="' + i + '">' +
      main +
      '<div class="letter-actions">' +
        '<button type="button" class="btn mini" data-act="propose" data-decision="accept">接受</button>' +
        '<button type="button" class="btn mini" data-act="propose" data-decision="reject">拒绝</button>' +
        '<span class="letter-done" data-role="done"></span>' +
      '</div>' +
    '</article>';
  }).join('');
  return '<section class="letters">' +
    '<div class="sec-head"><h2>编辑部来信</h2><span class="sec-note">' + letters.length + ' 封待你批复</span></div>' +
    body + '</section>';
}

/* ---------------------------------------------------------
 * 6. 报纸主页
 * ------------------------------------------------------- */

function isPaperRoute() {
  var n = parseHash().name;
  return n === 'home' || n === 'inbox';
}

/** 报头副标题的「今日新知 x% · 反方 n 条」（契约第 7.2 节，取 stats.novelty_mix） */
function noveltyMixLine(stats) {
  var mix = stats && stats.novelty_mix;
  if (!mix) return '';
  var known = ['new_fact', 'new_mechanism', 'counter', 'known', 'confirming'];
  var total = 0, n = 0, i;
  for (i = 0; i < known.length; i++) {
    var v = Number(mix[known[i]]);
    if (isFinite(v) && v > 0) { total += v; if (known[i] !== 'known' && known[i] !== 'confirming') n += v; }
  }
  if (!total) return '';
  var pct = Math.round((n / total) * 100);
  return '今日新知 ' + pct + '% · 反方 ' + (Number(mix.counter) || 0) + ' 条';
}

function renderMasthead() {
  // 单篇页 / 管道页顶部不出现报头（期号与「池 → 选 → AI 处理」统计行）
  if (!isPaperRoute()) { dom.masthead.hidden = true; renderAuthBar(); renderRiskBar(); renderFlash(); return; }
  var first = state.editions[0];
  if (!first) { dom.masthead.hidden = true; renderAuthBar(); renderRiskBar(); renderFlash(); return; }
  dom.masthead.hidden = false;
  dom.mastEdition.textContent = fmtDateCN(first.date) + ' · 东京时间 ' + fmtTime(first.built_at).slice(-5);
  var mixLine = noveltyMixLine(first.stats);
  dom.mastSub.innerHTML = '第 <span class="issue">' + issueNo(first.date) + '</span> 期 · ' +
    (first.stats ? '今日 ' + first.stats.picked + ' 条，自 ' + first.stats.pool + ' 条里挑的' : '') +
    (mixLine ? '<br><span class="mix-line">' + esc(mixLine) + '</span>' : '');
  renderAuthBar();
  renderRiskBar();
  renderFlash();
}

function appendEdition(ed) {
  if (!ed) return;
  for (var i = 0; i < state.editions.length; i++) {
    if (state.editions[i].date === ed.date) return;
  }
  state.editions.push(ed);
  var isFirst = state.editions.length === 1;
  dom.home.insertAdjacentHTML('beforeend', editionHTML(ed, isFirst));
  renderMasthead();
  hideImages(dom.home);
}

function hideImages(root) {
  $$('img', root).forEach(function (im) {
    if (im.getAttribute('data-wired')) return;
    im.setAttribute('data-wired', '1');
    im.addEventListener('error', function () { im.hidden = true; });
    im.addEventListener('load', function () { im.classList.add('ok'); });
    if (im.complete) { if (im.naturalWidth === 0) im.hidden = true; else im.classList.add('ok'); }
  });
  tuneInboxBodies(root);   // 本机简报正文按行数截断后才量得出是否溢出
  trackObserve(root);      // 新渲染出来的条目挂上曝光观察
}

/**
 * 页尾（第 10.4 节）：一期结束显示「今天就这些 · 预计阅读 N 分钟」+ 按钮「看昨天的」。
 * 取消了原先的「滚到底自动加载前一期」：现在只有点按钮才加载，加载后页尾再出现同样的按钮。
 */
function estReadMin() {
  var n = 0;
  state.editions.forEach(function (ed) {
    var s = (ed && ed.sections) || {};
    for (var k in s) {
      if (!Object.prototype.hasOwnProperty.call(s, k)) continue;
      var arr = s[k];
      if (!Array.isArray(arr)) continue;
      for (var i = 0; i < arr.length; i++) {
        var it = arr[i] || {};
        var chars = String(it.lede || '').length + String(it.one_liner || '').length;
        if (chars) n += chars;
      }
    }
  });
  // 中文约 400 字/分钟；少于 1 分钟按 1 分钟算
  return Math.max(1, Math.round(n / 400));
}

function setMoreNote() {
  var last = state.editions[state.editions.length - 1];
  var lastDate = last ? last.date : '';
  if (!state.started) {
    dom.moreNote.textContent = '正在铺版…';
    if (dom.moreBtn) dom.moreBtn.hidden = true;
    return;
  }
  if (state.exhausted) {
    dom.moreNote.textContent = '今天就这些 · 预计阅读 ' + estReadMin() + ' 分钟' +
      (state.editions.length > 1 ? '（已看到 ' + fmtDateCN(lastDate) + '）' : '') + ' · 没有更早的了';
    if (dom.moreBtn) dom.moreBtn.hidden = true;
    return;
  }
  dom.moreNote.textContent = '今天就这些 · 预计阅读 ' + estReadMin() + ' 分钟' +
    (state.editions.length > 1 ? '（已看到 ' + fmtDateCN(lastDate) + '）' : '');
  if (dom.moreBtn) dom.moreBtn.hidden = false;
}

function loadFirstPage() {
  // 10-05 验收 H106=H202：在途不加第二发（showHome/showItemPage 都可能触发，深链/Esc 并发会重复 push、丢编辑部来信）
  if (state.firstLoading) return;
  state.firstLoading = true;
  // 10-05 验收 漏报-4：重铺前先清掉旧的错误条，「再试一次」成功后不再残留
  $$('.err-page', dom.view).forEach(function (n) { if (n.parentNode) n.parentNode.removeChild(n); });
  state.editions = [];
  state.nextBefore = undefined;
  state.exhausted = false;
  state.started = false;
  var note = document.createElement('p');
  note.className = 'loading';
  note.textContent = '正在铺版…';
  if (dom.itemPage && dom.itemPage.parentNode === dom.view) dom.view.insertBefore(note, dom.itemPage);
  else dom.view.appendChild(note);

  api.editions(null, 1).then(function (data) {
    state.firstLoading = false;
    if (note.parentNode) note.parentNode.removeChild(note);
    if (dom.home && dom.home.parentNode) dom.home.parentNode.removeChild(dom.home);  // 重铺时不留旧版面
    dom.home = document.createElement('div');
    dom.home.className = 'home';
    (data.editions || []).forEach(function (ed) {
      // 10-05 验收 H106=H202：按日期去重后再 push，state.editions 不出现重复期
      for (var i = 0; i < state.editions.length; i++) {
        if (state.editions[i].date === ed.date) return;
      }
      state.editions.push(ed);
      var isFirst = state.editions.length === 1;
      dom.home.insertAdjacentHTML('beforeend', editionHTML(ed, isFirst));
    });
    if (!state.editions.length) {
      var empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = '这一期还没有内容。';
      dom.home.appendChild(empty);
    }
    // 异步铺版可能晚于路由切换：只在报纸页把版面挂进文档，避免单篇页顶部出现期号统计行
    if (isPaperRoute()) {
      if (dom.itemPage && dom.itemPage.parentNode === dom.view) dom.view.insertBefore(dom.home, dom.itemPage);
      else dom.view.appendChild(dom.home);
    }

    state.nextBefore = (data.next_before === undefined) ? null : data.next_before;
    state.exhausted = state.nextBefore === null;
    state.started = true;
    renderMasthead();
    hideImages(dom.home);
    setMoreNote();
    if (state.pendingInbox) { tryPendingInbox(); return; }
    if (state.pendingRestore) {
      state.pendingRestore = false;
      var memo = state.scrollMemo;
      requestAnimationFrame(function () { window.scrollTo(0, memo); });
    }
  }).catch(function (err) {
    state.firstLoading = false;
    if (note.parentNode) note.parentNode.removeChild(note);
    var p = document.createElement('p');
    p.className = 'err-page';
    p.innerHTML = '今天的报纸没取上来：' + esc(err.message) +
      ' <button type="button" class="btn mini" data-act="retry">再试一次</button>';
    if (dom.itemPage && dom.itemPage.parentNode === dom.view) dom.view.insertBefore(p, dom.itemPage);
    else dom.view.appendChild(p);
  });
}

function loadMore() {
  if (state.loadingMore || state.exhausted || !state.started) return;
  if (!state.nextBefore) { state.exhausted = true; setMoreNote(); return; }
  state.loadingMore = true;
  if (dom.moreBtn) dom.moreBtn.disabled = true;
  dom.moreNote.textContent = '正在取更早的一期…';
  var before = state.nextBefore;
  api.editions(before, 2).then(function (data) {
    state.loadingMore = false;
    if (dom.moreBtn) dom.moreBtn.disabled = false;
    (data.editions || []).forEach(appendEdition);
    state.nextBefore = (data.next_before === undefined) ? null : data.next_before;
    state.exhausted = state.nextBefore === null;
    setMoreNote();
  }).catch(function (err) {
    state.loadingMore = false;
    if (dom.moreBtn) { dom.moreBtn.disabled = false; dom.moreBtn.hidden = false; }
    dom.moreNote.innerHTML = '更早的一期没取上来：' + esc(err.message) +
      ' <button type="button" class="btn mini" data-act="retry-more">再试一次</button>';
  });
}

/** 把单篇页 / 管道页 / 登录状态页 / 全部来源页从文档里摘掉 */
function clearAllPages() {
  [dom.itemPage, dom.pipePage, dom.authPage, dom.archivePage].forEach(function (n) {
    if (n && n.parentNode) n.parentNode.removeChild(n);
  });
  dom.itemPage = null;
  dom.pipePage = null;
  dom.authPage = null;
  dom.archivePage = null;
}

function showHome() {
  if (!dom.home) { loadFirstPage(); return; }
  if (!dom.home.parentNode) dom.view.appendChild(dom.home);
  clearAllPages();
  dom.masthead.hidden = false;
  dom.moreWrap.hidden = false;
  hideImages(dom.home);
  setMoreNote();
  renderMasthead();
  if (state.pendingRestore) {
    state.pendingRestore = false;
    var memo = state.scrollMemo;
    requestAnimationFrame(function () { window.scrollTo(0, memo); });
  }
}

/* ---------------------------------------------------------
 * 7. 单篇页
 * ------------------------------------------------------- */

function itemMetaLine(item) {
  // 单篇页只留 来源·作者·时间，不带报纸的期号与统计
  return '<p class="meta">' + esc(item.source_label || item.source || '—') +
    '<span class="dot">·</span>' + bylineHTML(item) +
    '<span class="dot">·</span>' + esc(fmtTime(item.published_at)) +
    '</p>';
}

function tagsHTML(item) {
  var out = '';
  if (item.topic) out += '<p class="meta">主题：' + esc(item.topic) + '</p>';
  var tags = item.style_tags || [];
  if (tags.length) {
    out += '<p class="meta" style="margin-top:4px">文风：' + tags.map(function (t) {
      return '<span class="tag">' + esc(t) + '</span>';
    }).join('') + '</p>';
  }
  return out;
}

/** 「同一来源当天的其它内容 →」：跳到 #/archive?source=…&date=…（契约第 6.2 节） */
function sameDayHTML(item) {
  if (!item.same_day_url) return '';
  return '<p class="same-day"><a href="' + esc(item.same_day_url) + '">同一来源当天的其它内容 →</a></p>';
}

/** 综述单篇页：正文 + 全部成员原帖按时间平铺（作者、时间、正文、原帖链接） */
function digestBodyHTML(item) {
  var members = (item.members_detail && item.members_detail.length)
    ? item.members_detail.slice()
    : (item.members || []).slice();
  members.sort(function (a, b) {
    return String((a && a.published_at) || '').localeCompare(String((b && b.published_at) || ''));
  });
  if (!members.length) return '';
  var rows = members.map(function (m) {
    if (!m || typeof m === 'string') {
      return '<article class="dg-post"><div class="dg-post-meta">' +
        '<a class="linkout" href="#/item/' + encodeURIComponent(String(m)) + '">' + esc(String(m)) + '</a>' +
        '</div></article>';
    }
    return '<article class="dg-post">' +
      '<div class="dg-post-meta">' +
        '<span class="dg-author">' + esc(m.author_label || '匿名') + '</span>' +
        '<span class="dot">·</span>' +
        '<span class="dg-time">' + esc(fmtTime(m.published_at)) + '</span>' +
        (extUrl(m.url) ? '<a class="linkout" href="' + esc(extUrl(m.url)) + '" target="_blank" rel="noopener noreferrer">原帖 ↗</a>' : '') +
      '</div>' +
      '<p class="dg-text">' + esc(m.one_liner || m.lede || m.text || '') + '</p>' +
    '</article>';
  }).join('');
  return '<div class="sec-head"><h2>全部原帖</h2><span class="sec-note">' + members.length + ' 条 · 按时间</span></div>' +
    '<div class="dg-posts">' + rows + '</div>';
}

/** 单篇页里新知判定的完整说明（why 一定显示，counter 另显示 against） */
function noveltyDetailHTML(item) {
  var nv = item.novelty;
  if (!nv || !nv.kind) return '';
  var label = NOVELTY_LABEL[nv.kind] || nv.kind;
  return '<p class="why nov-detail">新知判定 · ' + esc(label) + '：' + esc(nv.why || '—') +
    (nv.kind === 'counter' && nv.against ? '<br>这条在挑战你的：' + esc(nv.against) : '') + '</p>';
}

/** 机会三行（单篇页也显示） */
function opportunityDetailHTML(item) {
  var op = item.opportunity;
  if (!op) return '';
  return '<div class="sec-head"><h2>机会</h2><span class="sec-note">不含个性化投资建议</span></div>' +
    '<dl class="oppo-rows">' +
      '<div class="oppo-row"><dt>若为真：</dt><dd>' + esc(op.if_true || '—') + '</dd></div>' +
      '<div class="oppo-row"><dt>证伪信号：</dt><dd>' + esc(op.kill_signal || '—') + '</dd></div>' +
      '<div class="oppo-row"><dt>时间尺度：</dt><dd>' + esc(op.horizon || '—') + '</dd></div>' +
    '</dl>';
}

function showItemPage(itemId, from) {
  var samePage = dom.itemPage && dom.itemPage.parentNode &&
    dom.itemPage.getAttribute('data-item-id') === itemId;
  // open_item 只在这里发一次：重复点同一条不重复记
  if (!samePage) trackItemOpen(from || 'paper', itemId, state.pendingOpenSection || null);
  state.pendingOpenSection = '';
  if (dom.itemPage && dom.itemPage.parentNode) trackItemLeave();  // 篇→篇直跳也要结算上一篇
  state.scrollMemo = window.scrollY || 0;
  state.pendingRestore = true;
  if (!dom.home) loadFirstPage();
  // 单篇页顶部不要出现报纸的期号统计行：把版面从文档里摘掉，只留「← 回到报纸」
  if (dom.home && dom.home.parentNode) dom.home.parentNode.removeChild(dom.home);
  [dom.pipePage, dom.authPage, dom.archivePage].forEach(function (n) {
    if (n && n.parentNode) n.parentNode.removeChild(n);
  });
  dom.pipePage = null;
  dom.authPage = null;
  dom.archivePage = null;
  dom.masthead.hidden = true;
  dom.moreWrap.hidden = true;
  renderAuthBar();
  renderRiskBar();
  renderFlash();

  if (!dom.itemPage) {
    dom.itemPage = document.createElement('article');
    dom.itemPage.className = 'itempage';
  }
  // 10-05 验收 漏报-1：每次进页都写 data-item-id，篇对篇直跳时停留计时才记给当前条目而不是上一篇
  dom.itemPage.setAttribute('data-item-id', itemId);
  dom.itemPage.innerHTML = '<p class="backlink"><a href="#/">← 回到报纸</a></p><p class="loading">正在取全文…</p>';
  if (!dom.itemPage.parentNode) dom.view.appendChild(dom.itemPage);
  window.scrollTo(0, 0);

  state.itemEnterFg = trackFgNow();
  state.itemDwellSent = false;
  state.itemMaxScroll = 0;
  state.itemFulltextChars = 0;

  var token = ++state.itemSeq;  // 10-05 验收 H201：请求带递增序号，晚到的旧响应一律丢弃
  api.item(itemId).then(function (item) {
    // 10-05 验收 H201：序号或路由对不上（用户已切走）就不写页面，停留计时才记给当前显示条目
    if (token !== state.itemSeq) return;
    var r = parseHash();
    if (r.name !== 'item' || r.id !== itemId) return;
    registerItem(item, item.edition_date || null);
    var full = item.fulltext;
    var paras = (typeof full === 'string' && full.trim())
      ? full.split(/\n\s*\n|\n/).filter(function (p) { return p.trim(); })
          .map(function (p) { return '<p>' + esc(p.trim()) + '</p>'; }).join('')
      : '<p class="notice">未抓到全文。</p>';

    state.itemFulltextChars = (typeof full === 'string') ? full.length : 0;

    // 音视频条目：全文区标题改为「转录全文」（契约第 6.4 节）
    var isMedia = item.kind === 'media';
    var isDigest = item.kind === 'digest';
    var fullHead = isMedia ? '转录全文' : '全文';
    var fullNote = '';
    if (isMedia) {
      fullNote = (item.media && item.media.type === 'video' ? '视频' : '音频');
      var mins = fmtMinutes(item.media && item.media.duration_s);
      if (mins) fullNote += ' · ' + mins;
      if (item.media && item.media.transcript_chars) fullNote += ' · 转录 ' + item.media.transcript_chars + ' 字';
    } else {
      fullNote = item.lang || '';
    }
    // 10-07：原文没抓到时后端回落到采集时带回的正文，新闻源可能只是摘要
    if (item.fulltext_source === 'feed') fullNote = (fullNote ? fullNote + ' · ' : '') + '原文未抓到，以下为采集时带回的正文（新闻源可能只是摘要）';

    dom.itemPage.setAttribute('data-item-id', item.item_id);
    dom.itemPage.innerHTML =
      '<p class="backlink"><a href="#/" data-act="back">← 回到报纸</a></p>' +
      titleHTML(item) +
      itemMetaLine(item) +
      imgHTML(item) +
      (item.lede ? '<p class="lede">' + esc(item.lede) + '</p>' : '') +
      quotesHTML(item) +
      (item.backstory ? '<p class="backstory">' + esc(item.backstory) + '</p>' : '') +
      (item.so_what ? '<p class="sowhat">' + esc(item.so_what) + '</p>' : '') +
      claimHTML(item) +
      whyHTML(item) +
      noveltyDetailHTML(item) +
      verificationHTML(item) +
      (item.verification && item.verification.note
        ? '<p class="why">核实依据：' + esc(item.verification.note) +
          (item.verification.named && item.verification.named.length
            ? '（具名：' + esc(item.verification.named.join('、')) + '）' : '') + '</p>'
        : '') +
      tagsHTML(item) +
      opportunityDetailHTML(item) +
      (isDigest ? digestBodyHTML(item) : '') +
      // 10-05 验收 漏报-3：没有原文链接时不渲染 href="#" 的「阅读原文」（同 1150 follow-go 的写法），
      // 点了会打开报纸自身并记一条 open_original
      (extUrl(item.url)
        ? '<p style="margin-top:20px"><a class="btn orig" href="' + esc(extUrl(item.url)) + '" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a></p>'
        : '<p style="margin-top:20px"><span class="btn orig is-disabled" title="这条没有原文链接">阅读原文（无链接）</span></p>') +
      sameDayHTML(item) +
      fbHTML(item, true) +
      '<div class="sec-head"><h2>' + esc(fullHead) + '</h2><span class="sec-note">' + esc(fullNote) + '</span></div>' +
      '<div class="fulltext">' + paras + '</div>' +
      '<p class="reading">读完离开即可，我会记下你在这里花了多久。</p>';
    hideImages(dom.itemPage);
    if (state.itemDwellSent) return;
  }).catch(function (err) {
    if (token !== state.itemSeq) return;  // 10-05 验收 H201：旧请求的失败也不许写进新页面
    var r2 = parseHash();
    if (r2.name !== 'item' || r2.id !== itemId) return;
    dom.itemPage.innerHTML = '<p class="backlink"><a href="#/">← 回到报纸</a></p>' +
      '<p class="err-page">这篇没打开：' + esc(err.message) + '</p>';
  });
}

function leaveItemPage() { trackItemLeave(); }

/* ---------------------------------------------------------
 * 8. 管道页
 * ------------------------------------------------------- */

function trustBarHTML(v) {
  var pct = Math.max(0, Math.min(1, Number(v) || 0)) * 100;
  return '<span class="trust">' + fmtTrust(v) + '</span><span class="bar"><i style="width:' + pct.toFixed(0) + '%"></i></span>';
}

function sourceRowHTML(s) {
  return '<tr data-kind="source" data-key="' + esc(s.source) + '"' + (s.muted ? ' class="muted"' : '') + '>' +
    '<td>' + esc(s.label || s.source) + (s.boosted ? ' <span class="tag">加权</span>' : '') + '</td>' +
    '<td>' + trustBarHTML(s.trust) + '</td>' +
    '<td class="dimname">' + esc(s.items_30d) + ' / ' + esc(s.on_paper_30d) + '</td>' +
    '<td><div class="pipe-btns">' +
      '<button type="button" class="btn mini" data-act="pipe" data-kind="source" data-action="' + (s.muted ? 'unmute' : 'mute') + '">' + (s.muted ? '取消屏蔽' : '屏蔽') + '</button>' +
      '<button type="button" class="btn mini" data-act="pipe" data-kind="source" data-action="' + (s.boosted ? 'unboost' : 'boost') + '">' + (s.boosted ? '取消加权' : '加权') + '</button>' +
      '<button type="button" class="btn mini" data-act="pipe" data-kind="source" data-action="reset">重置</button>' +
    '</div><div class="pipe-flash" data-role="flash"></div></td>' +
  '</tr>';
}

function authorRowHTML(a) {
  return '<tr data-kind="author" data-key="' + esc(a.author_key) + '"' + (a.muted ? ' class="muted"' : '') + '>' +
    '<td>' + esc(a.label || a.author_key) + '</td>' +
    '<td>' + trustBarHTML(a.trust) + '</td>' +
    '<td class="dimname">👍 ' + esc(a.n_up) + ' · 👎 ' + esc(a.n_down) + '</td>' +
    '<td><div class="pipe-btns">' +
      '<button type="button" class="btn mini" data-act="pipe" data-kind="author" data-action="' + (a.muted ? 'unmute' : 'mute') + '">' + (a.muted ? '取消屏蔽' : '屏蔽') + '</button>' +
      '<button type="button" class="btn mini" data-act="pipe" data-kind="author" data-action="reset">重置</button>' +
    '</div><div class="pipe-flash" data-role="flash"></div></td>' +
  '</tr>';
}

function recentRowHTML(r) {
  var d = null;
  for (var i = 0; i < DIMS.length; i++) if (DIMS[i].dim === r.dim) d = DIMS[i];
  var dir = Number(r.value) > 0 ? '👍' : (Number(r.value) < 0 ? '👎' : '—');
  return '<tr><td class="dimname">' + esc(fmtTime(r.ts)) + '</td>' +
    '<td>' + esc(r.title || r.item_id) + '</td>' +
    '<td class="dimname">' + esc(d ? d.name : (r.dim || '')) + '</td>' +
    '<td>' + dir + '</td></tr>';
}

var ADJ_KIND = { source: '信息源', author: '作者', topic: '主题' };

function adjRowHTML(a) {
  var rev = !!a.reverted;
  var kindLabel = ADJ_KIND[a.kind] || a.kind || '—';
  var delta = (a.before === null || a.before === undefined || a.after === null || a.after === undefined)
    ? '—' : (fmtTrust(a.before) + '→' + fmtTrust(a.after));
  return '<tr data-adj-id="' + esc(a.id) + '"' + (rev ? ' class="is-reverted"' : '') + '>' +
    '<td class="dimname adj-when">' + esc(fmtTime(a.ts)) + '</td>' +
    '<td class="adj-obj"><span class="adj-kind">' + esc(kindLabel) + '</span> ' + esc(a.label || a.key || '—') + '</td>' +
    '<td class="adj-delta">' + esc(delta) + '</td>' +
    '<td class="adj-reason">' + esc(a.reason || '—') + '</td>' +
    '<td>' + (rev
      ? '<span class="adj-flag">已撤销</span>'
      : '<button type="button" class="btn mini" data-act="revert-adj">撤销</button>') +
      '<div class="pipe-flash" data-role="flash"></div></td>' +
  '</tr>';
}

function showAdjustments(host) {
  var sec = document.createElement('section');
  sec.className = 'adjustments';
  sec.innerHTML = '<div class="sec-head"><h2>AI 调整记录</h2><span class="sec-note">自动小调，每条可撤销</span></div>' +
    '<div class="pipe-scroll-x"><table class="pipe-tbl adj-tbl"><thead><tr>' +
    '<th>时间</th><th>对象</th><th>调整</th><th>理由</th><th>操作</th>' +
    '</tr></thead><tbody><tr><td colspan="5" class="dimname">正在读调整记录…</td></tr></tbody></table></div>';
  host.innerHTML = '';          // 容器复用：先清干净再放新的
  host.appendChild(sec);

  api.adjustments(50).then(function (data) {
    var list = data && data.adjustments ? data.adjustments : [];
    var tb = $('tbody', sec);
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="5" class="dimname">还没有调整记录。</td></tr>';
      return;
    }
    tb.innerHTML = list.map(adjRowHTML).join('');
  }).catch(function (err) {
    var tb2 = $('tbody', sec);
    if (tb2) tb2.innerHTML = '<tr><td colspan="5" class="err-page">调整记录读不出来：' + esc(err.message) + '</td></tr>';
  });
}

/* 撤销一条 AI 调整 */
function doRevertAdj(btn) {
  var tr = btn.closest('tr');
  if (!tr || btn.disabled) return;
  var id = tr.getAttribute('data-adj-id');
  btn.disabled = true;
  api.revertAdjustment(id).then(function (res) {
    tr.classList.add('is-reverted');
    var flash = $('[data-role="flash"]', tr);
    var msg = '已撤销';
    if (res && res.before !== undefined && res.after !== undefined &&
        res.before !== null && res.after !== null) {
      msg = '已撤销 ' + fmtTrust(res.before) + '→' + fmtTrust(res.after);
    }
    if (flash) { flash.textContent = msg; flash.style.color = ''; }
    var cell = btn.parentNode;
    if (cell) cell.innerHTML = '<span class="adj-flag">已撤销</span><div class="pipe-flash" data-role="flash">' + esc(msg) + '</div>';
  }).catch(function (err) {
    btn.disabled = false;
    var f = $('[data-role="flash"]', tr);
    if (f) { f.textContent = '没撤成：' + err.message; f.style.color = 'var(--danger)'; }
  });
}

/* ---------- 管道页 · 行程（契约 v4 第 9 节） ---------- */

function tripRowHTML(t) {
  return '<tr data-trip-id="' + esc(t.trip_id || '') + '">' +
    '<td>' + esc(t.place || '—') + '</td>' +
    '<td class="dimname">' + esc(t.start_date || '—') + ' → ' + (t.end_date ? esc(t.end_date) : '未填（自开始日起监测 14 天）') + '</td>' +
    '<td class="trip-note">' + esc(t.note || '—') + '</td>' +
    '<td><button type="button" class="btn mini" data-act="trip-del" data-trip="' + esc(t.trip_id || '') + '">删除</button>' +
      '<div class="pipe-flash" data-role="flash"></div></td>' +
  '</tr>';
}

function showTrips(host) {
  var sec = document.createElement('section');
  sec.className = 'trips';
  sec.innerHTML = '<h2>行程</h2>' +
    '<p class="dimname">出发前 30 天起，每期给你一份行前风险简报。结束日期可不填：留空=从开始日起持续监测 14 天，之后自动停。</p>' +
    '<div class="pipe-scroll-x"><table class="pipe-tbl trip-tbl"><thead><tr>' +
    '<th>地点</th><th>起止</th><th>备注</th><th>操作</th>' +
    '</tr></thead><tbody><tr><td colspan="4" class="dimname">正在读行程…</td></tr></tbody></table></div>' +
    '<form class="trip-form" data-role="tripform">' +
      '<label class="tf-lab">地点<input type="text" name="place" data-role="place" placeholder="例如：京都" autocomplete="off"></label>' +
      '<label class="tf-lab">开始<input type="date" name="start_date" data-role="start"></label>' +
      '<label class="tf-lab">结束（留空=持续监测 14 天）<input type="date" name="end_date" data-role="end" title="留空=从开始日起持续监测 14 天"></label>' +
      '<label class="tf-lab tf-note">备注<input type="text" name="note" data-role="note" placeholder="可选" autocomplete="off"></label>' +
      '<button type="submit" class="btn" data-act="trip-add">加一个行程</button>' +
    '</form>' +
    '<div class="pipe-flash" data-role="tripflash"></div>';
  host.appendChild(sec);

  var tb = $('tbody', sec);
  return api.trips().then(function (data) {
    var list = (data && data.trips) ? data.trips : [];
    if (!tb || !tb.parentNode) return;
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="4" class="dimname">还没有安排行程。</td></tr>';
      return;
    }
    tb.innerHTML = list.map(tripRowHTML).join('');
  }).catch(function (err) {
    if (tb) tb.innerHTML = '<tr><td colspan="4" class="err-page">行程读不出来：' + esc(err.message) + '</td></tr>';
  });
}

/** 提交行程表单 */
function addTrip(sec) {
  if (state.tripBusy) return;
  var place = $('[data-role="place"]', sec);
  var start = $('[data-role="start"]', sec);
  var end = $('[data-role="end"]', sec);
  var note = $('[data-role="note"]', sec);
  var flash = $('[data-role="tripflash"]', sec);
  var p = place ? String(place.value || '').trim() : '';
  var s = start ? String(start.value || '').trim() : '';
  var e = end ? String(end.value || '').trim() : '';
  var n = note ? String(note.value || '').trim() : '';
  if (!p) { if (flash) { flash.textContent = '得先写地点。'; flash.style.color = 'var(--danger)'; } return; }
  state.tripBusy = true;
  var btn = $('[data-act="trip-add"]', sec);
  if (btn) btn.disabled = true;
  api.tripAdd(p, s, e, n).then(function (res) {
    state.tripBusy = false;
    if (btn) btn.disabled = false;
    if (flash) { flash.textContent = '已加上 ' + p; flash.style.color = ''; }
    var tb = $('tbody', sec);
    if (tb) {
      var empty = $('td[colspan]', tb);
      if (empty) empty.textContent = '正在读行程…';
      return api.trips().then(function (data) {
        if (!tb || !tb.parentNode) return;
        var list = (data && data.trips) ? data.trips : [];
        tb.innerHTML = list.length
          ? list.map(tripRowHTML).join('')
          : '<tr><td colspan="4" class="dimname">还没有安排行程。</td></tr>';
      });
    }
  }).catch(function (err) {
    state.tripBusy = false;
    if (btn) btn.disabled = false;
    if (flash) { flash.textContent = '没加上：' + err.message; flash.style.color = 'var(--danger)'; }
  });
}

/** 删除行程 */
function delTrip(btn) {
  var id = btn.getAttribute('data-trip');
  var tr = btn.closest('tr');
  if (!id || !tr || btn.disabled) return;
  btn.disabled = true;
  api.tripDelete(id).then(function () {
    if (tr.parentNode) tr.parentNode.removeChild(tr);
  }).catch(function (err) {
    btn.disabled = false;
    var f = $('[data-role="flash"]', tr);
    if (f) { f.textContent = '没删掉：' + err.message; f.style.color = 'var(--danger)'; }
  });
}

/* ---------- 管道页 · 推荐关注（列出全部建议及状态） ---------- */

function showFollowAll(host) {
  var sec = document.createElement('section');
  sec.className = 'follow-all';
  sec.innerHTML = '<h2>推荐关注</h2>' +
    '<p class="dimname">系统被动发现的账号。关注由你自己去平台点；回来后系统看到它出现在你的关注流里就算「已关注」。</p>' +
    '<div class="follow-list" data-role="followall"><p class="loading">正在读建议…</p></div>';
  host.appendChild(sec);
  var box = $('[data-role="followall"]', sec);
  return api.followSuggestions().then(function (data) {
    if (!box || !box.parentNode) return;
    var list = (data && data.suggestions) ? data.suggestions : [];
    if (!list.length) { box.innerHTML = '<p class="dimname">这周没有新的推荐。</p>'; return; }
    box.innerHTML = list.map(function (sg) { return followRowHTML(sg, false); }).join('');
  }).catch(function (err) {
    if (box) box.innerHTML = '<p class="err-page">推荐读不出来：' + esc(err.message) + '</p>';
  });
}

/* ---------- 采集健康（10-05 验收 C1：后端 health 字段 → 管道页顶部一栏） ---------- */

function healthStatusLabel(h) {
  var parts = [];
  if (h.status && h.status !== 'ok') parts.push('降级');
  if ((h.errors || []).length) parts.push('有失败');
  var streak = Number(h.zero_streak) || 0;
  if (streak >= 3) parts.push('连续 ' + streak + ' 轮 0 条');
  if (h.stale) parts.push('超过 48 小时没跑');
  return parts.join(' / ') || '有异常';
}

function healthRowHTML(h) {
  var errors = h.errors || [];
  var shown = errors.slice(0, 3);
  var rest = errors.length - shown.length;
  var errs = '';
  if (shown.length) {
    errs = '<ul class="health-errors">' + shown.map(function (e) { return '<li>' + esc(e) + '</li>'; }).join('') +
      (rest > 0 ? '<li class="health-more">等 ' + rest + ' 条</li>' : '') + '</ul>';
  }
  return '<li class="health-row">' +
    '<span class="health-adapter">' + esc(h.adapter) + '</span>' +
    '<span class="health-status">' + esc(healthStatusLabel(h)) + '</span>' +
    '<span class="health-time">' + esc(fmtTime(h.run_at_utc) || '—') + '</span>' +
    errs + '</li>';
}

function healthPanelHTML(health) {
  var rows = health || [];
  var body = rows.length
    ? '<ul class="health-list">' + rows.map(healthRowHTML).join('') + '</ul>'
    : '<p class="health-ok">所有采集器最近一轮正常</p>';
  return '<div class="health-panel"><h2>采集健康</h2>' + body + '</div>';
}

function showPipeline() {
  leaveItemPage();
  state.pendingRestore = false;
  if (dom.home && dom.home.parentNode) dom.home.parentNode.removeChild(dom.home);
  [dom.itemPage, dom.authPage, dom.archivePage].forEach(function (n) {
    if (n && n.parentNode) n.parentNode.removeChild(n);
  });
  dom.itemPage = null;
  dom.authPage = null;
  dom.archivePage = null;
  dom.masthead.hidden = true;
  dom.moreWrap.hidden = true;
  renderAuthBar();
  renderRiskBar();
  renderFlash();

  if (!dom.pipePage) {
    dom.pipePage = document.createElement('section');
    dom.pipePage.className = 'pipe';
  }
  // 「AI 调整记录」固定占最上面：单独一个容器，重绘下面几块时不动它
  if (!dom.adjBox) {
    dom.adjBox = document.createElement('div');
    dom.adjBox.className = 'adj-box';
  }
  dom.pipePage.innerHTML = '';
  dom.pipePage.appendChild(dom.adjBox);
  var pipeBody = document.createElement('div');
  pipeBody.className = 'pipe-body';
  pipeBody.innerHTML = '<h2 style="margin-top:0">管道</h2>' +
    '<p class="pipe-top-links"><a href="#/auth">登录状态</a> · <a href="#/archive">全部来源</a></p>' +
    '<p class="loading">正在读管道…</p>';
  dom.pipePage.appendChild(pipeBody);
  if (!dom.pipePage.parentNode) dom.view.appendChild(dom.pipePage);
  showAdjustments(dom.adjBox);
  window.scrollTo(0, 0);

  api.pipeline().then(function (p) {
    state.pipeline = p;
    pipeBody.innerHTML =
      '<h2 style="margin-top:0">管道</h2>' +
      '<p class="pipe-top-links"><a href="#/auth">登录状态</a> · <a href="#/archive">全部来源</a></p>' +
      healthPanelHTML(p.health) +   // 10-05 验收 C1：采集健康放在管道页顶部
      '<p class="pipe-dir">画像目录：' + esc(p.profile_dir || '—') + '</p>' +
      '<h2>信息源</h2>' +
      '<div class="pipe-scroll-x"><table class="pipe-tbl"><thead><tr><th>来源</th><th>信任</th><th>近 30 天 入库/上版</th><th>操作</th></tr></thead><tbody>' +
        (p.sources || []).map(sourceRowHTML).join('') + '</tbody></table></div>' +
      '<h2>作者</h2>' +
      '<div class="pipe-scroll-x"><table class="pipe-tbl"><thead><tr><th>作者</th><th>信任</th><th>👍 / 👎</th><th>操作</th></tr></thead><tbody>' +
        (p.authors || []).map(authorRowHTML).join('') + '</tbody></table></div>' +
      '<h2>最近反馈</h2>' +
      '<div class="pipe-scroll-x"><table class="pipe-tbl"><thead><tr><th>时间</th><th>标题</th><th>维度</th><th>方向</th></tr></thead><tbody>' +
        (p.recent || []).map(recentRowHTML).join('') + '</tbody></table></div>';
    showFollowAll(pipeBody);
    showTrips(pipeBody);
  }).catch(function (err) {
    pipeBody.innerHTML = '<h2 style="margin-top:0">管道</h2><p class="err-page">管道读不出来：' + esc(err.message) + '</p>';
  });
}

function applyPipeResult(tr, res, action) {
  var kind = tr.getAttribute('data-kind');
  var flash = $('[data-role="flash"]', tr);
  var before = res ? res.before : null, after = res ? res.after : null;
  var hasB = before !== null && before !== undefined;
  var hasA = after !== null && after !== undefined;
  var delta = (hasB && hasA) ? (fmtTrust(before) + '→' + fmtTrust(after)) : '';
  var txt;
  if (action === 'mute' || action === 'unmute') {
    // 屏蔽与否以「点了哪个」为准：后端返回的 muted 只作参考，
    // 否则 mock 恒回 muted:false 时，屏蔽后行不会变灰。
    tr.classList.toggle('muted', action === 'mute');
    txt = delta || (action === 'mute' ? '已屏蔽' : '已取消屏蔽');
  } else if (action === 'reset') {
    txt = delta || '已重置';
  } else {
    txt = delta || (action === 'boost' ? '已加权' : '已取消加权');
  }
  if (flash) { flash.textContent = txt; }
  // 就地更新按钮文案 / 数值
  var p = state.pipeline;
  if (p && kind) {
    var list = kind === 'source' ? p.sources : p.authors;
    var key = tr.getAttribute('data-key');
    var row = null;
    for (var i = 0; i < list.length; i++) {
      if ((kind === 'source' ? list[i].source : list[i].author_key) === key) { row = list[i]; break; }
    }
    if (row) {
      if (action === 'mute') row.muted = true;
      else if (action === 'unmute') row.muted = false;
      else if (action === 'boost') row.boosted = true;
      else if (action === 'unboost') row.boosted = false;
      if (action === 'reset' && hasA) row.trust = after;
      if ((action === 'boost' || action === 'unboost') && hasA) row.trust = after;
      var fresh = kind === 'source' ? sourceRowHTML(row) : authorRowHTML(row);
      var tmp = document.createElement('tbody');
      tmp.innerHTML = fresh;
      var newTr = tmp.firstChild;
      var keep = flash ? flash.textContent : txt;
      var muted = tr.classList.contains('muted');
      tr.innerHTML = newTr.innerHTML;
      tr.className = newTr.className || '';
      if (muted) tr.classList.add('muted');
      var f2 = $('[data-role="flash"]', tr);
      if (f2) f2.textContent = keep;
    }
  }
}

/* ---------- 到期结算：三个按钮调 POST /api/claims/resolve ---------- */

function resolveClaim(btn) {
  var claimId = btn.getAttribute('data-claim');
  var outcome = btn.getAttribute('data-outcome');
  var row = btn.closest('.settle-row');
  if (!claimId || !row || btn.disabled) return;
  $$('[data-act="resolve"]', row).forEach(function (b) { b.disabled = true; });
  api.claimResolve(claimId, outcome).then(function () {
    $$('[data-act="resolve"]', row).forEach(function (b) {
      b.disabled = false;
      b.classList.toggle('on', b.getAttribute('data-outcome') === outcome);
    });
    var f = $('[data-role="flash"]', row);
    if (f) { f.textContent = '记下了：' + (OUTCOME_LABEL[outcome] || outcome); f.style.color = ''; }
  }).catch(function (err) {
    $$('[data-act="resolve"]', row).forEach(function (b) { b.disabled = false; });
    var f2 = $('[data-role="flash"]', row);
    if (f2) { f2.textContent = '没记上：' + err.message; f2.style.color = 'var(--danger)'; }
  });
}

/* ---------------------------------------------------------
 * 9. 全部来源 #/archive（契约 v4 第 6.2 节）
 *    左侧来源列表（近 7 天条数降序）+ 右侧条目流；
 *    顶部搜索框 q 与日期选择；往下滚自动翻页（next_before）；单一滚动容器。
 * ------------------------------------------------------- */

function archiveState() { return state.archive; }

function archiveHashQuery(source, date, q) {
  var parts = [];
  if (source) parts.push('source=' + encodeURIComponent(source));
  if (date) parts.push('date=' + encodeURIComponent(date));
  if (q) parts.push('q=' + encodeURIComponent(q));
  return parts.length ? '?' + parts.join('&') : '';
}

/** 解析 #/archive?source=…&date=…&q=… */
function parseArchiveQuery() {
  var h = location.hash || '';
  var qi = h.indexOf('?');
  if (qi < 0) return { source: '', date: '', q: '' };
  var out = { source: '', date: '', q: '' };
  h.slice(qi + 1).split('&').forEach(function (kv) {
    var i = kv.indexOf('=');
    if (i < 0) return;
    var k = kv.slice(0, i), v = kv.slice(i + 1);
    try { v = decodeURIComponent(v); } catch (e) { /* 保留原值 */ }
    if (k === 'source' || k === 'date' || k === 'q') out[k] = v;
  });
  return out;
}

/** 左侧来源列表的一行：条数 + 最近时间 */
function arcSourceHTML(s, active) {
  return '<li><button type="button" class="arc-src' + (active ? ' on' : '') + '" data-act="arc-src" data-source="' +
    esc(s.source) + '" data-date="' + esc(s.date || '') + '">' +
    '<span class="arc-src-label">' + esc(s.label || s.source) + '</span>' +
    '<span class="arc-src-n">' + esc(s.count_7d) + ' 条</span>' +
    '<span class="arc-src-t">' + esc(fmtTime(s.last_at) || '—') + '</span>' +
  '</button></li>';
}

/** 右侧一条：标题、来源、时间、one_liner；已上过报纸的标「见 MM-DD 报」 */
function arcItemHTML(it, rank) {
  var onPaper = it.on_paper ? ('见 ' + String(it.on_paper).slice(5) + ' 报') : '';
  return '<article class="arc-item" data-item-id="' + esc(it.item_id) + '" data-sec="archive" data-rank="' +
    (Number(rank) || 0) + '" tabindex="-1">' +
    '<div class="arc-line">' +
      '<a class="arc-title" href="#/item/' + encodeURIComponent(it.item_id) + '">' + esc(it.title) + '</a>' +
      (extUrl(it.url) ? '<a class="linkout" href="' + esc(extUrl(it.url)) + '" target="_blank" rel="noopener noreferrer">↗</a>' : '') +
    '</div>' +
    '<p class="arc-meta">' + esc(it.source_label || it.source || '—') +
      (it.author_label ? '<span class="dot">·</span>' + esc(it.author_label) : '') +
      '<span class="dot">·</span>' + esc(fmtTime(it.published_at)) +
      (onPaper ? '<span class="arc-onpaper">' + esc(onPaper) + '</span>' : '') +
    '</p>' +
    (it.one_liner ? '<p class="arc-one">' + esc(it.one_liner) + '</p>' : '') +
  '</article>';
}

function arcItemsHTML(list, startRank) {
  return list.map(function (it, i) { return arcItemHTML(it, startRank + i + 1); }).join('');
}

function showArchive() {
  leaveItemPage();
  state.pendingRestore = false;
  if (dom.home && dom.home.parentNode) dom.home.parentNode.removeChild(dom.home);
  [dom.itemPage, dom.pipePage, dom.authPage].forEach(function (n) {
    if (n && n.parentNode) n.parentNode.removeChild(n);
  });
  dom.itemPage = null;
  dom.pipePage = null;
  dom.authPage = null;
  dom.masthead.hidden = true;
  dom.moreWrap.hidden = true;
  renderAuthBar();
  renderRiskBar();
  renderFlash();

  var qy = parseArchiveQuery();
  state.archive = {
    source: qy.source, date: qy.date, q: qy.q,
    nextBefore: undefined, exhausted: false, loading: false,
    count: 0, sources: []
  };

  if (!dom.archivePage) {
    dom.archivePage = document.createElement('section');
    dom.archivePage.className = 'archive';
  }
  dom.archivePage.innerHTML =
    '<p class="backlink"><a href="#/">← 回到报纸</a></p>' +
    '<h2>全部来源</h2>' +
    '<form class="arc-bar" data-role="arcbar">' +
      '<input type="search" class="arc-q" data-role="q" placeholder="搜标题与正文" value="' + esc(qy.q) + '" autocomplete="off">' +
      '<input type="date" class="arc-date" data-role="date" value="' + esc(qy.date) + '">' +
      '<button type="submit" class="btn" data-act="arc-go">搜索</button>' +
      '<button type="button" class="btn" data-act="arc-clear">清空</button>' +
    '</form>' +
    '<div class="arc-grid">' +
      '<aside class="arc-side"><div class="arc-side-head">来源 · 近 7 天</div>' +
        '<ul class="arc-sources" data-role="sources"><li class="dimname">正在读来源…</li></ul></aside>' +
      '<div class="arc-main">' +
        '<div class="arc-status" data-role="status">正在取条目…</div>' +
        '<div class="arc-items" data-role="items"></div>' +
        '<div class="arc-more-wrap"><button type="button" class="btn" data-act="arc-more" hidden>往下翻页</button></div>' +
      '</div>' +
    '</div>';
  if (!dom.archivePage.parentNode) dom.view.appendChild(dom.archivePage);
  window.scrollTo(0, 0);

  loadArchivePage(null, true);
  loadArchiveSources();
}

function loadArchiveSources() {
  return api.archiveSources().then(function (data) {
    if (!dom.archivePage || !dom.archivePage.parentNode) return;
    var ul = $('[data-role="sources"]', dom.archivePage);
    if (!ul) return;
    var list = (data && (data.sources || data.items)) ? (data.sources || data.items) : [];
    // 按近 7 天条数降序（后端已排的也不打乱口径，这里再排一次保证顺序）
    list = list.slice().sort(function (a, b) { return (Number(b.count_7d) || 0) - (Number(a.count_7d) || 0); });
    state.archive.sources = list;
    var cur = state.archive;
    var html = '<li><button type="button" class="arc-src' + (cur.source ? '' : ' on') +
      '" data-act="arc-src" data-source="" data-date="' + esc(cur.date || '') + '">' +
      '<span class="arc-src-label">全部来源</span></button></li>' +
      list.map(function (s) { return arcSourceHTML(s, cur.source === s.source); }).join('');
    ul.innerHTML = html;
  }).catch(function (err) {
    if (!dom.archivePage || !dom.archivePage.parentNode) return;
    var ul2 = $('[data-role="sources"]', dom.archivePage);
    if (ul2) ul2.innerHTML = '<li class="err-page">来源读不出来：' + esc(err.message) + '</li>';
  });
}

/** 取一页：before 为 null 表示第一页（换查询条件时） */
function loadArchivePage(before, replace) {
  var st = state.archive;
  if (!st) return;
  if (st.loading) return;
  st.loading = true;
  var page = { source: st.source, date: st.date, q: st.q, before: before || null, limit: 50 };
  return api.archive(page).then(function (data) {
    st.loading = false;
    if (!dom.archivePage || !dom.archivePage.parentNode) return;
    var box = $('[data-role="items"]', dom.archivePage);
    var status = $('[data-role="status"]', dom.archivePage);
    var moreBtn = $('[data-act="arc-more"]', dom.archivePage);
    var items = (data && data.items) ? data.items : [];
    if (replace) {
      // 换查询条件：整块重画
      if (box) box.innerHTML = items.length ? arcItemsHTML(items, 0) : '<p class="empty">这个条件下没有条目。</p>';
    } else if (box) {
      // 翻页：追加，不动已有的；上次翻页失败留下的提示行先清掉
      var oldErr = $('.arc-more-err', box);
      if (oldErr) oldErr.remove();
      if (items.length) box.insertAdjacentHTML('beforeend', arcItemsHTML(items, st.count));
    }
    st.count += items.length;
    st.nextBefore = (data && data.next_before !== undefined) ? data.next_before : null;
    st.exhausted = st.nextBefore === null;
    if (status) {
      var q = [];
      if (st.source) q.push('来源 ' + (st.sourceLabel || st.source));
      if (st.date) q.push('日期 ' + st.date);
      if (st.q) q.push('「' + st.q + '」');
      status.textContent = '共 ' + st.count + ' 条' + (q.length ? ' · ' + q.join(' · ') : ' · 全部来源');
    }
    if (moreBtn) moreBtn.hidden = st.exhausted;
    hideImages(dom.archivePage);
    trackObserve(dom.archivePage);
    watchArchiveEnd();
  }).catch(function (err) {
    st.loading = false;
    if (!dom.archivePage || !dom.archivePage.parentNode) return;
    var box2 = $('[data-role="items"]', dom.archivePage);
    if (!box2) return;
    var retry = ' <button type="button" class="btn mini" data-act="arc-more">再试一次</button>';
    if (replace) {
      // 第一页失败：整块换成错误条；「再试一次」重取第一页（此时还没有 next_before 游标）
      st.retryFirst = true;
      box2.innerHTML = '<p class="err-page">条目取不上来：' + esc(err.message) + retry + '</p>';
      return;
    }
    // 翻页失败：保留已加载条目，只在末尾追加一行提示；游标与计数不动，重试接着取同一页
    var prev = $('.arc-more-err', box2);
    if (prev) prev.remove();
    box2.insertAdjacentHTML('beforeend', '<p class="err-page arc-more-err">加载更多失败：' + esc(err.message) + retry + '</p>');
    var wrap2 = $('.arc-more-wrap', dom.archivePage);
    var moreBtn2 = wrap2 ? $('[data-act="arc-more"]', wrap2) : null;
    if (moreBtn2) moreBtn2.hidden = true;  // 底部哨兵按钮先收起，免得滚动时自动重试刷屏；成功后按 exhausted 恢复
  });
}

/** 往下滚到底：自动翻下一页（next_before） */
function archiveMore() {
  var st = state.archive;
  if (!st || st.loading) return;
  if (st.retryFirst) { st.retryFirst = false; loadArchivePage(null, true); return; }
  if (st.exhausted) return;
  if (!st.nextBefore) { st.exhausted = true; return; }
  loadArchivePage(st.nextBefore, false);
}

/** 搜索框 / 日期选择提交：改 hash 重新定位（支持 #/archive?source=…&date=… 直接定位） */
var archiveSearchSubmit = debounce(function () {
  if (!dom.archivePage || !dom.archivePage.parentNode) return;
  var qEl = $('[data-role="q"]', dom.archivePage);
  var dEl = $('[data-role="date"]', dom.archivePage);
  var st = state.archive || { source: '' };
  var nq = qEl ? String(qEl.value || '').trim() : '';
  var nd = dEl ? String(dEl.value || '').trim() : '';
  var next = '#/archive' + archiveHashQuery(st.source || '', nd, nq);
  if (location.hash === next) { archiveRerun(); return; }
  location.hash = next;
}, 260);

/** hash 没变但条件改了（清空后同 hash）也要重跑 */
function archiveRerun() {
  var st = state.archive;
  if (!st) return;
  var qEl = $('[data-role="q"]', dom.archivePage);
  var dEl = $('[data-role="date"]', dom.archivePage);
  st.q = qEl ? String(qEl.value || '').trim() : '';
  st.date = dEl ? String(dEl.value || '').trim() : '';
  st.count = 0;
  st.exhausted = false;
  loadArchivePage(null, true);
  loadArchiveSources();
}

document.addEventListener('input', function (ev) {
  var t = ev.target;
  if (!t || !t.closest) return;
  if (t.getAttribute && (t.getAttribute('data-role') === 'q' || t.getAttribute('data-role') === 'date')) {
    archiveSearchSubmit();
  }
});

document.addEventListener('submit', function (ev) {
  var t = ev.target;
  if (!t || !t.closest) return;
  var f = t.closest('[data-role="arcbar"]');
  if (!f) return;
  ev.preventDefault();
  archiveRerun();
});

/* 全局委托：编辑部来信 / 管道 / 重试 */
document.addEventListener('click', function (ev) {
  var t = ev.target;
  if (!t.closest) return;
  var btn = t.closest('[data-act]');
  if (!btn) return;
  var act = btn.getAttribute('data-act');

  if (act === 'propose') {
    var art = btn.closest('.letter');
    if (!art) return;
    var line = art.getAttribute('data-line');
    var decision = btn.getAttribute('data-decision');
    $$('button', art).forEach(function (b) { b.disabled = true; });
    api.proposal(line, decision).then(function (res) {
      art.classList.add('is-gone');
      setTimeout(function () {
        art.classList.add('is-settled');
        var done = $('[data-role="done"]', art);
        if (done) {
          done.textContent = (decision === 'accept' ? '已接受，明天的版面跟着变' : '已拒绝');
        }
        var h = $('.sec-head .sec-note', art.parentNode);
        if (h) {
          var m = h.textContent.match(/^(\d+)/);
          if (m) h.textContent = Math.max(0, Number(m[1]) - 1) + ' 封待你批复';
        }
      }, 150);
      if (res && typeof res.pending_count === 'number') { /* pending_count 供后端/调试用 */ }
    }).catch(function (err) {
      $$('button', art).forEach(function (b) { b.disabled = false; });
      var e = document.createElement('span');
      e.className = 'err';
      e.textContent = '没提交上：' + err.message;
      $('.letter-actions', art).appendChild(e);
    });
    return;
  }

  if (act === 'revert-adj') { doRevertAdj(btn); return; }

  if (act === 'pipe') {
    var tr = btn.closest('tr');
    if (!tr || btn.disabled) return;
    var kind = btn.getAttribute('data-kind');
    var action = btn.getAttribute('data-action');
    var key = tr.getAttribute('data-key');
    var btns = $$('button', tr);
    btns.forEach(function (b) { b.disabled = true; });
    api.pipelinePost(kind, key, action).then(function (res) {
      btns.forEach(function (b) { b.disabled = false; });
      applyPipeResult(tr, res, action);
    }).catch(function (err) {
      btns.forEach(function (b) { b.disabled = false; });
      var f = $('[data-role="flash"]', tr);
      if (f) { f.textContent = '没改成：' + err.message; f.style.color = 'var(--danger)'; }
    });
    return;
  }

  if (act === 'retry') { loadFirstPage(); return; }
  if (act === 'retry-more') { loadMore(); return; }
  if (act === 'load-more') { loadMore(); return; }     // 页尾「看昨天的」：取代原先的滚到底自动加载

  if (act === 'relogin') { doRelogin(btn); return; }

  if (act === 'resolve') { resolveClaim(btn); return; }

  if (act === 'follow-dismiss') { dismissFollow(btn); return; }
  if (act === 'follow-go') { trackFollowGo(btn.getAttribute('data-fs')); return; }

  if (act === 'trip-del') { delTrip(btn); return; }
  if (act === 'trip-add') {
    ev.preventDefault();
    var tsec = btn.closest('.trips');
    if (tsec) addTrip(tsec);
    return;
  }

  if (act === 'arc-more') { archiveMore(); return; }
  if (act === 'arc-src') {
    ev.preventDefault();
    var src = btn.getAttribute('data-source') || '';
    var d = btn.getAttribute('data-date') || '';
    location.hash = '#/archive' + archiveHashQuery(src, d, (archiveState() || {}).q || '');
    return;
  }
  if (act === 'arc-clear') {
    ev.preventDefault();
    location.hash = '#/archive';
    return;
  }

  if (act === 'ib-more') {
    // 正文默认展开在标题下；超出才给「展开」。点一次开、再点一次收。
    var box = btn.closest('[data-ib-id]');
    if (!box) return;
    var inner = $('[data-role="bodyin"]', box);
    var body = $('[data-role="body"]', box);
    if (!inner || !body) return;
    var open = body.classList.toggle('open');
    btn.textContent = open ? '收起' : (box.classList.contains('flash-row') ? '展开全部' : '展开');
    if (open) {
      trackOpenInbox(box.getAttribute('data-ib-id'), box.classList.contains('flash-row') ? 'flash_expand' : 'expand');
    }
    return;
  }

  if (act === 'flash-ack') { ackFlash(btn); return; }
});

/* ---------------------------------------------------------
 * 9. 行为监测（paper_v2_contract.md 第 4 节）
 *    前缀 track 的这一段是独立模块：采集 → 攒批 → 上报。
 *    唯一出口是 POST /api/paper/events（mock 模式只写 console 与 window.__trackLog）。
 * ------------------------------------------------------- */

var TRACK_URL = '/api/paper/events';
var TRACK_BATCH_MS = 10000;   // 攒批周期
var TRACK_BATCH_MAX = 50;     // 攒满 50 条立刻发
var TRACK_QUEUE_MAX = 500;    // 内存队列上限，超出丢最旧
var TRACK_LOG_MAX = 200;      // window.__trackLog 保留条数
var TRACK_IMPRESSION_MIN = 800;   // 可见多久才算一次曝光
var TRACK_IMPRESSION_TH = 0.5;    // 可见面积阈值
var TRACK_SCAN_MS = 200;      // 曝光计时的检查间隔

var trackSessionId = trackRandId();
var trackQueue = [];          // 待发送
var trackLog = [];            // 最近 200 条（验收用）
var trackSending = false;
var trackSessionEndSent = false;
var trackSessionStartFg = 0;
var trackEditionsSeen = {};
var trackInboxDates = {};  // inbox_id -> 所在期 date
var trackAppActive = true;    // Mac App 是否在前台
var trackImpObserver = null;
var trackImpActive = [];      // 正在计时的曝光元素
var trackImpObserved = [];    // 已挂上曝光观察的元素（10-05 验收 漏报-2：恢复可见时要重挂）
var trackImpScanTimer = null;
var trackBatchTimer = null;

// 前台时钟：只有「可见 + App 在前台」时才推进；后台时间不计入任何 ms。
var trackFg = { acc: 0, at: Date.now(), on: true };

function trackRandId() {
  var s = '';
  for (var i = 0; i < 4; i++) s += Math.floor(Math.random() * 0x100000000).toString(36);
  return s;
}

/** 前台累计毫秒（单调递增，后台期间不增长） */
function trackFgNow() {
  return trackFg.on ? trackFg.acc + (Date.now() - trackFg.at) : trackFg.acc;
}

/** 把 on 期间的时间落到 acc 上 */
function trackFgSync() {
  var now = Date.now();
  if (trackFg.on) trackFg.acc += now - trackFg.at;
  trackFg.at = now;
}

/** 是否应当处于「可见 + App 前台」。
 *  注意 window.__todayAppActive 是函数（契约要求 Mac App 调它），
 *  它本身永远不会 === false，所以后台状态要看内部的 trackAppActive。 */
function trackWanted() {
  return document.visibilityState === 'visible' &&
    trackAppActive &&
    window.__todayAppActive !== false;
}

/** 前台状态变化时暂停/恢复所有计时 */
function trackFgApply() {
  var want = trackWanted();
  if (want === trackFg.on) return;
  trackFgSync();
  trackFg.on = want;
  trackFg.at = Date.now();
}

/* ---------- 采集 ---------- */

function trackLogPush(ev) {
  trackLog.push(ev);
  while (trackLog.length > TRACK_LOG_MAX) trackLog.shift();
}

/**
 * 记一个事件。
 * opts: {item_id, edition_date, ms, meta}
 */
function trackEmit(kind, opts) {
  opts = opts || {};
  var ev = {
    ts: new Date().toISOString(),
    kind: kind,
    item_id: opts.item_id === undefined ? null : opts.item_id,
    edition_date: opts.edition_date === undefined ? null : opts.edition_date,
    ms: (opts.ms === undefined || opts.ms === null) ? null : Math.max(0, Math.round(opts.ms)),
    meta: opts.meta || {}
  };
  trackLogPush(ev);
  while (trackQueue.length >= TRACK_QUEUE_MAX) trackQueue.shift(); // 超上限丢最旧
  trackQueue.push(ev);
  if (trackQueue.length >= TRACK_BATCH_MAX) trackFlush('攒满 ' + TRACK_BATCH_MAX + ' 条');
  return ev;
}

function trackMarkEditions() {
  (state.editions || []).forEach(function (ed) {
    if (ed && ed.date) trackEditionsSeen[ed.date] = 1;
  });
}

function trackEditionsSeenCount() {
  var n = 0;
  for (var k in trackEditionsSeen) if (Object.prototype.hasOwnProperty.call(trackEditionsSeen, k)) n++;
  return n;
}

function trackSessionStart() {
  trackSessionEndSent = false;
  trackMarkEditions();
  trackSessionStartFg = trackFgNow();
  trackEmit('session_start', { meta: { editions_seen: trackEditionsSeenCount() } });
}

function trackSessionEnd() {
  if (trackSessionEndSent) return;   // 一次页面会话只收一次尾
  trackSessionEndSent = true;
  trackMarkEditions();
  trackEmit('session_end', {
    ms: trackFgNow() - trackSessionStartFg,
    meta: { editions_seen: trackEditionsSeenCount() }
  });
}

/** Mac App 切前台/后台 */
function trackSetAppActive(active) {
  var next = !!active;
  var changed = (next !== trackAppActive);
  trackAppActive = next;
  trackFgApply();                    // 先暂停/恢复计时，再记事件
  if (changed) trackEmit(next ? 'app_active' : 'app_inactive', { meta: {} });
}

/** 点标题进单篇页。sec 传 'archive' 时 meta.section=archive（全部来源页点进来的） */
function trackItemOpen(from, itemId, sec) {
  if (!itemId) return;
  var meta = { from: from || 'paper' };
  if (sec) meta.section = sec;
  trackEmit('open_item', { item_id: itemId, edition_date: digestOf(itemId), meta: meta });
}

/** 点 ↗ 或「阅读原文」 */
function trackOpenOriginal(from, itemId) {
  trackEmit('open_original', { item_id: itemId || null, edition_date: itemId ? digestOf(itemId) : null, meta: { from: from || 'paper' } });
}

/** 展开「更多」面板 */
function trackExpandFeedback(itemId) {
  trackEmit('expand_feedback', { item_id: itemId || null, edition_date: itemId ? digestOf(itemId) : null, meta: {} });
}

/** j/k 把焦点移到某条 */
function trackFocus(itemId) {
  trackEmit('focus', { item_id: itemId || null, edition_date: itemId ? digestOf(itemId) : null, meta: {} });
}

/** 打开/展开/标已读一条本机投递箱条目：item_id 填 inbox:<id>，ms=null */
function trackOpenInbox(inboxId, how) {
  if (inboxId === null || inboxId === undefined || inboxId === '') return;
  var key = 'inbox:' + inboxId;
  trackEmit('open_inbox', {
    item_id: key,
    edition_date: trackInboxDate(key),
    ms: null,
    meta: { section: 'inbox', how: how || 'open' }
  });
}

/** 本机投递箱条目所在期（渲染时登记，用于事件里的 edition_date） */
function trackInboxDate(key) {
  var m = String(key || '').match(/^inbox:(.+)$/);
  if (!m) return null;
  return trackInboxDates[m[1]] || null;
}

/* ---------- 曝光（IntersectionObserver，阈值 0.5，持续 ≥800ms） ---------- */

function trackImpState(el) {
  if (!el.__imp) el.__imp = { start: null, ok: false };
  return el.__imp;
}

function trackImpScan() {
  var still = [];
  for (var i = 0; i < trackImpActive.length; i++) {
    var el = trackImpActive[i];
    var st = el.__imp;
    if (!st || st.start === null) continue;
    if (!st.ok && trackFgNow() - st.start >= TRACK_IMPRESSION_MIN) st.ok = true;
    still.push(el);
  }
  trackImpActive = still;
  if (!trackImpActive.length) {
    if (trackImpScanTimer) { clearInterval(trackImpScanTimer); trackImpScanTimer = null; }
    return;
  }
  trackImpScanTimer = setTimeout(trackImpScan, TRACK_SCAN_MS);
}

function trackImpStartScan() {
  if (trackImpScanTimer) return;
  trackImpScanTimer = setTimeout(trackImpScan, TRACK_SCAN_MS);
}

function trackImpEnter(el) {
  var st = trackImpState(el);
  if (st.start === null) {
    // 元素仍在视口内、只是滚过另一个 threshold 时 IO 会重发 entry，
    // 已在计时就不能重置 start/ok，否则慢滚阅读的可见时长被清零、800ms 永远凑不满。
    st.start = trackFgNow();
    st.ok = false;
  }
  if (trackImpActive.indexOf(el) < 0) trackImpActive.push(el);
  trackImpStartScan();
}

/** 条目 id：新闻条目是 data-item-id，本机投递箱条目是 data-ib-id（上报时前缀 inbox:） */
function trackElId(el) {
  if (!el) return null;
  var ib = el.getAttribute('data-ib-id');
  if (ib !== null && ib !== '') return 'inbox:' + ib;
  return el.getAttribute('data-item-id');
}

/** 离开视口：够 800ms 才发一条，记总可见时长 */
function trackImpLeave(el) {
  var st = el.__imp;
  if (!st || st.start === null) return;
  var dur = trackFgNow() - st.start;
  var ok = st.ok;
  st.start = null;
  st.ok = false;
  var i = trackImpActive.indexOf(el);
  if (i >= 0) trackImpActive.splice(i, 1);
  if (!ok || dur < TRACK_IMPRESSION_MIN) return;
  var id = trackElId(el);
  trackEmit('impression', {
    item_id: id,
    edition_date: (id && id.indexOf('inbox:') === 0) ? trackInboxDate(id) : digestOf(id),
    ms: dur,
    meta: {
      section: el.getAttribute('data-sec') || null,
      rank: Number(el.getAttribute('data-rank')) || 0
    }
  });
}

/** 隐藏/关闭时把正在计时的曝光结掉（后台时间已由前台时钟排除） */
function trackImpCloseAll() {
  var list = trackImpActive.slice();
  for (var i = 0; i < list.length; i++) trackImpLeave(list[i]);
}

/** 给新渲染出来的条目挂观察者（新闻条目 data-item-id，本机简报条目 data-ib-id） */
function trackObserve(root) {
  if (!('IntersectionObserver' in window)) return;
  if (!trackImpObserver) {
    trackImpObserver = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
        if (e.isIntersecting && (e.intersectionRatio >= TRACK_IMPRESSION_TH || e.intersectionRect.height >= window.innerHeight * 0.5)) trackImpEnter(e.target);  // 高于 2 倍视口的条目比例到不了 0.5，占满半屏也算
        else trackImpLeave(e.target);
      }
    }, { threshold: [0, 0.1, 0.25, TRACK_IMPRESSION_TH, 0.75, 1] });
  }
  $$('[data-item-id][data-sec], [data-ib-id][data-sec]', root).forEach(function (el) {
    if (el.__impObserved) return;
    el.__impObserved = true;
    trackImpObserver.observe(el);
    trackImpObserved.push(el);
  });
}

/* ---------- 单篇页停留 ---------- */

/** 以全文区域底部为 100，算当前视口滚到了百分之几 */
function trackItemScrollPct() {
  var page = dom.itemPage;
  if (!page) return 0;
  var region = $('.fulltext', page) || page;
  var rect = region.getBoundingClientRect();
  var top = rect.top + (window.pageYOffset || 0);
  var h = region.offsetHeight || 1;
  var viewBottom = (window.pageYOffset || 0) + (window.innerHeight || 0);
  var pct = ((viewBottom - top) / h) * 100;
  if (!isFinite(pct) || pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  return Math.round(pct);
}

/** 离开单篇页时记一次 item_dwell（只计可见且 App 在前台的时间） */
function trackItemLeave() {
  if (!dom.itemPage || !dom.itemPage.parentNode) return;
  var id = dom.itemPage.getAttribute('data-item-id');
  if (!id || state.itemDwellSent) return;
  state.itemDwellSent = true;
  trackItemScroll();
  trackEmit('item_dwell', {
    item_id: id,
    edition_date: digestOf(id),
    ms: trackFgNow() - state.itemEnterFg,
    meta: {
      max_scroll_pct: state.itemMaxScroll,
      fulltext_chars: state.itemFulltextChars
    }
  });
}

/** 停在单篇页时滚动，记最深滚到哪儿 */
function trackItemScroll() {
  if (!dom.itemPage || !dom.itemPage.parentNode || state.itemDwellSent) return;
  var pct = trackItemScrollPct();
  if (pct > state.itemMaxScroll) state.itemMaxScroll = pct;
}

/* ---------- 攒批与发送 ---------- */

function trackFlush(why) {
  if (MOCK) {
    if (!trackQueue.length) return;
    var mb = trackQueue.splice(0, trackQueue.length);
    console.debug('[track]', mb);
    return;
  }
  if (trackSending || !trackQueue.length) return;
  var batch = trackQueue.splice(0, TRACK_BATCH_MAX);
  trackSending = true;
  postJSON(TRACK_URL, { session_id: trackSessionId, events: batch }).then(function () {
    trackSending = false;
    if (trackQueue.length) trackFlush('补发积压');
  }).catch(function () {
    trackSending = false;
    var back = batch.concat(trackQueue);              // 发送失败的批次留在内存队列里下次重发
    if (back.length > TRACK_QUEUE_MAX) back = back.slice(back.length - TRACK_QUEUE_MAX);
    trackQueue = back;
  });
}

/** 页面隐藏/关闭：用 sendBeacon 把剩下的发出去。
 *  Content-Type 必须是 application/json：服务端跨站防护（2026-10-09）只收 JSON，text/plain 会被 403 且 beacon 不报错、静默丢事件。
 *  个别浏览器对非 text/plain 的 Blob 会抛错或返回 false，此时退到 fetch keepalive（同源、同样带 JSON 头）。 */
function trackBeacon(why) {
  if (!trackQueue.length) return;
  if (MOCK) { trackFlush(why || 'beacon'); return; }
  var events = trackQueue.splice(0, trackQueue.length);
  var body = JSON.stringify({ session_id: trackSessionId, events: events });
  var requeue = function () {
    var back = events.concat(trackQueue);
    if (back.length > TRACK_QUEUE_MAX) back = back.slice(back.length - TRACK_QUEUE_MAX);
    trackQueue = back;
  };
  var ok = false;
  try {
    if (navigator && navigator.sendBeacon) {
      ok = navigator.sendBeacon(TRACK_URL, new Blob([body], { type: 'application/json' }));
    }
  } catch (e) { ok = false; }
  if (ok) return;
  if (typeof fetch === 'function') {
    try {
      fetch(TRACK_URL, { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: body })
        .then(function (res) { if (!res.ok) requeue(); }, requeue);
      return;
    } catch (e) { /* 落到下面重排队 */ }
  }
  requeue();
}

/** 隐藏或关闭：结算 → 收尾 → 走 beacon */
function trackTeardown(why) {
  trackFgApply();
  trackItemLeave();
  trackImpCloseAll();
  trackSessionEnd();
  trackBeacon(why || 'teardown');
}

/** 把已挂曝光观察的条目先 unobserve 再 observe，强制重新回调（10-05 验收 漏报-2） */
function trackImpReobserveAll() {
  if (!trackImpObserver) return;
  // 重铺版面后旧节点已脱离文档：顺手剔掉，免得数组只增不减
  trackImpObserved = trackImpObserved.filter(function (el) { return el.isConnected !== false; });
  for (var i = 0; i < trackImpObserved.length; i++) {
    var el = trackImpObserved[i];
    trackImpObserver.unobserve(el);
    trackImpObserver.observe(el);
  }
}

/** 恢复可见：重开会话、补单篇停留起点、重挂曝光观察（10-05 验收 漏报-2。
 *  hidden 时 trackTeardown 把三个状态都结掉了，不复位就会永久丢事件） */
function trackResume() {
  if (state.itemDwellSent && dom.itemPage && dom.itemPage.parentNode &&
      parseHash().name === 'item') {
    state.itemDwellSent = false;
    state.itemEnterFg = trackFgNow();
  }
  trackSessionStart();
  trackImpReobserveAll();
}

function trackInit() {
  window.__trackLog = trackLog;
  window.__trackFlush = function () { trackFlush('手动'); };
  window.__trackBeacon = function () { trackBeacon('手动'); };
  window.__trackQueueSize = function () { return trackQueue.length; };
  window.__todayAppActive = function (active) { trackSetAppActive(active); };
  trackFgApply();
  trackSessionStart();
  trackBatchTimer = setInterval(function () { trackFlush('每 ' + (TRACK_BATCH_MS / 1000) + ' 秒'); }, TRACK_BATCH_MS);
  window.addEventListener('scroll', trackItemScroll, { passive: true });

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') trackTeardown('页面隐藏');
    else {
      trackFgApply();                      // 回到前台：恢复所有计时
      trackResume();                       // 10-05 验收 漏报-2：复位 hidden 时结算掉的计时
    }
  });
  window.addEventListener('pagehide', function () { trackTeardown('页面关闭'); });
}

/* ---------------------------------------------------------
 * 10. 路由
 * ------------------------------------------------------- */

function parseHash() {
  var h = location.hash || '#/';
  if (h.indexOf('#/item/') === 0) {
    var raw = h.slice('#/item/'.length);
    var id = raw;
    try { id = decodeURIComponent(raw); } catch (e) { id = raw; }
    return { name: 'item', id: id };
  }
  if (h.indexOf('#/inbox/') === 0) {
    var rawI = h.slice('#/inbox/'.length);
    var ibId = rawI;
    try { ibId = decodeURIComponent(rawI); } catch (e) { ibId = rawI; }
    return { name: 'inbox', id: ibId };
  }
  if (h.indexOf('#/pipeline') === 0) return { name: 'pipeline' };
  if (h.indexOf('#/auth') === 0) return { name: 'auth' };
  if (h.indexOf('#/archive') === 0) return { name: 'archive' };
  return { name: 'home' };
}

/** 找到这条投递箱条目在当前页面里的 DOM 节点 */
function findInboxNode(inboxId) {
  var id = String(inboxId == null ? '' : inboxId);
  if (!id) return null;
  var sel = '[data-ib-id="' + id.replace(/"/g, '\\"') + '"]';
  var inFlash = dom.flash ? $(sel, dom.flash) : null;
  if (inFlash) return inFlash;
  return dom.home ? $(sel, dom.home) : null;
}

function clearInboxHit() {
  if (state.inboxHitTimer) { clearTimeout(state.inboxHitTimer); state.inboxHitTimer = null; }
  $$('.ib-hit').forEach(function (n) { n.classList.remove('ib-hit'); });
}

/** 滚到该条并高亮 2 秒 */
function focusInboxNode(node, inboxId) {
  clearInboxHit();
  try { node.scrollIntoView({ block: 'center' }); } catch (e) { node.scrollIntoView(); }
  node.classList.add('ib-hit');
  state.inboxHitTimer = setTimeout(function () {
    node.classList.remove('ib-hit');
    state.inboxHitTimer = null;
  }, 2000);
  trackOpenInbox(inboxId, 'route');
}

/** 在已加载的版面里找这条；找不到按需再取一期，找不到就报一行「这条已不在版面上」 */
function gotoInbox(inboxId) {
  var node = findInboxNode(inboxId);
  if (node) { focusInboxNode(node, inboxId); return; }
  if (state.inboxMissTried) { showInboxMissing(); return; }
  state.inboxMissTried = true;
  state.pendingInbox = String(inboxId);
  if (!state.started) { loadFirstPage(); return; }   // 还没铺版，铺完再找
  loadFirstPage();                                   // 已加载的期里没有：加载最新一期后再找
}

function showInboxMissing() {
  if (!dom.home || !dom.home.parentNode) return;
  var old = $('.inbox-miss', dom.home);
  if (old) old.parentNode.removeChild(old);
  var p = document.createElement('p');
  p.className = 'inbox-miss';
  p.textContent = '这条已不在版面上。';
  dom.home.insertBefore(p, dom.home.firstChild);
  window.scrollTo(0, 0);
}

/** 铺版完成后：如果路由等着定位某条投递箱条目，这时去找 */
function tryPendingInbox() {
  if (!state.pendingInbox) return;
  var id = state.pendingInbox;
  state.pendingInbox = null;
  var node = findInboxNode(id);
  if (node) focusInboxNode(node, id);
  else showInboxMissing();
}

function route() {
  var r = parseHash();
  if (r.name === 'item') {
    var from = state.pendingOpenFrom || (state.pendingOpenSection === 'archive' ? 'archive' : 'paper');
    state.pendingOpenFrom = '';
    showItemPage(r.id, from);
    return;
  }
  state.pendingOpenFrom = '';
  state.pendingOpenSection = '';
  leaveItemPage();
  if (r.name === 'pipeline') { showPipeline(); return; }
  if (r.name === 'auth') { showAuthPage(); return; }
  if (r.name === 'archive') { showArchive(); return; }
  if (r.name === 'inbox') {
    state.inboxMissTried = false;
    if (!dom.home) { state.pendingInbox = r.id; showHome(); return; }  // showHome 会触发首次铺版
    showHome();
    gotoInbox(r.id);
    return;
  }
  state.inboxMissTried = false;
  state.pendingInbox = null;
  clearInboxHit();
  showHome();
}

window.addEventListener('hashchange', route);

/* ---------------------------------------------------------
 * 10. 键盘：j/k 移动焦点，o 打开，1/2 评分，Esc 返回
 * ------------------------------------------------------- */

function isTyping() {
  var el = document.activeElement;
  if (!el) return false;
  var tag = (el.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
}

function visibleItems() {
  return $$('[data-item-id]').filter(function (n) {
    return !!(n.offsetWidth || n.offsetHeight || n.getClientRects().length);
  });
}

function focusItem(idx) {
  var list = visibleItems();
  if (!list.length) return;
  if (idx < 0) idx = 0;
  if (idx >= list.length) idx = list.length - 1;
  var prev = list[state.kbdIndex];
  if (prev && prev !== list[idx]) prev.classList.remove('kbd-focus');
  state.kbdIndex = idx;
  var node = list[idx];
  node.classList.add('kbd-focus');
  try { node.scrollIntoView({ block: 'nearest' }); } catch (e) { node.scrollIntoView(); }
  trackFocus(node.getAttribute('data-item-id'));
}

document.addEventListener('keydown', function (ev) {
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
  if (isTyping()) return;
  var r = parseHash();
  var k = ev.key;

  if (k === 'Escape') {
    if (r.name === 'item') {
      ev.preventDefault();
      var memo = state.scrollMemo;
      location.hash = '#/';
      requestAnimationFrame(function () { window.scrollTo(0, memo); });
    }
    return;
  }
  if (r.name !== 'home') return;

  if (k === 'j' || k === 'k') {
    ev.preventDefault();
    var list = visibleItems();
    if (!list.length) return;
    var next = state.kbdIndex + (k === 'j' ? 1 : -1);
    if (state.kbdIndex < 0) next = (k === 'j' ? 0 : list.length - 1);
    focusItem(next);
    return;
  }
  if (k === 'o') {
    var list2 = visibleItems();
    if (!list2.length) return;
    var i = state.kbdIndex < 0 ? 0 : Math.min(state.kbdIndex, list2.length - 1);
    var node = list2[i];
    var id = node.getAttribute('data-item-id');
    if (id) { ev.preventDefault(); state.pendingOpenFrom = 'keyboard'; location.hash = '#/item/' + encodeURIComponent(id); }
    return;
  }
  if (k === '1' || k === '2') {
    var list3 = visibleItems();
    if (!list3.length) return;
    var j = state.kbdIndex < 0 ? 0 : Math.min(state.kbdIndex, list3.length - 1);
    var node3 = list3[j];
    var id3 = node3.getAttribute('data-item-id');
    var fbEl = $('.fb', node3);
    if (!id3 || !fbEl) return;
    ev.preventDefault();
    doRate(id3, 'overall', k === '1' ? 1 : -1, fbEl);
  }
});

/* ---------------------------------------------------------
 * 11. 启动
 * ------------------------------------------------------- */

/**
 * 全部来源页的自动翻页：条目流底部的哨兵进视口就取 next_before。
 * 报纸主页**不再**有自动加载前一期（契约第 10.4 节）：改成页尾按钮「看昨天的」。
 */
function initArchiveScroll() {
  if (!('IntersectionObserver' in window)) {
    window.addEventListener('scroll', debounce(function () {
      if (parseHash().name !== 'archive') return;
      if (!dom.archivePage || !dom.archivePage.parentNode) return;
      var btn = $('[data-act="arc-more"]', dom.archivePage);
      if (!btn || btn.hidden) return;
      var top = dom.archivePage.getBoundingClientRect().top + (window.pageYOffset || 0);
      var h = dom.archivePage.offsetHeight || 1;
      var viewBottom = (window.pageYOffset || 0) + (window.innerHeight || 0);
      if (viewBottom >= top + h - 600) archiveMore();
    }, 200));
    return;
  }
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return;
      if (parseHash().name !== 'archive') return;
      var wrap = e.target;
      var btn = wrap && wrap.querySelector ? wrap.querySelector('[data-act="arc-more"]') : null;
      if (!btn || btn.hidden) return;
      archiveMore();
    });
  }, { rootMargin: '600px 0px' });
  window.__archiveIO = io;
}

/** 每次铺完 archive 条目，把哨兵挂上观察（换页时旧节点已换掉，重挂一次） */
function watchArchiveEnd() {
  var io = window.__archiveIO;
  if (!io || !dom.archivePage) return;
  var wrap = $('.arc-more-wrap', dom.archivePage);
  if (wrap) io.observe(wrap);
}

function boot() {
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  cacheDom();
  trackInit();               // 行为监测：先起来，才能记到 session_start
  initArchiveScroll();
  route();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}

})();
