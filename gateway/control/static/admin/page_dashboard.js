// Pages: Dashboard + Settings + Audit (R12 §16/§18/§19/§30/§31/§25/§26).
import { T, setLang } from './i18n.js';
import {
  RPAGES, POSTRENDER, ACTIONS, api, apiErr, toast, tbl, badge, usdFmt, ms,
  UIS, esc, $, render, applyCompact, scheduleRefresh, dispName, costStateBadge,
  openDrawer, closeDrawer, openModal, closeModal, confirmModal, dtHM,
} from './core.js';

// ══ DASHBOARD (R12 §16/§18/§19/§30/§31) ══════════════════════════════════
RPAGES['Dashboard'] = async () => {
  // R12-B2: each data source loads INDEPENDENTLY. A failed endpoint renders
  // an ERROR state for its own card — it can never zero out the others.
  // States: LOADING -> VALUE | EMPTY(0) | ERROR(endpoint+status).
  const ST = {loading:'<span class="spin"></span>', };
  const errbox = (r) => '<div class="errbox small">'+esc(T('dataError'))+': <code>'+esc(r.url||'')+'</code> HTTP '+esc(r.status)+'</div>';
  const val = (r, field) => r.ok ? (r.data[field] ?? 0) : null;

  const [h, rh, sum, usage, ops, alerts, eco, att, obsR, obsA, obsB] = await Promise.all([
    api('/admin/healthz').catch(e=>({ok:false,status:0,url:'/admin/healthz',data:{},err:String(e)})),
    api('/admin/runtime/health').catch(e=>({ok:false,status:0,url:'/admin/runtime/health',data:{},err:String(e)})),
    api('/admin/dashboard/summary').catch(e=>({ok:false,status:0,url:'/admin/dashboard/summary',data:{},err:String(e)})),
    api('/admin/insights/usage').catch(e=>({ok:false,status:0,url:'/admin/insights/usage',data:{},err:String(e)})),
    api('/admin/insights/opportunities').catch(e=>({ok:false,status:0,url:'/admin/insights/opportunities',data:{},err:String(e)})),
    api('/admin/insights/alerts').catch(e=>({ok:false,status:0,url:'/admin/insights/alerts',data:{},err:String(e)})),
    api('/admin/insights/economics').catch(e=>({ok:false,status:0,url:'/admin/insights/economics',data:{},err:String(e)})),
    api('/admin/attention').catch(e=>({ok:false,status:0,url:'/admin/attention',data:{},err:String(e)})),
    api('/admin/obs/readiness').catch(e=>({ok:false,status:0,url:'/admin/obs/readiness',data:{},err:String(e)})),
    api('/admin/obs/alerts').catch(e=>({ok:false,status:0,url:'/admin/obs/alerts',data:{},err:String(e)})),
    api('/admin/obs/budgets').catch(e=>({ok:false,status:0,url:'/admin/obs/budgets',data:{},err:String(e)})),
  ]);

  const card = (t, v, sub) =>
    '<div class="card"><div class="t">'+t+'</div><div class="v">'+v+'</div><div class="s">'+(sub||'')+'</div></div>';
  const vcell = (r, field, subFn) => r.ok
    ? (r.data[field] ?? 0)
    : '<span class="bad small">'+esc(T('dataError'))+' HTTP '+esc(r.status)+'</span>';

  if(!h.ok) return errbox(h) + '<div class="empty">'+esc('Управление (control plane) недоступно — остальные данные неизвестны.')+'</div>';

  const s = sum.ok ? (sum.data||{}) : {};
  const u = usage.ok ? (usage.data||{}) : {};
  const rhOK = rh.ok && rh.data && rh.data.ok;
  const rd = (rh.data&&rh.data.data)||{};
  const provs = u.providers||{};
  const provNames = Object.keys(provs);
  const online = provNames.filter(p=>provs[p].requests>0||provs[p].successes>0).length;
  const costCard = !usage.ok
    ? card(T('spendTitle'), '<span class="bad small">'+esc(T('dataError'))+'</span>', errbox(usage))
    : u.cost_total_usd!=null
      ? card(T('spendSinceStart'), usdFmt(u.cost_total_usd)+' '+costStateBadge(u.cost_state),
          Math.round((u.uptime_s||0)/3600)+' ч работы · '+T('spendCumulativeNote'))
      : card(T('spendTitle'), '—', 'нет данных');
  const provRows = provNames.map(p=>{
    const v = provs[p];
    const used = (v.requests||0)>0;
    return ['<b>'+esc(p)+'</b>',
      used ? badge(T('usedByRouter'),'ok') : badge(T('notUsed'),'mut'),
      used ? (v.successes||0)+' / '+(v.failures||0) : '—',
      used ? usdFmt(v.cost_usd) : '—',
      used ? ms(v.ttft_p50_ms) : '—',
      used ? '' : '<button class="ghost small" data-action="whyProvider" data-p="'+esc(p)+'">'+esc(T('whyNotUsed'))+'</button>'];
  });
  const opsList = (ops.ok && ops.data && ops.data.opportunities)||[];
  const opsHtml = ops.ok
    ? (opsList.map(o =>
        '<div class="card" style="margin:6px 0"><div class="row"><b>'+esc(dispName(o))+'</b>'+
        badge(o.discount_pct+'%','ok')+'</div>'+
        '<div class="muted small">'+esc((o.providers||[]).join(' + '))+(o.context_max?(' · '+T('context')+': '+(o.context_max/1000)+'k'):'')+
        ' · '+esc('не в моём пуле')+'</div>'+
        '<div style="margin-top:6px"><button class="small" data-action="opAdd" data-c="'+esc(o.canonical)+'">'+esc(T('add'))+'</button></div></div>').join('')
      || '<div class="empty">'+esc(T('empty'))+'</div>')
    : errbox(ops);
  const alertList = (alerts.ok && alerts.data && alerts.data.alerts)||[];
  const alertHtml = alerts.ok
    ? alertList.slice(0,8).map(a=>'<li>'+esc(a.text)+' <span class="muted small">· '+esc(dispName(a))+'</span></li>').join('')
    : '';
  if(!alerts.ok && alerts.status!==0) { /* surfaced below */ }
  const ecoList = (eco.ok && eco.data && eco.data.anomalies)||[];
  const ecoHtml = ecoList.slice(0,5).map(a=>'<li>'+esc(a.note)+'</li>').join('');
  const attItems = [];
  if(att.ok){
    const a0 = att.data||{};
    if(a0.stale_probes_count) attItems.push(a0.stale_probes_count+' моделей не проверялись >24ч');
    if(a0.unknown_price_count) attItems.push(a0.unknown_price_count+' маршрутов с неизвестной ценой');
  }

  // R15 §9: readiness card
  const RD = obsR.ok ? (obsR.data||{}) : null;
  const rdLevel = RD ? RD.level : null;
  const rdCount = RD ? (RD.warning_count!=null ? RD.warning_count : (RD.warnings||[]).length) : 0;
  const rdBadge = RD
    ? (rdLevel==='GREEN' ? badge('Router OK','ok')
      : rdLevel==='YELLOW' ? badge('Есть предупреждения '+rdCount,'warn')
      : badge('Критическая ошибка','bad'))
    : '<span class="bad small">'+esc(T('dataError'))+'</span>';
  const rdChecks = RD ? (RD.checks||[]).map(c =>
    '<li>'+esc(c.name_ru||c.name)+': '+(c.state==='ok'?badge('OK','ok'):c.state==='warn'?badge('проблема','warn'):badge('критично','bad'))+
    (c.reason?' <span class="muted small">— '+esc(c.reason)+'</span>':'')+'</li>').join('') : '';

  // R15 §10: alert center
  const OA = obsA.ok ? (obsA.data||{}) : null;
  const oaList = OA ? (OA.alerts||[]) : [];
  const CAT_RU = {price:'Цена', provider:'Provider', model:'Model', catalog:'Catalog',
                  availability:'Availability', economics:'Routing economics', budget:'Budget',
                  config:'Configuration'};
  const SEV_CLS = {critical:'bad', warning:'warn', info:'mut'};
  const SEV_RU = {critical:'Критично', warning:'Внимание', info:'Инфо'};
  const alertCenterHtml = OA
    ? (oaList.length
      ? '<table class="tbl"><tr><th>Важность</th><th>Категория</th><th>Объект</th><th>Причина</th><th>Рекомендация</th><th></th></tr>'+
        oaList.slice(0,12).map(a=>
          '<tr><td>'+badge(SEV_RU[a.severity]||a.severity, SEV_CLS[a.severity]||'mut')+'</td>'+
          '<td>'+esc(CAT_RU[a.category]||a.category||'')+'</td>'+
          '<td>'+esc(dispName(a))+'</td>'+
          '<td>'+esc(a.reason||'')+'</td>'+
          '<td class="muted">'+esc(a.recommended||'')+'</td>'+
          '<td>'+(a.acked?'<span class="muted small">подтверждено</span>'
            :'<button class="ghost small" data-action="obsAck" data-id="'+esc(String(a.alert_key))+'">Подтвердить</button>')+'</td></tr>').join('')+
        '</table><div class="muted small" style="margin-top:4px">Показаны первые '+Math.min(12,oaList.length)+' из '+oaList.length+'; подтверждённых: '+(OA.acked_count!=null?OA.acked_count:0)+'</div>'
      : '<div class="empty">Активных предупреждений нет</div>')
    : errbox(obsA);

  // R15 §3: budget mini-card
  const OB = obsB.ok ? (obsB.data||{}) : null;
  const budCard = OB
    ? card('Бюджеты',
        (OB.daily && OB.daily.budget_usd) ? (OB.daily.pct!=null?Math.round(OB.daily.pct)+'%':'—') : 'не задан',
        (OB.daily && OB.daily.budget_usd)
          ? 'сегодня '+usdFmt(OB.daily.spent_usd)+' из '+usdFmt(OB.daily.budget_usd)+
            (OB.daily.forecast_usd!=null?' · прогноз '+usdFmt(OB.daily.forecast_usd):'')
          : 'дневной/месячный лимит не настроен (только предупреждения)')
    : card('Бюджеты','—', errbox(obsB));

  const poolV = vcell(sum,'models_in_pool'), eligV = vcell(sum,'models_eligible');
  return '<h2>'+esc(T('routerState')||'Состояние Router')+'</h2>'+ '<div class="global-status">'+rdBadge+'</div>'+
    (RD ? '<div class="card" style="margin-bottom:8px"><div class="row" style="justify-content:space-between"><b>'+rdBadge+'</b>'+
      '<span class="muted small">'+(RD.level_ru||'')+'</span></div>'+
      (rdChecks?'<ul class="attention" style="margin-top:6px">'+rdChecks+'</ul>':'')+'</div>'
      : errbox(obsR))+
    '<div class="cards">'+
    card(T('routerWorks'), rhOK?badge(T('works'),'ok'):(rh.ok?badge('DOWN','bad'):'<span class="bad small">'+esc(T('dataError'))+'</span>'), 'uptime '+Math.round((rd.uptime_s||0)/3600)+' ч')+
    card(T('providersN'), usage.ok ? provNames.filter(p=>p!=='none').length+' '+T('connected') : '<span class="bad small">'+esc(T('dataError'))+'</span>', usage.ok ? online+' online' : errbox(usage))+
    card(T('models')+' · '+T('inMyPool'), poolV, usage.ok ? (eligV+' подходят политике') : '')+
    card(T('availModels'), vcell(sum,'models_available_now'), 'на рынке прямо сейчас')+
    card(T('routerRoutes'), vcell(sum,'router_routes'), (sum.runtime_canonicals??'—')+' моделей в runtime')+
    costCard+
    budCard+
    '</div>'+
    (sum.ok?'':errbox(sum))+
    '<h2>'+esc(T('catalog'))+'</h2>'+
    '<div class="cards">'+
    Object.entries(sum.ok?(sum.catalog_counts||{}):{}).map(([p,n])=>card(esc(p), n, T('catalog').toLowerCase())).join('')+
    card(T('unmatchedShort'), vcell(sum,'unmatched'), '<a href="#" data-action="goUnmatched">'+esc(T('unmatchedTitle'))+'</a>')+
    card(T('marketOffers'),
      sum.ok ? (((typeof sum.market_offers_provider_b_sellers==='number'?sum.market_offers_provider_b_sellers:(sum.market_offers_provider_b_sellers||{}).sum_sellers_across_models||0)+(sum.market_offers_provider_a_asks||0))) : '<span class="bad small">'+esc(T('dataError'))+'</span>',
      T('offersSumNote')+' · '+(sum.market_offers_provider_a_asks??0)+' '+T('priceOffers')+' Provider A')+
    '</div>'+
    '<h2>'+esc(T('providerState'))+'</h2>'+
    (usage.ok ? (provRows.length ? tbl([T('provCol'),'','OK / Сбой','Расходы','TTFT p50',''], provRows) : '<div class="empty">'+esc(T('noData'))+'</div>') : errbox(usage))+
    '<h2>Экономические аномалии</h2>'+
    (ecoList.length ? '<div class="card anomaly-list"><ul>'+ecoList.slice(0,5).map(a=>'<li><b>'+esc(a.selected&&a.selected.canonical||'выбрано')+'</b> → '+esc(a.alternative&&a.alternative.canonical||'альтернатива')+' · ×'+(a.ratio||'—')+' · '+esc(a.reason||a.note||'')+'</li>').join('')+'</ul><button class="ghost small" data-action="goMonitoring">Подробнее в Мониторинге</button></div>' : '<div class="empty">Экономических аномалий нет</div>')+
    '<h2>'+esc(T('opportunitiesTitle'))+'</h2>'+opsHtml+
    '<h2>'+esc(T('needsAttention'))+'</h2>'+
    alertCenterHtml+
    (alertHtml?'<details class="raw" style="margin-top:6px"><summary>Устаревшие детекторы R13</summary><ul class="attention">'+alertHtml+'</ul></details>':'')+
    (alerts.ok?'':errbox(alerts))+
    '<details class="raw"><summary>'+esc(T('diagnostics'))+' / Raw</summary><pre>'+esc(JSON.stringify({summary:sum,usage:u,runtime:rd},null,2))+'</pre></details>';
};

