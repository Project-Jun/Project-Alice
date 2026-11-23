/* CodeVision web UI: start screen, project view and the tree diagram.
   The diagram engine (measure, fam, place, plan, draw, upd, fit, panel, act, tog, key, relayout)
   is a port of design/mockup.html; keep the two in step. Differences from the mockup:
   - it switches the whole diagram between rows and the outline, while plan/draw here mix them (rows
     for the open families that fit, the outline from the first one that does not);
   - on screens wide enough not to be phones, a family too wide for one row wraps onto a few lines
     (famw) before it falls back to the outline;
   - from 1024px the panel stands beside the diagram and stays in view (app.css .cvb); below that it
     is under the diagram, and choosing a file brings it into view.
   AI explanations (aiCtl) are not in the mockup: the header chip opens the AI panel, and while AI is on
   the side panel and the tree labels use the AI's text, node.x (docs/architecture.md 6 and 7). */
(() => {
  'use strict';

  const app = document.getElementById('app');
  const tokenMeta = document.querySelector('meta[name="cv-token"]');
  const TOKEN = tokenMeta ? tokenMeta.getAttribute('content') || '' : '';

  // ---------- helpers ----------
  const ESC = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ESC[c]);
  const str = v => (v == null ? '' : String(v));
  const list = v => (Array.isArray(v) ? v : []);
  const own = (o, k) => o != null && typeof o === 'object' && Object.prototype.hasOwnProperty.call(o, k);
  const dict = () => Object.create(null); // id-keyed maps: ids like "constructor" stay plain keys
  const num = v => (Number.isFinite(+v) ? Math.max(0, Math.round(+v)) : 0);
  // One formatter for every count: toLocaleString makes a new one on each call, slow for a big diagram.
  const NUMS = typeof Intl !== 'undefined' && Intl.NumberFormat ? new Intl.NumberFormat('ko-KR') : null;
  const fmt = v => (NUMS ? NUMS.format(num(v)) : num(v).toLocaleString('ko-KR'));
  const nItems = n => fmt(n) + '개 항목', nLinks = n => '연결 ' + fmt(n) + '개'; // "1,200개 항목", "연결 1,200개"
  const baseName = p => str(p).replace(/[\\/]+$/, '').split(/[\\/]/).pop() || str(p);
  // A path as HTML that breaks only after a "\" or "/": each part up to its separator is kept whole
  // (app.css .src-p, .rc-p, .er-p, .er-path) unless it is wider than the line by itself. Spaces that start a
  // part stay in front of it, where they show and copy as in plain text (the part's own start would drop them).
  const pathHtml = p => (str(p).match(/[^\\/]*[\\/]+|[^\\/]+$/g) || []).map(s => {
    const w = s.match(/^[ \t\n\r\f]*/)[0];
    return esc(w) + (s.length > w.length ? `<span>${esc(s.slice(w.length))}</span>` : '');
  }).join('<wbr>');
  // Where text already names path p (the server's "폴더를 찾을 수 없어요: C:\…"): p as a whole word, read the
  // way the server reads it (no spaces or quotes around it), with "/" and "\" alike and in any case.
  // [start, end] of it in text, or null; [0, 0] for an empty p, or when lower case changed the length.
  function named(text, p) {
    const k = s => str(s).replace(/\//g, '\\').toLowerCase();
    const t = k(text), q = k(str(p).trim().replace(/^"+|"+$/g, '').trim());
    if (!q) return [0, 0];
    for (let at = t.indexOf(q); at >= 0; at = t.indexOf(q, at + 1)) {
      if (!/[^\s:"'(]/.test(t.charAt(at - 1)) && !/[^\s"').,]/.test(t.charAt(at + q.length))) return t.length === str(text).length ? [at, at + q.length] : [0, 0];
    }
    return null;
  }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function icon(name) {
    const i = el('i', 'ti ' + name);
    i.setAttribute('aria-hidden', 'true');
    return i;
  }

  const FC = {ui: '#534AB7', sv: '#0F6E56', ot: '#5F5E5A'};
  const AREA = {ui: ['화면 코드', 'tu'], sv: ['서버 코드', 'ts'], ot: ['문서·설정', 'tn']};
  const ICON = /^ti-[a-z0-9-]+$/;
  // Only for a graph that says it is cut short but gives no reason (the server words its own line).
  const TRUNC = '파일이 너무 많아서 일부만 분석했어요.';
  const NAMES = typeof Intl !== 'undefined' && Intl.Collator ? new Intl.Collator('ko', {numeric: true, sensitivity: 'base'}) : null;
  const byName = (a, b) => (NAMES ? NAMES.compare(a, b) : a.toLowerCase() < b.toLowerCase() ? -1 : a.toLowerCase() > b.toLowerCase() ? 1 : 0) || (a < b ? -1 : a > b ? 1 : 0);
  const bd = (c, t, ic) => `<span class="bd ${c}">${ic ? `<i class="ti ${ic}" aria-hidden="true"></i>` : ''}${esc(t)}</span>`;
  const SB = bd('tn', '시작점', 'ti-player-play');
  const OB = bd('tw', '정리 후보', 'ti-alert-triangle');
  const areaName = (G, a) => (own(G.areas, a) && typeof G.areas[a] === 'string' && G.areas[a] ? G.areas[a] : AREA[a][0]);
  const projName = G => str(G.name) || baseName(G.source && G.source.path) || '프로젝트';

  // ---------- server ----------
  // An error keeps the HTTP status and the "code" that the /api/ai/ replies add ("timeout", "busy" ...).
  function fail(msg, code, status) {
    const e = new Error(msg);
    e.code = code || '';
    e.status = status || 0;
    return e;
  }
  // body: the JSON to POST ({} when none). ms: stop waiting after that long (the error's code is then "timeout").
  async function api(url, method, body, ms) {
    const post = method === 'POST';
    const headers = {'X-CV-Token': TOKEN};
    if (post) headers['Content-Type'] = 'application/json';
    const ctl = ms && typeof AbortController === 'function' ? new AbortController() : null;
    const tm = ctl ? setTimeout(() => ctl.abort(), ms) : 0;
    let r, j = null;
    try {
      try {
        r = await fetch(url, {method: post ? 'POST' : 'GET', headers, body: post ? JSON.stringify(body || {}) : undefined, cache: 'no-store', signal: ctl ? ctl.signal : undefined});
      } catch (e) {
        if (ctl && ctl.signal.aborted) throw fail('답이 너무 늦어서 기다리기를 멈췄어요. 잠시 뒤에 다시 해 주세요.', 'timeout');
        throw fail('CodeVision 프로그램에 연결하지 못했어요. 프로그램이 켜져 있는지 확인해 주세요.', 'network');
      }
      try { j = await r.json(); } catch (e) { j = null; }
    } finally {
      clearTimeout(tm);
    }
    if (j && typeof j.error === 'string' && j.error) throw fail(j.error, typeof j.code === 'string' ? j.code : '', r.status);
    if (!r.ok || !j || typeof j !== 'object') throw fail('요청을 처리하지 못했어요. 잠시 뒤에 다시 해 주세요.', '', r.status);
    return j;
  }

  // Keep the open project in the address bar so a reload opens it again.
  function setQuery(r) {
    const q = !r ? '' : r.kind === 'demo' ? '?demo=1' : '?path=' + encodeURIComponent(r.path);
    try { history.replaceState(null, '', location.pathname + q); } catch (e) { /* not allowed on file:// */ }
  }

  // ---------- screens ----------
  let screen = '', busy = false, last = null, typed = '', view = null, ro = null, aic = null;
  const BRAND = '<h1 class="st-name" tabindex="-1"><i class="ti ti-binary-tree-2" aria-hidden="true"></i>CodeVision</h1>';

  function leave() {
    view = null;
    if (aic) { aic.quit(); aic = null; } // a job goes on in the server; this screen just stops following it
    if (ro) { ro.disconnect(); ro = null; }
    app.classList.remove('wide');
  }
  // After a screen change, keyboard focus moves to the new screen's heading or first useful control.
  function focusTo(sel) {
    const e = sel && app.querySelector(sel);
    if (e) e.focus();
  }

  function showStart(fc) {
    leave();
    screen = 'start';
    document.title = 'CodeVision';
    app.innerHTML = `<section class="st">${BRAND}
<p class="st-lead">프로젝트 폴더를 고르면 폴더와 파일이 어떻게 이어져 있는지 그림으로 보여 드려요.</p>
<div class="card st-card">
<div class="st-row"><button type="button" class="pri" data-act="pick"><i class="ti ti-folder-search" aria-hidden="true"></i>폴더 선택…</button><button type="button" data-act="demo"><i class="ti ti-sparkles" aria-hidden="true"></i>예제 프로젝트 열기</button></div>
<form class="st-form" novalidate><label class="st-lab" for="cv-path">또는 폴더 경로를 직접 넣어 주세요</label><div class="st-in"><input id="cv-path" type="text" spellcheck="false" autocomplete="off" placeholder="예: C:\\Users\\me\\projects\\my-app"><button type="submit">분석</button></div></form>
<div class="st-msg" role="status"></div>
</div>
<div class="st-rc" hidden><div class="st-lab">최근에 연 폴더</div><div class="st-list"></div></div>
</section>`;
    const inp = app.querySelector('#cv-path');
    inp.value = typed;
    inp.addEventListener('input', () => { typed = inp.value; });
    loadRecent();
    focusTo(fc);
  }

  async function loadRecent() {
    let j;
    try { j = await api('/api/recent'); } catch (e) { return; }
    const box = app.querySelector('.st-rc');
    if (!box || screen !== 'start') return;
    const items = list(j.items).filter(it => it && typeof it.path === 'string' && it.path).slice(0, 10);
    if (!items.length) return;
    const rows = box.querySelector('.st-list');
    rows.textContent = '';
    items.forEach(it => {
      const n = str(it.name) || baseName(it.path), b = el('button', 'rc'), t = el('span', 'rc-t'), p = el('span', 'rc-p');
      b.type = 'button';
      b.dataset.act = 'recent';
      b.dataset.path = it.path;
      b.disabled = busy;
      // The path breaks only after "\" or "/", like the one in the project header. A name built from those parts
      // would have spaces between them ("C:\ Users\ …"), so the button's name is given whole.
      p.innerHTML = pathHtml(it.path);
      b.setAttribute('aria-label', n + ' ' + it.path);
      t.append(el('span', 'rc-n', n), p);
      b.append(icon('ti-folder'), t);
      rows.appendChild(b);
    });
    box.hidden = false;
  }

  function setBusy(msg, name) {
    busy = !!msg;
    app.querySelectorAll('.st button, .st input').forEach(b => { b.disabled = busy; });
    const m = app.querySelector('.st-msg');
    if (!m) return;
    m.textContent = '';
    if (msg) {
      m.append(icon('ti-loader-2 spin'), el('span', '', msg));
      if (name) m.append(el('span', 'st-fn', name));
    }
  }

  function showError(msg) {
    leave();
    screen = 'error';
    document.title = 'CodeVision';
    app.innerHTML = `<section class="st">${BRAND}<div class="card er" role="alert"><div class="er-hd"><i class="ti ti-alert-circle" aria-hidden="true"></i><span class="er-msg"></span></div><div class="er-path"></div><div class="er-row"><button type="button" class="pri" data-act="retry"><i class="ti ti-refresh" aria-hidden="true"></i>다시 시도</button><button type="button" data-act="home"><i class="ti ti-arrow-left" aria-hidden="true"></i>처음 화면으로</button></div></div></section>`;
    const p = !last ? '' : last.kind === 'path' ? last.path : last.kind === 'demo' ? '예제 프로젝트' : '';
    const m = str(msg), at = named(m, p), em = app.querySelector('.er-msg');
    // A path in the message breaks only after "\" or "/", like the one in the project header (app.css .er-p).
    if (at && at[1] > at[0]) em.innerHTML = `${esc(m.slice(0, at[0]))}<span class="er-p">${pathHtml(m.slice(at[0], at[1]))}</span>${esc(m.slice(at[1]))}`;
    else em.textContent = m;
    // Not twice when the message names it; on its own line the path breaks the same way (app.css .er-path).
    app.querySelector('.er-path').innerHTML = at ? '' : pathHtml(p);
    focusTo('[data-act="retry"]');
  }

  // r: {kind: 'path', path} | {kind: 'demo'} | {kind: 'pick'} | {kind: 'boot'}
  async function run(r) {
    if (busy) return;
    last = r;
    if (r.kind === 'boot') { showProject(window.CV_BOOT_GRAPH); return; }
    if (screen !== 'start') showStart(screen ? '.st-name' : ''); // its buttons are about to be disabled
    if (r.kind === 'pick') {
      setBusy('폴더 선택 창에서 분석할 폴더를 골라 주세요.');
      let j;
      try { j = await api('/api/pick-folder', 'POST'); } catch (e) { setBusy(null); showError(e.message); return; }
      setBusy(null);
      if (typeof j.path !== 'string' || !j.path) { focusTo('[data-act="pick"]'); return; } // the user closed the dialog
      r = last = {kind: 'path', path: j.path};
    }
    const demo = r.kind === 'demo';
    if (!demo) {
      typed = r.path;
      const inp = app.querySelector('#cv-path');
      if (inp) inp.value = typed;
    }
    setQuery(r);
    setBusy('분석 중이에요…', demo ? '예제 프로젝트' : baseName(r.path));
    let g;
    try {
      g = await api(demo ? '/api/analyze?demo=1' : '/api/analyze?path=' + encodeURIComponent(r.path));
    } catch (e) {
      setBusy(null);
      showError(e.message);
      return;
    }
    setBusy(null);
    showProject(g);
  }

  function showProject(G) {
    if (!G || typeof G !== 'object' || !G.nodes || typeof G.nodes !== 'object') {
      showError('분석 결과를 읽지 못했어요. 다시 시도해 주세요.');
      return;
    }
    leave();
    screen = 'project';
    document.title = projName(G) + ' · CodeVision';
    app.classList.add('wide');
    app.innerHTML = header(G);
    const Cv = app.querySelector('.cvd');
    try {
      aic = aiCtl(G);
      view = tree(G, Cv, app.querySelector('.pnl'), aic);
      aic.attach(view);
      view.relayout();
    } catch (e) {
      console.error(e);
      showError('화면을 그리지 못했어요. 다시 시도해 주세요.');
      return;
    }
    if (window.ResizeObserver) {
      ro = new ResizeObserver(() => { requestAnimationFrame(() => { if (view) view.resize(); }); });
      ro.observe(Cv);
    }
    const h1 = app.querySelector('.ttl h1');
    if (h1) h1.focus({preventScroll: true});
    view.reveal(); // the first selected file can sit far down a big project
  }

  function header(G) {
    const st = G.stats && typeof G.stats === 'object' ? G.stats : {};
    const src = G.source && typeof G.source === 'object' ? G.source : {};
    const hidden = list(G.hidden).map(str).filter(Boolean);
    const warns = list(G.warnings).map(str).filter(Boolean);
    if (G.truncated && !warns.length) warns.push(TRUNC);

    // One run of text, so the path follows "로컬 폴더 ·" on the same line and wraps between its parts.
    let h = `<div class="cv"><div class="src"><span class="src-t"><i class="ti ti-folder" aria-hidden="true"></i><span class="src-k">${src.kind === 'demo' ? '예제 프로젝트' : '로컬 폴더'}</span>`;
    if (src.path) h += ` <span class="src-p">${pathHtml(src.path)}</span>`; // the space: for copying and screen readers
    h += '</span>';
    // The AI chip opens the AI panel under it (a sheet on phones); aiCtl fills in both.
    h += '<div class="src-r"><button type="button" class="bd tc ai" data-act="ai" aria-haspopup="dialog" aria-expanded="false" aria-controls="cv-ai">'
      + `<i class="ti ti-sparkles" aria-hidden="true"></i><span>AI 설명 꺼짐</span></button>${AI_PANEL}`
      + '<button type="button" class="chip" data-act="home">다른 폴더</button></div></div>';

    h += `<div class="ttl"><i class="ti ti-folder-open" aria-hidden="true"></i><h1 tabindex="-1">${esc(projName(G))}</h1>`
      + `<span class="ttl-s">폴더 ${fmt(st.folders)} · 파일 ${fmt(st.files)} · 시작점 ${fmt(st.entries)} · 정리 후보 ${fmt(st.orphans)}</span>`;
    if (hidden.length) h += `<span class="hid" title="${esc(hidden.join(', '))}"><i class="ti ti-eye-off" aria-hidden="true"></i>${esc(hidden.slice(0, 2).join(', '))}${hidden.length > 2 ? ' 등' : ''} 숨김</span>`;
    h += '</div>';
    if (warns.length) h += `<div class="wn">${warns.map(w => `<div><i class="ti ti-info-circle" aria-hidden="true"></i><span>${esc(w)}</span></div>`).join('')}</div>`;

    // 한눈에 보기: arrows only between items that are there.
    const stack = list(G.stack).filter(s => s && typeof s === 'object' && (s.label || s.tech));
    if (stack.length) {
      h += '<div class="stk"><span class="stk-t">한눈에 보기</span>';
      stack.forEach((s, j) => {
        const a = own(AREA, s.area) ? s.area : 'ot', p = stack[j - 1];
        const w = !p ? '' : str(s.label) === 'DB' ? '저장' : p.area === 'ui' && a !== 'ui' ? '요청' : '';
        if (w) h += `<span class="arw">${w}<i class="ti ti-arrow-right" aria-hidden="true"></i></span>`;
        h += `<span class="bd ${AREA[a][1]}">${esc([str(s.label), str(s.tech)].filter(Boolean).join(' · '))}</span>`;
      });
      h += '</div>';
    }

    const occ = new Set();
    Object.keys(G.nodes).forEach(id => {
      const x = G.nodes[id];
      if (id !== G.root && x && typeof x === 'object' && !x.o && own(AREA, x.a)) occ.add(x.a);
    });
    let lg = '';
    ['ui', 'sv', 'ot'].forEach(a => { if (occ.has(a)) lg += `<span><span class="lgd f-${a}"></span>${esc(areaName(G, a))}</span>`; });
    lg += '<span><span class="lgd f-o"></span>정리 후보</span>'
      + `<span>${SB}처음 실행되는 파일</span>`
      + '<span><span class="bd ta">연결</span>선택한 파일과 이어진 파일</span>'
      + '<span><span class="lgt"><i class="ti ti-plus" aria-hidden="true"></i></span>폴더 펼치기·접기</span>';

    // On wide screens the panel stands beside the diagram and stays in view (app.css .cvb).
    h += '<div class="cvb"><div class="card dg"><div class="cvd" role="group" aria-label="프로젝트 구조 도식"><svg aria-hidden="true"><path stroke-width="1" stroke-linecap="square"/></svg></div>'
      + `<div class="lg">${lg}</div></div><div class="card pnl" role="region" aria-label="선택한 항목 설명"></div></div>`
      + '<div class="sr-only" id="cv-say" aria-live="polite"></div></div>'; // AI news, also with the AI panel closed
    return h;
  }

  // ---------- diagram (port of design/mockup.html) ----------
  // ai: the AI explanations (aiCtl) for the labels and the side panel.
  function tree(G, Cv, Pn, ai) {
    const AR = {ui: [areaName(G, 'ui'), 'tu'], sv: [areaName(G, 'sv'), 'ts'], ot: [areaName(G, 'ot'), 'tn']};
    const MT = [{RF: 22, RL: 18, GP: 8, G0: 14, G1: 36, IF: 20, IL: 16, V: 100}, {RF: 20, RL: 16, GP: 8, G0: 10, G1: 28, IF: 18, IL: 14, V: 96}];
    const Sv = Cv.querySelector('svg'), PT = Sv.querySelector('path');

    // Copy the nodes into a prototype-free map and keep only the tree reachable from the root.
    const N = G.nodes, D = dict();
    Object.keys(N).forEach(id => {
      const x = N[id] && typeof N[id] === 'object' ? N[id] : {}, f = x.t === 'd';
      D[id] = {
        n: str(x.n) || id, p: str(x.p) || id, t: f ? 'd' : 'f', a: own(FC, x.a) ? x.a : 'ot',
        s: str(x.s), d: str(x.d), l: num(x.l), e: !!x.e, o: !!x.o, i: ICON.test(str(x.i)) ? x.i : '',
        k: f ? list(x.k).map(str) : [], u: list(x.u).map(str), b: list(x.b).map(str), h: list(x.h).map(str),
        f: list(x.f).filter(Array.isArray).map(r => [str(r[0]), str(r[1])]),
        lang: str(x.lang), x: xOf(x.x), // x: the AI's explanation, when the server has one (s, d, f are the rule-based ones)
        xs: xsOf(x.xs) // xs: why the server never sends this code file to the AI (XS_NOTE); '' for the others
      };
    });
    const RT = own(D, str(G.root)) ? str(G.root) : '.';
    if (!own(D, RT)) throw new Error('graph has no root node');
    const I = [], par = dict(), DP = dict(), seen = new Set([RT]), todo = [RT];
    DP[RT] = 0;
    while (todo.length) { // depth-first, so DOM and tab order follow the outline
      const i = todo.pop(), x = D[i];
      I.push(i);
      x.k = x.k.filter(c => own(D, c) && !seen.has(c) && seen.add(c));
      for (let j = x.k.length - 1; j >= 0; j--) { par[x.k[j]] = i; DP[x.k[j]] = DP[i] + 1; todo.push(x.k[j]); }
    }
    const dir = i => D[i].t === 'd';
    const files = v => [...new Set(v.filter(z => seen.has(z) && !dir(z)))];
    I.forEach(i => {
      const x = D[i];
      if (dir(i)) { x.u = []; x.b = []; x.h = []; } else { x.u = files(x.u); x.b = files(x.b); x.h = files(x.h); }
    });
    const anc = i => { const a = []; while (par[i] !== undefined) a.unshift(i = par[i]); return a; };
    const REL = dict();
    I.forEach(i => { REL[i] = dir(i) ? [] : [...new Set([...D[i].u, ...D[i].b])]; });
    const fill = i => (D[i].o ? '#BA7517' : FC[D[i].a]);
    // The summary under a name: the AI's while AI is on and it has one.
    const lbl = i => { const X = ai.on() && D[i].x; return X && X.s ? X.s : D[i].s; };

    // Every badge a node can show, so measure() can reserve room for the widest.
    const PR = dict(), MX = dict();
    I.forEach(i => { PR[i] = []; });
    I.forEach(f => {
      if (dir(f)) return;
      D[f].u.forEach(x => PR[x].push(['ta', D[f].n + '에서 사용']));
      D[f].b.forEach(x => PR[x].push(['ta', D[f].n + ' 사용']));
    });
    I.forEach(f => { // MX[g]: most links one outside file has into folder g
      if (dir(f) || !REL[f].length) return;
      const A = new Set(anc(f)), c = dict();
      REL[f].forEach(z => { for (let g = par[z]; g !== undefined && !A.has(g); g = par[g]) c[g] = (c[g] || 0) + 1; });
      for (const g in c) if (c[g] > (MX[g] || 0)) MX[g] = c[g];
    });
    let MXA = 0;
    I.forEach(g => {
      if (!dir(g)) return;
      if (MX[g]) { PR[g].push(['ta', nLinks(MX[g])]); MXA = Math.max(MXA, MX[g]); }
      PR[g].push(['tc', nItems(D[g].k.length)]);
    });

    // OL: nodes drawn as the outline; ROW: family row (0 = the root) of the nodes drawn as rows;
    // LN: the line they sit on (a family too wide for one line wraps, see famw); VO: siblings in drawn order.
    // late: labels changed since the diagram was last planned (AI answers put in place, see relabel).
    let path = [], sel = RT, fo = null, W = 0, PL = null, POS = dict(), ORD = [], OPEN = new Set(), SHOWN = new Set(), prev = null, OL = new Set(), ROW = dict(), LN = dict(), VO = dict(), late = false;
    const MW = dict(), SW = dict(), BW = dict(), KW = new Map(), E = dict(), ST = dict();
    const vis = i => anc(i).every(a => path.includes(a));
    const f0 = str(G.focus);
    if (seen.has(f0) && !dir(f0)) { path = anc(f0); sel = fo = f0; }
    else if (seen.has(f0)) { path = [...anc(f0), f0]; sel = f0; }
    else path = [RT];

    // One element per node, built from a template; ids go through dataset only.
    const TPL = el('div', 'nd');
    // s1/s2: 시작점·정리 후보 badges on line 1 or 2; b1/b2/tg: the relation badge.
    TPL.innerHTML = '<div class="hit" tabindex="0" role="button"><span class="dot"><i class="ti" aria-hidden="true"></i></span><span class="lb"><span class="nl"><span class="nm" data-cv-name></span><span class="s1"></span><span class="b1"></span></span><span class="sl"><span class="s2"></span><span class="b2"></span><span class="sm"></span></span><span class="tg"></span></span></div>';
    const TG = el('button', 'tgl');
    TG.type = 'button';
    const frag = document.createDocumentFragment();
    I.forEach(i => {
      const x = D[i], n = TPL.cloneNode(true), h = n.firstChild, dot = h.firstChild, lb = h.lastChild;
      const nl = lb.firstChild, sl = nl.nextSibling;
      n.dataset.cvBox = i;
      h.dataset.cvId = i;
      n.style.display = 'none';
      dot.firstChild.className = 'ti ' + (dir(i) ? 'ti-folder' : x.i || 'ti-file-code');
      nl.firstChild.textContent = x.n;
      nl.firstChild.title = x.n; // long names can be cut short in the outline
      ST[i] = (x.e ? SB : '') + (x.o ? OB : '');
      nl.children[1].innerHTML = ST[i];
      sl.lastChild.textContent = sl.lastChild.title = lbl(i);
      let t = null;
      if (dir(i) && x.k.length) { t = TG.cloneNode(true); t.dataset.cvToggle = i; n.appendChild(t); }
      E[i] = {n, h, dot, ic: dot.firstChild, lb, nl, s1: nl.children[1], b1: nl.lastChild, s2: sl.firstChild, b2: sl.children[1], sm: sl.lastChild, tg: lb.lastChild, t, sp: 1};
      frag.appendChild(n);
    });
    Cv.appendChild(frag);

    // Text widths, read once per relayout from one hidden batch (one layout pass).
    function measure() {
      const z = el('div', 'mz'), K = new Map();
      I.forEach(i => PR[i].forEach(b => K.set(b[0] + b[1], b)));
      for (let v = 1; v <= MXA; v++) K.set('ta' + nLinks(v), ['ta', nLinks(v)]);
      const ks = [...K.values()], n = I.length;
      z.innerHTML = I.map(i => { const x = D[i]; return `<span class="nl"><span class="nm">${esc(x.n)}</span>${x.e ? SB : ''}${x.o ? OB : ''}</span><span class="sm">${esc(lbl(i))}</span>`; }).join('')
        + ks.map(b => bd(b[0], b[1])).join('');
      Cv.appendChild(z);
      const c = z.children, w = e => Math.ceil(e.getBoundingClientRect().width);
      I.forEach((i, k) => { MW[i] = w(c[2 * k]); SW[i] = w(c[2 * k + 1]); });
      KW.clear();
      ks.forEach((b, j) => KW.set(b[0] + b[1], w(c[2 * n + j])));
      I.forEach(i => { BW[i] = PR[i].reduce((m, b) => Math.max(m, KW.get(b[0] + b[1])), 0); });
      z.remove();
    }
    // Only the summary widths of ids, after their labels changed (AI explanations came in, or AI was turned on or off).
    function remeasure(ids) {
      const z = el('div', 'mz');
      z.innerHTML = ids.map(i => `<span class="sm">${esc(lbl(i))}</span>`).join('');
      Cv.appendChild(z);
      ids.forEach((i, k) => { SW[i] = Math.ceil(z.children[k].getBoundingClientRect().width); });
      z.remove();
    }

    const sum = a => a.reduce((s, v) => s + v, 0);
    const wf = (lo, hi, B) => {
      let a = 0, b = hi.reduce((m, v) => Math.max(m, v), 0);
      for (let k = 0; k < 30; k++) { const t = (a + b) / 2; sum(hi.map((h, j) => Math.max(lo[j], Math.min(h, t)))) > B ? b = t : a = t; }
      return hi.map((h, j) => Math.max(lo[j], Math.min(h, a)));
    };
    // Column widths and gap for one line of siblings, or null if it cannot fit in Wl.
    function fam(L, m, Wl = W) {
      const n = L.length, r = L.map(i => dir(i) ? m.RF : m.RL), lo = L.map(i => Math.max(MW[i], BW[i])), hi = L.map((i, j) => Math.max(lo[j], SW[i])), S0 = 2 * sum(r) + n * m.GP;
      let g = m.G1, C = hi;
      if (S0 + sum(hi) + (n - 1) * g > Wl) {
        g = (Wl - S0 - sum(hi)) / Math.max(1, n - 1);
        if (g < m.G0) {
          g = m.G0;
          const B = Wl - S0 - (n - 1) * g, fh = hi.map((h, j) => dir(L[j]) ? h : lo[j]);
          if (sum(lo) > B) return null;
          C = sum(fh) > B ? wf(lo, fh, B) : wf(fh, hi, B);
        }
      }
      return {L, r, C, g, m};
    }
    // A family too wide for one line wraps (wrap on): its files on up to three lines, then all of
    // its folders on the last line, so an open folder's family still hangs straight below it. The
    // upper lines keep LM px free on the left for the spine that joins every line's bus.
    // Returns the lines (one line when the family fits), or null: then it is drawn as the outline.
    // Narrow screens (phones) never wrap: the outline is their layout (docs/architecture.md 7-1).
    const LM = 18, SPX = 5, MAXL = 4, WRAPW = 560;
    function famw(L, m, wrap) {
      const one = fam(L, m);
      if (one) return [one];
      if (!wrap || L.length < 2 || W < WRAPW) return null;
      const ds = L.filter(dir), fs = L.filter(i => !dir(i)), Wu = W - LM;
      const last = ds.length ? fam(ds, m) : null;
      if (ds.length && !last) return null;
      const room = MAXL - (last ? 1 : 0), lines = [];
      for (const f of fs) { // fewest lines first
        const cur = lines[lines.length - 1];
        if (cur && fam([...cur, f], m, Wu)) cur.push(f);
        else if (lines.length < room && fam([f], m, Wu)) lines.push([f]);
        else return null;
      }
      let rows = lines;
      if (lines.length > 1) { // the same number of lines, with even counts when they fit
        const q = lines.length, even = [];
        for (let j = 0, at = 0; j < q; j++) { const c = Math.floor(fs.length / q) + (j < fs.length % q ? 1 : 0); even.push(fs.slice(at, at += c)); }
        if (even.every(c => fam(c, m, Wu))) rows = even;
      }
      const out = rows.map(c => fam(c, m, Wu));
      if (last) out.push(last);
      return out.length > 1 ? out : null;
    }
    const xs = (F, g) => { const x = [F.r[0]]; for (let j = 1; j < F.L.length; j++) x.push(x[j - 1] + F.r[j - 1] + F.m.GP + F.C[j - 1] + g + F.r[j]); return x; };
    // Centres for a line under parent x px, from x0 on, keeping clear of the line above (U).
    function place(F, px, U, x0 = 0) {
      const n = F.L.length, G = [F.g];
      let B = null;
      if (!n) return [];
      if (px != null && n > 1) for (let g = Math.ceil(F.m.G0 - 2); g <= 3 * F.m.G1; g++) G.push(g);
      for (const g of G) {
        const x = xs(F, g), R = W - x0 - (x[n - 1] + F.r[n - 1] + F.m.GP + F.C[n - 1]);
        if (R < -.5) continue;
        const c = px == null ? R / 2 : Math.max(0, Math.min(R, px - x0 - (x[0] + x[n - 1]) / 2)), c0 = Math.round(c);
        for (let s = -40; s <= 40; s++) {
          const o = c0 + s;
          if (o < 0 || o > Math.max(0, R)) continue;
          const X = x.map(v => Math.round(v + o + x0));
          let p = Math.abs(g - F.g) + 2 * Math.abs(s);
          if (px != null) X.forEach(v => { const d = Math.abs(v - px); if (d > .5 && d < 14) p += 1e3; U.forEach(u => { const e = Math.abs(v - u); p += e < .5 ? 60 : e < 52 ? (52 - e) * 4 : 0; }); });
          if (!B || p < B.p) B = {p, X};
        }
      }
      return B ? B.X : xs(F, F.g).map(v => Math.round(v + x0));
    }
    // Plans only the visible families (the root's children and those of each open folder on the path).
    // k: how many of them, from the top, are drawn as rows; the rest hang under path[k] as the outline.
    // k = 0 (the root row does not fit) makes the whole diagram the outline.
    function plan() {
      const fs = path.length ? path : [RT]; // a closed root still sits where its row would put it
      let B = null;
      // standard, then compact if that draws more of the visible rows; only then wrapped lines
      for (const wrap of [false, true]) {
        for (const m of MT) {
          const F = dict();
          let k = 0;
          while (k < fs.length) { const v = famw(D[fs[k]].k, m, wrap); if (!v) break; F[fs[k++]] = v; }
          if (!B || k > B.k) B = {m, F, k};
          if (k === fs.length) return B;
        }
      }
      if (!B.k) B.m = MT[W < 480 ? 1 : 0];
      return B;
    }
    const rd = i => (dir(i) ? PL.m.RF : PL.m.RL);

    function draw() {
      const m = PL.m, k = PL.k, S = [], PI = 2 * m.RF + 12;
      POS = dict();
      ORD = [];
      OL = new Set();
      ROW = dict();
      LN = dict();
      VO = dict();
      const wk = i => { ORD.push(i); if (OPEN.has(i)) D[i].k.forEach(wk); };
      wk(RT);
      // The indented outline of everything open under f (already placed): one item per PI from y0, each level
      // IND to the right. The stem drops straight from f; under a row (ye given) it bends left at ye first
      // when f sits too far right to leave the labels room.
      const outline = (f, y0, ye) => {
        const L = [], z = DP[f], q = POS[f];
        const w = i => { L.push(i); if (OPEN.has(i)) D[i].k.forEach(w); };
        if (OPEN.has(f)) D[f].k.forEach(w);
        if (!L.length) return;
        // Indent shrinks a little only when a deep branch would leave labels no room.
        const md = L.reduce((v, i) => Math.max(v, DP[i] - z), 1);
        const IND = Math.max(m.RF + 2, Math.min(m.RF + 12, Math.floor((W - 2 * m.RF - m.GP - 140) / md)));
        const need = L.reduce((v, i) => Math.max(v, (DP[i] - z) * IND + m.RF + m.GP + Math.max(MW[i], BW[i], 140)), 0);
        const sx = ye == null ? q.x : Math.max(m.RF, Math.min(q.x, Math.floor(W - need)));
        L.forEach((i, j) => { const x = sx + (DP[i] - z) * IND; POS[i] = {x, y: y0 + j * PI, C: W - x - m.RF - m.GP}; OL.add(i); });
        path.slice(path.indexOf(f)).forEach(g => {
          const p = POS[g], ks = D[g].k, s = g === f ? sx : p.x;
          if (!ks.length) return;
          const end = POS[ks[ks.length - 1]].y + .5;
          S.push(s === p.x ? `M${p.x + .5} ${p.y + rd(g) - 1}V${end}` : `M${p.x + .5} ${p.y + rd(g) - 1}V${ye}H${s + .5}V${end}`);
          ks.forEach(c => S.push(`M${s + .5} ${POS[c].y + .5}H${POS[c].x - rd(c) + 1}`));
        });
      };
      if (k) {
        const V = m.V, T = Math.max(m.RF, 18) + 8, F = PL.F, L0 = F[RT], X1 = place(L0[0], null, [], L0.length > 1 ? LM : 0);
        const rx = X1.length ? Math.round((X1[0] + X1[X1.length - 1]) / 2) : m.RF;
        POS[RT] = {x: rx, y: T, C: Math.min(Math.max(MW[RT], BW[RT], SW[RT]), W - rx - m.RF - m.GP)};
        ROW[RT] = 0;
        LN[RT] = 0;
        for (let d = 1; d <= Math.min(k, path.length); d++) {
          const p = path[d - 1], q = POS[p], Fw = F[p], n = Fw.length, Xs = [];
          if (!Fw[0].L.length) break;
          // Each line keeps clear of the one above: for the first, the parent's own line (without the parent).
          let U = d === 1 ? [] : VO[path[d - 2]].filter(i => i !== p && LN[i] === LN[p]).map(i => POS[i].x);
          Fw.forEach((Fl, li) => {
            const first = d === 1 && li === 0, X = first ? X1 : place(Fl, d === 1 ? Math.round(W / 2) : q.x, U, li < n - 1 ? LM : 0);
            const y = q.y + (li + 1) * V;
            Fl.L.forEach((i, j) => { POS[i] = {x: X[j], y, C: Fl.C[j]}; ROW[i] = d; LN[i] = d * 8 + li; });
            Xs.push(X);
            U = X;
          });
          VO[p] = Fw.flatMap(Fl => Fl.L);
          const yb = q.y + V / 2 + .5;
          if (n === 1) {
            const X = Xs[0], L = Fw[0].L, y = q.y + V, a = Math.min(q.x, ...X), b = Math.max(q.x, ...X);
            if (X.length === 1 && X[0] === q.x) S.push(`M${q.x + .5} ${q.y + rd(p) - 1}V${y - rd(L[0]) + 1}`);
            else { S.push(`M${q.x + .5} ${q.y + rd(p) - 1}V${yb}M${a + .5} ${yb}H${b + .5}`); X.forEach((x, j) => S.push(`M${x + .5} ${yb}V${y - rd(L[j]) + 1}`)); }
          } else {
            // Wrapped: a bus over every line, joined by a spine left of all the upper lines.
            const sp = Math.max(SPX, Math.min(...Fw.slice(0, -1).map((Fl, li) => Xs[li][0] - rd(Fl.L[0]))) - 12);
            S.push(`M${q.x + .5} ${q.y + rd(p) - 1}V${yb}M${sp + .5} ${yb}V${q.y + n * V - V / 2 + .5}`);
            Fw.forEach((Fl, li) => {
              const X = Xs[li], y = q.y + (li + 1) * V, b = y - V / 2 + .5;
              S.push(`M${Math.min(sp, ...X, li ? Infinity : q.x) + .5} ${b}H${Math.max(...X, li ? -Infinity : q.x) + .5}`);
              X.forEach((x, j) => S.push(`M${x + .5} ${b}V${y - rd(Fl.L[j]) + 1}`));
            });
          }
        }
        // The first family that does not fit hangs under its parent as the outline, starting one row lower.
        if (k < path.length) { const q = POS[path[k]]; outline(path[k], q.y + V, q.y + V / 2 + .5); }
      } else {
        const T = Math.max(m.RF, 18) + 6;
        POS[RT] = {x: m.RF, y: T, C: W - 2 * m.RF - m.GP};
        OL.add(RT);
        outline(RT, T + PI);
      }
      PT.setAttribute('d', S.join(''));
      ORD = ORD.filter(i => POS[i]);
      const now = new Set(ORD);
      SHOWN.forEach(i => { if (!now.has(i)) E[i].n.style.display = 'none'; });
      ORD.forEach(i => {
        const q = POS[i], e = E[i], r = rd(i), t = Math.max(r, 18), f = dir(i);
        e.n.style.display = '';
        e.n.style.left = q.x - r + 'px';
        e.n.style.top = q.y - t + 'px';
        e.dot.style.cssText = `width:${2 * r}px;height:${2 * r}px;margin-top:${t - r}px;font-size:${f ? m.IF : m.IL}px;background:${fill(i)}`;
        e.lb.style.margin = `${t - 18}px 0 0 ${OL.has(i) ? m.RF - r + m.GP : m.GP}px`;
        e.lb.style.maxWidth = Math.max(0, Math.floor(q.C)) + 'px';
        if (e.t) { const c = Math.round(r * .707 - 9); e.t.style.left = r + c + 'px'; e.t.style.top = t + c + 'px'; }
      });
      SHOWN = now;
    }

    // Selection ring, folder toggles and relation badges for the visible nodes.
    function upd() {
      const af = fo && POS[fo] ? fo : null, A = af && D[af], Bs = new Set(A ? A.b : []), Us = new Set(A ? A.u : []), cnt = dict();
      if (A) REL[af].forEach(z => { for (let g = par[z]; g !== undefined; g = par[g]) cnt[g] = (cnt[g] || 0) + 1; });
      if (prev !== null && prev !== sel) { E[prev].n.classList.remove('sel'); E[prev].h.setAttribute('aria-current', 'false'); }
      ORD.forEach(i => {
        const e = E[i], x = D[i], o = OPEN.has(i), f = dir(i), q = POS[i];
        if (f) {
          e.ic.className = 'ti ' + (o ? 'ti-folder-open' : 'ti-folder');
          if (e.t) {
            e.t.innerHTML = `<i class="ti ti-${o ? 'minus' : 'plus'}" aria-hidden="true"></i>`;
            const l = x.n + (o ? ' 접기' : ' 펼치기');
            e.t.setAttribute('aria-expanded', o);
            e.t.setAttribute('aria-label', l);
            e.t.title = l;
          }
        }
        e.n.classList.toggle('sel', i === sel);
        e.h.setAttribute('aria-current', i === sel);
        let g = null;
        if (A && Bs.has(i)) g = ['ta', A.n + ' 사용'];
        else if (A && Us.has(i)) g = ['ta', A.n + '에서 사용'];
        else if (f && !o) g = cnt[i] ? ['ta', nLinks(cnt[i])] : ['tc', nItems(x.k.length)];
        // Outline: a badge that would overflow the name line moves to the summary line.
        const ol = OL.has(i), gw = g ? 5 + (KW.get(g[0] + g[1]) || BW[i]) : 0, two = ol && MW[i] + gw > q.C + .5;
        const sp = two && ST[i] && MW[i] > q.C + .5 ? 2 : 1;
        if (ST[i] && e.sp !== sp) { e.s1.innerHTML = sp === 1 ? ST[i] : ''; e.s2.innerHTML = sp === 2 ? ST[i] : ''; e.sp = sp; }
        e.b1.innerHTML = e.b2.innerHTML = e.tg.innerHTML = '';
        if (g) {
          const hb = bd(g[0], g[1]);
          if (!ol) e.tg.innerHTML = hb;
          else if (two) e.b2.innerHTML = hb;
          else e.b1.innerHTML = hb;
        }
      });
      prev = sel;
    }

    // Canvas height, plus the first row (0 = the root) where a node overlaps, leaves the card or has its
    // name cut short; Infinity if none. The outline part may cut long names short, as the outline always has.
    function fit() {
      const cr = Cv.getBoundingClientRect(), L = new Map();
      let H = 0, bad = Infinity;
      ORD.forEach(i => {
        const q = POS[i], b = E[i].n.getBoundingClientRect();
        H = Math.max(H, b.bottom - cr.top, q.y + rd(i) + 4);
        if (OL.has(i)) return;
        if (b.left < cr.left - .5 || b.right > cr.right + .5 || Math.max(MW[i], BW[i]) > q.C + .5) bad = Math.min(bad, ROW[i]);
        if (!L.has(LN[i])) L.set(LN[i], []);
        L.get(LN[i]).push([b, ROW[i]]);
      });
      L.forEach(l => { l.sort((a, b) => a[0].left - b[0].left); for (let j = 1; j < l.length; j++) if (l[j][0].left < l[j - 1][0].right + 2) bad = Math.min(bad, l[j][1]); });
      H = Math.ceil(H + 2);
      Cv.style.height = H + 'px';
      Sv.setAttribute('width', W);
      Sv.setAttribute('height', H);
      return bad;
    }

    const lab = t => `<div class="pl">${esc(t)}</div>`;
    // Chip text: the name, with parent folders put in front while two chips of one list would read the
    // same (exceptions/__init__.py, models/__init__.py; a top-level file gets the project's name).
    function labels(ids) {
      const out = dict(), parts = dict();
      ids.forEach(i => { out[i] = D[i].n; parts[i] = i === RT ? [D[i].n] : [D[RT].n, ...D[i].p.split('/').filter(Boolean)]; });
      for (let depth = 2, left = ids; left.length && depth < 64; depth++) {
        const count = new Map();
        left.forEach(i => count.set(out[i], (count.get(out[i]) || 0) + 1));
        left = left.filter(i => count.get(out[i]) > 1 && parts[i].length >= depth);
        left.forEach(i => { out[i] = parts[i].slice(-depth).join('/'); });
      }
      return out;
    }
    // sort: the connection lists read in name order; a folder's contents keep the diagram's order.
    function chips(box, ids, hs, sort) {
      if (!ids.length) { box.className = 'none'; box.textContent = '없음'; return; }
      box.className = 'chips';
      const H = new Set(hs), T = labels(ids);
      (sort ? [...ids].sort((a, b) => byName(T[a], T[b])) : ids).forEach(i => {
        const b = el('button', 'chip', T[i] + (H.has(i) ? ' · HTTP' : ''));
        b.type = 'button';
        b.dataset.go = i;
        b.title = D[i].p;
        box.appendChild(b);
      });
    }
    // keep: the same node drawn again (an AI answer came in, 다시 시도, 기본 설명 보기): the panel keeps its
    // scroll, and the focus stays on the same control (or the AI line, when that control is gone).
    function panel(keep) {
      const id = sel, x = D[id], ar = AR[x.a], dr = dir(id);
      ai.want(id); // while AI is on, a code file or folder without an AI explanation asks for one
      // The AI's text when it is shown; its f has the same signatures, with the rule text where it said nothing.
      const X = ai.use(id, x) ? x.x : null, s = X && X.s || x.s, d = X && X.d || x.d;
      const fs = X ? x.f.map(([k, v]) => [k, X.f.get(k) || v]) : x.f;
      let tags = x.o ? OB : id === RT ? bd('tn', '프로젝트') : bd(ar[1], ar[0]);
      if (x.e) tags += SB;
      const ic = dr ? (OPEN.has(id) ? 'ti-folder-open' : 'ti-folder') : x.i || 'ti-file-code';
      const meta = dr ? nItems(x.k.length) : x.l ? fmt(x.l) + '줄' : '';
      // The bold line is left out when the description starts with it anyway (a docstring's first
      // sentence, often cut short with "…").
      const sq = t => t.replace(/\s+/g, ' ').trim(), dup = sq(d).startsWith(sq(s).replace(/(…|\.\.\.)$/, '').trimEnd());
      let h = `<div class="ph"><span class="pd ${x.o ? 'f-o' : 'f-' + x.a}"><i class="ti ${ic}" aria-hidden="true"></i></span><span class="pn">${esc(x.n)}</span>${tags}<span class="pm">${meta}</span></div>`
        + `<div class="pp">${esc(x.p)}</div>${ai.row(id, x)}${dup ? '' : `<div class="ps">${esc(s)}</div>`}<div class="pds">${esc(d)}</div>`;
      if (dr) h += `<div class="sc">${lab('들어 있는 것')}<div data-chips="k"></div></div>`;
      else {
        const c = `<div class="sc">${lab('이 파일이 쓰는 파일')}<div data-chips="u"></div><div class="gap"></div>${lab('이 파일을 쓰는 파일')}<div data-chips="b"></div></div>`;
        // Each description sits right under its name (app.css .fn).
        h += fs.length ? `<div class="pg"><div class="sc">${lab('주요 함수')}${fs.map(([k, v]) => `<div class="fn"><code>${esc(k)}</code>${v ? `<span>${esc(v)}</span>` : ''}</div>`).join('')}</div>${c}</div>` : c;
      }
      const a = keep ? document.activeElement : null, was = a && a !== Pn && Pn.contains(a) ? a : null, top = Pn.scrollTop;
      Pn.innerHTML = h;
      Pn.querySelectorAll('[data-chips]').forEach(box => chips(box, x[box.dataset.chips], x.h, box.dataset.chips !== 'k'));
      if (!keep) { Pn.scrollTop = 0; return; } // beside the diagram the panel scrolls by itself when it is taller than the window
      Pn.scrollTop = top;
      if (was) {
        const k = was.dataset.ai != null ? 'ai' : was.dataset.go != null ? 'go' : '';
        const t = (k && [...Pn.querySelectorAll(`[data-${k}]`)].find(b => b.dataset[k] === was.dataset[k])) || Pn.querySelector('.pa');
        if (t) t.focus({preventScroll: true});
      }
    }

    // Planned on every render, so a new open path gets a new plan. A row that turns out not to fit
    // once drawn becomes the outline under its parent (the root row: the whole diagram).
    // mode: 'keep' draws the panel again for the same node (see panel), 'skip' leaves it as it is.
    function render(mode) {
      OPEN = new Set(path);
      late = false;
      PL = plan();
      for (;;) {
        draw();
        upd();
        const b = fit();
        if (b > PL.k) break;
        PL.k = Math.max(0, b - 1);
        if (!PL.k) PL.m = MT[W < 480 ? 1 : 0];
      }
      if (mode !== 'skip') panel(mode === 'keep');
    }
    // New labels for ids (AI explanations came in, or AI was turned on or off): their summaries have new widths,
    // so the diagram is planned again with the same open folders and selection, and the page keeps its scroll.
    // force: draw the side panel again even when the selected node is not one of them.
    // quiet: AI answers that come in by themselves (전체 설명 batches, the answer for a file picked earlier). Nothing
    // moves under the pointer: each new text goes in place, cut short at its node's current width (its tooltip has
    // it whole), and the next plan (a click, a folder, the window, the end of 전체 설명: replan) gives it its room.
    function relabel(ids, force, quiet) {
      ids = ids.filter(i => seen.has(i));
      if (!W) return;
      if (!ids.length) { if (force) panel(true); return; }
      ids.forEach(i => { E[i].sm.textContent = E[i].sm.title = lbl(i); });
      remeasure(ids);
      if (quiet) {
        late = true;
        if (force || ids.includes(sel)) panel(true);
        return;
      }
      const sx = window.scrollX, sy = window.scrollY;
      render(force || ids.includes(sel) ? 'keep' : 'skip');
      if (window.scrollX !== sx || window.scrollY !== sy) window.scrollTo(sx, sy);
    }
    // The plan again for labels put in quietly (see relabel), with the same open folders, selection and scroll.
    function replan() {
      if (!late || !W) return;
      const sx = window.scrollX, sy = window.scrollY;
      render('skip');
      if (window.scrollX !== sx || window.scrollY !== sy) window.scrollTo(sx, sy);
    }
    function act(i) {
      if (dir(i)) { if (!path.includes(i)) path = [...anc(i), i]; } else { if (!vis(i)) path = anc(i); fo = i; }
      sel = i;
      render();
    }
    function tog(i) {
      const k = path.indexOf(i);
      path = k < 0 ? [...anc(i), i] : path.slice(0, k);
      if (!vis(sel)) sel = i;
      render();
    }
    // Wide screens keep the panel beside the diagram, always in view (app.css: sticky); narrow ones below it.
    const side = () => getComputedStyle(Pn).position === 'sticky';
    // Scrolls the page just enough to show e; mid: a little above the middle of the window instead.
    function show(e, mid) {
      const r = e.getBoundingClientRect(), h = window.innerHeight || document.documentElement.clientHeight, M = 12;
      if (r.top >= M && r.bottom <= h - M) return;
      const dy = mid ? r.top + r.height / 2 - h * .4 : r.top < M || r.height > h - 2 * M ? r.top - M : r.bottom - h + M;
      window.scrollBy(0, Math.round(dy));
    }
    // A file chosen in the diagram: below the diagram its explanation can be out of sight, so it comes
    // into view. Folders only open, and the diagram is what the user looks at then.
    function pick(i) {
      act(i);
      if (!dir(i) && !side()) show(Pn);
    }
    function key(e, i) {
      const k = e.key;
      if (k === 'Enter' || k === ' ') { e.preventDefault(); pick(i); return; }
      const s = par[i] === undefined ? [RT] : VO[par[i]] || D[par[i]].k, j = s.indexOf(i), o = ORD.indexOf(i);
      const dn = OPEN.has(i) ? (VO[i] || D[i].k)[0] : null;
      // Rows: siblings left and right in drawn order (a wrapped family reads line by line), parent up,
      // first child down. Outline: up and down in list order (up from the first item reaches the
      // parent above it), parent left, first child right.
      const t = !OL.has(i)
        ? (k === 'ArrowRight' ? s[j + 1] : k === 'ArrowLeft' ? s[j - 1] : k === 'ArrowUp' ? par[i] : k === 'ArrowDown' ? dn : null)
        : (k === 'ArrowDown' ? OL.has(ORD[o + 1]) && ORD[o + 1] : k === 'ArrowUp' ? ORD[o - 1] : k === 'ArrowLeft' ? par[i] : k === 'ArrowRight' ? dn : null);
      if (t && POS[t]) { e.preventDefault(); E[t].h.focus(); }
    }

    Cv.addEventListener('click', e => {
      const t = e.target.closest('[data-cv-toggle]');
      if (t) { tog(t.dataset.cvToggle); return; }
      const h = e.target.closest('[data-cv-id]');
      if (h) pick(h.dataset.cvId);
    });
    Cv.addEventListener('keydown', e => {
      const h = e.target.closest('[data-cv-id]');
      if (h) key(e, h.dataset.cvId);
    });
    // A chip redraws the diagram above or beside the panel. Below the diagram, the panel keeps its place
    // on screen (its top comes back into view if it was scrolled past); beside it, the panel stays put
    // and the newly chosen node is brought into view.
    Pn.addEventListener('click', e => {
      const b = e.target.closest('[data-go]');
      if (!b) return;
      const g = b.dataset.go, top = Pn.getBoundingClientRect().top;
      act(g);
      if (side()) show(E[g].h, true);
      else {
        const dy = Pn.getBoundingClientRect().top - (top < 0 ? 12 : top);
        if (Math.abs(dy) > .5) window.scrollBy(0, Math.round(dy));
      }
      E[g].h.focus({preventScroll: true});
    });

    function relayout() {
      W = Cv.clientWidth || 655;
      measure();
      render();
    }
    return {
      relayout, resize() { if (Cv.clientWidth !== W) relayout(); }, reveal() { if (POS[sel]) show(E[sel].h, true); },
      // for aiCtl
      sel: () => sel,
      node: i => (own(D, i) && seen.has(i) ? D[i] : null),
      ids: () => I,
      setX(i, v) { const X = own(D, i) && seen.has(i) ? xOf(v) : null; if (X) D[i].x = X; return !!X; },
      clearX() { const c = I.filter(i => D[i].x); c.forEach(i => { D[i].x = null; }); return c; },
      // the server's node.xs from a new analysis; true when it changed
      setXs(i, v) { const m = xsOf(v); if (!own(D, i) || !seen.has(i) || D[i].xs === m) return false; D[i].xs = m; return true; },
      relabel,
      replan,
      repaint() { if (W) panel(true); }
    };
  }

  // ---------- AI explanations ----------
  // A model on this computer, through the server (docs/architecture.md 6 and 7). The header chip opens the AI
  // panel (#cv-ai): the server's status sentence, "Ollama 켜기", the on/off switch, the model, and 전체 설명
  // 만들기 with its progress. It is a popover under the chip, and a sheet along the bottom on phones (app.css).
  // While AI is on, the tree labels and the side panel use node.x where there is one, and a selected code file
  // or folder without one goes to /api/ai/explain, one request at a time. While AI is off, nothing here asks
  // the server for anything by itself.
  // The estimate before 전체 설명 만들기, measured with qwen2.5:7b fully on an 8 GB GPU: a folder takes about 1 s,
  // a file about 2 s plus 0.55 s for each main function the AI describes (8 at most). Above AI_ASK s, ask first.
  const AI_DIR = 1, AI_FILE = 2, AI_FN = 0.55, AI_ASK = 300;
  const AI_COLD = 3000, AI_BATCH = 1500, AI_POLL = 1000; // ms: before the cold-start note; between job batches; job checks
  // ms to wait for one explanation and for Ollama to start: longer than the server waits (180 s, 20 s),
  // so its own sentence comes first
  const AI_WAIT = 200000, AI_START = 45000;
  const AI_NONE = 'AI 설명을 쓸 수 없어요.';
  const COLD_NOTE = '처음 한 번은 AI가 준비하느라 30초쯤 걸릴 수 있어요.';
  const REFRESHING = '이미 만들어 둔 설명을 확인하고 있어요…'; // while refreshX runs (the AI panel and the side panel)
  // node.xs (/api/analyze, while AI is on): why the server never sends this code file to the AI. The side panel
  // says so instead of asking, and 전체 설명 만들기 does not count it. The same texts as messages.MARK_NOTES.
  const XS_NOTE = {secret: '비밀 정보가 들어 있을 수 있는 파일이라 AI에 보내지 않아요.', too_big: '파일이 너무 커서 AI 설명을 만들지 않아요.',
    unreadable: '이 파일을 읽을 수 없어서 AI 설명을 만들지 않아요.'};
  const xsOf = v => (typeof v === 'string' && own(XS_NOTE, v) ? v : '');
  const SHEET = window.matchMedia ? window.matchMedia('(max-width: 560px)') : {matches: false}; // app.css, the same width
  const OLLAMA = ['running', 'stopped', 'missing'];
  const AI_PANEL = '<div class="aip" id="cv-ai" role="dialog" aria-labelledby="cv-ai-h" aria-describedby="cv-ai-m" hidden>'
    + '<div class="aip-hd"><h2 id="cv-ai-h" tabindex="-1"><i class="ti ti-sparkles" aria-hidden="true"></i>AI 설명</h2>'
    + '<button type="button" class="aip-x" data-ai="close" aria-label="닫기" title="닫기"><span aria-hidden="true">×</span></button></div>'
    + '<p class="aip-m" id="cv-ai-m" role="status"></p>'
    + '<p class="aip-help" hidden>ollama.com에서 Ollama를 받아 설치해 주세요.</p>'
    + '<div class="aip-st" hidden><button type="button" class="pri" data-ai="start"><i class="ti ti-loader-2 spin" aria-hidden="true" hidden></i><span>Ollama 켜기</span></button></div>'
    + '<div class="aip-row" data-p="sw"><label class="aip-l" for="cv-ai-sw">AI 설명 쓰기</label>'
    + '<button type="button" class="sw" id="cv-ai-sw" role="switch" aria-checked="false" data-ai="switch"><span class="sw-k"></span></button></div>'
    + '<div class="aip-row" data-p="model"><label class="aip-l" for="cv-ai-model">AI 모델</label><select id="cv-ai-model"></select></div>'
    + '<p class="aip-hint" hidden></p>'
    + '<p class="aip-err" role="alert" hidden></p>'
    + '<div class="aip-all" hidden><p class="aip-end" hidden></p><p class="aip-est"></p><button type="button" class="pri" data-ai="run">전체 설명 만들기</button>'
    + '<div class="aip-cf" hidden><p class="aip-cft"></p><div class="aip-btns"><button type="button" class="pri" data-ai="go">시작하기</button><button type="button" data-ai="nogo">취소</button></div></div>'
    + '<div class="aip-job" hidden><div class="aip-jt"><span class="aip-jn"></span><span class="aip-jc"></span></div>'
    + '<div class="aip-bar" role="progressbar" aria-label="전체 설명 진행 상황" aria-valuemin="0"><span></span></div>'
    + '<p class="aip-cur"></p><p class="aip-eta"></p><button type="button" data-ai="stop">멈추기</button></div>'
    + '</div></div><div class="aib" hidden></div>';

  const txt = v => (typeof v === 'string' ? v.trim() : '');
  // The status block (graph.ai, /api/ai/status, /settings, /start). ok: the server has local AI; the skeleton
  // before it sent only provider, available and message, and then the panel shows that sentence alone.
  function aiStatus(a) {
    a = a && typeof a === 'object' ? a : {};
    const ok = typeof a.enabled === 'boolean', enabled = ok && a.enabled;
    return {
      ok, enabled, available: enabled && a.available === true, model: txt(a.model), message: txt(a.message),
      ollama: OLLAMA.includes(a.ollama) ? a.ollama : '', canStart: a.can_start === true,
      models: list(a.models).filter(m => m && typeof m === 'object' && txt(m.name)).map(m => ({name: txt(m.name), size: +m.size || 0})),
      rec: list(a.recommended).map(txt).filter(Boolean)
    };
  }
  // node.x, {s, d, f: [[signature, text], …]}, as {s, d, f: Map(signature -> text)}; null when it says nothing.
  function xOf(v) {
    if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
    const s = txt(v.s), d = txt(v.d), f = new Map();
    list(v.f).forEach(r => { if (Array.isArray(r) && txt(r[1])) f.set(str(r[0]), txt(r[1])); });
    return s || d || f.size ? {s, d, f} : null;
  }
  const canAI = x => !!x && (x.t === 'd' || !!x.lang); // code files and folders (the root too)
  const gb = b => (b >= 1e9 ? (b / 1e9).toFixed(1) + 'GB' : b >= 1e6 ? Math.round(b / 1e6) + 'MB' : '');
  // "약 18분", "약 1시간 5분"
  function dur(sec) {
    const m = Math.max(1, Math.ceil(sec / 60));
    return '약 ' + (m < 60 ? fmt(m) + '분' : fmt(Math.floor(m / 60)) + '시간' + (m % 60 ? ' ' + (m % 60) + '분' : ''));
  }
  // The AI panel is updated in place (a job reports every second): only what changed is written, so focus,
  // a screen reader's place and an open model list stay where they are.
  const putT = (e, t) => { if (e.textContent !== t) e.textContent = t; };
  const putA = (e, k, v) => { if (e.getAttribute(k) !== v) e.setAttribute(k, v); };
  const putH = (e, h) => { if (e.hidden !== h) e.hidden = h; };
  const shown = e => !!e && e.getClientRects().length > 0;

  function aiCtl(G) {
    const src = G.source && typeof G.source === 'object' ? G.source : {};
    const proj = src.kind === 'demo' ? 'demo' : txt(src.path); // the project, as /api/ai/ calls name it
    const errs = dict(), pend = new Map(); // errs: node -> {msg, code, last (the server will not explain it), busy}
    let st = aiStatus(G.ai), V = null, dead = false, stamp = 0, applied = 0, gen = 0, xReq = 0;
    let chip = null, pop = null, back = null, P = null, open = false, ask = false, busy = '', err = '', refreshing = false;
    let req = null, coldT = 0, peek = null, job = null, pollT = 0, flushT = 0, lastFlush = 0, opener = null;

    const on = () => st.enabled;
    const running = () => !!job && job.state === 'running';
    const use = (i, x) => on() && !!x.x && peek !== i; // the side panel shows the AI's text (peek: the rule text instead)
    // The node's own request for the model in use is out. Then that request speaks for it while refreshX runs: its
    // loader (and cold note) stays on the AI line, and its answer is what the screen reader hears (see settled).
    const asking = i => !!req && req.id === i && req.gen === gen;
    function say(m) { // the screen reader hears it, even with the AI panel closed
      const r = app.querySelector('#cv-say');
      if (!r || !m) return;
      r.textContent = '';
      setTimeout(() => { if (!dead) r.textContent = m; }, 50);
    }
    // Code files and folders with no AI explanation yet: how many (n), and about how many seconds they take
    // (sec, see AI_DIR). Files the server never sends to the AI (node.xs, or turned down when asked) are left out
    // and counted in m.
    function todo() {
      let n = 0, sec = 0, m = 0;
      V.ids().forEach(i => {
        const x = V.node(i);
        if (!canAI(x) || x.x) return;
        if (x.xs || (errs[i] && errs[i].last)) { m++; return; }
        n++;
        sec += x.t === 'd' ? AI_DIR : AI_FILE + AI_FN * Math.min(8, x.f.length);
      });
      return {n, sec, m};
    }

    // ----- one explanation at a time for the side panel -----
    // When the answer comes, the node selected by then goes next if it still needs one. An answer for a node that
    // is no longer selected is kept for its label and for later, but the panel stays on what the user looks at.
    // While refreshX runs nothing is asked: what it brings may be the answer (x) or a mark (xs); it asks when it settles.
    function want(i) {
      const x = V && V.node(i);
      if (dead || req || refreshing || !x || !proj || !st.available || !canAI(x) || x.x || x.xs || errs[i]) return;
      const r = req = {id: i, cold: false, gen};
      clearTimeout(coldT);
      coldT = setTimeout(() => { if (req === r) { r.cold = true; redo(); } }, AI_COLD);
      // mine: the answer is for the model in use (one asked before the model changed is dropped). The side panel and
      // the screen reader hear only of answers the page shows: for the selected node, while AI is on.
      api('/api/ai/explain', 'POST', {project: proj, node: i}, AI_WAIT).then(j => {
        if (dead) return;
        free(r);
        const mine = r.gen === gen;
        if (mine && !got([[i, j.x]], true)) errs[i] = {msg: 'AI의 답을 읽지 못했어요.', code: 'bad_answer'};
        if (mine && on() && V.sel() === i) { if (errs[i]) V.repaint(); say(errs[i] ? errs[i].msg : 'AI 설명이 나왔어요.'); }
        want(V.sel());
      }, e => {
        if (dead) return;
        free(r);
        const c = e.code, mine = r.gen === gen;
        if (c === 'off' || c === 'not_running' || c === 'no_model') { st = Object.assign({}, st, {available: false, message: e.message}); status(); }
        else if (mine) errs[i] = {msg: e.message, code: c, last: c === 'not_code' || c === 'secret' || c === 'too_big', busy: c === 'busy'};
        if (mine && on() && V.sel() === i) { V.repaint(); if (c !== 'busy' || !running()) say(e.message); }
        paint();
        want(V.sel());
      });
    }
    function free(r) {
      if (req === r) req = null;
      clearTimeout(coldT);
    }
    function redo() { // the side panel again, when it shows a node still waiting for its explanation
      const x = V.node(V.sel());
      if (on() && canAI(x) && !x.x) V.repaint();
    }
    // The AI line of the side panel, under the path ('' while AI is off and for files that are not code).
    function row(i, x) {
      if (!on() || !canAI(x)) return '';
      // with an icon, the words and their button wrap beside it (pa-b), not under it
      const line = (c, ic, body) => `<div class="pa${c}" tabindex="-1">${ic ? `<i class="ti ${ic}" aria-hidden="true"></i><div class="pa-b">${body}</div>` : body}</div>`;
      const t = s => `<span class="pa-t">${esc(s)}</span>`, e = errs[i];
      if (x.x) {
        return peek === i ? line('', '', '<span class="bd tn">기본 설명</span><button type="button" class="lnk" data-ai="peek">AI 설명 보기</button>')
          : line('', '', '<span class="bd aix"><i class="ti ti-sparkles" aria-hidden="true"></i>AI 설명</span><button type="button" class="lnk" data-ai="peek">기본 설명 보기</button>');
      }
      if (x.xs) return line(' note', 'ti-info-circle', t(XS_NOTE[x.xs])); // never sent: nothing to ask or retry
      if (e && e.busy && running()) return line(' note', 'ti-info-circle', t('전체 설명을 만드는 중이에요. 이 항목 차례가 오면 설명이 보여요.'));
      if (e && e.last) return line(' note', 'ti-info-circle', t(e.msg));
      if (e) return line(' bad', 'ti-alert-circle', t(e.msg) + '<button type="button" class="sbtn" data-ai="retry"><i class="ti ti-refresh" aria-hidden="true"></i>다시 시도</button>');
      if (!st.available || !proj) return line(' note', 'ti-info-circle', t(st.message || AI_NONE) + (proj ? '<button type="button" class="sbtn" data-ai="open">AI 설정 열기</button>' : ''));
      if (refreshing && !asking(i)) return line(' load', 'ti-loader-2 spin', t(REFRESHING)); // nothing is asked for it yet (see want)
      return line(' load', 'ti-loader-2 spin', t('AI가 설명을 쓰고 있어요…') + (req && req.cold ? `<span class="pa-n">${esc(COLD_NOTE)}</span>` : ''));
    }
    // AI explanations for some nodes: kept, and shown in the tree and the side panel while AI is on.
    // quiet: they came in by themselves (an answer, a job batch), so the tree keeps its plan (see tree relabel).
    function got(rs, quiet) {
      const ch = [];
      rs.forEach(r => { if (V.setX(r[0], r[1])) { ch.push(r[0]); delete errs[r[0]]; } });
      if (ch.length) {
        if (on()) V.relabel(ch, false, quiet);
        paint();
      }
      return ch.length;
    }

    // ----- status and settings -----
    // Replies are applied in the order the requests went out, so an old status never undoes a newer setting.
    function status() {
      const k = ++stamp;
      api('/api/ai/status').then(j => { if (!dead) setStatus(j, k); }, () => {});
    }
    function setStatus(a, k) {
      if (k < applied) return;
      applied = k;
      const was = st, wasOn = on();
      st = aiStatus(a);
      const model = st.model !== was.model;
      let ids = [];
      if (model) { // explanations belong to the model that wrote them
        gen++;
        peek = null;
        for (const i in errs) delete errs[i];
        ids = V.clearX();
      }
      if (on() !== wasOn) ids = ids.concat(V.ids().filter(i => V.node(i).x));
      const again = on() && (!wasOn || model) && proj;
      if (again) refreshing = true; // before the panel is drawn again, so it does not ask for the selected node yet
      if (ids.length || on() !== wasOn) V.relabel(ids, true);
      else if (st.available !== was.available || (on() && st.message !== was.message)) V.repaint(); // the AI line may say it
      if (again) refreshX();
      paint();
    }
    // After AI is turned on or the model changes: the explanations the server already has for this project
    // (/api/analyze adds them as node.x; it never calls the model), and the files it never sends (node.xs).
    function refreshX() {
      if (!proj) return;
      const k = ++xReq, kg = gen; // kg: the model it was asked with (see want)
      refreshing = true;
      paint();
      api(proj === 'demo' ? '/api/analyze?demo=1' : '/api/analyze?path=' + encodeURIComponent(proj)).then(g => {
        if (dead || k !== xReq) return;
        refreshing = false;
        const N = g && g.nodes && typeof g.nodes === 'object' ? g.nodes : {};
        const n = i => (own(N, i) && N[i] && typeof N[i] === 'object' ? N[i] : null);
        const s = V.sel(), marked = V.ids().filter(i => V.setXs(i, (n(i) || {}).xs)); // first, so a marked file is not asked
        // Its x are the old model's when the model changed while it ran with AI off (no new refresh dropped it then).
        // The marks do not depend on the model.
        const xs = kg === gen ? V.ids().filter(i => n(i) && n(i).x && !V.node(i).x).map(i => [i, n(i).x]) : [];
        got(xs);
        settled(s, marked.includes(s), xs.some(r => r[0] === s));
      }, () => {
        if (dead || k !== xReq) return;
        refreshing = false;
        settled(V.sel(), false, false);
      });
    }
    // The end of refreshX for the selected node s. Its AI line is drawn again (it said REFRESHING), which asks for its
    // explanation if it still needs one (panel calls want), and the screen reader hears what the refresh brought it:
    // its explanation (fresh) or why it is never sent (mark), as when the answer comes from /api/ai/explain. A node that
    // still holds its explanation keeps showing it (row: x before xs), so a new mark says nothing then; and while its own
    // request is out (asking), that answer says it, not this refresh.
    function settled(s, mark, fresh) {
      const x = V.node(s);
      paint();
      if (on() && x && canAI(x)) {
        V.repaint();
        if (fresh && x.x) { if (!asking(s)) say('AI 설명이 나왔어요.'); }
        else if (mark && x.xs && !x.x) say(XS_NOTE[x.xs]);
      }
      want(s);
    }
    function settings(body) {
      if (busy) return;
      busy = 'set';
      err = '';
      paint();
      const k = ++stamp;
      api('/api/ai/settings', 'POST', body).then(j => {
        if (dead) return;
        busy = '';
        setStatus(j, k);
      }, e => {
        if (dead) return;
        busy = '';
        err = e.message;
        paint(); // the switch and the model list go back to what the server has
      });
    }
    function start() {
      if (busy || !st.canStart) return;
      busy = 'start';
      err = '';
      paint();
      const k = ++stamp;
      api('/api/ai/start', 'POST', {}, AI_START).then(j => {
        if (dead) return;
        busy = '';
        setStatus(j, k);
        if (!open) say(st.message);
      }, e => {
        if (dead) return;
        busy = '';
        err = e.message;
        paint();
        status();
      });
    }

    // ----- 전체 설명 만들기 -----
    function runAll(sure) {
      if (busy || running() || refreshing || !st.available) return;
      const w = todo();
      if (!w.n) return;
      if (!sure && w.sec > AI_ASK) { ask = true; paint(); return; } // a long job: say how long first
      ask = false;
      busy = 'run';
      err = '';
      paint();
      api('/api/ai/explain-all', 'POST', {project: proj}).then(j => {
        if (dead) return;
        busy = '';
        const id = str(j.job);
        if (!id) { err = '전체 설명을 시작하지 못했어요. 다시 해 주세요.'; paint(); return; }
        // est0: the estimate's seconds for what is left at the start; tw, t1, e1: see eta
        job = {id, state: 'running', done: 0, total: 0, errors: 0, current: '', message: '', next: 0, t0: Date.now(), fails: 0, stopping: false,
          est0: w.sec, tw: 0, t1: 0, e1: 0};
        take(job, j);
        say('전체 설명을 만들기 시작했어요.');
      }, e => {
        if (dead) return;
        busy = '';
        err = e.message;
        if (e.code === 'off' || e.code === 'not_running' || e.code === 'no_model') status();
        paint();
      });
    }
    // A job block: its results wait in pend and reach the tree in batches, at most one every AI_BATCH ms.
    function take(J, j) {
      const rs = list(j.results);
      rs.forEach(r => { if (Array.isArray(r) && typeof r[0] === 'string') pend.set(r[0], r[1]); });
      J.next = Number.isFinite(j.next) ? j.next : J.next + rs.length;
      J.done = num(j.done);
      J.total = Math.max(num(j.total), J.done);
      J.errors = num(j.errors);
      J.current = str(j.current);
      if (J.current && !J.tw) J.tw = Date.now(); // its first item began (it may wait for another folder's job first)
      J.message = str(j.message);
      const s = str(j.state);
      J.state = s === 'done' || s === 'cancelled' || s === 'failed' ? s : 'running';
      if (J.state !== 'running') { end(J); return; }
      if (pend.size && !flushT) flushT = setTimeout(flush, Math.max(0, AI_BATCH - (Date.now() - lastFlush)));
      clearTimeout(pollT);
      pollT = setTimeout(poll, AI_POLL);
      paint();
    }
    function poll() {
      pollT = 0;
      const J = job;
      if (dead || !J || J.state !== 'running') return;
      api('/api/ai/job?id=' + encodeURIComponent(J.id) + '&since=' + J.next).then(j => {
        if (dead || job !== J || J.state !== 'running') return;
        J.fails = 0;
        take(J, j);
      }, e => {
        if (dead || job !== J || J.state !== 'running') return;
        if (e.status === 404) { J.state = 'failed'; J.message = e.message; end(J); return; }
        J.fails++;
        pollT = setTimeout(poll, Math.min(10000, AI_POLL * 2 ** J.fails));
        paint();
      });
    }
    function flush() {
      clearTimeout(flushT);
      flushT = 0;
      if (!pend.size) return;
      lastFlush = Date.now();
      const rs = [...pend];
      pend.clear();
      got(rs, true);
    }
    function end(J) {
      clearTimeout(pollT);
      pollT = 0;
      flush();
      for (const i in errs) if (errs[i].busy) delete errs[i];
      if (!J.message) J.message = J.state === 'done' ? '전체 설명을 다 만들었어요.' : J.state === 'cancelled' ? '전체 설명 만들기를 멈췄어요.' : '전체 설명을 만들다가 멈췄어요.';
      say(J.message);
      if (J.state === 'failed') status();
      paint();
      V.replan(); // once, now: the labels that came in during the job get their room
      V.repaint(); // the selected node may go next now
    }
    function stop() { // the server stops after the item it is on
      const J = job;
      if (!J || J.state !== 'running' || J.stopping) return;
      J.stopping = true;
      paint();
      api('/api/ai/job/cancel', 'POST', {job: J.id}).then(() => {}, e => {
        if (dead || job !== J) return;
        J.stopping = false;
        err = e.message;
        paint();
      });
    }
    function eta(J) {
      const out = [];
      if (J.fails >= 3) out.push('진행 상황을 받지 못하고 있어요. 계속 확인하고 있어요.');
      // What is left by the estimate (todo: items the cache already had are done at once and are not in it),
      // at the speed measured since the first new answer came in (t1, e1), so a slow first answer is left out.
      const left = todo().sec, did = Math.max(0, J.est0 - left);
      if (did > 0 && !J.t1) { J.t1 = Date.now(); J.e1 = did; }
      // No new answer yet: the model may be loading, once this job is on its first item (tw). Before that it only waits
      // for another folder's job, and the server's own sentence says so.
      if (!J.t1 && left > 0) { if (J.tw && Date.now() - J.tw > AI_COLD) out.push(COLD_NOTE); }
      else if (J.total > J.done) {
        const ran = did - J.e1, speed = J.t1 && ran >= 6 ? Math.min(4, Math.max(.25, (Date.now() - J.t1) / 1000 / ran)) : 1;
        const rest = left * speed;
        out.push(rest < 60 ? '곧 끝나요.' : `남은 시간은 ${dur(rest)}이에요.`);
      }
      if (J.errors) out.push(`만들지 못한 항목이 ${fmt(J.errors)}개 있어요.`);
      return out.join(' ');
    }

    // ----- the chip and the AI panel -----
    function paintChip() {
      const en = on(), run = running();
      putA(chip, 'class', 'bd ai ' + (!en ? 'tc' : st.available || run ? 'ta' : 'tw'));
      putA(chip.firstChild, 'class', 'ti ' + (run ? 'ti-loader-2 spin' : en && !st.available ? 'ti-alert-triangle' : 'ti-sparkles'));
      putT(chip.lastChild, 'AI 설명 ' + (en ? '켜짐' : '꺼짐') + (run ? ` · ${fmt(job.done)}/${fmt(job.total)}` : ''));
      putA(chip, 'title', st.message || AI_NONE);
    }
    function paint() {
      if (!P || dead) return;
      const a = document.activeElement, inside = !!a && pop.contains(a);
      paintChip();
      const ok = st.ok, en = on(), run = running(), w = todo(), n = w.n;
      // the models, recommended ones first (in their order), then the rest as the server lists them
      const rank = m => { const k = st.rec.indexOf(m.name); return k < 0 ? st.rec.length : k; };
      const ms = st.models.map((m, j) => [m, j]).sort((p, q) => rank(p[0]) - rank(q[0]) || p[1] - q[1]).map(p => p[0]);
      putT(P.msg, busy === 'start' ? 'Ollama를 켜고 있어요. 20초쯤 걸릴 수 있어요.' : st.message || AI_NONE);
      putH(P.help, !(ok && st.ollama === 'missing'));
      putH(P.stw, !(ok && en && (st.canStart || busy === 'start')));
      putA(P.start, 'aria-disabled', String(!!busy));
      putH(P.start.firstChild, busy !== 'start');
      putT(P.start.lastChild, busy === 'start' ? '켜는 중…' : 'Ollama 켜기');
      putH(P.swr, !ok);
      putA(P.sw, 'aria-checked', String(en));
      putA(P.sw, 'aria-disabled', String(busy === 'set'));
      const showM = ok && st.ollama !== 'missing' && (ms.length > 0 || (en && !!st.model)); // no models without Ollama
      putH(P.mr, !showM);
      if (showM) {
        const o = ms.length ? ms.map(m => [m.name, m.name + (gb(m.size) ? ' · ' + gb(m.size) : '') + (st.rec.includes(m.name) ? ' · 추천' : '')]) : [];
        if (!o.some(p => p[0] === st.model)) o.unshift([st.model, st.model || '모델을 골라 주세요']);
        const k = JSON.stringify(o);
        if (P.sel.dataset.k !== k) {
          P.sel.textContent = '';
          o.forEach(p => { const e = el('option', '', p[1]); e.value = p[0]; P.sel.appendChild(e); });
          P.sel.dataset.k = k;
        }
        if (P.sel.value !== st.model) P.sel.value = st.model;
        P.sel.disabled = !ms.length || run;
      }
      putT(P.mh, 'Ollama가 켜지면 설치된 다른 모델도 고를 수 있어요.');
      putH(P.mh, !(showM && !ms.length && st.ollama !== 'running')); // with Ollama on, no list means no model installed
      putT(P.err, err);
      putH(P.err, !err);
      const all = ok && (st.available || run || (!!job && en)); // a job's last word stays while AI is on
      putH(P.all, !all);
      if (all) {
        putT(P.est, refreshing ? REFRESHING
          : !n ? (w.m ? `AI에 보내지 않는 파일 ${fmt(w.m)}개를 빼면 모두 AI 설명이 있어요.` : '모든 코드 파일과 폴더에 AI 설명이 있어요.')
          : `아직 AI 설명이 없는 파일과 폴더가 ${fmt(n)}개 있어요. ${w.sec < 60 ? '1분 안에 끝나요.' : dur(w.sec) + ' 걸려요.'}`);
        putH(P.est, run || ask);
        putH(P.run, run || ask);
        putA(P.run, 'aria-disabled', String(!!busy || refreshing || !n || !st.available));
        putH(P.cf, run || !ask);
        if (ask) putT(P.cft, `${fmt(n)}개를 만들려면 ${dur(w.sec)} 걸려요. 그동안 컴퓨터가 조금 느려질 수 있어요. 시작할까요?`);
        putH(P.job, !run);
        if (run) {
          const J = job;
          putT(P.jn, J.stopping ? '멈추는 중이에요. 지금 만드는 설명까지만 마칠게요.' : J.message || 'AI가 설명을 만들고 있어요.');
          putT(P.jc, `${fmt(J.done)} / ${fmt(J.total)}`);
          putA(P.bar, 'aria-valuemax', String(Math.max(1, J.total)));
          putA(P.bar, 'aria-valuenow', String(J.done));
          putA(P.bar, 'aria-valuetext', `${fmt(J.total)}개 중 ${fmt(J.done)}개`);
          P.fill.style.width = (J.total ? Math.min(100, J.done / J.total * 100) : 0).toFixed(1) + '%';
          const c = J.current && V.node(J.current);
          putT(P.cur, c ? '지금: ' + c.p : '');
          putH(P.cur, !c);
          const t = J.stopping ? '' : eta(J);
          putT(P.eta, t);
          putH(P.eta, !t);
          putA(P.stop, 'aria-disabled', String(J.stopping));
          putT(P.stop, J.stopping ? '멈추는 중…' : '멈추기');
        }
        const fin = !!job && !run;
        putH(P.end, !fin);
        if (fin) { putT(P.end, job.message); putA(P.end, 'class', 'aip-end ' + job.state); }
      }
      // A control that went away (the job started, Ollama is on) hands the focus to the next useful one.
      if (inside && !shown(a)) { const f = [P.go, P.stop, P.run, P.sw, P.h].find(shown); if (f) f.focus(); }
    }
    function openP() {
      if (!pop || open) return;
      const a = document.activeElement;
      opener = a && (a === chip || a.dataset.ai === 'open') ? a : null; // where the focus goes back to
      open = true;
      ask = false;
      putH(pop, false);
      media();
      putA(chip, 'aria-expanded', 'true');
      paint();
      P.h.focus();
      status(); // fresh: Ollama may have been started or stopped outside CodeVision
    }
    // ret: the focus goes back to the chip, or to the side panel's "AI 설정 열기" when that opened it.
    function close(ret) {
      if (!open) return;
      open = false;
      ask = false;
      putH(pop, true);
      putH(back, true);
      putA(chip, 'aria-expanded', 'false');
      if (ret) (opener && opener.isConnected && shown(opener) ? opener : chip).focus();
      opener = null;
    }
    // Phones get a sheet with a backdrop: it holds the focus (aria-modal). The popover leaves it free.
    function media() {
      if (!open) return;
      if (SHEET.matches) pop.setAttribute('aria-modal', 'true');
      else pop.removeAttribute('aria-modal');
      putH(back, !SHEET.matches);
    }
    function key(e) {
      if (!open) return;
      if (e.key === 'Escape' || e.key === 'Esc') {
        e.preventDefault();
        if (ask) { ask = false; paint(); P.run.focus(); return; }
        const a = document.activeElement;
        close(!a || a === document.body || a === chip || pop.contains(a));
        return;
      }
      if (e.key !== 'Tab' || !SHEET.matches) return;
      const f = [...pop.querySelectorAll('button, select')].filter(b => !b.disabled && shown(b)), a = document.activeElement;
      if (!f.length) return;
      if (e.shiftKey ? a === f[0] || !f.includes(a) : a === f[f.length - 1] || !pop.contains(a)) {
        e.preventDefault();
        (e.shiftKey ? f[f.length - 1] : f[0]).focus();
      }
    }
    function down(e) { // a click outside closes the panel (the sheet's backdrop: see attach)
      if (open && e.target !== back && !pop.contains(e.target) && !chip.contains(e.target)) close(false);
    }
    function act(a, b) {
      if (dead || !V || !P || b.getAttribute('aria-disabled') === 'true') return;
      const s = V.sel();
      if (a === 'close') close(true);
      else if (a === 'start') start();
      else if (a === 'switch') {
        if (busy) return;
        if (st.enabled && running()) stop();
        settings({enabled: !st.enabled});
      } else if (a === 'run') runAll(false);
      else if (a === 'go') runAll(true);
      else if (a === 'nogo') { ask = false; paint(); P.run.focus(); }
      else if (a === 'stop') stop();
      else if (a === 'peek') { peek = peek === s ? null : s; V.repaint(); }
      else if (a === 'retry') { delete errs[s]; V.repaint(); }
      else if (a === 'open') openP();
    }
    function attach(v) {
      V = v;
      chip = app.querySelector('.cv button.ai');
      pop = app.querySelector('#cv-ai');
      back = app.querySelector('.cv .aib');
      if (!chip || !pop || !back) return;
      const q = s => pop.querySelector(s);
      P = {h: q('h2'), msg: q('.aip-m'), help: q('.aip-help'), stw: q('.aip-st'), start: q('[data-ai="start"]'),
        swr: q('[data-p="sw"]'), sw: q('[data-ai="switch"]'), mr: q('[data-p="model"]'), sel: q('select'), mh: q('.aip-hint'),
        err: q('.aip-err'), all: q('.aip-all'), est: q('.aip-est'), run: q('[data-ai="run"]'), cf: q('.aip-cf'), cft: q('.aip-cft'),
        go: q('[data-ai="go"]'), job: q('.aip-job'), jn: q('.aip-jn'), jc: q('.aip-jc'), bar: q('.aip-bar'), cur: q('.aip-cur'),
        eta: q('.aip-eta'), stop: q('[data-ai="stop"]'), end: q('.aip-end')};
      P.fill = P.bar.firstChild;
      P.sel.addEventListener('change', () => {
        const m = P.sel.value;
        if (busy || !m || m === st.model) { paint(); return; }
        settings({enabled: st.enabled, model: m});
      });
      // A tap on the sheet's backdrop is like 닫기. On click, not on pointerdown: the press itself takes the focus.
      back.addEventListener('click', () => close(true));
      // The popover closes when the focus moves on past it (the sheet keeps it, see key).
      pop.addEventListener('focusout', e => {
        const t = e.relatedTarget;
        if (open && !SHEET.matches && t && !pop.contains(t) && t !== chip) close(false);
      });
      paint();
    }
    function quit() {
      dead = true;
      clearTimeout(pollT);
      clearTimeout(flushT);
      clearTimeout(coldT);
    }
    return {on, use, row, want, attach, act, key, down, media, quit, toggle() { if (open) close(true); else openP(); }};
  }

  // ---------- events and start-up ----------
  app.addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (!b || b.disabled) return;
    const a = b.dataset.act;
    if (a === 'pick') run({kind: 'pick'});
    else if (a === 'demo') run({kind: 'demo'});
    else if (a === 'recent') run({kind: 'path', path: b.dataset.path});
    else if (a === 'retry') { if (last) run(last); }
    else if (a === 'home') { setQuery(null); showStart('[data-act="pick"]'); }
    else if (a === 'ai') { if (aic) aic.toggle(); }
  });
  // The AI panel's controls, and the AI line in the side panel (aiCtl).
  app.addEventListener('click', e => {
    const b = e.target.closest('[data-ai]');
    if (b && aic) aic.act(b.dataset.ai, b);
  });
  document.addEventListener('keydown', e => { if (aic) aic.key(e); });
  document.addEventListener('pointerdown', e => { if (aic) aic.down(e); }, true);
  if (SHEET.addEventListener) SHEET.addEventListener('change', () => { if (aic) aic.media(); });
  app.addEventListener('submit', e => {
    e.preventDefault();
    const inp = app.querySelector('#cv-path');
    const v = inp ? inp.value.trim().replace(/^"(.*)"$/, '$1').trim() : '';
    if (v) { run({kind: 'path', path: v}); return; }
    const m = app.querySelector('.st-msg');
    if (m) m.textContent = '분석할 폴더 경로를 넣어 주세요.';
    if (inp) inp.focus();
  });
  addEventListener('resize', () => { if (view) view.resize(); });
  if (document.fonts) {
    document.fonts.ready.then(() => { if (view) view.relayout(); });
    if (document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', () => { if (view) view.relayout(); });
  }

  if (window.CV_BOOT_GRAPH) run({kind: 'boot'});
  else {
    const q = new URLSearchParams(location.search);
    if (q.get('demo') === '1') run({kind: 'demo'});
    else if (q.get('path')) run({kind: 'path', path: q.get('path')});
    else showStart();
  }
})();
