// Pages: Models (R12 §2/§6/§7/§8/§12/§13/§15/§20/§22/§24/§28) — основная таблица,
// drawer с табами, "Почему?", wizard добавления, человеческое удаление,
// unmatched v2, калькулятор стоимости.
import { T } from './i18n.js';
import {
  RPAGES, POSTRENDER, ACTIONS, api, apiErr, toast, badge, usdFmt, ms, ago, dt, dtHMS,
  UIS, esc, $, render, openDrawer, closeDrawer, openModal, closeModal, confirmModal,
  drawerState, price1m, priceFreshness, valueBadge, costStateBadge, offersCell, dispName,
} from './core.js';

// ── state ────────────────────────────────────────────────────────────────
let INV = null;               // cached /admin/inventory
let MFL = { q:'', f:'fPool' };  // models filter
let UFL = { q:'', family:'', provider:'' };  // unmatched filter
let CALC = { open:false, canonical:'', in:100000, out:5000, cr:0, cw:0 };
let WIZ = null;               // add-model wizard state

async function loadInv(force){
  if(INV && !force) return INV;
  const r = await api('/admin/inventory');
  if(!r.ok) throw new Error('inventory HTTP '+r.status);
  INV = r.data;
  return INV;
}

// ── helpers ──────────────────────────────────────────────────────────────
const filterChips = () => [['fPool','fPool'],['fAll','fAll'],['fAvailable','fAvailable'],
  ['fUnavailable','fUnavailable'],['fHidden','fHidden'],['fMulti','fMulti']]
  .map(([k,lbl])=>'<button class="ghost small'+(MFL.f===k?' active':'')+'" data-action="mfl" data-f="'+k+'">'+esc(T(lbl))+'</button>').join(' ');

function statusBadge(g){
  // R12-B4: availability + last check evidence inline
  const t = g.last_probe_at ? ' (проверено '+ago(g.last_probe_at)+')' : '';
  if(g.hidden) return badge(T('fHidden').toLowerCase(),'mut');
  if(g.market_active && g.last_probe_at && !g.ttft_ms)
    return badge(T('available')+' · '+T('checkedRecently').toLowerCase(), 'ok', t);
  if(g.market_active && g.ttft_ms!=null)
    return badge(T('available')+' · '+((g.ttft_ms/1000).toFixed(1)+' с'), 'ok', t);
  if(g.eligible || g.in_pool) return badge(T('available'),'warn',(g.eligibility_reason||'')+t);
  return badge(T('unavailable'),'mut',(g.eligibility_reason||'')+t);
}
function usedBadge(g){
  return g.eligible ? badge(T('usedByRouter'),'ok') : '<span class="muted">—</span>';
}
function speedCell(g){
  if(g.ttft_ms != null){
    const v = g.ttft_ms;
    return (v < 3000 ? '<span class="ok">Быстро: </span>' : '<span class="warn">Медленно: </span>') + (v/1000).toFixed(1)+' с TTFT';
  }
  if(g.last_probe_at) return '<span class="muted small">'+esc(T('checkedRecently')+' '+ago(g.last_probe_at))+'</span>';
  return '<span class="muted small">'+esc(T('checkedNever'))+'</span>';
}
function discCell(g){
  return g.discount_pct != null ? badge(g.discount_pct+'%', g.discount_pct>=90?'ok':g.discount_pct>=80?'acc':'warn') : '<span class="muted">—</span>';
}
function ctxCell(g){
  return g.context_max != null ? (g.context_max/1000)+'k' : '<span class="muted small">неизвестен</span>';
}

// ── MAIN TABLE (R12 §2) ──────────────────────────────────────────────────
function modelRows(){
  let list = INV.models;
  const q = MFL.q.toLowerCase();
  if(q) list = list.filter(g => (g.display_name+' '+(g.canonical||'')).toLowerCase().includes(q));
  if(MFL.f==='fPool') list = list.filter(g=>g.in_pool && !g.hidden);
  if(MFL.f==='fAvailable') list = list.filter(g=>g.market_active && !g.hidden);
  if(MFL.f==='fUnavailable') list = list.filter(g=>!g.market_active && !g.hidden);
  if(MFL.f==='fHidden') list = list.filter(g=>g.hidden);
  if(MFL.f==='fMulti') list = list.filter(g=>new Set(g.providers).size>1);
  if(MFL.f==='fAll') list = list.filter(g=>!g.hidden);
  return list;
}

function renderTable(list){
  const head = [
    ['Модель','col-model'], ['Используется','col-used'], ['Доступность','col-availability'],
    ['Провайдеры','col-providers'], ['Предложения','col-offers'], ['Контекст','col-context'],
    ['Официальная цена','col-official'], ['Цена сейчас','col-current-price'], ['Скидка','col-discount'],
    ['Мой минимум','col-minimum'], ['Скорость','col-speed'], ['Выгодность','col-value'], ['Действия','col-actions']
  ];
  const rows = list.map(g=>{
    const key = g.key||g.canonical||'';
    const providers = (g.providers||[]);
    const providerText = providers.length > 1 ? providers[0]+' +'+(providers.length-1) : (providers[0]||'—');
    const providerCell = '<span title="'+esc(providers.join(', ')||'Нет данных')+'">'+esc(providerText)+'</span>';
    const price = g.best_input!=null ? usdFmt(g.best_input)+' / '+usdFmt(g.best_output) : '<span class="muted">—</span>';
    const priceTitle = g.last_priced ? 'обновлено '+ago(g.last_priced) : 'цена не проверена';
    const nm = '<a class="model-link" href="#" data-action="openModel" data-k="'+esc(key)+'" title="'+esc(dispName(g))+'"><b>'+esc(dispName(g))+'</b></a>';
    const cells = [
      ['col-model',nm], ['col-used',usedBadge(g)], ['col-availability',statusBadge(g)],
      ['col-providers',providerCell], ['col-offers','<span title="'+esc(offersCell(g).replace(/<[^>]+>/g,''))+'">'+offersCell(g)+'</span>'],
      ['col-context',ctxCell(g)],
      ['col-official',g.official_input!=null?usdFmt(g.official_input)+' / '+usdFmt(g.official_output):'<span class="muted">—</span>'],
      ['col-current-price','<span title="'+esc(priceTitle)+'">'+price+'</span>'],
      ['col-discount',discCell(g)], ['col-minimum',Math.round((g.effective_min_discount??0.8)*100)+'%'],
      ['col-speed',speedCell(g)], ['col-value',valueBadge(g._value)],
      ['col-actions','<button class="small" data-action="openModel" data-k="'+esc(key)+'">Подробнее</button>']
    ];
    return '<tr>'+cells.map(([cls,c])=>'<td class="'+cls+'">'+c+'</td>').join('')+'</tr>';
  });
  const tblHtml = '<div class="tbl-wrap"><table class="tbl-main"><thead><tr>'+head.map(([h,c])=>'<th class="'+c+'">'+h+'</th>').join('')+'</tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
  return tblHtml;
}

