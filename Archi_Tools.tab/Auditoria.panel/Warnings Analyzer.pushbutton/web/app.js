// Warnings Analyzer — dashboard. Habla con Revit por chrome.webview (ver script.py).
'use strict';

const $ = sel => document.querySelector(sel);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmt = n => Number(n).toLocaleString('es-CL');
const fmt1 = n => Number(n).toLocaleString('es-CL', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const PRIOS = ['high', 'medium', 'low'];
const ROW_STEP = 300;
// Fecha local (no UTC): en Chile, despues de las ~21 h la UTC ya es mañana.
const localDate = () => new Date().toLocaleDateString('sv-SE');
const ICONS = {
  wand: '<svg viewBox="0 0 24 24"><path d="M15 4V2M15 16v-2M8 9h2M20 9h2M17.8 11.8 19 13M17.8 6.2 19 5M3 21l9-9M12.2 6.2 11 5"/></svg>',
  okBig: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m8 12 3 3 5-6"/></svg>',
};

const S = {
  data: null, items: [], groups: [], active: '*', checked: new Set(),
  prio: new Set(PRIOS), q: '', facet: null, dist: 'cat',
  showAccepted: false, viewSet: null, viewName: '', additional: true, limit: ROW_STEP,
};

// --- Puente con Revit --------------------------------------------------------
const host = window.chrome && window.chrome.webview;
const pending = new Map();
let seq = 0;
if (host) {
  host.addEventListener('message', e => {
    const m = e.data, p = pending.get(m.id);
    if (!p) return;
    pending.delete(m.id);
    m.ok ? p.resolve(m.data) : p.reject(new Error(m.error));
  });
}
function call(action, payload = {}) {
  // La clave del modelo escaneado: Revit rechaza la acción si el modelo activo es otro.
  if (S.data && action !== 'scan') payload = Object.assign({ docKey: S.data.doc.key }, payload);
  if (!host) return window.mockCall(action, payload); // vista previa en navegador
  return new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    host.postMessage({ id, action, payload });
  });
}
let busyCount = 0;
function busy(on) {
  busyCount += on ? 1 : -1;
  $('#status').classList.toggle('busy', busyCount > 0);
  document.querySelectorAll('[data-act],[data-fix]').forEach(b => { b.disabled = busyCount > 0 || b.dataset.off === '1'; });
}
async function run(action, payload, okMsg) {
  busy(true);
  try {
    const r = await call(action, payload);
    if (okMsg) toast(typeof okMsg === 'function' ? okMsg(r) : okMsg);
    if (r && r.note) toast(r.note, true);
    return r;
  } catch (e) {
    toast(e.message, true);
    return null;
  } finally { busy(false); }
}
function toast(text, err) {
  const el = document.createElement('div');
  el.className = 'toast' + (err ? ' err' : '');
  el.textContent = text;
  $('#toast').appendChild(el);
  setTimeout(() => el.remove(), err ? 7000 : 3500);
}

// --- Datos -------------------------------------------------------------------
const info = id => S.data.elements[String(id)];
const itemKey = w => w.d + '|' + [...w.f].sort((a, b) => a - b).join(',');

function ingest(data) {
  S.data = data;
  const accepted = new Set(data.accepted || []);
  S.items = data.warnings.map((w, i) => {
    const rule = classify(w.d);
    const key = itemKey(w);
    return { i, d: w.d, f: w.f, a: w.a, rule, prio: rule.prio, key, accepted: accepted.has(key) };
  });
  const byDesc = new Map();
  S.items.forEach(it => {
    if (!byDesc.has(it.d)) byDesc.set(it.d, { d: it.d, rule: it.rule, items: [] });
    byDesc.get(it.d).items.push(it);
  });
  S.groups = [...byDesc.values()];
  S.checked.clear();
  S.viewSet = null;
  if (S.active !== '*' && !byDesc.has(S.active)) S.active = '*';
}

