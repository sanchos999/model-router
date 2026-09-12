// Core: state, api, helpers, modal/drawer (R12 §9), navigation, i18n glue.
import { T, lang, setLang } from './i18n.js';

export const PAGES = ['Dashboard','Providers','Models','Monitoring','Routing Policy','Task/Tier Policy','Overrides','Simulator','Audit / Revisions','Settings','How it works'];
export const RPAGES = {}, POSTRENDER = {};
export const ACTIONS = (globalThis.MR_ACTIONS = globalThis.MR_ACTIONS || {});

// ── UI settings (localStorage) ───────────────────────────────────────────
export const UIS = {
  get lang(){ return lang(); },
  set lang(v){ setLang(v); },
  get(k, d){ const v = localStorage.getItem('mr_ui_'+k); return v === null ? d : (v === '1' || v === 'true'); },
  set(k, v){ localStorage.setItem('mr_ui_'+k, v ? '1' : '0'); },
  getNum(k, d){ const v = parseFloat(localStorage.getItem('mr_ui_'+k)); return isNaN(v) ? d : v; },
  setNum(k, v){ localStorage.setItem('mr_ui_'+k, String(v)); },
  diag(){ return this.get('diagnosticsMode', false); },  // R12: единый переключатель диагностики
  showCache(){ return this.get('cachePrices', true); },
  showEstimated(){ return this.get('estimatedPrices', true); },
  showCanonical(){ return this.get('canonicalId', false); },
  showProviderId(){ return this.get('providerModelId', false); },
  showScore(){ return this.get('score', false); },
  showRaw(){ return this.get('rawJson', false); },
};

export const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const $ = id => document.getElementById(id);

export async function api(path, opts){
  // R12-B2: fetch itself can reject (network/refused during restarts).
  // Never throw to callers — return an error result so each UI block can
  // render its own ERROR state instead of blanking the page.
  let r;
  try{
    r = await fetch(path, opts);
  }catch(e){
    return {ok:false, status:0, url:(opts&&opts.method||'GET')+' '+path, data:{}, err:String(e)};
  }
  let data = {};
  try{ data = await r.json(); }catch(e){}
  return {ok:r.ok, status:r.status, url:(opts&&opts.method||'GET')+' '+path, data};
}

export function toast(msg, kind){
  const d = document.createElement('div');
  d.className = 'toast ' + (kind||'');
  d.textContent = msg;
  $('toast').appendChild(d);
  setTimeout(()=>d.remove(), 4200);
}

export function apiErr(r, prefix){
  const d = r.data && r.data.detail;
  const msg = typeof d === 'string' ? d : JSON.stringify(d||{});
  return '<div class="errbox">'+esc(prefix||T('error'))+' — <code>'+esc(r.url)+'</code> HTTP '+esc(r.status)+': '+esc(msg)+'</div>';
}

export function tbl(head, rows, cls){
  if(!rows.length) return '<div class="empty">'+esc(T('noData'))+'</div>';
  return '<table class="tbl-main '+(cls||'')+'"><tr>'+head.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr>'+
    rows.map(r=>'<tr>'+r.map(c=>'<td>'+(c==null?'':c)+'</td>').join('')+'</tr>').join('')+'</table>';
}
export const badge = (txt, kind, tip) => '<span class="badge '+kind+'"'+(tip?' title="'+esc(tip)+'"':'')+'>'+esc(txt)+'</span>';
export const pct = v => v==null ? '—' : (100*v).toFixed(1)+'%';