RPAGES['Models'] = async () => {
  try{ await loadInv(); }catch(e){ return apiErr({status:0,url:'/admin/inventory'},T('httpErr')); }
  // R12-B3: market freshness banner (per-provider, honest)
  // R13 §5: + auto-refresh schedule (last run / next run / last error)
  const [fr, sch] = await Promise.all([api('/admin/market/freshness'),
                                       api('/admin/refresh/schedule')]);
  let freshHtml = '';
  if(fr.ok){
    const d = fr.data||{};
    const provs = Object.entries(d.providers||{}).map(([p,v])=>
      '<span class="badge '+(v.state==='свежие'?'ok':v.state==='нет данных'?'mut':'warn')+'">'+esc(p)+': '+esc(v.state)+'</span>').join(' ');
    freshHtml = '<p class="muted small">'+esc(T('marketUpdated'))+': <b>'+esc(dtHMS(d.overall_updated_at))+'</b>'
      +' ('+esc(ago(d.overall_updated_at)||'—')+') '+provs+'</p>';
    if(sch.ok){
      const jobs = Object.entries(sch.data.jobs||{}).map(([k,j])=>
        esc(j.label)+': '+(j.last_run?('обновлено '+ago(j.last_run)):'ещё не запускался')
        +(j.next_run?(', след. через '+Math.max(0,Math.round((j.next_run-sch.data.now)/60))+' мин'):'')
        +(j.last_error?(' <span class="warn">ошибка: '+esc(j.last_error)+'</span>'):'')
      ).join(' · ');
      freshHtml += '<p class="muted small">Автообновление — '+jobs+'</p>';
    }
  }
  // R12 §5: value rating computed client-side from inventory (deterministic:
  // discount vs floor + market activity; tooltip = why-string)
  for(const g of INV.models){
    const floor = Math.round((g.effective_min_discount??0.8)*100);
    const d = g.discount_pct;
    if(d==null) g._value = {rating:'unknown', why:'нет данных о рыночной цене'};
    else {
      const rating = d>=95?'excellent':d>=90?'good':d>=80?'average':'expensive';
      const why = 'скидка '+d+'%'+(d>=floor?(' ≥ минимум '+floor+'%'):(' < минимум '+floor+'%'))
        +(g.market_active?'':'; нет активных предложений');
      g._value = {rating, why};
    }
  }
  const list = modelRows();
  const umCount = INV.counts.unmatched||0;
  return freshHtml+
    '<div class="row">'+
    '<button data-action="addModel">'+esc(T('addModel'))+'</button>'+
    '<button data-action="refreshData">'+esc(T('refreshData'))+'</button>'+
    '<button class="ghost" data-action="refreshCatalog">'+esc(T('refreshCatalog'))+'</button>'+
    '<button class="ghost" data-action="refreshMarketPrices">'+esc(T('refreshMarketPrices'))+'</button>'+
    '<button class="ghost" data-action="checkAvailability">'+esc(T('checkAvailability'))+'</button>'+
    '<button class="ghost" data-action="costCalc">'+esc(T('costCalc'))+'</button>'+
    '<button class="ghost" data-action="unmatchedDrawer">'+esc(T('unmatchedTitle'))+' <span class="badge acc">'+umCount+'</span></button>'+
    '</div>'+
    '<div class="row" style="margin-top:8px">'+
    '<input id="mf-q" placeholder="'+esc(T('search'))+'…" value="'+esc(MFL.q)+'" style="width:220px">'+
    filterChips()+
    '</div>'+
    '<p class="muted small">'+list.length+' '+esc(T('models').toLowerCase()||'')+'</p>'+
    renderTable(list);
};

POSTRENDER['Models'] = () => {
  const q = $('mf-q');
  if(q) q.oninput = ()=>{ MFL.q = q.value; };  // applied on Enter/blur
  if(q) q.onkeydown = e=>{ if(e.key==='Enter') render(); };
  // R12 §9: restore drawer from URL hash after refresh
  const st = drawerState();
  if(st.model && !WIZ) openModelDrawer(st.model);
};