// ── R14 §4: единые entry points добавления модели. opAdd на Dashboard и
// goUnmatched ведут в тот же wizard / список, что и «+ Добавить модель»
// на странице Models — не отдельные реализации.
ACTIONS.opAdd = (el)=>{
  // выбрать каноническую модель в wizard: переходим на Models и открываем
  // единый wizard (page_models.ACTIONS.addModel) с предзаполненным поиском
  location.hash = 'Models';
  const pre = el.dataset.c || '';
  const t = setInterval(()=>{
    if(window.MR_ACTIONS && window.MR_ACTIONS.addModel){
      clearInterval(t);
      window.MR_ACTIONS.addModel();
      const q = document.getElementById('wz-q');
      if(q && pre){ q.value = pre; window.MR_ACTIONS.wzSearch && window.MR_ACTIONS.wzSearch(); }
    }
  }, 120);
  setTimeout(()=>clearInterval(t), 8000);
};
ACTIONS.goUnmatched = ()=>{
  location.hash = 'Models';
};
ACTIONS.goMonitoring = ()=>{ location.hash = 'Monitoring'; };

// ══ SETTINGS// ── R14: «Почему провайдер не используется» — честное объяснение ────────
ACTIONS.obsAck = async (el)=>{
    const id = el.dataset.id;
    const r = await api('/admin/obs/alerts/'+encodeURIComponent(id)+'/ack', {method:'POST'});
    if(r.ok){ toast('Предупреждение подтверждено'); render(); }
    else apiErr(r, 'obs/alerts ack');
  };
  ACTIONS.whyProvider = async (el)=>{
  const name = el.dataset.p;
  const [cf, us] = await Promise.all([api('/admin/config/active'), api('/admin/insights/usage')]);
  const cfg = (cf.ok && cf.data.config) || {};
  const prov = ((cfg.providers||{})[name]) || {};
  const enabled = prov.enabled !== false;
  const u = (us.ok && (us.data.providers||{})[name]) || {};
  const used = (u.requests||0) > 0;
  const items = [];
  items.push((enabled?'<li><span class="ok">✓</span> провайдер включён в политике</li>':'<li><span class="bad">✗</span> провайдер отключён в политике — маршруты исключены из выбора</li>'));
  items.push('<li><span class="ok">✓</span> минимальная скидка провайдера: '+Math.round((prov.min_discount!=null?prov.min_discount:cfg.min_discount||0.8)*100)+'%</li>');
  if(used) items.push('<li><span class="ok">✓</span> есть фактические запросы ('+(u.successes||0)+' успехов / '+(u.failures||0)+' сбоев)</li>');
  else items.push('<li><span class="muted">•</span> фактических запросов пока не было — провайдер участвует в выборе, но пока не выигрывал конкуренцию по цене/качеству/задержке</li>');
  items.push('<li class="muted small">Провайдер не является фактором приоритета: Router выбирает маршрут по здоровью, надёжности, цене и задержке. Provider A и Provider B конкурируют на равных — фиксированных долей нет.</li>');
  openDrawer('<div class="drawer-head"><h3>'+esc('Почему провайдер '+(used?'используется':'не используется'))+': '+esc(name)+'</h3>'+
    '<span style="margin-left:auto"></span><button class="ghost small" data-action="closeDrawer">✕</button></div>'+
    '<ul class="why-list">'+items.join('')+'</ul>', 'whyprov='+encodeURIComponent(name));
};

