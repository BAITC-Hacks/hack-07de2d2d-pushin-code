/* Windcast frontend — vanilla JS, all data from the real API (relative /api/...). No build step. */
(() => {
'use strict';
const $ = s => document.querySelector(s);
const RM = matchMedia('(prefers-reduced-motion: reduce)').matches;
const sleep = ms => new Promise(r => setTimeout(r, RM ? Math.min(ms, 60) : ms));
const pad = n => String(n).padStart(2, '0');
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const avg = a => a.length ? a.reduce((s, v) => s + v, 0) / a.length : 0;
const pct = v => Math.round(v * 100) + ' %';
const f1 = v => (Math.round(v * 10) / 10).toFixed(1).replace('.', ',');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const H = 3600e3;

/* ISO with an offset ("2026-02-14T00:00+05:00") or "…Z" → Date whose getUTC* fields equal the WALL-CLOCK fields.
   The UI shows local Astana time exactly as the API wrote it, no browser-zone conversion. */
const wall = s => { const m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(s || ''); return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0))) : null; };
const dm = d => pad(d.getUTCDate()) + '.' + pad(d.getUTCMonth() + 1);
const hm = d => `${dm(d)} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
const dmy = d => `${dm(d)}.${d.getUTCFullYear()}`;
const fmtLocal = s => { const d = wall(s); return d ? `${dmy(d)} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}` : (s || '—'); };
const fmtRun = s => { const d = wall(s); return d ? `${hm(d)} UTC` : (s || '—'); };
const fmtRec = s => { const d = wall(s); return d ? `${dm(d)} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}` : (s || ''); };

/* ---------- API ---------- */
async function api(path, opts){
  let r;
  try { r = await fetch(path, opts); }
  catch (e){ const err = new Error('API недоступен: ' + path); err.status = 0; throw err; }
  if (!r.ok){
    let msg = `HTTP ${r.status} · ${path}`;
    try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (e) { /* not JSON */ }
    const err = new Error(msg); err.status = r.status; throw err;
  }
  return r.json();
}
const post = (path, body) => api(path, {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
const errHTML = (m, what) => `<div class="err">⚠ ${esc(what ? what + ': ' : '')}${esc(m)}</div>`;
const toastEl = $('#toast'); let tt;
function toast(m){ toastEl.textContent = m; toastEl.hidden = false; clearTimeout(tt); tt = setTimeout(() => toastEl.hidden = true, 4000); }

/* ---------- forecast helpers ---------- */
const TURB_LABEL = {plant: 'ВЭС', '1': 'Т1', '2': 'Т2'};
function seriesOf(fc, turb){
  const rows = (fc.rows || []).filter(r => String(r.turbine) === String(turb)).sort((a, b) => a.h - b.h);
  return {
    n: rows.length,
    p10: rows.map(r => +r.p10), p50: rows.map(r => +r.p50), p90: rows.map(r => +r.p90),
    wind: rows.map(r => r.wind_fc_ms == null ? null : +r.wind_fc_ms),
    temp: rows.map(r => r.temp_fc_c),
    times: rows.map(r => wall(r.target_time_local) || new Date(0)),
    fact: rows.some(r => r.actual != null) ? rows.map(r => r.actual == null ? null : +r.actual) : null,
  };
}
const FLAG_MARK = {ramp: '⚠', ice: '❄', wind_gt20: '💨', models_diverge: '≠'};
const FLAG_NAME = {ramp: 'рампа', ice: 'обледенение', wind_gt20: 'ветер > 20 м/с', models_diverge: 'модели расходятся'};
function chartFlags(flags){
  return (flags || []).map(f => ({kind: f.kind, from: Math.max(0, (f.from_h || 1) - 1), to: Math.max(0, (f.to_h || f.from_h || 1) - 1), text: f.text || FLAG_NAME[f.kind] || f.kind,
    mark: f.kind === 'ramp' ? (/спад|−|-/.test(f.text || '') ? '↘' : '↗') : (FLAG_MARK[f.kind] || '•')}));
}
function flagLabel(f){ return `${f.mark} ${f.text}`; }
function legendHTML(flags, hasPrev, prevV, hasFact){
  let h = `<span class="lg-i"><span class="sw-l"></span>P50</span><span class="lg-i"><span class="sw-b"></span>коридор P10–P90</span>`;
  if (hasPrev) h += `<span class="lg-i"><span class="sw-v"></span>v${prevV} до пересчёта</span>`;
  if (hasFact) h += `<span class="lg-i"><span class="sw-f"></span>факт</span>`;
  h += flags.length ? flags.map(f => `<span class="fl ${esc(f.kind)}">${esc(flagLabel(f))}</span>`).join('') : '<span class="fl none">✓ рисков нет</span>';
  return h;
}

/* ---------- chart (SVG, no libraries) ---------- */
function drawChart(el, data){
  const n = data.p50.length;
  if (!n){ el.innerHTML = errHTML('в выпуске нет строк для этой турбины'); return; }
  const W = Math.max(320, el.clientWidth || 700), CH = 250, WH = data.wind ? 74 : 0, m = {l: 44, r: 12, t: 22, b: 24};
  const iw = W - m.l - m.r, ih = CH - m.t - m.b;
  const x = i => m.l + (n > 1 ? (i / (n - 1)) * iw : iw / 2), y = v => m.t + (1 - clamp(v, 0, 1)) * ih;
  const path = (arr, close) => { let s = '', started = false; arr.forEach((v, i) => { if (v == null || Number.isNaN(v)) { started = false; return; } s += `${started ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`; started = true; }); return s; };
  let s = `<svg viewBox="0 0 ${W} ${CH + WH + 8}" role="img" aria-label="Прогноз выработки на ${n} часов">`;
  (data.flags || []).forEach(f => {
    const x0 = x(Math.max(0, f.from - .5)), x1 = x(Math.min(n - 1, f.to + .5));
    const col = f.kind === 'ramp' ? 'rgba(240,180,90,.10)' : f.kind === 'wind_gt20' ? 'rgba(255,123,114,.10)' : 'rgba(169,221,245,.08)';
    const ink = f.kind === 'ramp' ? '#F0B45A' : f.kind === 'wind_gt20' ? '#FF7B72' : '#A9DDF5';
    s += `<rect x="${x0}" y="${m.t}" width="${Math.max(3, x1 - x0)}" height="${ih}" fill="${col}"/>`;
    s += `<text x="${(x0 + x1) / 2}" y="${m.t - 7}" text-anchor="middle" style="fill:${ink}; font-size:12px">${f.mark}</text>`;
  });
  [0, .25, .5, .75, 1].forEach(g => { s += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(g)}" y2="${y(g)}" stroke="#1E2C35" stroke-width="1"/><text x="${m.l - 7}" y="${y(g) + 4}" text-anchor="end">${g * 100}%</text>`; });
  const labelEvery = n > 60 ? 12 : 6;
  data.times.forEach((t, i) => {
    const hh = t.getUTCHours();
    if (hh === 0 || i === 0){ if (i > 0) s += `<line x1="${x(i)}" x2="${x(i)}" y1="${m.t}" y2="${CH - m.b + 4}" stroke="#2F4553" stroke-dasharray="3 3"/>`; s += `<text class="dl" x="${x(i) + 4}" y="${m.t + 12}">${dm(t)}</text>`; }
    if (hh % labelEvery === 0) s += `<text x="${x(i)}" y="${CH - 6}" text-anchor="middle">${pad(hh)}</text>`;
  });
  s += `<line x1="${m.l}" x2="${m.l}" y1="${m.t}" y2="${CH - m.b}" stroke="#7CC4FF" stroke-width="1.5"/>`;
  const band = data.p90.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('') + data.p10.map((v, i) => [i, v]).reverse().map(([i, v]) => `L${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('') + 'Z';
  s += `<path d="${band}" fill="rgba(124,196,255,.20)"/>`;
  if (data.prev) s += `<path d="${path(data.prev)}" fill="none" stroke="#8B9CA5" stroke-width="1.6" stroke-dasharray="5 4"/>`;
  s += `<path d="${path(data.p50)}" fill="none" stroke="#7CC4FF" stroke-width="2.4" stroke-linejoin="round"/>`;
  if (data.fact) s += `<path d="${path(data.fact)}" fill="none" stroke="#E4ECEF" stroke-width="1.4"/>`;
  if (data.wind){
    const y0 = CH + 8, wy = w => y0 + WH - 16 - (clamp(w, 0, 20) / 20) * (WH - 26), bw = Math.max(2, iw / n - 2);
    s += `<text x="${m.l - 7}" y="${y0 + 10}" text-anchor="end">м/с</text>`;
    data.wind.forEach((w, i) => { if (w == null) return; s += `<rect x="${x(i) - bw / 2}" y="${wy(w)}" width="${bw}" height="${Math.max(0, y0 + WH - 16 - wy(w))}" fill="${w >= 20 ? '#8A4A48' : w >= 11.5 ? '#56707F' : '#34495A'}" rx="1"/>`; });
    [[3, 'пуск 3'], [11.5, 'номинал 11,5']].forEach(([w, l]) => { s += `<line x1="${m.l}" x2="${W - m.r}" y1="${wy(w)}" y2="${wy(w)}" stroke="#2F4553" stroke-dasharray="2 3"/><text x="${W - m.r}" y="${wy(w) - 3}" text-anchor="end">${l}</text>`; });
    s += `<text x="${m.l}" y="${y0 + WH - 2}">ветер на высоте ступицы, прогноз</text>`;
  }
  s += `<line class="hv" x1="0" x2="0" y1="${m.t}" y2="${CH - m.b}" stroke="#E4ECEF" stroke-opacity=".35" visibility="hidden"/>`;
  s += `<rect x="${m.l}" y="${m.t}" width="${iw}" height="${CH - m.t}" fill="transparent" style="cursor:crosshair"/></svg><div class="tip" hidden></div>`;
  el.innerHTML = s;
  const svg = el.querySelector('svg'), tip = el.querySelector('.tip'), hv = el.querySelector('.hv');
  svg.addEventListener('mousemove', e => {
    const b = svg.getBoundingClientRect(), k = W / b.width, mx = (e.clientX - b.left) * k;
    const i = clamp(Math.round((mx - m.l) / iw * (n - 1)), 0, n - 1);
    hv.setAttribute('x1', x(i)); hv.setAttribute('x2', x(i)); hv.setAttribute('visibility', 'visible');
    tip.hidden = false; tip.style.left = (x(i) / k) + 'px'; tip.style.top = (y(data.p90[i]) / k - 8) + 'px';
    tip.innerHTML = `${hm(data.times[i])} · h${i + 1}<br><b>P50 ${pct(data.p50[i])}</b> · P10–P90 ${pct(data.p10[i])}–${pct(data.p90[i])}` +
      (data.wind && data.wind[i] != null ? `<br>ветер ${f1(data.wind[i])} м/с` : '') +
      (data.temp && data.temp[i] != null ? ` · ${f1(data.temp[i])} °C` : '') +
      (data.prev && data.prev[i] != null ? `<br>до пересчёта ${pct(data.prev[i])}` : '') +
      (data.fact && data.fact[i] != null ? `<br>факт ${pct(data.fact[i])}` : '');
  });
  svg.addEventListener('mouseleave', () => { tip.hidden = true; hv.setAttribute('visibility', 'hidden'); });
}

/* ---------- agent panel: stepper + trace from real events ---------- */
const STAGES = [['weather', 'погода'], ['prep', 'подготовка'], ['model', 'модель'], ['forecast', 'прогноз'], ['analysis', 'анализ'], ['recalc', 'пересчёт']];
const TYPE_LABEL = {thought: 'мысль', verdict: 'итог', error: 'ошибка', action: 'действие', tool_call: 'вызов', tool_result: 'результат'};
function renderStepper(el, events, running){
  const state = {};
  events.forEach((ev, k) => {
    const g = ev.meta && ev.meta.stage; if (!g) return;
    const st = (ev.meta.status === 'skip') ? 'skip' : (ev.meta.status === 'error') ? 'skip' : 'done';
    if (running && k === events.length - 1) state[g] = 'run'; else if (state[g] !== 'done' || st === 'done') state[g] = st;
  });
  el.innerHTML = STAGES.map(([k, l]) => `<span class="stg ${state[k] || ''}"><i></i>${l}${state[k] === 'skip' ? ' · не нужен' : ''}</span>`).join('');
}
function stepHTML(ev, state){
  const mt = ev.meta || {}, status = mt.status || 'ok';
  const cls = ['st', state || 'done', esc(ev.type || '')];
  if (status === 'skip') cls.push('skip'); if (status === 'warn') cls.push('warn'); if (status === 'error' || ev.type === 'error') cls.push('error');
  const ic = state === 'run' ? '<i class="spin"></i>' : status === 'skip' ? '—' : status === 'warn' ? '⚠' : (status === 'error' || ev.type === 'error') ? '✕' : '✓';
  const code = mt.tool ? esc(mt.tool) : esc(TYPE_LABEL[ev.type] || ev.type || '');
  let pills = '';
  if (mt.stage) pills += `<span class="pill${status === 'skip' ? ' skip' : ''}">${esc(mt.stage)}</span>`;
  if (ev.type === 'action') pills += '<span class="act">действие</span>';
  if (ev.type === 'verdict') pills += '<span class="pill">verdict</span>';
  const metaBits = [];
  if (mt.decision) metaBits.push(`решение: ${esc(mt.decision)}`);
  if (mt.by) metaBits.push(`by ${esc(mt.by)}`);
  if (mt.source) metaBits.push(`source ${esc(mt.source)}`);
  if (mt.version != null) metaBits.push(`v${esc(mt.version)}`);
  if (mt.issue_date) metaBits.push(esc(mt.issue_date));
  return `<li class="${cls.join(' ')}"><span class="no">${esc(ev.seq ?? '')}</span><div>
    <div class="tl"><code>${code}</code>${pills}<span class="ic">${ic}</span></div>
    <div class="dt">${esc(ev.title || '')}</div>${ev.body ? `<div class="why">${esc(ev.body)}</div>` : ''}${metaBits.length ? `<div class="mt">${metaBits.join(' · ')}</div>` : ''}</div></li>`;
}
function renderTrace(listEl, stepEl, events){ listEl.innerHTML = events.map(ev => stepHTML(ev, 'done')).join(''); renderStepper(stepEl, events, false); }
function appendEvent(listEl, stepEl, events, ev){
  const prev = listEl.lastElementChild; if (prev && prev.classList.contains('run')) prev.outerHTML = stepHTML(events[events.length - 1], 'done');
  events.push(ev);
  const last = ev.type === 'verdict' || ev.type === 'error';
  listEl.insertAdjacentHTML('beforeend', stepHTML(ev, last ? 'done' : 'run').replace('class="st ', 'class="st new '));
  renderStepper(stepEl, events, !last);
  listEl.lastElementChild.scrollIntoView({block: 'nearest'});
}

/* Run the agent: POST /api/runs → SSE /api/runs/{id}/events. Resolves with the list of events after `verdict`. */
async function runAgent(body, listEl, stepEl, sumEl, opts = {}){
  const events = [];
  listEl.innerHTML = ''; renderStepper(stepEl, events, true);
  if (sumEl) sumEl.textContent = 'агент работает…';
  let id;
  try { id = (await post('/api/runs', body)).id; }
  catch (e){ listEl.innerHTML = errHTML(e.message, 'запуск агента'); renderStepper(stepEl, [], false); if (sumEl) sumEl.textContent = '—'; throw e; }
  listEl.insertAdjacentHTML('beforeend', `<li class="loading">запуск ${esc(id)} · жду события…</li>`);
  await new Promise(resolve => {
    let done = false;
    const finish = () => { if (done) return; done = true; es.close(); resolve(); };
    const es = new EventSource(`/api/runs/${encodeURIComponent(id)}/events`);
    es.onmessage = e => {
      let ev; try { ev = JSON.parse(e.data); } catch (err){ return; }
      const ld = listEl.querySelector('.loading'); if (ld) ld.remove();
      appendEvent(listEl, stepEl, events, ev);
      if (opts.onEvent) opts.onEvent(ev);
      if (ev.type === 'verdict') finish();
    };
    es.onerror = async () => {
      // SSE dropped: fall back to GET /api/runs/{id} (poll until done).
      es.close();
      for (let k = 0; k < 60 && !done; k++){
        try {
          const r = await api(`/api/runs/${encodeURIComponent(id)}`);
          const evs = r.events || [];
          for (let j = events.length; j < evs.length; j++) appendEvent(listEl, stepEl, events, evs[j]);
          if (r.done){ break; }
        } catch (err){ listEl.insertAdjacentHTML('beforeend', errHTML(err.message, 'события прогона')); break; }
        await sleep(1000);
      }
      finish();
    };
  });
  renderStepper(stepEl, events, false);
  const verdict = events.find(e => e.type === 'verdict') || events[events.length - 1];
  if (sumEl) sumEl.textContent = verdict ? (verdict.body || verdict.title || '—') : '—';
  return events;
}

/* ---------- state ---------- */
let issues = [], sel = null, played = Infinity, playing = false, busy = false, febTurb = 'plant', liveTurb = 'plant';
let febFc = null, febPrev = null, febTraceRec = null, liveFc = null, liveStatus = null, health = null, seqFeb = 0;
let tab = 'feb';
function lock(on){ busy = on; document.querySelectorAll('#btn-reissue,#btn-newrun,#btn-outage,#btn-live-issue,#btn-live-check').forEach(b => b.disabled = on); $('#btn-play').disabled = on && !playing; }

/* ---------- header ---------- */
async function loadHealth(){
  const chip = $('#mode-chip');
  try {
    health = await api('/health');
    const mode = health.mode === 'agent' ? 'LLM' : health.mode === 'deterministic' ? 'по правилам' : (health.mode || '?');
    const ports = health.ports ? Object.entries(health.ports).map(([k, v]) => `${k}: ${v}`).join(', ') : '';
    chip.className = 'chip' + (health.ok ? '' : ' off');
    chip.innerHTML = `<span class="d"></span>агент: ${esc(mode)}${health.model_version ? ' · ' + esc(health.model_version) : ''}`;
    chip.title = `GET /health · mode=${health.mode}${ports ? ' · ' + ports : ''}${health.issues_ready != null ? ' · выпусков: ' + health.issues_ready : ''}`;
    $('#feb-agent-chip').textContent = health.mode === 'agent' ? 'function calling · LLM' : 'детерминированный режим';
    $('#live-watch').textContent = health.live_watch_minutes ? `каждые ${health.live_watch_minutes} мин` : 'выключено (LIVE_WATCH_MINUTES=0)';
  } catch (e){ chip.className = 'chip off'; chip.innerHTML = `<span class="d"></span>API недоступен`; chip.title = e.message; }
}

/* ---------- February: calendar ---------- */
function anyFlags(f){ return f && Object.values(f).some(v => +v > 0); }
function renderCells(){
  if (!issues.length) return;
  $('#cells').innerHTML = issues.map((d, i) => {
    const dt = wall(d.issue_date), nv = (d.versions || []).length, missing = d.status === 'missing';
    return `<button class="cell${d.issue_date === sel ? ' on' : ''}${i > played ? ' fut' : ''}${missing ? ' missing' : ''}" data-d="${esc(d.issue_date)}" aria-label="Выпуск ${dm(dt)}${missing ? ' — не посчитан' : ''}" aria-pressed="${d.issue_date === sel}" title="${esc(d.issue_date)} · ${d.status}${d.mean_p50 != null ? ' · средняя ' + pct(d.mean_p50) : ''}">
      <span class="dn">${dt.getUTCDate()}</span>${(i === 0 || dt.getUTCDate() === 1) ? `<span class="mo">${['янв', 'фев', 'мар'][dt.getUTCMonth()] || ''}</span>` : ''}${anyFlags(d.flags) ? '<span class="fd"></span>' : ''}${nv > 1 ? `<span class="vb">v${nv}</span>` : ''}${d.status === 'running' ? '<span class="vb">…</span>' : ''}
      <span class="bar" style="height:${missing ? 0 : Math.round(4 + (+d.mean_p50 || 0) * 40)}px"></span></button>`;
  }).join('');
}
async function loadIssues(){
  try {
    issues = await api('/api/issues?from=2026-01-31&to=2026-02-28');
    if (!Array.isArray(issues) || !issues.length){ $('#cells').innerHTML = errHTML('API вернул пустой список выпусков', 'календарь'); return false; }
    if (!sel){ const pick = issues.find(d => d.issue_date === '2026-02-13' && d.status !== 'missing') || issues.filter(d => d.status !== 'missing').pop() || issues[0]; sel = pick.issue_date; }
    renderCells(); return true;
  } catch (e){ $('#cells').innerHTML = errHTML(e.message, 'календарь'); return false; }
}

/* ---------- February: issue ---------- */
function renderProv(fc){
  const runs = fc.weather_runs || [];
  let rows = runs.map(r => `<tr><td>Часы ${esc(r.hours)}</td><td>${esc(r.model || '—')} · прогон ${esc(fmtRun(r.init_utc))}</td><td class="${r.before_issue === false ? 'no' : ''}">${r.before_issue === false ? 'после момента выпуска ✕' : 'опубликован до ' + esc(fmtRun(fc.issue_time_utc)) + ' ✓'}</td></tr>`).join('');
  if (febPrev && febPrev.weather_runs) rows += febPrev.weather_runs.map(r => `<tr><td>До пересчёта (v${febPrev.version}) · часы ${esc(r.hours)}</td><td>${esc(r.model || '—')} · прогон ${esc(fmtRun(r.init_utc))}</td><td class="${r.before_issue === false ? 'no' : ''}">${r.before_issue === false ? 'после момента выпуска ✕' : 'до момента выпуска ✓'}</td></tr>`).join('');
  rows += `<tr><td>Фактическая выработка, SCADA</td><td>в прогнозе не используется</td><td>—</td></tr>`;
  const ok = runs.every(r => r.before_issue !== false);
  $('#btn-honest').className = 'honest' + (ok ? '' : ' bad'); $('#btn-honest').textContent = ok ? '✓ Без будущего' : '✕ Есть прогон из будущего';
  $('#prov').innerHTML = `<table>${rows || '<tr><td>weather_runs пуст</td></tr>'}</table><div class="foot">Момент выпуска T: ${esc(fmtLocal(fc.issue_time_local))} по Астане = ${esc(fmtRun(fc.issue_time_utc))}. Правило: старт прогона + 8 ч ≤ T. scripts/verify.py проверяет это для всех выпусков.</div>`;
}
function renderFebChart(){
  const fc = febFc; if (!fc) return;
  const s = seriesOf(fc, febTurb), prev = febPrev ? seriesOf(febPrev, febTurb).p50 : null, flags = chartFlags(fc.flags);
  drawChart($('#feb-chart'), {...s, prev: prev && prev.length === s.n ? prev : null, flags});
  $('#feb-flags').innerHTML = legendHTML(flags, !!(prev && prev.length === s.n), febPrev ? febPrev.version : 0, !!s.fact);
}
function renderFebHead(fc){
  const dt = wall(fc.issue_date), t = seriesOf(fc, 'plant').times;
  const clk = `${dmy(dt)} 24:00`;
  $('#feb-clock').textContent = clk; $('#feb-clock2').textContent = clk;
  const span = t.length ? `${dm(t[0])}–${dm(t[t.length - 1])}` : '—';
  $('#feb-title').innerHTML = `Выпуск ${dm(dt)}<small>прогноз на ${span} · ${t.length} часов · нормированная мощность, 100 % = номинал${fc.model_version ? ' · ' + esc(fc.model_version) : ''}</small>`;
  const nv = (fc.versions || []).length;
  $('#feb-ver').textContent = nv > 1 ? `v${fc.version} · пересчитан` : `v${fc.version || 1}`;
  const ch = $('#feb-change'); if (fc.change_note){ ch.hidden = false; ch.innerHTML = `<b>Что изменилось против v${febPrev ? febPrev.version : (fc.version - 1)}:</b> ${esc(fc.change_note)}`; } else ch.hidden = true;
  $('#feb-sum').textContent = fc.summary ? '«' + fc.summary + '»' : '—';
}
async function loadForecast(d){
  const my = ++seqFeb;
  const w = $('#feb-chart'); w.classList.add('pending');
  try {
    const fc = await api(`/api/forecasts/${encodeURIComponent(d)}`);
    let prev = null;
    const vs = (fc.versions || []).map(Number).filter(v => v < fc.version);
    if (vs.length){ try { prev = await api(`/api/forecasts/${encodeURIComponent(d)}?version=${Math.max(...vs)}`); } catch (e){ prev = null; } }
    if (my !== seqFeb) return;
    febFc = fc; febPrev = prev;
    renderFebHead(fc); renderProv(fc); renderFebChart();
  } catch (e){
    if (my !== seqFeb) return;
    febFc = null; febPrev = null;
    $('#feb-title').innerHTML = `Выпуск ${esc(d)}<small>—</small>`; $('#feb-ver').textContent = '—';
    w.innerHTML = errHTML(e.message, 'выпуск ' + d); $('#feb-flags').innerHTML = ''; $('#feb-change').hidden = true; $('#feb-sum').textContent = '—';
    $('#prov').innerHTML = errHTML(e.message, 'weather_runs');
  } finally { w.classList.remove('pending'); }
}
async function loadTrace(d){
  const listEl = $('#feb-trace'), stepEl = $('#feb-stepper');
  try {
    const tr = await api(`/api/traces/${encodeURIComponent(d)}`);
    febTraceRec = tr;
    renderTrace(listEl, stepEl, tr.events || []);
    $('#feb-rec').textContent = `выпуски — запись прогона агента от ${fmtRec(tr.recorded_at)} · v${tr.version}`;
  } catch (e){ febTraceRec = null; listEl.innerHTML = errHTML(e.message, 'лента агента'); renderStepper(stepEl, [], false); $('#feb-rec').textContent = 'запись прогона недоступна'; }
}
async function selectDay(d){ sel = d; renderCells(); await Promise.all([loadForecast(d), loadTrace(d)]); }

/* ---------- February: buttons ---------- */
$('#cells').addEventListener('click', e => { const c = e.target.closest('.cell'); if (!c || busy) return; selectDay(c.dataset.d); });
function seg(id, set){ $(id).addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; $(id).querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', String(x === b))); set(b.dataset.t); }); }
seg('#feb-seg', t => { febTurb = t; renderFebChart(); });
seg('#live-seg', t => { liveTurb = t; renderLiveChart(); });
$('#btn-honest').addEventListener('click', () => { const p = $('#prov'); p.hidden = !p.hidden; $('#btn-honest').setAttribute('aria-expanded', String(!p.hidden)); });
$('#btn-csv').addEventListener('click', () => { if (sel) window.open(`/api/forecasts/${encodeURIComponent(sel)}.csv`, '_blank'); });
$('#btn-csv-all').addEventListener('click', () => window.open('/api/forecasts/february.csv', '_blank'));
$('#btn-live-csv').addEventListener('click', () => window.open('/api/forecasts/live.csv', '_blank'));

$('#btn-play').addEventListener('click', async () => {
  if (playing){ playing = false; return; }
  if (busy || !issues.length) return;
  playing = true; lock(true); $('#btn-play').textContent = '■ Стоп'; played = -1; renderCells();
  $('#feb-rec').textContent = 'воспроизведение — запись прогона агента, без LLM';
  let i = 0;
  for (; i < issues.length && playing; i++){
    if (issues[i].status === 'missing') continue;
    played = i; await selectDay(issues[i].issue_date);
    const c = $('#cells').children[i]; if (c && c.scrollIntoView) c.scrollIntoView({block: 'nearest', inline: 'nearest'});
    await sleep(450);
  }
  const done = i >= issues.length; played = Infinity; playing = false; lock(false); $('#btn-play').textContent = '▶ Воспроизвести февраль'; renderCells();
  toast(done ? `${issues.length} выпусков воспроизведены из записи прогона агента` : 'Воспроизведение остановлено');
});

async function febRun(body, label){
  if (busy || !sel) return; lock(true);
  const w = $('#feb-chart'); w.classList.add('pending');
  $('#feb-rec').textContent = `${label} · живой прогон агента`;
  try {
    const events = await runAgent(body, $('#feb-trace'), $('#feb-stepper'), $('#feb-sum'));
    await Promise.all([loadForecast(sel), loadIssues()]);
    const v = events.find(e => e.type === 'verdict');
    toast(v ? v.title : 'Прогон завершён');
    if (febTraceRec === null) loadTrace(sel).then(() => {}); // keep the live events; only the rec line comes back
    else $('#feb-rec').textContent = `${label} — завершён · выпуск обновлён`;
  } catch (e){ toast('Не удалось запустить агента: ' + e.message); }
  finally { w.classList.remove('pending'); lock(false); }
}
$('#btn-reissue').addEventListener('click', () => febRun({issue_date: sel, trigger: 'issue'}, '↻ перевыпуск'));
$('#btn-newrun').addEventListener('click', () => febRun({issue_date: sel, trigger: 'new_weather_run'}, '⚡ новый прогон погоды'));
$('#btn-outage').addEventListener('click', () => febRun({issue_date: sel, trigger: 'issue', scenario: 'weather_outage'}, 'имитация сбоя погоды'));

/* ---------- Live ---------- */
function renderLiveStatus(st){
  $('#live-now').textContent = st.now_local ? fmtLocal(st.now_local) : '—';
  $('#live-last').textContent = st.latest_run_utc ? fmtRun(st.latest_run_utc) : '—';
  $('#live-next').textContent = st.next_run_utc ? `${fmtRun(st.next_run_utc)} → ≈ ${st.next_run_available_local ? fmtLocal(st.next_run_available_local).slice(11) : '—'} по Астане` : '—';
}
async function loadLiveStatus(){
  try { liveStatus = await api('/api/live/status'); renderLiveStatus(liveStatus); }
  catch (e){ $('#live-now').textContent = 'нет данных'; $('#live-now').title = e.message; }
}
function renderLiveChart(){
  const fc = liveFc; if (!fc) return;
  const s = seriesOf(fc, liveTurb), flags = chartFlags(fc.flags);
  drawChart($('#live-chart'), {...s, flags});
  $('#live-flags').innerHTML = legendHTML(flags, false, 0, !!s.fact);
}
function renderLiveEmpty(msg){
  liveFc = null;
  $('#live-sub').textContent = 'выпуска ещё нет'; $('#live-ver').textContent = '—';
  $('#live-chart').innerHTML = `<div class="empty">${esc(msg || 'Live-выпуска ещё нет.')}<br>Нажмите «▶ Выпустить прогноз сейчас» — агент возьмёт последний прогон Open-Meteo и посчитает 48 часов от следующего полного часа.</div>`;
  $('#live-flags').innerHTML = ''; $('#live-change').hidden = true; $('#live-sum').textContent = '—';
}
async function loadLiveForecast(){
  try {
    const fc = await api('/api/forecasts/live'); liveFc = fc;
    const t = seriesOf(fc, 'plant').times, cur = liveStatus && liveStatus.current;
    const run = (fc.weather_runs && fc.weather_runs[0] && fc.weather_runs[0].init_utc) || (cur && cur.weather_run_utc);
    $('#live-sub').textContent = `${t.length ? hm(t[0]) + ' → ' + hm(t[t.length - 1]) : '—'} · выпущен ${fmtLocal(fc.issue_time_local)}${run ? ' на прогоне ' + fmtRun(run) : ''}`;
    $('#live-ver').textContent = (fc.versions || []).length > 1 ? `v${fc.version} · пересчитан` : `v${fc.version || 1}`;
    const ch = $('#live-change'); if (fc.change_note){ ch.hidden = false; ch.innerHTML = `<b>Что изменилось:</b> ${esc(fc.change_note)}`; } else ch.hidden = true;
    $('#live-sum').textContent = fc.summary ? '«' + fc.summary + '»' : '—';
    renderLiveChart();
  } catch (e){
    if (e.status === 404) renderLiveEmpty(e.message); else { liveFc = null; $('#live-chart').innerHTML = errHTML(e.message, 'live-выпуск'); }
  }
}
const OUTCOME = {published: 'опубликован', kept: 'оставлен', refused: 'отказ', error: 'ошибка'};
async function loadJournal(){
  const el = $('#journal');
  try {
    const j = await api('/api/live/journal?limit=20');
    if (!Array.isArray(j) || !j.length){ el.innerHTML = '<div class="empty">Журнал пуст — агент ещё ничего не делал в Live.</div>'; return; }
    el.innerHTML = j.map(r => `<li><span class="ts">${esc(fmtRec(r.ts))}</span><span class="who ${esc(r.initiator || '')}">${esc(r.initiator || '?')}</span><span class="oc ${esc(r.outcome || '')}">${esc(OUTCOME[r.outcome] || r.outcome || '')}${r.version != null ? ' · v' + esc(r.version) : ''}</span><span class="ttl">${esc(r.title || '')}${r.trigger ? ` <span class="mono" style="color:var(--dim)">· ${esc(r.trigger)}</span>` : ''}</span></li>`).join('');
  } catch (e){ el.innerHTML = errHTML(e.message, 'журнал'); }
}
async function loadLive(){ await loadLiveStatus(); await Promise.all([loadLiveForecast(), loadJournal()]); }
async function liveRun(body){
  if (busy) return; lock(true);
  const w = $('#live-chart'); w.classList.add('pending');
  try {
    const events = await runAgent(body, $('#live-trace'), $('#live-stepper'), $('#live-sum'));
    await loadLive();
    const v = events.find(e => e.type === 'verdict'); toast(v ? v.title : 'Прогон завершён');
  } catch (e){ toast('Не удалось запустить агента: ' + e.message); }
  finally { w.classList.remove('pending'); lock(false); }
}
$('#btn-live-issue').addEventListener('click', () => liveRun({issue_date: 'live', trigger: 'issue'}));
$('#btn-live-check').addEventListener('click', () => liveRun({issue_date: 'live', trigger: 'new_weather_run'}));

/* ---------- Quality ---------- */
const METHOD_RU = {model: 'Наша модель', power_curve: 'Кривая мощности по прогнозу ветра', climatology: 'Средняя по часу и месяцу', persistence: '«Завтра как сегодня»'};
const pctv = v => (Math.round(v * 1000) / 10).toFixed(1).replace('.', ',') + ' %';
let metrics = null, series = null;
function drawHorizon(el, bh){
  if (!bh || !bh.length){ el.innerHTML = errHTML('нет данных by_horizon'); return; }
  const W = Math.max(300, el.clientWidth || 500), HH = 200, m = {l: 44, r: 12, t: 12, b: 26}, iw = W - m.l - m.r, ih = HH - m.t - m.b;
  const n = bh.length, top = Math.max(.2, Math.ceil(Math.max(...bh.map(r => Math.max(r.model || 0, r.power_curve || 0))) * 20) / 20);
  const x = h => m.l + (h - 1) / Math.max(1, n - 1) * iw, y = v => m.t + (1 - v / top) * ih;
  let s = `<svg viewBox="0 0 ${W} ${HH}" role="img" aria-label="Ошибка по горизонту">`;
  const step = top / 4;
  [0, 1, 2, 3, 4].forEach(k => { const g = k * step; s += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(g)}" y2="${y(g)}" stroke="#1E2C35"/><text x="${m.l - 6}" y="${y(g) + 4}" text-anchor="end">${Math.round(g * 100)}%</text>`; });
  [1, 12, 24, 36, 48].filter(h => h <= n).forEach(h => { s += `<text x="${x(h)}" y="${HH - 8}" text-anchor="middle">h${h}</text>`; });
  if (n >= 25) s += `<line x1="${x(24.5)}" x2="${x(24.5)}" y1="${m.t}" y2="${HH - m.b}" stroke="#2F4553" stroke-dasharray="3 3"/>`;
  const line = key => bh.map((r, k) => `${k ? 'L' : 'M'}${x(r.h || k + 1).toFixed(1)},${y(+r[key] || 0).toFixed(1)}`).join('');
  s += `<path d="${line('power_curve')}" fill="none" stroke="#5E717B" stroke-width="2"/>`;
  s += `<path d="${line('model')}" fill="none" stroke="#7CC4FF" stroke-width="2.4"/>`;
  const last = bh[n - 1];
  s += `<text x="${x(n) - 2}" y="${y(last.power_curve || 0) - 7}" text-anchor="end">кривая мощности</text><text x="${x(n) - 2}" y="${y(last.model || 0) + 15}" text-anchor="end" style="fill:#7CC4FF">модель</text></svg>`;
  el.innerHTML = s;
}
function renderMetrics(mt){
  const methods = mt.methods || [], model = methods.find(x => x.key === 'model'), base = methods.filter(x => x.key !== 'model' && x.nmae != null);
  const best = base.length ? base.reduce((a, b) => a.nmae < b.nmae ? a : b) : null;
  const gain = model && best ? (model.nmae - best.nmae) / best.nmae : null;
  const tiles = $('#q-tiles').querySelectorAll('.tile');
  tiles[0].querySelector('.v').textContent = model ? pctv(model.nmae) : '—';
  const g = tiles[1].querySelector('.v'); g.textContent = gain == null ? '—' : (gain <= 0 ? '−' : '+') + pctv(Math.abs(gain)); g.className = 'v ' + (gain == null ? '' : gain < 0 ? 'good' : 'bad');
  tiles[1].querySelector('.k').textContent = best ? `против лучшего простого метода (${METHOD_RU[best.key] || best.label})` : 'против лучшего простого метода';
  tiles[2].querySelector('.v').textContent = mt.coverage_p10_p90 != null ? Math.round(mt.coverage_p10_p90 * 100) + ' %' : '—';
  tiles[3].querySelector('.v').textContent = mt.issues_count != null ? mt.issues_count : '—';
  tiles[3].querySelector('.k').textContent = mt.period ? `выпусков проверено: ${dm(wall(mt.period.from))} → ${dm(wall(mt.period.to))}` : 'выпусков проверено';
  const mx = Math.max(...methods.map(x => x.nmae || 0)) || 1;
  $('#q-bars').innerHTML = methods.length ? methods.map(x => `<div class="brow${x.key === 'model' ? ' me' : ''}"><span title="${esc(x.label || '')}">${esc(METHOD_RU[x.key] || x.label || x.key)}</span><div class="tr"><div class="fi" style="width:${Math.round((x.nmae || 0) / mx * 100)}%"></div></div><span class="val">${x.nmae != null ? pctv(x.nmae) : '—'}</span></div>`).join('') : errHTML('нет методов в ответе', 'метрики');
  drawHorizon($('#q-h'), mt.by_horizon);
}
function drawWeek(el, rows){
  if (!rows || !rows.length){ el.innerHTML = errHTML('нет данных ряда'); return; }
  const data = {p10: rows.map(r => +r.p10), p50: rows.map(r => +r.p50), p90: rows.map(r => +r.p90), fact: rows.map(r => r.actual == null ? null : +r.actual), times: rows.map(r => wall(r.target_time_local) || new Date(0)), flags: []};
  drawChart(el, data);
}
async function loadQuality(){
  if (!metrics){
    try { metrics = await api('/api/metrics?from=2025-12-31&to=2026-01-29'); renderMetrics(metrics); }
    catch (e){ $('#q-bars').innerHTML = errHTML(e.message, 'метрики'); $('#q-h').innerHTML = errHTML(e.message, 'метрики'); }
  } else renderMetrics(metrics);
  if (!series){
    try { series = await api('/api/metrics/series?from=2026-01-15&to=2026-01-21&turbine=plant'); drawWeek($('#q-week'), series); }
    catch (e){ $('#q-week').innerHTML = errHTML(e.message, 'ряд прогноз/факт'); }
  } else drawWeek($('#q-week'), series);
}