// ── MODEL DETAIL DRAWER (R12 §12) with tabs ─────────────────────────────
let MTAB = 'overview';
let OPEN_G = null;   // currently open model group (for lazy tabs)
async function openModelDrawer(key){
  const g = INV.models.find(m=>(m.key||m.canonical)===key)
         || INV.models.find(m=>m.canonical===key);
  if(!g){ toast(T('errNotFound')||'не найдено','bad'); return; }
  OPEN_G = g;
  const canonical = g.canonical||'';
  const inh = canonical ? await api('/admin/policy/inheritance/'+encodeURIComponent(canonical)) : {ok:false};
  const inhD = inh.ok ? inh.data : null;
  const tabs = [['overview','tabOverview'],['providers','tabProviders'],['prices','tabPrices'],
                ['policy','tabPolicy'],['history','tabHistory'],['diag','tabDiag']];
  let body = '';
  if(MTAB==='overview') body = modelTabOverview(g, inhD);
  else if(MTAB==='providers') body = modelTabProviders(g);
  else if(MTAB==='prices') body = modelTabPrices(g);
  else if(MTAB==='policy') body = canonical ? modelTabPolicy(g, inhD)
    : '<div class="empty">Модель не сопоставлена — политика появится после сопоставления с моделью Router.</div>';
  else if(MTAB==='history') body = '<div id="m-history"><span class="spin"></span></div>';
  else body = modelTabDiag(g);
  const head = '<div class="drawer-head"><h3>'+esc(dispName(g))+'</h3>'+
    '<span style="margin-left:auto"></span>'+
    '<button class="ghost small" data-action="whyModel" data-c="'+esc(canonical)+'">'+esc(T('why'))+'</button>'+
    '<button class="ghost small" data-action="closeDrawer">✕</button></div>';
  const badges = '<div class="row">'+badge(T('fPool').toLowerCase(), g.in_pool?'ok':'mut')+' '+
    badge(T('available'), g.market_active?'ok':'bad')+' '+
    badge(g.eligible?('Подходит политике'):('Не подходит политике'), g.eligible?'ok':'warn')+'</div>';
  const tabsHtml = '<div class="tabs">'+tabs.map(([k,l])=>
    '<button class="'+(MTAB===k?'active':'')+'" data-action="mtab" data-t="'+k+'" data-k="'+esc(key)+'">'+esc(T(l))+'</button>').join('')+'</div>';
  openDrawer(head+badges+tabsHtml+'<div>'+body+'</div>', 'model='+encodeURIComponent(key));
  if(MTAB==='history') loadHistory(canonical);
}
function modelTabOverview(g, inhD){
  const cards = [
    [T('bestNow'), (g.best_input!=null?usdFmt(g.best_input)+' / '+usdFmt(g.best_output):'—')],
    [T('discount'), g.discount_pct!=null?g.discount_pct+'%':'—'],
    [T('context'), g.context_max!=null?(g.context_max/1000)+'k':'неизвестен'],
    [T('offers'), offersCell(g)],
    ['TTFT', g.ttft_ms!=null?(g.ttft_ms/1000).toFixed(2)+' с':'нет данных проверки'],
    [T('valueCol'), valueBadge(g._value)],
  ];
  return '<div class="kv-cards">'+cards.map(([t,v])=>'<div class="card"><div class="t">'+esc(t)+'</div><div class="v" style="font-size:15px">'+v+'</div></div>').join('')+'</div>'+
    '<p class="muted small">'+esc(T('asOf')+' '+dtHMS(g.last_priced)+' · '+ago(g.last_priced))+'</p>'+
    lifecycleActions(g);
}
function modelTabProviders(g){
  const rows = (g.routes||[]).map(r=>[
    '<b>'+esc(r.provider)+'</b>'+(UIS.showProviderId()?'<br><code class="muted small">'+esc(r.provider_model_id)+'</code>':''),
    r.official_input!=null?usdFmt(r.official_input)+' / '+usdFmt(r.official_output):'—',
    r.best_input!=null?usdFmt(r.best_input)+' / '+usdFmt(r.best_output):'—',
    r.discount_pct!=null?r.discount_pct+'%':'—',
    r.market_active?badge(T('available'),'ok'):badge(T('unavailable'),'bad'),
    r.sellers!=null?r.sellers+(r.offers_kind==='sellers'?' продавцов':' предложений'):'—',
    r.last_probe ? (r.last_probe.ok ? 'Доступна' : 'Ошибка: '+(r.last_probe.error_code||'проверка не прошла'))+' · '+ago(r.last_probe.checked_at) : T('checkedNever'),
  ]);
  return '<div class="tbl-wrap"><table class="tbl-main"><tr><th>'+T('provCol')+'</th><th>'+T('officialPrice')+'</th><th>'+T('bestNow')+'</th><th>'+T('discount')+'</th><th>'+T('available')+'</th><th>'+T('offers')+'</th><th>'+T('checkedRecently')+'</th></tr>'
    + rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</table></div>';
}
function modelTabPrices(g){
  const floor = g.effective_min_discount??0.8;
  const maxIn = g.official_input!=null ? g.official_input*(1-floor) : null;
  const maxOut = g.official_output!=null ? g.official_output*(1-floor) : null;
  const rows = [
    [T('officialPrice'), g.official_input!=null?usdFmt(g.official_input)+' / '+usdFmt(g.official_output):'—', costStateBadge('ESTIMATED')],
    [T('bestNow'), g.best_input!=null?usdFmt(g.best_input)+' / '+usdFmt(g.best_output):'—', costStateBadge('ESTIMATED')],
    [T('maxPrice'), maxIn!=null?usdFmt(maxIn)+' / '+usdFmt(maxOut):'—', '<span class="muted small">'+esc('при скидке '+Math.round(floor*100)+'%')+'</span>'],
    [T('actualCost')+' (последний период)', (g._actual!=null?usdFmt(g._actual):'<span class="muted">нет данных биллинга</span>'), g._actual!=null?costStateBadge('ACTUAL'):''],
  ];
  return '<dl class="kv">'+rows.map(([k,v,b])=>'<dt>'+esc(k)+'</dt><dd>'+v+' '+b+'</dd>').join('')+'</dl>'+
    '<p class="muted small">'+esc('Вход / Выход, $ за 1M токенов. ')+priceFreshness(g.last_priced)+'</p>';
}
function modelTabPolicy(g, inhD){
  let html = '<h4>'+esc(T('minDiscount'))+'</h4>';
  if(inhD){
    const md = inhD.min_discount;
    html += '<div class="row">'+
      '<label class="fld chk"><input type="radio" name="md-mode" data-action="mdMode" data-mode="inherit"'+(md.source==='inherit'?' checked':'')+'> <span>'+esc(T('useGlobal')+': '+Math.round(inhD.global_min_discount*100)+'%')+'</span></label>'+
      '<label class="fld chk"><input type="radio" name="md-mode" data-action="mdMode" data-mode="own"'+(md.source==='override'?' checked':'')+'> <span>'+esc(T('useOwn'))+': <input id="md-own" type="number" min="50" max="99" value="'+Math.round(md.value*100)+'" style="width:70px"> %</span></label>'+
      (md.overridden?'<button class="ghost small" data-action="mdReset" data-c="'+esc(g.canonical)+'">'+esc(T('resetToGlobal'))+'</button>':'')+
      '</div>';
    html += '<div id="md-live" class="kv-cards" style="margin-top:8px"></div>'+
      '<div class="row" style="margin-top:8px"><button id="md-save" data-c="'+esc(g.canonical)+'">'+esc(T('save'))+'</button></div>';
    // R12 §22: live preview — рынок сейчас vs выбранный минимум
    const upd = ()=>{
      const own = document.querySelector('input[name=md-mode][data-mode=own]');
      const mode = own && own.checked ? 'own' : 'inherit';
      const v = mode==='own' ? (+($('md-own')?$('md-own').value:md.value*100))/100 : inhD.global_min_discount;
      const floor = Math.round(v*100);
      const disc = g.discount_pct;
      const box = $('md-live');
      if(!box) return;
      box.innerHTML = '<div class="card"><div class="t">'+esc(T('marketNow'))+'</div><div class="v">'+(disc==null?'—':disc+'%')+'</div></div>'+
        '<div class="card"><div class="t">'+esc(T('yourMin'))+'</div><div class="v">'+floor+'%</div></div>'+
        '<div class="card"><div class="t">'+esc(T('verdict'))+'</div><div class="v">'+(disc==null?esc(T('unknown')):disc>=floor?'<span class="ok">✓ '+esc(T('fits'))+'</span>':'<span class="bad">✗ '+esc(T('notUsedNow'))+'</span>')+'</div></div>';
    };
    setTimeout(upd, 0);
    document.querySelectorAll('input[name=md-mode]').forEach(r=>r.addEventListener('change', upd));
    const oi = $('md-own');
    if(oi) oi.addEventListener('input', upd);
  }
  return html;
}
function modelTabDiag(g){
  return '<details class="raw" open><summary>'+esc(T('forDeveloper'))+'</summary><pre>'+esc(JSON.stringify(g,null,2))+'</pre></details>';
}
function lifecycleActions(g){
  // R12 §8: человеческое удаление — отдельные обратимые действия
  const c = esc(g.canonical||'');
  const btn = (act,lbl,cls)=>'<button class="'+(cls||'ghost')+' small" data-action="mlc" data-c="'+c+'" data-act="'+act+'">'+esc(T(lbl))+'</button>';
  let out = '<div class="row" style="margin-top:12px">';
  if(g.in_pool) out += btn('remove_auto','removeFromPool');
  else out += btn('restore','restoreModel','');
  if(g.hidden) out += btn('unhide','unhideModel');
  else out += btn('hide','hideModel');
  out += btn('disable','disableModel','warn') + btn('unmap','unmapModel','danger');
  out += '</div><p class="muted small">'+esc(T('cannotDeleteProviderModel'))+'</p>';
  return out;
}