// ══ SETTINGS (R14 §28–§34: карточки + точки восстановления) ═════════════
RPAGES['Settings'] = async () => {
  const lang = UIS.lang;
  const [bl, cfgR, sch] = await Promise.all([api('/admin/baselines'), api('/admin/config/active'), api('/admin/refresh/schedule')]);
  const blList = (bl.data&&bl.data.baselines)||[];
  const primary = (bl.data&&bl.data.primary_known_good)||null; // {baseline_id,...}
  const activeRev = (bl.data&&bl.data.active_revision)||(cfgR.data&&cfgR.data.revision_id)||'—';
  const ck = (k, lbl) => '<label class="fld chk"><input type="checkbox" data-set="'+k+'"'+(UIS.get(k, false)?' checked':'')+'> <span>'+esc(lbl)+'</span></label>';

  // §30: список точек восстановления
  const blRows = blList.map(b=>{
    const isPrimary = !!b.is_primary_known_good || (primary && primary.baseline_id===b.id);
    const isFactory = b.kind==='FACTORY_DEFAULT';
    const title = isFactory ? 'Заводские настройки' : (b.description||'Точка восстановления');
    return '<tr><td>'+esc(title)+(isPrimary?' '+badge('Основная рабочая','ok'):'')+'</td>'+
      '<td>'+esc(new Date(b.created_at*1000).toLocaleString('ru-RU'))+'</td>'+
      '<td><code class="small">'+esc((b.content_hash||'').slice(0,12))+'…</code></td>'+
      '<td class="actions">'+
      '<button class="ghost small" data-action="blDiff" data-id="'+esc(b.id)+'">Сравнить</button> '+
      '<button class="warn small" data-action="blRestore" data-id="'+esc(b.id)+'">Восстановить</button> '+
      (isFactory?'':(isPrimary?'':'<button class="ghost small" data-action="blPrimary" data-id="'+esc(b.id)+'">Сделать основной</button> '+
      '<button class="danger small" data-action="blDelete" data-id="'+esc(b.id)+'">Удалить</button>'))+
      '</td></tr>';
  }).join('');

  // §29: автообновление — отдельные job'ы с honest-статусами
  const jobs = (sch.ok && sch.data.jobs)||{};
  const jobRow = (key, lbl, every) => {
    const j = jobs[key]||{};
    const last = j.last_ok ? 'последний успех '+(j.last_run?dtHM(j.last_run):'—') : (j.last_run?('ошибка '+(j.last_error||'').slice(0,60)):'ещё не запускался');
    return '<tr><td>'+esc(lbl)+'</td><td>'+esc(every)+'</td><td class="small">'+esc(last)+'</td><td class="small">'+(j.next_run?esc('в '+(new Date(j.next_run*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}))):'—')+'</td></tr>';
  };

  return '<h2>'+esc(T('Settings'))+'</h2>'+

  '<div class="card" style="margin:8px 0"><h3 style="margin:4px 0">'+esc('Интерфейс')+'</h3>'+
  '<div class="row">'+
  '<label class="fld"><span>'+esc(T('language'))+'</span>'+
  '<select id="st-lang"><option value="ru"'+(lang==='ru'?' selected':'')+'>Русский</option>'+
  '<option value="en"'+(lang==='en'?' selected':'')+'>English</option></select></label>'+
  '<label class="fld chk" style="margin-top:18px"><input type="checkbox" data-set="compact"'+(UIS.get('compact',false)?' checked':'')+'> <span>'+esc(T('compactMode'))+'</span></label>'+
  '</div>'+
  '<p class="muted small">Автообновление окна браузера: интервал <input id="st-interval" type="number" min="5" step="5" value="'+UIS.getNum('refreshInterval',30)+'" style="width:70px"> сек '+
  '<label class="chk" style="display:inline-flex"><input type="checkbox" data-set="autoRefresh"'+(UIS.get('autoRefresh',false)?' checked':'')+'> включить</label> — это обновление страницы, не данных провайдеров.</p>'+
  '<div class="row"><button id="st-save">'+esc(T('save'))+'</button><button class="ghost" id="st-reset">'+esc('Сбросить')+'</button></div></div>'+

  '<div class="card" style="margin:8px 0"><h3 style="margin:4px 0">'+esc('Автообновление данных')+'</h3>'+
  (sch.ok ? '<div class="tbl-wrap"><table class="tbl-main"><tr><th>Данные</th><th>Частота</th><th>Статус</th><th>Следующий</th></tr>'+
    jobRow('catalog','Каталог и цены','каждые 6 ч')+
    jobRow('market','Цены рынка','каждый 1 ч')+
    jobRow('availability','Доступность (пул)','каждые 30 мин')+
    '</table></div>' : apiErr(sch,'refresh/schedule'))+
  '<p class="muted small">Расписание фиксировано и безопасно: бесплатные catalog-проверки, платных inference-проб нет.</p></div>'+

  '<div class="card" style="margin:8px 0"><h3 style="margin:4px 0">'+esc('Точки восстановления конфигурации')+'</h3>'+
  '<p class="muted small">Текущая конфигурация: <code>'+esc(activeRev)+'</code>. '+
  'Основная рабочая — проверенное состояние, к которому можно вернуться одной кнопкой. '+
  'Заводские настройки — исходные значения по умолчанию, не зависят от ваших изменений.</p>'+
  '<div class="row"><button data-action="blCreate">'+esc('Создать точку восстановления')+'</button></div>'+
  (blRows?'<div class="tbl-wrap" style="margin-top:8px"><table class="tbl-main"><tr><th>Точка</th><th>Дата</th><th>Хэш</th><th>'+T('actions')+'</th></tr>'+blRows+'</table></div>'
         :'<p class="muted small">'+esc('Точек восстановления пока нет.')+'</p>')+
  '<details class="raw"><summary>'+esc('Что сохраняется / не сохраняется')+'</summary>'+
  '<p class="small">Сохраняется: провайдеры, модели (пул и сопоставления), маршрутизация, классы задач, политика.<br>'+
  'Не сохраняется: API-ключи, метрики, биллинг, временное здоровье маршрутов.</p></details></div>'+

  '<div class="card" style="margin:8px 0"><h3 style="margin:4px 0">'+esc('Импорт / экспорт')+'</h3>'+
  '<div class="row">'+
  '<button class="ghost" data-action="cfgExport">'+esc(T('exportJson'))+'</button>'+
  '<button class="ghost" data-action="cfgImport">'+esc(T('importJson'))+'</button>'+
  '</div></div>'+

  '<div class="card" style="margin:8px 0"><h3 style="margin:4px 0">'+esc(T('diagnostics'))+' — '+esc('Для разработчика')+'</h3>'+
  '<p class="muted small">'+esc('Показ внутренних идентификаторов и raw JSON. В обычном режиме всё скрыто.')+'</p>'+
  '<label class="fld chk"><input type="checkbox" data-set="diagnosticsMode"'+(UIS.get('diagnosticsMode',false)?' checked':'')+'> <span><b>'+esc(T('diagnostics'))+'</b></span></label>'+
  ck('canonicalId', T('showCanonicalId'))+
  ck('providerModelId', T('showProviderModelId'))+
  ck('score', T('showScore'))+
  ck('rawJson', T('showRawJson'))+
  '</div>';
};