const open = () => S.items.filter(it => !it.accepted);
const idsOf = (it, additional = S.additional) => additional ? it.f.concat(it.a) : it.f;
const titleOf = g => g.rule.title || g.d;

function facetValues(id, kind) {
  const e = info(id);
  if (!e) return [];
  return [{ cat: e.cat, level: e.level || '(sin nivel)', creator: e.creator || '—', ws: e.ws || '—' }[kind]];
}
function matches(it) {
  if (!S.prio.has(it.prio)) return false;
  if (it.accepted && !S.showAccepted) return false;
  const ids = idsOf(it, true);
  if (S.viewSet && !ids.some(id => S.viewSet.has(id))) return false;
  if (S.facet && !ids.some(id => facetValues(id, S.facet.kind).includes(S.facet.value))) return false;
  if (S.q) {
    const q = S.q.toLowerCase();
    const hay = [it.d, it.rule.group, it.rule.title].concat(ids.map(id => {
      const e = info(id) || {};
      return [id, e.cat, e.name, e.level].join(' ');
    })).join(' ').toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}
const filtered = () => S.items.filter(matches);
const activeItems = () => filtered().filter(it => S.active === '*' || it.d === S.active);
function checkedIds() {
  const ids = new Set();
  S.items.forEach(it => { if (S.checked.has(it.i)) idsOf(it).forEach(id => ids.add(id)); });
  return [...ids];
}

// --- Render: cabecera y KPIs -----------------------------------------------------
function renderHeader() {
  const d = S.data.doc;
  $('#model').textContent = d.title;
  $('#meta').textContent = `Escaneado ${S.data.scannedAt} · ${fmt(d.elementCount)} elementos en el modelo` + (d.workshared ? ' · colaborativo' : '');
}

function renderKpis() {
  const o = open();
  const elements = new Set();
  o.forEach(it => idsOf(it, true).forEach(id => elements.add(id)));
  const high = o.filter(it => it.prio === 'high').length;
  const accepted = S.items.length - o.length;
  const density = S.data.doc.elementCount ? (o.length / S.data.doc.elementCount * 1000) : 0;
  const today = localDate();
  const prev = (S.data.history || []).filter(h => h.date !== today).slice(-1)[0];
  let delta = '—', deltaSub = 'sin registro anterior', deltaCls = '';
  if (prev) {
    const diff = o.length - (prev.open != null ? prev.open : prev.total);
    delta = (diff > 0 ? '+' : '') + fmt(diff);
    deltaSub = 'desde ' + prev.date.split('-').reverse().join('-');
    deltaCls = diff > 0 ? 'delta-up' : diff < 0 ? 'delta-down' : '';
  }
  const kpi = (label, value, sub, cls = '') => `<div class="kpi ${cls}"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  $('#kpis').innerHTML = [
    kpi('Warnings abiertos', fmt(o.length), accepted ? `${fmt(accepted)} aceptados aparte` : 'ninguno aceptado'),
    kpi('Prioridad alta', fmt(high), high ? 'resolver primero' : 'sin críticos', high ? 'alert' : ''),
    kpi('Elementos afectados', fmt(elements.size), S.data.doc.elementCount ? `${fmt1(elements.size / S.data.doc.elementCount * 100)} % del modelo` : ''),
    kpi('Tipos distintos', fmt(new Set(o.map(it => it.d)).size), `${fmt(new Set(o.map(it => it.rule.group)).size)} grupos`),
    kpi('Densidad', fmt1(density), 'warnings por 1.000 elementos'),
    kpi('Variación', `<span class="${deltaCls}">${delta}</span>`, deltaSub),
  ].join('');
}

// --- Render: gráficos ----------------------------------------------------------
function bars(items, onKey) {
  const max = Math.max(1, ...items.map(x => x.value));
  return '<div class="bars">' + items.map(x => `
    <div class="bar ${x.on ? 'on' : ''}" data-${onKey}="${esc(x.key)}" title="${esc(x.label)} — ${fmt(x.value)}">
      <div><div class="lbl">${esc(x.label)}</div><div class="track"><div class="fill" style="width:${x.value / max * 100}%;background:${x.color || '#333'}"></div></div></div>
      <div class="num">${fmt(x.value)}</div>
    </div>`).join('') + '</div>';
}

function renderTypesChart() {
  const counts = new Map();
  open().forEach(it => counts.set(it.d, (counts.get(it.d) || 0) + 1));
  const top = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([d, value]) => {
    const rule = classify(d);
    return { key: d, label: rule.title || d, value, color: PRIORITY[rule.prio].color, on: S.active === d };
  });
  $('#chart-types').innerHTML = top.length ? bars(top, 'type') : '<div class="muted">Sin warnings abiertos.</div>';
}

function arc(cx, cy, r, R, a0, a1) {
  const p = (rad, a) => [cx + rad * Math.cos(a), cy + rad * Math.sin(a)];
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const [x0, y0] = p(R, a0), [x1, y1] = p(R, a1), [x2, y2] = p(r, a1), [x3, y3] = p(r, a0);
  return `M${x0} ${y0} A${R} ${R} 0 ${large} 1 ${x1} ${y1} L${x2} ${y2} A${r} ${r} 0 ${large} 0 ${x3} ${y3} Z`;
}
function renderDonut() {
  const o = open();
  const counts = PRIOS.map(p => o.filter(it => it.prio === p).length);
  const total = counts.reduce((a, b) => a + b, 0);
  let a = -Math.PI / 2, paths = '';
  PRIOS.forEach((p, i) => {
    if (!counts[i]) return;
    const span = counts[i] / total * Math.PI * 2;
    const end = a + Math.min(span, Math.PI * 2 - 1e-4);
    paths += `<path d="${arc(60, 60, 36, 56, a, end)}" fill="${PRIORITY[p].color}" class="${S.prio.has(p) ? '' : 'off'}" data-prio="${p}"><title>${PRIORITY[p].label}: ${counts[i]}</title></path>`;
    a += span;
  });
  const center = `<text x="60" y="58" text-anchor="middle" font-size="20" font-weight="300" fill="#333">${fmt(total)}</text><text x="60" y="74" text-anchor="middle" font-size="9" fill="#A1A1A0">abiertos</text>`;
  const legend = PRIOS.map((p, i) => `<div data-prio="${p}" class="${S.prio.has(p) ? '' : 'off'}"><span class="sw" style="background:${PRIORITY[p].color}"></span>${PRIORITY[p].label}<b style="margin-left:auto;font-weight:400">${fmt(counts[i])}</b></div>`).join('');
  $('#chart-prio').innerHTML = `<div class="donut-wrap"><svg viewBox="0 0 120 120">${paths || '<circle cx="60" cy="60" r="46" stroke="#E6E4E8" stroke-width="20" fill="none"/>'}${center}</svg><div class="legend" style="flex:1">${legend}</div></div>`;
}

const DIST_TABS = { cat: 'Categoría', level: 'Nivel', creator: 'Creador', ws: 'Workset' };
function renderDist() {
  const tabs = Object.keys(DIST_TABS).filter(k => S.data.doc.workshared || (k !== 'creator' && k !== 'ws'));
  if (!tabs.includes(S.dist)) S.dist = 'cat';
  $('#dist-tabs').innerHTML = tabs.map(k => `<button class="${k === S.dist ? 'on' : ''}" data-dist="${k}">${DIST_TABS[k]}</button>`).join('');
  const counts = new Map();
  open().forEach(it => {
    const vals = new Set();
    idsOf(it, true).forEach(id => facetValues(id, S.dist).forEach(v => vals.add(v)));
    vals.forEach(v => counts.set(v, (counts.get(v) || 0) + 1));
  });
  const top = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([v, value]) => ({
    key: v, label: v, value, color: '#A1A1A0',
    on: S.facet && S.facet.kind === S.dist && S.facet.value === v,
  }));
  $('#chart-dist').innerHTML = top.length ? bars(top, 'facet') : '<div class="muted">Sin datos.</div>';
}

function renderTrend() {
  const h = (S.data.history || []).slice(-30);
  if (h.length < 2) {
    $('#chart-trend').innerHTML = '<div class="empty">Se guarda una medición por día al abrir el analizador. La tendencia aparece desde el segundo día.</div>';
    return;
  }
  const W = 300, H = 120, P = 14;
  const vals = h.map(x => x.open != null ? x.open : x.total);
  const max = Math.max(1, ...vals);
  const pt = (v, i) => [P + i * (W - 2 * P) / (h.length - 1), H - P - v / max * (H - 2 * P - 10)];
  const line = (key, color) => {
    const pts = h.map((x, i) => pt(key(x) || 0, i));
    return `<polyline points="${pts.map(p => p.join(',')).join(' ')}" fill="none" stroke="${color}" stroke-width="1.8"/>` +
      pts.map((p, i) => `<circle cx="${p[0]}" cy="${p[1]}" r="2.4" fill="${color}"><title>${h[i].date}: ${fmt(key(h[i]) || 0)}</title></circle>`).join('');
  };
  const last = pt(vals[vals.length - 1], h.length - 1);
  $('#chart-trend').innerHTML = `<svg viewBox="0 0 ${W} ${H}">
    <line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}" stroke="#E6E4E8"/>
    ${line(x => x.open != null ? x.open : x.total, '#333333')}${line(x => x.high, PRIORITY.high.color)}
    <text x="${last[0]}" y="${last[1] - 6}" text-anchor="end" font-size="9" fill="#333">${fmt(vals[vals.length - 1])}</text>
    <text x="${P}" y="${H - 2}" font-size="8" fill="#A1A1A0">${h[0].date}</text>
    <text x="${W - P}" y="${H - 2}" font-size="8" fill="#A1A1A0" text-anchor="end">${h[h.length - 1].date}</text></svg>
    <div class="legend" style="flex-direction:row;gap:14px;font-size:11px"><div><span class="sw" style="background:#333"></span>Abiertos</div><div><span class="sw" style="background:${PRIORITY.high.color}"></span>Prioridad alta</div></div>`;
}

// --- Render: lista de tipos ------------------------------------------------------
function renderSidebar() {
  const chips = PRIOS.map(p => `<span class="chip ${S.prio.has(p) ? 'on' : ''}" data-prio="${p}"><span class="sw" style="background:${PRIORITY[p].color}"></span>${PRIORITY[p].label}</span>`);
  chips.push(`<span class="chip ${S.viewSet ? 'on' : ''}" data-toggle="view" title="Solo warnings con elementos visibles en la vista activa">${S.viewSet ? 'Vista: ' + esc(S.viewName) : 'Solo vista activa'}</span>`);
  chips.push(`<span class="chip ${S.showAccepted ? 'on' : ''}" data-toggle="accepted">Ver aceptados</span>`);
  if (S.facet) chips.push(`<span class="chip on" data-toggle="facet">${DIST_TABS[S.facet.kind]}: ${esc(S.facet.value)} <span class="x">×</span></span>`);
  $('#chips').innerHTML = chips.join('');

  const items = filtered();
  const groups = new Map();
  items.forEach(it => groups.set(it.d, (groups.get(it.d) || 0) + 1));
  const list = S.groups.filter(g => groups.has(g.d)).sort((a, b) =>
    PRIORITY[b.rule.prio].weight - PRIORITY[a.rule.prio].weight || groups.get(b.d) - groups.get(a.d));
  const row = (key, dot, t, g, n) => `<div class="type ${S.active === key ? 'on' : ''}" data-type="${esc(key)}">
      <span class="dot" style="background:${dot}"></span><div><div class="t">${esc(t)}</div><div class="g">${esc(g)}</div></div><span class="n">${fmt(n)}</span></div>`;
  $('#types').innerHTML = row('*', '#333', 'Todos los warnings', `${list.length} tipos con el filtro actual`, items.length) +
    list.map(g => row(g.d, PRIORITY[g.rule.prio].color, titleOf(g), g.rule.group + (g.rule.title ? ' · ' + g.d : ''), groups.get(g.d))).join('');
}

// --- Render: detalle -----------------------------------------------------------
function elementChips(it) {
  const chip = (id, add) => {
    const e = info(id);
    const tip = e ? `${e.cat} — ${e.name}${e.level ? ' — ' + e.level : ''}${e.view ? ' — vista: ' + e.view : ''}` : 'Ya no existe en el modelo';
    return `<span class="eid ${add ? 'add' : ''} ${e ? '' : 'gone'}" data-eid="${id}" title="${esc(tip)}&#10;Clic: mostrar · Ctrl+clic: aislar">${id}</span>`;
  };
  return '<div class="ids">' + it.f.map(id => chip(id, false)).join('') + it.a.map(id => chip(id, true)).join('') + '</div>';
}
function elementSummary(it) {
  const cats = new Map();
  idsOf(it, true).forEach(id => { const e = info(id); if (e) cats.set(e.cat, (cats.get(e.cat) || 0) + 1); });
  const e = info(it.f[0]) || info(it.a[0]);
  if (!e) return '<span class="muted">—</span>';
  const catText = [...cats].map(([c, n]) => n > 1 ? `${c} ×${n}` : c).join(', ');
  const who = e.creator ? `<div class="muted">Creó: ${esc(e.creator)}${e.editor && e.editor !== e.creator ? ' · editó: ' + esc(e.editor) : ''}</div>` : '';
  return `<div class="elinfo"><span class="cat">${esc(catText)}</span> · ${esc(e.name)}<div class="muted">${esc(e.level || e.view || '')}</div>${who}</div>`;
}

function renderExplain(items) {
  if (S.active === '*') {
    const hi = items.filter(it => it.prio === 'high').length;
    return `<div style="padding:14px 16px;border-bottom:1px solid var(--slate)"><h2 style="font-size:17px">Todos los warnings</h2>
      <div class="muted">${fmt(items.length)} con el filtro actual${hi ? ` · ${fmt(hi)} de prioridad alta` : ''}. Elige un tipo a la izquierda para ver su explicación y cómo resolverlo.</div></div>`;
  }
  const g = S.groups.find(x => x.d === S.active);
  const r = g.rule, pr = PRIORITY[r.prio];
  const fixable = items.filter(it => !it.accepted);
  const fix = r.autofix ? `<div class="fixbox"><button class="primary" data-fix="${r.autofix}" ${fixable.length ? '' : 'data-off="1" disabled'}>
      ${ICONS.wand}${r.autofix === 'duplicates' ? 'Eliminar duplicados' : 'Eliminar líneas contenidas'} (${fmt(fixable.length)})</button>
      <span class="muted">Corrección automática sobre los ${fmt(fixable.length)} warnings filtrados de este tipo. Se deshace con Ctrl+Z en Revit.</span></div>` : '';
  return `<div class="explain"><div class="schema">${SCHEMAS[r.schema]}</div><div>
    <h2>${esc(titleOf(g))}<span class="pill" style="background:${pr.color}">Prioridad ${pr.label.toLowerCase()}</span></h2>
    <div class="desc">${esc(g.d)}</div>
    <div class="why"><b style="font-weight:400">Por qué importa:</b> ${esc(r.why)}</div>
    <b style="font-weight:400">Cómo resolverlo:</b><ol>${r.fix.map(f => `<li>${esc(f)}</li>`).join('')}</ol>${fix}</div></div>`;
}

function renderDetail() {
  const items = activeItems();
  const all = S.active === '*';
  let html = renderExplain(items);
  if (!items.length) {
    html += `<div class="empty-state">${ICONS.okBig}<div>No hay warnings con este filtro.</div></div>`;
  } else {
    const shown = items.slice(0, S.limit);
    const allChecked = shown.every(it => S.checked.has(it.i));
    html += `<div class="tablewrap"><table><thead><tr><th class="c"><input type="checkbox" data-checkall ${allChecked ? 'checked' : ''} title="Marcar todos los visibles"></th><th>#</th>
      ${all ? '<th>Tipo</th>' : ''}<th>Elementos (ID)</th><th>Detalle</th></tr></thead><tbody>` +
      shown.map((it, n) => `<tr data-row="${it.i}" class="${S.checked.has(it.i) ? 'checked' : ''} ${it.accepted ? 'accepted' : ''}">
        <td class="c"><input type="checkbox" ${S.checked.has(it.i) ? 'checked' : ''} tabindex="-1"></td><td class="num">${n + 1}</td>
        ${all ? `<td><span class="sw" style="display:inline-block;background:${PRIORITY[it.prio].color}"></span> ${esc(it.rule.title || it.d)}</td>` : ''}
        <td>${elementChips(it)}${it.accepted ? '<div class="muted">Aceptado</div>' : ''}</td><td>${elementSummary(it)}</td></tr>`).join('') +
      '</tbody></table>' +
      (items.length > S.limit ? `<div style="padding:12px;text-align:center"><button data-more>Mostrar ${fmt(Math.min(ROW_STEP, items.length - S.limit))} más (de ${fmt(items.length)})</button></div>` : '') + '</div>';
  }
  $('#detail-body').innerHTML = html;
  renderActionbar();
}

function renderActionbar() {
  const nWarn = S.checked.size, ids = checkedIds();
  $('#sel-count').innerHTML = nWarn ? `<b style="font-weight:400">${fmt(nWarn)}</b> warnings · ${fmt(ids.length)} elementos` : '<span class="muted">Marca warnings en la tabla</span>';
  document.querySelectorAll('[data-needs-sel]').forEach(b => { b.dataset.off = nWarn ? '0' : '1'; b.disabled = !nWarn || busyCount > 0; });
}

function renderAll() {
  renderHeader(); renderKpis(); renderTypesChart(); renderDonut(); renderDist(); renderTrend(); renderSidebar(); renderDetail();
}

// --- Acciones --------------------------------------------------------------------
async function scan() {
  const data = await run('scan');
  if (!data) return;
  ingest(data);
  S.limit = ROW_STEP;
  renderAll();
  const o = open();
  const counts = { total: S.items.length, open: o.length, elements: data.doc.elementCount };
  PRIOS.forEach(p => { counts[p] = o.filter(it => it.prio === p).length; });
  const history = await run('snapshot', counts);
  if (history) { S.data.history = history; renderKpis(); renderTrend(); }
}

const VIEW_ACTIONS = {
  select: ['select', {}, r => `${fmt(r.count)} elementos seleccionados en Revit`],
  show: ['show', {}, r => `Mostrando ${fmt(r.count)} elementos`],
  isolate: ['isolate', { mode: 'isolate' }, r => `${fmt(r.count)} elementos aislados en «${r.view}»`],
  hide: ['isolate', { mode: 'hide' }, r => `${fmt(r.count)} elementos ocultos en «${r.view}»`],
  box: ['section_box', {}, r => `Caja 3D sobre ${fmt(r.count)} elementos en «${r.view}»`],
};

async function onAction(name) {
  if (VIEW_ACTIONS[name]) {
    const [action, extra, msg] = VIEW_ACTIONS[name];
    return run(action, Object.assign({ ids: checkedIds() }, extra), msg);
  }
  if (name === 'reset') return run('reset_view', {}, r => `Vista «${r.view}» restablecida`);
  if (name === 'colorize') {
    const groups = { high: new Set(), medium: new Set(), low: new Set() };
    filtered().filter(it => !it.accepted).forEach(it => idsOf(it).forEach(id => groups[it.prio].add(id)));
    const payload = {};
    PRIOS.forEach(p => { payload[p] = [...groups[p]]; });
    return run('colorize', payload, r => `${fmt(r.count)} elementos coloreados por prioridad en «${r.view}»`);
  }
  if (name === 'uncolor') {
    const ids = new Set();
    S.items.forEach(it => idsOf(it, true).forEach(id => ids.add(id)));
    return run('clear_colors', { ids: [...ids] }, r => `Color quitado a ${fmt(r.count)} elementos en «${r.view}»`);
  }
  if (name === 'accept' || name === 'reopen') {
    const keys = S.items.filter(it => S.checked.has(it.i)).map(it => it.key);
    const accepted = await run('accept', { keys, accepted: name === 'accept' },
      `${fmt(keys.length)} warnings ${name === 'accept' ? 'marcados como aceptados' : 'reabiertos'}`);
    if (!accepted) return;
    const set = new Set(accepted);
    S.items.forEach(it => { it.accepted = set.has(it.key); });
    S.checked.clear();
    renderAll();
  }
}

async function onFix(kind) {
  const groups = activeItems().filter(it => !it.accepted).map(it => it.f);
  const count = kind === 'duplicates' ? groups.reduce((n, g) => n + g.length - 1, 0) : groups.length;
  const text = kind === 'duplicates'
    ? `Se eliminarán ${count} elementos duplicados (se conserva el de menor ID de cada grupo). Sus etiquetas y cotas se borran con ellos.`
    : `Se revisarán ${count} pares de líneas y se eliminará la línea contenida dentro de otra. Las superposiciones parciales no se tocan.`;
  if (!confirm(text + '\n\nSe puede deshacer con Ctrl+Z en Revit. ¿Continuar?')) return;
  const r = await run(kind === 'duplicates' ? 'fix_duplicates' : 'fix_overlap_lines', { groups, description: S.active });
  if (!r) return;
  let msg = `${fmt(r.deleted.length)} elementos eliminados`;
  if (r.cascade) msg += ` (+${fmt(r.cascade)} dependientes: etiquetas, cotas…)`;
  if (r.stale) msg += ` · ${fmt(r.stale)} grupos ya no estaban vigentes y se saltaron`;
  if (r.skipped.length) msg += ` · ${fmt(r.skipped.length)} omitidos (los tiene otro usuario)`;
  if (r.partial) msg += ` · ${fmt(r.partial)} superposiciones parciales para revisar a mano`;
  toast(msg);
  await scan();
}

async function toggleViewFilter() {
  if (S.viewSet) { S.viewSet = null; return renderFiltered(); }
  const ids = new Set();
  S.items.forEach(it => idsOf(it, true).forEach(id => ids.add(id)));
  const r = await run('view_ids', { ids: [...ids] });
  if (!r) return;
  S.viewSet = new Set(r.ids);
  S.viewName = r.view;
  renderFiltered();
}

function exportCsv() {
  const cols = ['Prioridad', 'Grupo', 'Warning', 'Aceptado', 'ID', 'Rol', 'Categoría', 'Nombre', 'Nivel', 'Vista', 'Workset', 'Creador', 'Último editor'];
  const cell = v => {
    let t = String(v);
    if (/^[=+\-@]/.test(t)) t = "'" + t; // que Excel no lo lea como fórmula
    return /[";\n\r]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
  };
  const rows = [cols];
  filtered().forEach(it => {
    const add = (id, role) => {
      const e = info(id) || {};
      rows.push([PRIORITY[it.prio].label, it.rule.group, it.d, it.accepted ? 'Sí' : 'No', id, role, e.cat || '', e.name || '', e.level || '', e.view || '', e.ws || '', e.creator || '', e.editor || '']);
    };
    it.f.forEach(id => add(id, 'Principal'));
    it.a.forEach(id => add(id, 'Secundario'));
  });
  // ';' porque Excel en español usa la coma como separador decimal.
  const text = rows.map(r => r.map(cell).join(';')).join('\r\n');
  const name = `Warnings_${S.data.doc.title}_${localDate()}.csv`.replace(/[\\/:*?"<>|]+/g, '_');
  run('export_csv', { filename: name, text }, r => r.path ? `CSV guardado en ${r.path}` : 'Exportación cancelada');
}

// --- Eventos ---------------------------------------------------------------------
function renderFiltered() {
  S.limit = ROW_STEP;
  // Las acciones nunca deben tocar warnings que el filtro ya no muestra.
  const visible = new Set(activeItems().map(it => it.i));
  [...S.checked].forEach(i => { if (!visible.has(i)) S.checked.delete(i); });
  renderTypesChart(); renderDonut(); renderDist(); renderSidebar(); renderDetail();
}

document.addEventListener('click', e => {
  const t = e.target.closest('[data-type],[data-prio],[data-facet],[data-dist],[data-toggle],[data-eid],[data-row],[data-act],[data-fix],[data-more],[data-checkall]');
  if (!t || t.disabled) return;
  const d = t.dataset;
  if (d.type !== undefined) { S.active = d.type; S.checked.clear(); S.limit = ROW_STEP; renderTypesChart(); renderSidebar(); renderDetail(); }
  else if (d.prio) { S.prio.has(d.prio) ? S.prio.delete(d.prio) : S.prio.add(d.prio); if (!S.prio.size) S.prio = new Set(PRIOS); renderFiltered(); }
  else if (d.facet !== undefined) {
    const same = S.facet && S.facet.kind === S.dist && S.facet.value === d.facet;
    S.facet = same ? null : { kind: S.dist, value: d.facet }; renderFiltered();
  }
  else if (d.dist) { S.dist = d.dist; renderDist(); }
  else if (d.toggle === 'view') toggleViewFilter();
  else if (d.toggle === 'accepted') { S.showAccepted = !S.showAccepted; renderFiltered(); }
  else if (d.toggle === 'facet') { S.facet = null; renderFiltered(); }
  else if (d.eid) {
    const ids = [Number(d.eid)];
    e.ctrlKey ? run('isolate', { ids, mode: 'isolate' }, r => `Elemento ${d.eid} aislado en «${r.view}»`)
      : run('show', { ids }, `Mostrando elemento ${d.eid}`);
  }
  else if (d.checkall !== undefined) {
    const shown = activeItems().slice(0, S.limit);
    t.checked ? shown.forEach(it => S.checked.add(it.i)) : shown.forEach(it => S.checked.delete(it.i));
    renderDetail();
  }
  else if (d.row) {
    const i = Number(d.row);
    S.checked.has(i) ? S.checked.delete(i) : S.checked.add(i);
    t.classList.toggle('checked', S.checked.has(i));
    t.querySelector('input').checked = S.checked.has(i);
    renderActionbar();
  }
  else if (d.more !== undefined) { S.limit += ROW_STEP; renderDetail(); }
  else if (d.act) onAction(d.act);
  else if (d.fix) onFix(d.fix);
});

let searchTimer;
$('#search').addEventListener('input', e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { S.q = e.target.value.trim(); renderFiltered(); }, 180);
});
$('#additional').addEventListener('change', e => { S.additional = e.target.checked; renderActionbar(); });
$('#btn-scan').addEventListener('click', scan);
$('#btn-csv').addEventListener('click', exportCsv);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && S.checked.size) { S.checked.clear(); renderDetail(); }
  if (e.key === 'F5') { e.preventDefault(); scan(); }
});

scan();