async function loadHistory(canonical){
  const el = $('m-history');
  if(!el) return;
  el.innerHTML = '<span class="spin"></span>';
  // R12-B1: unmatched model (canonical='') — history by provider route, or
  // an honest empty state. NEVER a request to /admin/price-history/ (404).
  let r;
  if(canonical){
    r = await api('/admin/price-history/'+encodeURIComponent(canonical));
  } else {
    const g = OPEN_G;
    const route = g && (g.routes||[])[0];
    if(!route){
      el.innerHTML = '<div class="empty">'+esc('Модель не сопоставлена и не имеет маршрутов провайдера — история цен недоступна.')+'</div>';
      return;
    }
    r = await api('/admin/price-history/route?provider='+encodeURIComponent(route.provider)+'&pid='+encodeURIComponent(route.provider_model_id));
  }
  if(!r.ok){ el.innerHTML = apiErr(r,T('httpErr')); return; }
  const h = r.data;
  if(!h.points){
    el.innerHTML = '<div class="empty">'+esc(h.empty_reason||'История цен ещё не накапливалась. Нажмите «Обновить данные» — история растёт с каждым обновлением.')+'</div>';
    return;
  }
  const win = (name, lbl) => {
    const w = h[name]||{};
    const f = (x)=> x? 'min '+usdFmt(x.min)+' · avg '+usdFmt(x.avg)+' · max '+usdFmt(x.max) : '—';
    return '<tr><td>'+esc(lbl)+'</td><td>'+(w.best_input?f(w.best_input):'—')+'</td><td>'+(w.discount_pct?Math.round(w.discount_pct.min)+'–'+Math.round(w.discount_pct.max)+'%':'—')+'</td></tr>';
  };
  el.innerHTML = '<p class="muted small">'+esc('Точек истории: '+h.points+'. История накапливается с R12 при каждом обновлении каталогов.')+'</p>'+
    '<div class="tbl-wrap"><table class="tbl-main"><tr><th>Период</th><th>Цена вход (min/avg/max)</th><th>Скидка</th></tr>'+
    win('now','Сейчас')+win('h1','1 час')+win('d1','24 часа')+win('d7','7 дней')+win('d30','30 дней')+'</table></div>';
}

// ── "ПОЧЕМУ?" (R12 §6) ──────────────────────────────────────────────────
ACTIONS.whyModel = async (el)=>{
  const c = el.dataset.c;
  if(!c){
    // unmatched provider-discovered model — honest local explanation
    const g = OPEN_G||{};
    openDrawer('<div class="drawer-head"><h3>'+esc(T('whyNotUsedTitle'))+'</h3>'+
      '<span style="margin-left:auto"></span><button class="ghost small" data-action="closeDrawer">✕</button></div>'+
      '<h4>'+esc(dispName(g))+'</h4><ul class="why-list">'+
      '<li><span class="bad">✗</span> модель не сопоставлена с моделью Router (canonical)</li>'+
      (g.market_active?'<li><span class="ok">✓</span> есть предложения на рынке</li>':'<li><span class="bad">✗</span> нет активных предложений</li>')+
      '</ul><p><b>'+esc(T('verdict'))+':</b> '+esc(T('notInAutoRouting'))+'</p>'+
      '<p class="muted small">Сопоставьте модель во вкладке «Новые и несопоставленные», чтобы Router мог её использовать.</p>',
      'why=unmatched');
    return;
  }
  const r = await api('/admin/insights/why/'+encodeURIComponent(c));
  if(!r.ok){ toast(T('httpErr')+' '+r.status,'bad'); return; }
  const d = r.data;
  const items = d.checks.map(ch=>'<li>'+(ch.ok?'<span class="ok">✓</span>':'<span class="bad">✗</span>')+' '+esc(ch.text)+'</li>').join('');
  let html = '<div class="drawer-head"><h3>'+esc(d.used?T('whyUsedTitle'):T('whyNotUsedTitle'))+'</h3>'+
    '<span style="margin-left:auto"></span><button class="ghost small" data-action="closeDrawer">✕</button></div>'+
    '<h4>'+esc(dispName(d))+'</h4><ul class="why-list">'+items+'</ul>';
  if(d.best_route){
    html += '<p><b>'+esc(T('bestRoute'))+':</b> '+esc(d.best_route.provider)+' / <code>'+esc(d.best_route.provider_model_id)+'</code><br>'+
      '<b>'+esc(T('bestNow'))+':</b> '+usdFmt(d.best_route.best_input)+' / '+usdFmt(d.best_route.best_output)+'</p>';
  }
  html += '<p><b>'+esc(T('verdict'))+':</b> '+esc(d.used?T('inAutoRouting'):T('notInAutoRouting'))+'</p>';
  if(d.value) html += '<p><b>'+esc(T('valueCol'))+':</b> '+valueBadge(d.value)+' <span class="muted small">'+esc(d.value.why)+'</span></p>';
  if(UIS.diag()) html += '<details class="raw" open><summary>'+esc(T('forDeveloper'))+'</summary><pre>'+esc(JSON.stringify(d,null,2))+'</pre></details>';
  openDrawer(html, 'why='+encodeURIComponent(c));
};

// ── lifecycle actions (R12 §8) ──────────────────────────────────────────
ACTIONS.mlc = (el)=>{
  const c = el.dataset.c, act = el.dataset.act;
  const labels = {remove_auto:T('removeFromPool'), hide:T('hideModel'), disable:T('disableModel'),
                  unmap:T('unmapModel'), restore:T('restoreModel'), unhide:T('unhideModel'), enable:T('enableModel')};
  const hints = {remove_auto:T('removeFromPoolHint'), hide:T('hideHint'), disable:T('disableHint'), unmap:T('unmapHint')};
  confirmModal(labels[act]||act, hints[act]||'', async ()=>{
    const r = await api('/admin/model-lifecycle/'+encodeURIComponent(c), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:act})});
    if(r.ok){ toast(T('saved'),'ok'); await loadInv(true); render(); }
    else toast(T('httpErr')+' '+r.status,'bad');
  });
};

// ── tabs ─────────────────────────────────────────────────────────────────
ACTIONS.mtab = (el)=>{ MTAB = el.dataset.t; openModelDrawer(el.dataset.k || el.dataset.c); };
ACTIONS.openModel = (el)=>{ openModelDrawer(el.dataset.k || el.dataset.c); };

// ── filters ──────────────────────────────────────────────────────────────
ACTIONS.mfl = (el)=>{ MFL.f = el.dataset.f; render(); };