POSTRENDER['Settings'] = () => {
  $('st-lang').onchange = () => { setLang($('st-lang').value); render(); toast(T('languageSaved'),'ok'); };
  $('st-save').onclick = () => {
    document.querySelectorAll('[data-set]').forEach(inp=>{ UIS.set(inp.dataset.set, inp.checked); });
    UIS.setNum('refreshInterval', Math.max(5, parseInt($('st-interval').value)||30));
    applyCompact(); scheduleRefresh();
    toast(T('saved'),'ok');
  };
  $('st-reset').onclick = () => { render(); };
};

// ── baselines actions (R14 §31–§34) ─────────────────────────────────────
ACTIONS.blCreate = ()=>{
  openModal('<h3>'+esc('Создать точку восстановления')+'</h3>'+
    '<label class="fld"><span>Название</span><input id="bl-name" placeholder="KNOWN-GOOD '+new Date().toISOString().slice(0,10)+'"></label>'+
    '<label class="fld"><span>Описание</span><input id="bl-desc" placeholder="что изменено и почему проверено"></label>'+
    '<details class="raw" open><summary>Что сохранится</summary><p class="small">✓ провайдеры · ✓ модели · ✓ сопоставления · ✓ маршрутизация · ✓ классы задач · ✓ политика<br>'+
    '✗ API-ключи · ✗ метрики · ✗ биллинг · ✗ временное здоровье</p></details>'+
    '<div class="row" style="justify-content:flex-end;margin-top:10px">'+
    '<button class="ghost" data-action="closeModal">'+esc(T('cancel'))+'</button>'+
    '<button id="bl-ok">'+esc('Создать')+'</button></div>');
  $('bl-ok').onclick = async ()=>{
    const r = await api('/admin/baselines', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name: $('bl-name').value.trim(), description: $('bl-desc').value.trim()||$('bl-name').value.trim()})});
    if(r.ok){ closeModal(); toast(T('saved'),'ok'); render(); } else toast(T('httpErr')+' '+r.status,'bad');
  };
};
ACTIONS.blDiff = async (el)=>{
  const r = await api('/admin/baselines/'+encodeURIComponent(el.dataset.id)+'/diff');
  if(!r.ok){ toast(T('httpErr')+' '+r.status,'bad'); return; }
  const d = (r.data.diff)||{};
  const cfgCh = (d.config_diff&&Object.keys(d.config_diff))||[];
  let html = '<div class="drawer-head"><h3>'+esc('Сравнение с точкой восстановления')+'</h3><span style="margin-left:auto"></span>'+
    '<button class="ghost small" data-action="closeDrawer">✕</button></div>';
  html += '<h4>Политика</h4>'+(cfgCh.length? cfgCh.map(k=>'<p class="small">◦ '+esc(k)+': '+esc(String(d.config_diff[k].old))+' → '+esc(String(d.config_diff[k].new))+'</p>').join('') : '<p class="muted small">без изменений</p>');
  html += '<h4>Провайдеры</h4><p class="small">+'+((d.providers_diff||{}).added||0)+' / −'+((d.providers_diff||{}).removed||0)+'</p>';
  html += '<h4>Сопоставления моделей</h4>'+
    ((d.mappings_added||[]).length?'<p class="small">Добавятся: '+d.mappings_added.map(esc).join(', ')+'</p>':'')+
    ((d.mappings_removed||[]).length?'<p class="small warn">Удалятся: '+d.mappings_removed.map(esc).join(', ')+'</p>':'')+
    (!((d.mappings_added||[]).length||(d.mappings_removed||[]).length)?'<p class="muted small">без изменений</p>':'');
  html += '<h4>Пул моделей</h4><p class="small">+'+((d.pool_diff||{}).added||0)+' / −'+((d.pool_diff||{}).removed||0)+'</p>';
  html += '<div class="row" style="margin-top:10px"><button class="warn" data-action="blRestore" data-id="'+esc(el.dataset.id)+'">'+esc('Восстановить эту точку')+'</button></div>';
  openDrawer(html, 'bldiff='+encodeURIComponent(el.dataset.id));
};
ACTIONS.blRestore = (el)=>{
  const id = el.dataset.id;
  confirmModal(T('restoreBaseline'),
    esc('Текущая конфигурация будет заменена. Изменение пройдёт через ревизию с валидацией и попадёт в журнал. Восстановление можно отменить повторным восстановлением другой точки.'),
    async ()=>{
      const r = await api('/admin/baselines/'+encodeURIComponent(id)+'/restore', {method:'POST'});
      if(r.ok){ toast(T('saved'),'ok'); render(); } else toast(T('httpErr')+' '+r.status,'bad');
    }, T('restoreConfirm'));
};
ACTIONS.blPrimary = (el)=>{
  confirmModal(esc('Сделать основной рабочей точкой?'),
    esc('Эта точка станет «основной рабочей конфигурацией» — рекомендуемой целью возврата. Заводские настройки и текущая конфигурация не меняются.'),
    async ()=>{
      const r = await api('/admin/baselines/known-good/set', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({baseline_id: el.dataset.id})});
      if(r.ok){ toast(T('saved'),'ok'); render(); } else toast(T('httpErr')+' '+r.status,'bad');
    }, esc('Сделать основной'));
};
ACTIONS.blDelete = (el)=>{
  confirmModal(esc('Удалить точку восстановления?'),
    esc('Точка будет удалена из списка. Заводские настройки и основная рабочая точка защищены от удаления.'),
    async ()=>{
      const r = await api('/admin/baselines/'+encodeURIComponent(el.dataset.id)+'/delete', {method:'POST'});
      if(r.ok){ toast(T('saved'),'ok'); render(); } else toast(T('httpErr')+' '+r.status,'bad');
    }, esc('Удалить'));
};
ACTIONS.cfgExport = async ()=>{
  const r = await api('/admin/config/export');
  if(!r.ok){ toast(T('httpErr')+' '+r.status,'bad'); return; }
  const blob = new Blob([JSON.stringify(r.data,null,2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'model-router-config-'+new Date().toISOString().slice(0,10)+'.json';
  a.click();
};
ACTIONS.cfgImport = ()=>{
  const inp = document.createElement('input');
  inp.type='file'; inp.accept='.json';
  inp.onchange = async ()=>{
    const text = await inp.files[0].text();
    try{
      const payload = JSON.parse(text);
      const dry = await api('/admin/config/import', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({payload, dry_run:true})});
      if(!dry.ok || !(dry.data||{}).ok){ toast('Импорт: ошибки валидации','bad'); return; }
      confirmModal(T('importJson'), 'Файл прошёл валидацию. Применить конфигурацию?', async ()=>{
        const r = await api('/admin/config/import', {method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({payload, dry_run:false})});
        if(r.ok && r.data.applied){ toast(T('saved'),'ok'); render(); }
        else toast(T('httpErr'),'bad');
      }, T('apply'));
    }catch(e){ toast('Некорректный JSON','bad'); }
  };
  inp.click();
};