/* ---------- tabs, drawer, boot ---------- */
function setTab(t){
  tab = t;
  try { history.replaceState(null, '', '#' + t); } catch (e) { /* ignore */ }
  document.querySelectorAll('.tab').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === t)));
  $('#tab-feb').hidden = t !== 'feb'; $('#tab-live').hidden = t !== 'live'; $('#tab-q').hidden = t !== 'q';
  $('#tab-live').style.display = t === 'live' ? 'grid' : '';
  if (t === 'feb') renderFebChart();
  if (t === 'live'){ if (liveFc) renderLiveChart(); loadLive(); }
  if (t === 'q') loadQuality();
}
document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));

const drawer = $('#drawer'), scrim = $('#scrim');
const openD = () => { drawer.hidden = false; scrim.hidden = false; $('#close-tester').focus(); };
const closeD = () => { drawer.hidden = true; scrim.hidden = true; };
$('#open-tester').addEventListener('click', openD); $('#close-tester').addEventListener('click', closeD); scrim.addEventListener('click', closeD);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !drawer.hidden) closeD(); });
function flash(el){ if (!el) return; el.classList.remove('pulse'); void el.offsetWidth; el.classList.add('pulse'); el.scrollIntoView({behavior: RM ? 'auto' : 'smooth', block: 'center'}); setTimeout(() => el.classList.remove('pulse'), 2600); }
drawer.addEventListener('click', e => {
  const b = e.target.closest('[data-go]'); if (!b) return; closeD();
  const g = b.dataset.go;
  if (g === 'live' || g === 'q'){ setTab(g); setTimeout(() => flash(g === 'live' ? $('#btn-live-issue') : $('#q-tiles')), 50); return; }
  setTab('feb');
  if (g === 'cal') flash($('#calcard'));
  if (g === 'honest'){ $('#prov').hidden = false; $('#btn-honest').setAttribute('aria-expanded', 'true'); flash($('#prov')); }
  if (g === 'stepper') flash($('#feb-stepper'));
  if (g === 'newrun') flash($('#btn-newrun'));
  if (g === 'chart') flash($('#feb-chart'));
});

async function boot(){
  loadHealth();
  renderStepper($('#feb-stepper'), [], false); renderStepper($('#live-stepper'), [], false);
  $('#live-trace').innerHTML = '<div class="empty">Шаги агента появятся здесь после «▶ Выпустить прогноз сейчас».</div>';
  const want = (location.hash || '').slice(1);
  if (want === 'live' || want === 'q') setTab(want);
  if (await loadIssues()) await selectDay(sel);
}
boot();
setInterval(() => { if (tab === 'live' && !busy) loadLive(); }, 30000);
setInterval(loadHealth, 60000);
let rt, lastW = 0;
const ro = new ResizeObserver(en => { const w = Math.round(en[0].contentRect.width); if (w === lastW) return; lastW = w; clearTimeout(rt); rt = setTimeout(() => { if (tab === 'feb') renderFebChart(); if (tab === 'live') renderLiveChart(); if (tab === 'q'){ if (metrics) drawHorizon($('#q-h'), metrics.by_horizon); if (series) drawWeek($('#q-week'), series); } }, 120); });
ro.observe(document.querySelector('main'));
})();