// ── discovery refresh ────────────────────────────────────────────────────
ACTIONS.refreshDiscovery = async ()=>{ ACTIONS.refreshData(); };
// R12-B3: [Обновить данные] — safe full refresh (catalog + prices + market
// + price-history snapshots). This is THE button an admin needs.
ACTIONS.refreshData = async ()=>{
  toast(T('loading'),'');
  const r = await api('/admin/discovery/refresh', {method:'POST'});
  if(r.ok){
    const pts = r.data.providers||{};
    const ih = pts.provider_a||{}, sp = pts.provider_b||{};
    const line=(p,d)=>{
      if(!d||d.ok===false) return p+' : ошибка — '+((d&&(d.error||d.blocked_by))||'нет данных');
      const parts=[d.total!=null?d.total+' моделей':'—'];
      if(d.new) parts.push('новых '+d.new);
      if(d.gone) parts.push('исчезло '+d.gone);
      parts.push('ошибок 0');
      return p+' : '+parts.join(' · ');
    };
    toast(line('Provider A', ih)+' | '+line('Provider B', sp)+(r.data.price_history_points!=null?(' · история: +'+r.data.price_history_points):''), r.data.ok?'ok':'warn');
    INV=null; render();
  } else toast(T('httpErr')+' '+r.status,'bad');
};
// [Обновить каталог] — runtime registry re-discovery (runtime routes view)
ACTIONS.refreshCatalog = async ()=>{
  toast(T('loading'),'');
  const r = await api('/admin/catalog/refresh', {method:'POST'});
  if(r.ok){ toast(T('saved'),'ok'); INV=null; render(); }
  else toast(T('httpErr')+' '+r.status,'bad');
};
// [Обновить цены рынка] — discovery refresh is the market/pricing refresh
ACTIONS.refreshMarketPrices = async ()=>{ ACTIONS.refreshData(); };
// R12-B4: [Проверить доступность] — pool models ONLY (never hundreds of
// unmatched), catalog probes by default, with visible progress.
ACTIONS.checkAvailability = async ()=>{
  const pool = INV.models.filter(g=>g.in_pool && !g.hidden);
  if(!pool.length){ toast(T('empty'),'warn'); return; }
  let done = 0;
  const prog = document.createElement('div');
  prog.className='toast';
  prog.textContent = '0 / '+pool.length;
  $('toast').appendChild(prog);
  let okCnt=0, errCnt=0, errModels=[];
  for(const g of pool){
    prog.textContent = done+' / '+pool.length;
    const routes = (g.routes||[]).length ? g.routes : [{provider:(g.providers||[])[0]||'—', provider_model_id:g.canonical||'', last_probe:null}];
    for(const route of routes){
      let r;
      if(route.provider_model_id){
        r = await api('/admin/probe/route', {method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({provider:route.provider, provider_model_id:route.provider_model_id})});
      } else r = {ok:false,status:404,data:{error:'маршрут не найден'}};
      const checked = (r.data&& (r.data.checked_at||r.data.timestamp)) || Date.now()/1000;
      if(r.ok && r.data && r.data.ok!==false){ okCnt++; }
      else { errCnt++; errModels.push({model:g.display_name||g.canonical||'—', provider:route.provider||'—', route:route.provider_model_id||g.canonical||'—', reason:(r.data&&(r.data.error||r.data.error_code||r.data.detail))||'проверка недоступна', timestamp:checked}); }
    }
    done++;
  }
  prog.innerHTML = '<b>'+okCnt+' доступны · '+errCnt+' ошибка/ошибок</b>' +
    (errModels.length ? '<details open><summary>Показать ошибки</summary><table class="tbl"><tr><th>Модель</th><th>Провайдер</th><th>Маршрут</th><th>Причина</th><th>Время</th></tr>'+
      errModels.map(e=>'<tr><td>'+esc(e.model)+'</td><td>'+esc(e.provider)+'</td><td><code>'+esc(e.route)+'</code></td><td>'+esc(e.reason)+'</td><td>'+esc(dtHMS(e.timestamp))+'</td></tr>').join('')+'</table></details>' : '') + ' — '+T('saved');
  setTimeout(()=>prog.remove(), 6000);
  INV=null; render();
};