// ── Prices — R12 §13: never round cheap prices to $0.00 ─────────────────
// >=1 -> 2dp; >=0.01 -> 4dp; >=0.0001 -> 6dp. UNKNOWN is never $0.
export function usdFmt(v){
  if(v == null) return '—';
  v = Number(v);
  if(v >= 1) return '$'+v.toFixed(2);
  if(v >= 0.01) return '$'+v.toFixed(4);
  if(v >= 0.0001) return '$'+v.toFixed(6);
  return '$'+v.toFixed(8);
}
export function price1m(v, state){
  if(state === 'FREE' || (v === 0 && state !== 'UNKNOWN')) return T('free');
  if(state === 'UNKNOWN' || v == null || (v === 0 && state == null)) return '<span class="muted">'+esc(T('priceUnknown'))+'</span>';
  const prefix = state === 'ESTIMATED_UPPER_BOUND' || state === 'ESTIMATED' ? '≈ ' : '';
  return prefix+usdFmt(v)+esc(T('perM'));
}
export const usd6 = usdFmt;
export const ms  = v => v==null ? '—' : Math.round(v)+' мс';
export const dt  = ts => ts ? new Date(ts*1000).toLocaleString('ru-RU') : '—';
export const dtHM = ts => ts ? new Date(ts*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}) : '—';
export const dtHMS = ts => ts ? new Date(ts*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
export const ctxFmt = v => v==null ? '—' : Number(v).toLocaleString('ru-RU');
export function ago(ts){
  if(!ts) return null;
  const s = Math.max(0, Date.now()/1000 - ts);
  if(s < 60) return Math.round(s)+' с назад';
  if(s < 3600) return Math.round(s/60)+' мин назад';
  if(s < 86400) return Math.round(s/3600)+' ч назад';
  return Math.round(s/86400)+' дн назад';
}
// R12 §14: price snapshot TTL badge
export function priceFreshness(ts, ttlS=7200){
  // R12-B3: no timestamp -> unknown (mut), old -> stale (warn), fresh -> ago.
  // TTL 2h: snapshots age naturally between manual refreshes; the banner in
  // Models shows per-provider freshness so the admin knows what to refresh.
  if(!ts) return '<span class="badge mut" title="нет данных о времени обновления цены">'+esc(T('priceUnknown'))+'</span>';
  const a = ago(ts);
  if(Date.now()/1000 - ts > ttlS) return '<span class="badge warn" title="'+esc(T('updated')+' '+a)+'">'+esc(T('priceStale'))+'</span>';
  return '<span class="muted small" title="'+esc(T('asOf')+' '+dtHMS(ts))+'">'+esc(a||'')+'</span>';
}

// ── Human statuses ──────────────────────────────────────────────────────
export const LC_RU = {CORE:T('CORE'),WATCH:T('WATCH'),SPECIALIST:T('SPECIALIST'),
  FALLBACK_ONLY:T('FALLBACK_ONLY'),UNAVAILABLE:T('UNAVAILABLE'),DISABLED:T('DISABLED'),
  DEPRECATED:T('DEPRECATED'),SUNSET:T('SUNSET'),DOMINATED:T('DOMINATED')};
export const QS_RU = {VERIFIED:T('VERIFIED'),PROVISIONAL:T('PROVISIONAL'),
  UNKNOWN_FRONTIER:T('UNKNOWN_FRONTIER'),INCOMPLETE:T('INCOMPLETE')};
export function lcBadge(lc){
  const kind = lc==='CORE'?'ok':(lc==='UNAVAILABLE'||lc==='DISABLED')?'bad':'warn';
  return badge(LC_RU[lc]||lc, kind, lc);
}
export function qsBadge(qs){
  const kind = qs==='VERIFIED'?'ok':qs==='PROVISIONAL'?'acc':qs==='INCOMPLETE'?'warn':'mut';
  return badge(QS_RU[qs]||qs, kind, qs);
}
export function dispName(m){
  let n = m.display_name || m.canonical;
  n = String(n).replace(/-/g,' ').replace(/\b\w/g, c=>c.toUpperCase());
  n = n.replace(/\b(Gpt|Glm|Kimi|Qwen|Groq)\b/g, w=>w.toUpperCase());
  return n;
}
// R12 §5: value rating badge (deterministic, explanation in tooltip)
export function valueBadge(v){
  if(!v) return '<span class="muted">—</span>';
  const map = {excellent:['valueExcellent','ok'],good:['valueGood','acc'],
               average:['valueAverage','warn'],expensive:['valueExpensive','bad']};
  const [k, kind] = map[v.rating] || ['valueUnknown','mut'];
  return badge(T(k), kind, (v.why||'') + (UIS.diag() && v.rating!=='unknown' ? ' [score: '+(v.discount_pct)+'%]' : ''));
}
// R12 §17: ACTUAL / ESTIMATED badge
export function costStateBadge(state){
  if(state === 'ACTUAL') return badge(T('actualCost'), 'ok');
  if(state === 'ESTIMATED' || state === 'ESTIMATED_UPPER_BOUND') return badge(T('estimatedCost'), 'warn');
  return badge(T('unknown'), 'mut');
}
// R12 §3: offers / sellers formatting — never invent numbers
export function offersCell(g){
  if(!g) return '<span class="muted">—</span>';
  const parts = [];
  if(g.sellers_count) parts.push(g.sellers_count+' '+esc('продавцов'));
  if(g.offers_count) parts.push(g.offers_count+' '+esc('ценовых предложений'));
  if(!parts.length) return g.market_active ? '<span class="muted small">есть предложения</span>' : '<span class="muted">—</span>';
  return parts.join(' + ');
}

// ── Modal + Drawer — R12 §9 unified behavior ────────────────────────────
// Rules: click outside closes; Escape closes; X closes; click inside does
// NOT close; opening a new drawer replaces the old one; browser Back closes
// the drawer (popstate); the URL hash keeps the selected entity so refresh
// can restore it.
export function openModal(html){
  $('modal').innerHTML = html;
  $('modal-bg').classList.add('open');
}
export function closeModal(){ $('modal-bg').classList.remove('open'); }
export function confirmModal(title, text, onYes, yesLabel){
  openModal('<h3>'+esc(title)+'</h3><p>'+esc(text)+'</p>'+
    '<div class="row" style="justify-content:flex-end;margin-top:14px">'+
    '<button class="ghost" data-action="closeModal">'+esc(T('cancel'))+'</button>'+
    '<button id="cf-yes">'+esc(yesLabel||T('confirm'))+'</button></div>');
  $('cf-yes').onclick = async ()=>{ closeModal(); await onYes(); };
}

export function openDrawer(html, entity){
  const d = $('drawer');
  d.innerHTML = html;
  d.classList.add('open');
  $('drawer-bg').classList.add('open');
  if(entity){
    // hash: #Models?model=<canonical> — page navigation + drawer state
    const [pg] = (location.hash.replace(/^#/,'')||'Dashboard').split('?');
    const newHash = '#'+encodeURIComponent(pg)+'?'+entity;
    if(location.hash !== newHash){
      history.pushState({drawer:entity}, '', newHash);
    }
  }
}
export function closeDrawer(fromPop){
  const d = $('drawer');
  if(!d.classList.contains('open')) return;
  d.classList.remove('open');
  $('drawer-bg').classList.remove('open');
  if(!fromPop && history.state && history.state.drawer){
    history.back();  // pops to the state without the drawer
  }
}
export function drawerState(){
  // restore target: parse ?model=... etc. from the hash
  const h = location.hash.replace(/^#/,'');
  const q = h.split('?')[1] || '';
  const params = new URLSearchParams(q);
  const out = {};
  for(const [k,v] of params) out[k] = v;
  return out;
}

// ── Action delegation ────────────────────────────────────────────────────
export function handleAction(el){
  const action = el.dataset.action;
  if(action==='closeModal'){ closeModal(); return; }
  if(action==='closeDrawer'){ closeDrawer(); return; }
  if(action==='render'){ render(); return; }
  if(action==='page'){ location.hash = el.dataset.page; return; }
  const f = window.MR_ACTIONS && window.MR_ACTIONS[action];
  if(f) f(el);
}

export function installDelegates(){
  document.addEventListener('click', e=>{
    const el = e.target.closest('[data-action]');
    if(!el) return;
    if(el.id === 'modal-bg') return;
    e.preventDefault();
    handleAction(el);
  });
  document.addEventListener('change', e=>{
    const el = e.target.closest('[data-action]');
    if(!el || el.id === 'modal-bg') return;
    handleAction(el);
  });
  // backdrop close: target must be the backdrop itself, never its children
  $('modal-bg').addEventListener('click', e=>{
    if(e.target === $('modal-bg')) closeModal();
  });
  // drawer backdrop element (#drawer-bg) — click outside drawer closes it
  $('drawer-bg').addEventListener('click', e=>{
    if(e.target === $('drawer-bg')) closeDrawer();
  });
  // R14 §2: Escape closes the TOP layer only — modal (z 200) first, then
  // drawer (z 110). A confirmation over a drawer must not close both.
  document.addEventListener('keydown', e=>{
    if(e.key === 'Escape'){
      if($('modal-bg').classList.contains('open')) closeModal();
      else if($('drawer').classList.contains('open')) closeDrawer();
    }
  });
  // browser Back closes the drawer (hash popstate)
  window.addEventListener('popstate', ()=>{
    if(!(history.state && history.state.drawer)){
      $('drawer').classList.remove('open');
      $('drawer-bg').classList.remove('open');
    }
  });
}

// ── Navigation ───────────────────────────────────────────────────────────
let page = null;
export function curPage(){
  const h = decodeURIComponent(location.hash.replace(/^#/,'').split('?')[0]);
  return PAGES.includes(h) ? h : 'Dashboard';
}
export function setNav(){
  $('nav').innerHTML = PAGES.map(p=>
    '<button class="'+(p===page?'active':'')+'" data-action="page" data-page="'+esc(p)+'">'+esc(T(p))+'</button>').join('');
}
let _timer = null;
export async function render(){
  page = curPage();
  setNav();
  const el = $('main');
  el.innerHTML = '<span class="spin"></span> <span class="muted">'+esc(T('loading'))+'</span>';
  try{
    el.innerHTML = await (RPAGES[page]());
    if(POSTRENDER[page]) POSTRENDER[page]();
  }catch(e){
    el.innerHTML = '<div class="errbox">'+esc(T('error'))+': '+esc(String(e))+'</div>';
  }
  if(_timer) clearInterval(_timer);
  const iv = UIS.getNum('refreshInterval', 0);
  if(UIS.get('autoRefresh', false) && iv >= 5){
    _timer = setInterval(()=>{ if(!document.querySelector('#modal-bg.open') && !document.querySelector('#drawer.open')) render(); }, iv*1000);
  }
}
export function scheduleRefresh(){
  if(_timer) clearInterval(_timer);
  const iv = UIS.getNum('refreshInterval', 0);
  if(UIS.get('autoRefresh', false) && iv >= 5){
    _timer = setInterval(()=>{ if(!document.querySelector('#modal-bg.open') && !document.querySelector('#drawer.open')) render(); }, iv*1000);
  }
}

export function applyCompact(){
  document.body.classList.toggle('compact', UIS.get('compact', false));
}

ACTIONS.modalClose = ()=>closeModal();