// ── ADD-MODEL WIZARD (R12 §7) ───────────────────────────────────────────
ACTIONS.addModel = ()=>{
  WIZ = {step:1, q:'', results:[], sel:null, canonical:'', name:'', providers:[],
         minDiscountMode:'inherit', minDiscount:80, lifecycle:'WATCH'};
  renderWizard();
};
function wizHead(){
  const steps = [T('wizFindModel'),T('wizChooseVariant'),T('wizAddToRouter'),T('wizVerify'),T('wizDone')];
  return '<div class="drawer-head"><h3>'+esc(T('addModel'))+'</h3><span style="margin-left:auto"></span>'+
    '<button class="ghost small" data-action="closeDrawer">✕</button></div>'+
    '<div class="steps">'+steps.map((s,i)=>'<span class="step'+(WIZ.step===i+1?' active':(WIZ.step>i+1?' done':''))+'">'+(i+1)+'. '+esc(s)+'</span>').join('')+'</div>';
}
function renderWizard(){
  let body = '';
  if(WIZ.step===1){
    body = '<div class="row"><input id="wz-q" placeholder="'+esc(T('wizSearchPlaceholder'))+'" value="'+esc(WIZ.q)+'" style="flex:1">'+
      '<button id="wz-search">'+esc(T('search'))+'</button></div><div id="wz-res" style="margin-top:10px"></div>';
    if(WIZ.results.length) body += wizResultsHtml();
    else if(WIZ.searched) body += '<p class="muted">'+esc(T('wizNoResults'))+'</p>';
  } else if(WIZ.step===2){
    const s = WIZ.sel;
    body = '<h4>'+esc(s.display_name)+'</h4>'+
      (s.variants||[]).map((v,i)=>'<div class="card" style="margin:6px 0">'+
        '<div class="row"><b>'+esc(v.provider)+'</b>'+(v.unmatched?' <span class="badge warn">новая</span>':'')+'</div>'+
        '<div class="muted small mono" style="word-break:break-word">'+esc(v.provider_model_id)+'</div>'+
        '<div class="small">'+(v.best_input!=null?T('bestNow')+': '+usdFmt(v.best_input)+' / '+usdFmt(v.best_output):T('priceUnknown'))+
        (v.discount_pct!=null?' · '+T('discount')+': '+v.discount_pct+'%':'')+
        (v.context!=null?' · '+T('context')+': '+(v.context/1000)+'k':'')+'</div>'+
        '<div style="margin-top:6px"><button class="small" data-action="wzPick" data-i="'+i+'">'+esc(T('next'))+' →</button></div></div>').join('');
    body += '<div style="margin-top:10px"><button class="ghost" data-action="wzBack">← '+esc(T('back'))+'</button></div>';
  } else if(WIZ.step===3){
    const s = WIZ.sel;
    body = '<label class="fld"><span>'+esc(T('wizName'))+'</span><input id="wz-name" value="'+esc(WIZ.name)+'"></label>'+
      '<label class="fld"><span>'+esc(T('wizCanonical'))+'</span><input id="wz-canon" value="'+esc(WIZ.canonical)+'" class="mono"></label>'+
      '<label class="fld"><span>'+esc(T('wizProviders'))+'</span><div id="wz-provs">'+
      (s.variants||[]).map((v,i)=>'<label class="chk"><input type="checkbox" data-action="wzProv" data-i="'+i+'"'+(WIZ.providers.includes(i)?' checked':'')+'> '+esc(v.provider+' · '+v.provider_model_id)+'</label>').join('')+'</div></label>'+
      '<label class="fld"><span>'+esc(T('minDiscount'))+'</span><div class="row">'+
      '<label class="chk"><input type="radio" name="wz-md" data-action="wzMd" data-m="inherit"'+(WIZ.minDiscountMode==='inherit'?' checked':'')+'> '+esc(T('wizInheritMin'))+'</label>'+
      '<label class="chk"><input type="radio" name="wz-md" data-action="wzMd" data-m="own"'+(WIZ.minDiscountMode==='own'?' checked':'')+'> <input id="wz-mdv" type="number" min="50" max="99" value="'+WIZ.minDiscount+'" style="width:70px"> %</label>'+
      '</div></label>'+
      '<label class="fld"><span>'+esc(T('wizLifecycle'))+'</span><select id="wz-lc">'+
      ['WATCH','CORE','SPECIALIST','FALLBACK_ONLY'].map(l=>'<option value="'+l+'"'+(WIZ.lifecycle===l?' selected':'')+'>'+esc(T(l))+'</option>').join('')+'</select></label>'+
      '<div class="row" style="margin-top:12px"><button class="ghost" data-action="wzBack">← '+esc(T('back'))+'</button>'+
      '<button data-action="wzVerify">'+esc(T('wizVerify'))+' →</button></div>';
  } else if(WIZ.step===4){
    body = '<p><span class="spin"></span> '+esc(T('wizVerifying'))+'</p><div id="wz-verres"></div>';
  } else {
    body = '<p class="ok">'+esc(T('wizDone'))+'</p><pre class="mono">'+esc(JSON.stringify(WIZ.result||{},null,2))+'</pre>'+
      '<div class="row"><button data-action="wzClose">'+esc(T('close'))+'</button></div>';
  }
  openDrawer(wizHead()+body, 'addmodel=1');
  if(WIZ.step===1){
    const q = $('wz-q');
    q.onkeydown = e=>{ if(e.key==='Enter') ACTIONS.wzSearch(); };
    $('wz-search').onclick = ()=>ACTIONS.wzSearch();
  }
  if(WIZ.step===4) wizRunVerify();
}
function wizResultsHtml(){
  return (WIZ.results||[]).map((r,i)=>'<div class="card" style="margin:6px 0">'+
    '<div class="row"><b>'+esc(r.display_name)+'</b>'+(r.canonical?'':' <span class="badge warn">новая</span>')+
    (r.in_pool?' <span class="badge ok">в моём пуле</span>':'')+'</div>'+
    '<div class="muted small">'+esc((r.providers||[]).join(' + '))+(r.discount_pct!=null?' · '+T('discount')+': '+r.discount_pct+'%':'')+'</div>'+
    '<div style="margin-top:6px"><button class="small" data-action="wzSel" data-i="'+i+'">'+esc(T('next'))+' →</button></div></div>').join('');
}
ACTIONS.wzSearch = async ()=>{
  WIZ.q = $('wz-q') ? $('wz-q').value : WIZ.q;
  if(!WIZ.q || WIZ.q.length<2) return;
  const r = await api('/admin/search/catalog?q='+encodeURIComponent(WIZ.q));
  if(!r.ok){ toast(T('httpErr')+' '+r.status,'bad'); return; }
  WIZ.results = r.data.results||[]; WIZ.searched = true;
  renderWizard();
};
ACTIONS.wzSel = (el)=>{ WIZ.sel = WIZ.results[+el.dataset.i]; WIZ.step=2;
  WIZ.canonical = WIZ.sel.canonical || WIZ.sel.display_name.toLowerCase().replace(/[^a-z0-9.]+/g,'-');
  WIZ.name = WIZ.sel.display_name;
  renderWizard(); };
ACTIONS.wzPick = (el)=>{ WIZ.step=3; WIZ.providers=[+el.dataset.i]; renderWizard(); };
ACTIONS.wzBack = ()=>{ WIZ.step--; renderWizard(); };
ACTIONS.wzProv = (el)=>{ const i=+el.dataset.i; if(WIZ.providers.includes(i)) WIZ.providers=WIZ.providers.filter(x=>x!==i); else WIZ.providers.push(i); };
ACTIONS.wzMd = (el)=>{ WIZ.minDiscountMode = el.dataset.m; };
async function wizRunVerify(){
  WIZ.name = $('wz-name')?$('wz-name').value:WIZ.name;
  WIZ.canonical = $('wz-canon')?$('wz-canon').value:WIZ.canonical;
  const out = {checks:[], ok:true};
  // 1) map each chosen variant
  const s = WIZ.sel;
  for(const i of WIZ.providers){
    const v = s.variants[i];
    const r = await api('/admin/canonical-map', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({provider:v.provider, provider_model_id:v.provider_model_id, canonical:WIZ.canonical, source:'manual'})});
    out.checks.push({ok:r.ok, text:'Сопоставление '+v.provider+':'+v.provider_model_id+' → '+WIZ.canonical+(r.ok?'':' (HTTP '+r.status+')')});
    if(!r.ok) out.ok=false;
  }
  // 2) pool policy
  const pol = {in_pool:true, hidden:false, auto_routing:true, lifecycle_override:WIZ.lifecycle};
  if(WIZ.minDiscountMode==='own') pol.min_discount_override = (+($('wz-mdv')?$('wz-mdv').value:WIZ.minDiscount))/100;
  const r2 = await api('/admin/model-pool/'+encodeURIComponent(WIZ.canonical), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(pol)});
  out.checks.push({ok:r2.ok, text:'Политика модели применена'+(r2.ok?'':' (HTTP '+r2.status+')')});
  if(!r2.ok) out.ok=false;
  // 3) verify inventory sees it
  INV = null; await loadInv(true);
  const g = INV.models.find(m=>m.canonical===WIZ.canonical);
  out.checks.push({ok:!!g, text: g?('Модель видна в списке ('+(g.routes||[]).length+' маршрутов)'):'Модель не найдена в инвентаре'});
  if(!g) out.ok=false;
  WIZ.step = 5; WIZ.result = out;
  renderWizard();
}
ACTIONS.wzVerify = ()=>{ WIZ.step=4; renderWizard(); };
ACTIONS.wzClose = ()=>{ closeDrawer(); render(); };

// ── UNMATCHED DRAWER v2 (R12 §20) ───────────────────────────────────────
ACTIONS.unmatchedDrawer = async ()=>{
  const r = await api('/admin/unmatched?q='+encodeURIComponent(UFL.q)+'&family='+encodeURIComponent(UFL.family)+'&provider='+encodeURIComponent(UFL.provider));
  if(!r.ok){ toast(T('httpErr')+' '+r.status,'bad'); return; }
  const d = r.data;
  const famBtns = Object.entries(d.family_counts||{}).map(([f,n])=>
    '<button class="ghost small'+(UFL.family===f?' active':'')+'" data-action="uflFam" data-f="'+esc(f)+'">'+esc(f)+' — '+n+'</button>').join(' ');
  const items = (d.unmatched||[]).slice(0,300).map(u=>
    '<div class="card" style="margin:6px 0"><div class="row"><b>'+esc(u.display_name||u.provider_model_id)+'</b>'+
    '<span class="badge mut">'+esc(u.provider)+'</span></div>'+
    '<div class="muted small mono" style="word-break:break-word">'+esc(u.provider_model_id)+'</div>'+
    (u.discount_pct!=null?'<div class="small">'+T('discount')+': '+u.discount_pct+'%'+(u.market_active?'':' · '+T('unavailable'))+'</div>':'')+
    '<div style="margin-top:6px"><button class="small" data-action="mapOne" data-p="'+esc(u.provider)+'" data-id="'+esc(u.provider_model_id)+'">'+'Сопоставить с существующей моделью</button> '+
    '<button class="ghost small" data-action="mapNew" data-p="'+esc(u.provider)+'" data-id="'+esc(u.provider_model_id)+'" data-n="'+esc(u.display_name||'')+'">'+esc(T('addAsNew'))+'</button></div></div>').join('');
  openDrawer('<div class="drawer-head"><h3>'+esc(T('unmatchedTitle'))+' <span class="badge acc">'+d.total+'</span></h3>'+
    '<span style="margin-left:auto"></span><button class="ghost small" data-action="closeDrawer">✕</button></div>'+
    '<p class="muted small">'+esc(T('unmatchedHint'))+'</p>'+
    '<div class="row"><input id="um-q" placeholder="'+esc(T('search'))+'…" value="'+esc(UFL.q)+'" style="flex:1"></div>'+
    '<div class="row" style="margin-top:8px"><button class="ghost small'+(UFL.family===null||UFL.family===''?' active':'')+'" data-action="uflFam" data-f="">'+esc(T('fAll'))+'</button>'+famBtns+'</div>'+
    '<p class="muted small">'+d.shown+' / '+d.total+'</p>'+ items,
    'unmatched=1');
  const q = $('um-q');
  q.onkeydown = e=>{ if(e.key==='Enter'){ UFL.q=q.value; ACTIONS.unmatchedDrawer(); } };
};
ACTIONS.uflFam = (el)=>{ UFL.family = el.dataset.f; ACTIONS.unmatchedDrawer(); };
ACTIONS.mapOne = async (el)=>{
  const p=el.dataset.p, pid=el.dataset.id;
  const sr=await api('/admin/matching/suggest?provider='+encodeURIComponent(p)+'&provider_model_id='+encodeURIComponent(pid));
  if(!sr.ok){ toast('Не удалось получить сопоставления','bad'); return; }
  const d=sr.data||{}, exact=d.exact_safe||[], suggestions=d.suggestions||[];
  const matchingExisting = (d.exact_safe||[]).length > 0;
  const card=(x, exactMode)=>'<div class="card match-choice" data-canonical="'+esc(x.canonical||'')+'" style="margin:6px 0;cursor:pointer">'+
    '<div class="row"><b>'+esc(x.display_name||x.canonical)+'</b>'+badge(exactMode?'Высокая уверенность':'Похожая модель',exactMode?'ok':'warn')+'</div>'+
    '<div class="muted small mono">'+esc(x.canonical||'')+'</div><div class="muted small">Провайдеры: '+esc((x.providers||[]).join(', ')||'нет')+
    ' · маршрутов: '+(x.routes||[]).length+' · контекст: '+esc(x.context_max?Math.round(x.context_max/1000)+'k':'неизвестен')+
    '</div></div>';
  openModal('<h3>Сопоставить модель</h3><p class="muted small">'+esc(p)+' / '+esc(pid)+'</p>'+ 
    '<label class="fld"><span>Поиск существующей модели</span><input id="match-q" value="'+esc(d.display_name||pid)+'" placeholder="Название, canonical или provider ID"></label>'+ 
    (exact.length?'<h4>Точное безопасное сопоставление</h4>'+exact.map(x=>card(x,true)).join(''):'<div class="empty">Точного сопоставления нет</div>')+
    (suggestions.length?'<h4>Похожие модели — только предложение</h4>'+suggestions.map(x=>card(x,false)).join(''):'')+
    '<p class="muted small">После сохранения '+esc(p)+' станет ещё одним маршрутом выбранной существующей модели.</p>'+
    '<div class="row" style="justify-content:flex-end"><button class="ghost" data-action="closeModal">Отмена</button><button class="ghost" id="match-new">Создать новую модель осознанно</button></div>');
  document.querySelectorAll('.match-choice').forEach(x=>x.onclick=async()=>{
    const canonical=x.dataset.canonical;
    const r=await api('/admin/matching/attach',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:p,provider_model_id:pid,canonical})});
    if(r.ok){ closeModal(); toast('Маршрут присоединён к существующей модели','ok'); INV=null; await loadInv(true); ACTIONS.unmatchedDrawer(); }
    else toast('Сопоставление не сохранено','bad');
  });
  $('match-q').onkeydown=async e=>{if(e.key==='Enter'){const q=e.target.value; const r=await api('/admin/matching/suggest?provider='+encodeURIComponent(p)+'&provider_model_id='+encodeURIComponent(pid)+'&q='+encodeURIComponent(q)); if(r.ok){ closeModal(); el.dataset.id=pid; ACTIONS.mapOne(el); }}};
  $('match-new').onclick=()=>{closeModal(); ACTIONS.mapNew(el);};
};
ACTIONS.mapNew = (el)=>{
  const name = el.dataset.n || el.dataset.id;
  const canon = name.toLowerCase().replace(/[^a-z0-9.]+/g,'-');
  openModal('<h3>'+esc(T('addAsNew'))+'</h3>'+
    '<p class="card warn">Создание новой canonical модели — отдельное действие. Перед сохранением проверьте похожие модели.</p>'+
    '<p class="muted small mono" style="word-break:break-word">'+esc(el.dataset.p+':'+el.dataset.id)+'</p>'+
    '<label class="fld"><span>'+esc(T('wizName'))+'</span><input id="mn-n" value="'+esc(name)+'"></label>'+
    '<label class="fld"><span>'+esc(T('wizCanonical'))+'</span><input id="mn-c" value="'+esc(canon)+'" class="mono"></label>'+
    '<div class="row" style="justify-content:flex-end;margin-top:10px">'+
    '<button class="ghost" data-action="closeModal">'+esc(T('cancel'))+'</button>'+
    '<button id="mn-ok">'+esc(T('add'))+'</button></div>');
  $('mn-ok').onclick = async ()=>{
    const c = $('mn-c').value.trim();
    if(!c){ $('mn-c').classList.add('invalid'); return; }
    const r = await api('/admin/canonical-map', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({provider:el.dataset.p, provider_model_id:el.dataset.id, canonical:c, source:'manual'})});
    const r2 = await api('/admin/model-pool/'+encodeURIComponent(c), {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({in_pool:true, hidden:false, auto_routing:true, lifecycle_override:'WATCH'})});
    closeModal();
    if(r.ok && r2.ok){ toast(T('saved'),'ok'); INV=null; await loadInv(true); ACTIONS.unmatchedDrawer(); }
    else toast(T('httpErr'),'bad');
  };
};

// ── COST CALCULATOR (R12 §15) ───────────────────────────────────────────
ACTIONS.costCalc = async ()=>{
  await loadInv();
  const poolModels = INV.models.filter(g=>!g.hidden);
  const presets = [[10000,2000,'10k + 2k'],[100000,5000,'100k + 5k'],[500000,10000,'500k + 10k'],[1000000,20000,'1M + 20k']];
  openDrawer('<div class="drawer-head"><h3>'+esc(T('costCalc'))+'</h3><span style="margin-left:auto"></span>'+
    '<button class="ghost small" data-action="closeDrawer">✕</button></div>'+
    '<label class="fld"><span>'+esc(T('model'))+'</span><select id="cc-m">'+
    poolModels.map(g=>'<option value="'+esc(g.canonical||'')+'"'+(CALC.canonical===g.canonical?' selected':'')+'>'+esc(dispName(g))+'</option>').join('')+'</select></label>'+
    '<div class="row">'+
    '<label class="fld"><span>'+esc(T('inputTokens'))+'</span><input id="cc-in" type="number" value="'+CALC.in+'" style="width:120px"></label>'+
    '<label class="fld"><span>'+esc(T('outputTokens'))+'</span><input id="cc-out" type="number" value="'+CALC.out+'" style="width:120px"></label>'+
    '<label class="fld"><span>'+esc(T('cacheRead'))+'</span><input id="cc-cr" type="number" value="'+CALC.cr+'" style="width:120px"></label>'+
    '</div>'+
    '<div class="row">'+presets.map(([i,o,l])=>'<button class="ghost small" data-action="ccPreset" data-i="'+i+'" data-o="'+o+'">'+esc(l)+'</button>').join('')+'</div>'+
    '<div class="row" style="margin-top:8px"><button id="cc-go">'+esc(T('costCalc'))+'</button></div>'+
    '<div id="cc-res" style="margin-top:12px"></div>',
    'calc=1');
  $('cc-go').onclick = ()=>ACTIONS.ccRun();
};
ACTIONS.ccPreset = (el)=>{ CALC.in=+el.dataset.i; CALC.out=+el.dataset.o; ACTIONS.costCalc(); ACTIONS.ccRun(); };
ACTIONS.ccRun = async ()=>{
  CALC.canonical = $('cc-m') ? $('cc-m').value : CALC.canonical;
  CALC.in = +($('cc-in')?$('cc-in').value:CALC.in);
  CALC.out = +($('cc-out')?$('cc-out').value:CALC.out);
  CALC.cr = +($('cc-cr')?$('cc-cr').value:CALC.cr);
  const r = await api('/admin/market/cost-preview', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({canonical:CALC.canonical, input_tokens:CALC.in, output_tokens:CALC.out, cache_read_tokens:CALC.cr})});
  const el = $('cc-res');
  if(!r.ok){ el.innerHTML = apiErr(r,T('httpErr')); return; }
  const d = r.data;
  const row = (k,v,st)=>'<dt>'+esc(k)+'</dt><dd>'+(v==null?'—':usdFmt(v))+' '+(st||'')+'</dd>';
  el.innerHTML = '<dl class="kv">'+
    row(T('bestMarketNow'), d.best_ask_cost, costStateBadge('ESTIMATED'))+
    row(T('policyMax'), d.policy_worst_cost, '')+
    row(T('officialCost'), d.official_cost, '')+
    (d.savings_usd!=null
      ? row(T('savings')+' против официальной', d.savings_usd, d.savings_pct!=null?('· '+d.savings_pct+'%'):'')
      : '<dt>'+esc(T('savings'))+'</dt><dd class="muted">Экономию рассчитать нельзя — официальная цена неизвестна</dd>')+
    '</dl>'+
    (d.actual_last_cost?('<dl class="kv">'+row(T('lastRequestCost')+' ('+T('actualCost').toLowerCase()+')', d.actual_last_cost.avg_cost_usd, costStateBadge('ACTUAL'))+'</dl>'):'')+
    '<p class="muted small">'+esc(T('asOf')+' '+d.market_as_of)+'</p>'+
    ((d.per_provider||[]).length>1?'<h4>'+esc(T('comparison'))+'</h4><div class="tbl-wrap"><table class="tbl-main"><tr><th>'+T('provCol')+'</th><th>'+T('priceIn')+'</th><th>'+T('priceOut')+'</th><th>$</th></tr>'+
      d.per_provider.map(p=>'<tr><td>'+esc(p.provider)+(p.market_active?'':' <span class="badge bad">'+esc(T('unavailable'))+'</span>')+'</td><td>'+usdFmt(p.input_per_1m)+'</td><td>'+usdFmt(p.output_per_1m)+'</td><td>'+usdFmt(p.cost)+'</td></tr>').join('')+'</table></div>':'');
};

// ── per-model policy save (R12 §22) ─────────────────────────────────────
ACTIONS.mdMode = ()=>{ /* radio state read at save */ };
ACTIONS.mdReset = async (el)=>{
  const c = el.dataset.c;
  const r = await api('/admin/model-pool/'+encodeURIComponent(c), {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({min_discount_override:null})});
  if(r.ok){ toast(T('saved'),'ok'); await loadInv(true); openModelDrawer(OPEN_G?(OPEN_G.key||OPEN_G.canonical):c); }
  else toast(T('httpErr'),'bad');
};
POSTRENDER['Models_policy_save'] = null;
ACTIONS.mdSave = null;
// wired via delegation in POSTRENDER (dynamic element):
document.addEventListener('click', async e=>{
  const el = e.target.closest('#md-save');
  if(!el) return;
  const c = el.dataset.c;
  const own = document.querySelector('input[name="md-mode"][data-mode="own"]');
  const mode = own && own.checked ? 'own' : 'inherit';
  const body = {};
  if(mode==='own'){
    const v = +($('md-own')?$('md-own').value:80);
    if(!(v>=50&&v<=99)){ toast('Скидка 50–99%','bad'); return; }
    body.min_discount_override = v/100;
  } else body.min_discount_override = null;
  const r = await api('/admin/model-pool/'+encodeURIComponent(c), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  if(r.ok){ toast(T('saved'),'ok'); await loadInv(true); openModelDrawer(OPEN_G?(OPEN_G.key||OPEN_G.canonical):c); }
  else toast(T('httpErr')+' '+r.status,'bad');
});
