// R15: Monitoring page — production guards + observability.
// Provider/model degradation, economic anomalies, price spikes, budgets,
// shadow candidates, canary, schema drift, system events (audit is separate).

import {
  RPAGES, POSTRENDER, ACTIONS, api, apiErr, toast, esc, $,
  badge, tbl, openDrawer, closeDrawer, openModal, closeModal,
  usdFmt, ms, dt, dtHMS, ago, dispName
} from './core.js';
import { T } from './i18n.js';

const errbox = (r) => '<div class="card bad">Ошибка загрузки ' + esc((r && (r.url || r.err)) || 'данных') +
  (r && r.status ? ' (HTTP ' + esc(r.status) + ')' : '') + '</div>';

const SEV_CLS = {critical:'bad', warning:'warn', info:'mut'};
const SEV_RU = {critical:'Критично', warning:'Внимание', info:'Инфо'};
const ST_RU = {ok:'Работает', degraded:'Деградация', down:'Недоступен', no_data:'Нет свежих данных'};

function kindRu(k) {
  return {
    economic_inefficiency:'Возможная неэффективность',
    price_spike:'Резкий рост цены',
    provider_degraded:'Деградация провайдера',
    provider_down:'Провайдер недоступен',
    model_degraded:'Деградация модели',
    catalog_shrink:'Обвал каталога',
    schema_drift:'Изменение схемы провайдера',
    budget_exceeded:'Превышение бюджета',
    budget_forecast:'Прогноз превышения бюджета',
  }[k] || k;
}

RPAGES['Monitoring'] = async () => {
  const [pv, md, ea, bud, sh, can, ev, schedule] = await Promise.all([
    api('/admin/obs/providers').catch(e=>({ok:false,status:0,url:'',data:{},err:String(e)})),
    api('/admin/obs/models').catch(e=>({ok:false,status:0,url:'',data:{},err:String(e)})),
    api('/admin/obs/economic-anomalies').catch(e=>({ok:false,status:0,url:'',data:{},err:String(e)})),
    api('/admin/obs/budgets').catch(e=>({ok:false,status:0,url:'',data:{},err:String(e)})),
    api('/admin/obs/shadow').catch(e=>({ok:false,status:0,url:'',data:{},err:String(e)})),
    api('/admin/obs/canary').catch(e=>({ok:false,status:0,url:'',data:{},err:String(e)})),
    api('/admin/obs/events').catch(e=>({ok:false,status:0,url:'',data:{},err:String(e)})),
    api('/admin/refresh/schedule').catch(e=>({ok:false,status:0,url:'',data:{},err:String(e)})),
  ]);

  const jobD=schedule.ok?(schedule.data||{}):{};
  const jobRows=Object.entries(jobD.jobs||{}).map(([k,j])=>[
    '<b>'+esc(j.label||k)+'</b>', j.last_run?dtHMS(j.last_run):'Ещё не запускалась',
    j.next_run?('через '+Math.max(0,Math.round((j.next_run-jobD.now)/60))+' мин'):'—',
    badge(j.last_ok===false?'Ошибка':j.last_run?'Успешно':'Ожидает',j.last_ok===false?'bad':j.last_run?'ok':'mut'),
    j.last_error?'<span class="warn">'+esc(j.last_error)+'</span>':'—',
    '<button class="ghost small" data-action="runAutomation" data-job="'+esc(k)+'">Запустить сейчас</button>'
  ]);

  // ── providers ──
  const pvRows = pv.ok ? ((pv.data.providers)||[]).map(p=>[
    '<b>'+esc(p.provider)+'</b>',
    badge(ST_RU[p.status]||p.status, p.status==='ok'?'ok':p.status==='degraded'?'warn':p.status==='down'?'bad':'mut'),
    (p.success_rate!=null ? Math.round(p.success_rate*100)+'%' : '—'),
    p.ttft_p50_ms!=null ? ms(p.ttft_p50_ms) : '—',
    p.ttft_p95_ms!=null ? ms(p.ttft_p95_ms) : '—',
    p.timeouts!=null ? p.timeouts : '—',
    p.catalog_age_s!=null ? Math.round(p.catalog_age_s/60)+' мин' : '—',
    '<span class="muted small">'+esc(p.reason||'')+'</span>',
  ]) : null;

  // ── models ──
  const mdRows = md.ok ? ((md.data.models)||[]).map(m=>[
    '<button class="linklike" data-action="monitorModel" data-c="'+esc(m.canonical||'')+'">'+esc(dispName(m))+'</button>',
    badge(ST_RU[m.status]||m.status, m.status==='ok'?'ok':m.status==='degraded'?'warn':m.status==='down'?'bad':'mut'),
    (m.availability!=null ? Math.round(m.availability*100)+'%' : 'Нет данных'),
    (m.success_rate!=null ? Math.round(m.success_rate*100)+'% · '+(m.requests||0)+' запросов' : 'Нет трафика за период'),
    m.price_state_ru || (m.best_input!=null?usdFmt(m.best_input):'Цена неизвестна'),
    '<span class="muted small">'+esc(m.reason||'')+'</span>',
  ]) : null;

  // ── economic anomalies ──
  const eaD = ea.ok ? (ea.data||{}) : {};
  const eaRows = (eaD.anomalies||[]).map(a=>[
    dtHMS(a.ts),
    '<b>'+esc(dispName(a))+'</b>',
    usdFmt(a.selected_cost_usd)+' · '+esc(dispName(a.selected||{})),
    usdFmt(a.alternative_cost_usd)+' · '+esc(dispName(a.alternative||{})),
    '×'+(a.ratio!=null?a.ratio.toFixed(1):'—')+' · экономия '+usdFmt(a.difference_usd),
    '<span class="muted small">'+esc(a.reason||'')+'</span>',
  ]);

  // ── budgets ──
  const budD = bud.ok ? (bud.data||{}) : {};
  function budgetBlock(label, b) {
    if (!b || !b.budget_usd) return '<div class="card"><div class="t">'+label+'</div><div class="v muted">не задан</div><div class="s">только предупреждения; жёсткий стоп выключен по умолчанию</div></div>';
    const pct = b.pct!=null ? Math.round(b.pct*100) : null;
    const cls = b.exceeded ? 'bad' : (pct!=null && pct>=80 ? 'warn' : 'ok');
    return '<div class="card"><div class="t">'+label+'</div><div class="v">'+(pct!=null?pct+'%':'—')+'</div><div class="s">'+
      'потрачено '+usdFmt(b.spent_usd)+' из '+usdFmt(b.budget_usd)+
      (b.forecast_usd!=null?' · прогноз '+usdFmt(b.forecast_usd):'')+
      (b.exceeded?' · ПРЕВЫШЕН':'')+'</div>'+
      (pct!=null?'<div class="bar"><div class="bar-fill '+cls+'" style="width:'+Math.min(100,pct)+'%"></div></div>':'')+
      '</div>';
  }

  // ── shadow candidates ──
  const shD = sh.ok ? (sh.data||{}) : {};
  const shRows = (shD.candidates||[]).map(c=>[
    '<b>'+esc(dispName(c))+'</b>',
    badge(c.is_candidate?'Кандидат':'—', c.is_candidate?'warn':'mut'),
    (c.would_be_selected!=null ? c.would_be_selected : '—'),
    c.savings_usd!=null ? usdFmt(c.savings_usd) : '—',
    (c.task_coverage!=null ? Math.round(c.task_coverage*100)+'%' : '—'),
    '<span class="muted small">'+esc((c.rejection_reasons||[]).slice(0,2).join('; '))+'</span>',
  ]);

  // ── canary ──
  const canD = can.ok ? (can.data||{}) : {};
  const cE = canD.experiment||{};
  const canaryHtml = !cE.model
    ? '<div class="empty">Эксперимент не настроен (по умолчанию выключен). <button class="small" data-action="canWizard">Настроить</button></div>'
    : '<div class="card"><div class="row" style="justify-content:space-between"><b>'+esc(dispName(cE))+'</b>'+
      badge(cE.enabled?'Активен':'Выключен', cE.enabled?'warn':'mut')+'</div>'+
      '<div class="muted small">классы: '+esc((cE.task_classes||[]).join(', ')||'все')+
      ' · трафик '+cE.traffic_pct+'% · TTL '+Math.round((cE.ttl_s||0)/60)+' мин</div>'+
      (canD.stats ? '<div class="muted small">проверено: '+(canD.stats.requests||0)+' · ошибок '+(canD.stats.errors||0)+
        ' · p95 '+(canD.stats.ttft_p95_ms!=null?ms(canD.stats.ttft_p95_ms):'—')+'</div>' : '')+
      '<div style="margin-top:6px"><button class="ghost small" data-action="canStop">Остановить</button> '+
      '<button class="ghost small" data-action="canWizard">Изменить</button></div></div>';

  // ── events (system) ──
  const evRows = ev.ok ? ((ev.data.events)||[]).map(e=>[
    dtHMS(e.ts),
    badge(SEV_RU[e.severity]||e.severity||'info', SEV_CLS[e.severity]||'mut'),
    esc(kindRu(e.kind)),
    esc(dispName(e)),
    '<span class="muted small">'+esc(e.reason||e.text||'')+'</span>',
  ]) : null;

  return (pv.ok?'':errbox(pv))+
    '<h2>Автоматические задачи</h2>'+ (schedule.ok?tbl(['Задача','Последний запуск','Следующий','Статус','Ошибка','Действия'],jobRows):errbox(schedule))+'<h2>Провайдеры</h2>'+
    (pv.ok ? (pvRows && pvRows.length ? tbl(['Провайдер','Статус','Успехи','TTFT p50','TTFT p95','Таймауты','Каталог','Причина'], pvRows) : '<div class="empty">'+esc(T('noData'))+'</div>') : '')+

    '<h2>Модели пула</h2>'+
    (md.ok ? (mdRows && mdRows.length ? tbl(['Модель','Статус','Доступность','Успехи','Цена','Причина'], mdRows) : '<div class="empty">'+esc(T('noData'))+'</div>') : errbox(md))+

    '<h2>Возможная неэффективность маршрутизации</h2>'+
    (ea.ok ? (eaRows.length
      ? tbl(['Время','Выбрано','Стоимость','Альтернатива','Дороже','Причина'], eaRows)+
        '<div class="muted small">Детектор: выбран маршрут дороже равной/лучшей по качеству альтернативы в N раз (по умолчанию ×3). Маршрутизация НЕ меняется автоматически.</div>'
      : '<div class="empty">Неэффективностей не обнаружено'+(eaD.checked!=null?' (проверено решений: '+eaD.checked+')':'')+'</div>')
      : errbox(ea))+

    '<h2>Бюджеты</h2>'+
    (bud.ok
      ? '<div class="cards">'+budgetBlock('Сегодня', budD.daily)+budgetBlock('Месяц', budD.monthly)+'</div>'+
        '<div style="margin-top:6px"><button class="small" data-action="budEdit">Настроить бюджеты</button></div>'
      : errbox(bud))+

    '<h2>Кандидаты (shadow)</h2>'+
    (sh.ok ? (shRows.length
      ? tbl(['Модель','Статус','Выбрался бы','Экономия','Покрытие','Причины отказа'], shRows)+
        '<div class="muted small">Dry-run на реальных записях решений. Production-трафик кандидатам НЕ отправляется.</div>'
      : '<div class="empty">Кандидатов нет. «Кандидат» — состояние модели в её карточке на странице Модели.</div>')
      : errbox(sh))+

    '<h2>Канарейка</h2>'+canaryHtml+

    '<h2>События системы</h2>'+
    '<div class="muted small">Аномалии и предупреждения. История конфигурации — отдельный раздел «Audit / Revisions».</div>'+
    (ev.ok ? (evRows && evRows.length ? tbl(['Время','Важность','Тип','Объект','Причина'], evRows) : '<div class="empty">Событий нет</div>') : errbox(ev))+

    '<details class="raw"><summary>'+esc(T('diagnostics'))+' / Raw</summary><pre>'+
      esc(JSON.stringify({providers:pv.data, models:md.data, econ:ea.data, budgets:bud.data, shadow:sh.data, canary:can.data},null,1))+
      '</pre></details>';
};

ACTIONS.monitorModel = async el => {
  const r=await api('/admin/obs/models'); const m=(r.ok?(r.data.models||[]):[]).find(x=>x.canonical===el.dataset.c);
  if(!m)return; const rows=(m.routes||[]).map(x=>'<tr><td>'+esc(x.provider)+'</td><td class="mono">'+esc(x.provider_model_id)+'</td><td>'+esc(x.status)+'</td><td>'+x.requests+'</td><td>'+x.success+'</td><td>'+ (x.ttft_p50_ms!=null?ms(x.ttft_p50_ms):'—')+' / '+(x.ttft_p95_ms!=null?ms(x.ttft_p95_ms):'—')+'</td><td>'+ (x.price!=null?usdFmt(x.price):'Цена неизвестна')+'</td><td>'+esc(x.last_error||'')+'</td></tr>').join('');
  openDrawer('<div class="drawer-head"><h3>'+esc(dispName(m))+'</h3><button class="ghost small" data-action="closeDrawer">✕</button></div><p>'+esc(m.reason||'')+'</p><table class="tbl-main"><tr><th>Провайдер</th><th>Маршрут</th><th>Статус</th><th>Запросы</th><th>Успехи</th><th>TTFT p50/p95</th><th>Цена</th><th>Последняя ошибка</th></tr>'+rows+'</table>','monitor='+encodeURIComponent(m.canonical));
};

ACTIONS.runAutomation=async el=>{
  const r=await api('/admin/refresh/run/'+encodeURIComponent(el.dataset.job),{method:'POST'});
  if(r.ok){toast('Задача выполнена','ok');render();}else apiErr(r,'refresh/run');
};

// ── actions ──
ACTIONS.budEdit = async () => {
  const r = await api('/admin/obs/budgets');
  const d = r.ok ? (r.data||{}) : {};
  const dy = d.daily||{}, mo = d.monthly||{};
  openModal(
    '<h3>Бюджеты (только предупреждения)</h3>'+
    '<p class="muted small">Жёсткий стоп трафика НЕ включается автоматически — только уведомления и прогноз.</p>'+
    '<label>Дневной бюджет, $ <input id="bud-daily" type="number" step="0.01" min="0" value="'+(dy.budget_usd!=null?dy.budget_usd:'')+'" placeholder="не задан"></label>'+
    '<label>Месячный бюджет, $ <input id="bud-monthly" type="number" step="0.01" min="0" value="'+(mo.budget_usd!=null?mo.budget_usd:'')+'" placeholder="не задан"></label>'+
    '<div class="row" style="margin-top:10px;gap:8px">'+
    '<button class="primary" data-action="budSave">Сохранить</button>'+
    '<button class="ghost" data-action="modalClose">Отмена</button></div>');
};

ACTIONS.budSave = async () => {
  const daily = parseFloat($('bud-daily').value) || null;
  const monthly = parseFloat($('bud-monthly').value) || null;
  const r = await api('/admin/obs/budgets', {method:'PUT', body: JSON.stringify({daily_budget_usd: daily, monthly_budget_usd: monthly})});
  if (r.ok) { toast('Бюджеты сохранены'); closeModal(); render(); }
  else apiErr(r, 'obs/budgets');
};

ACTIONS.canWizard = async () => {
  const r = await api('/admin/obs/canary');
  const e = r.ok ? ((r.data||{}).experiment||{}) : {};
  openModal(
    '<h3>Канарейка (эксперимент)</h3>'+
    '<p class="muted small">Часть трафика выбранных классов задач направляется на модель-кандидат. Отдельна от основной политики. Автооткат по порогам ошибок/TTFT/цены.</p>'+
    '<label>Модель <input id="can-model" value="'+esc(e.model||'')+'" placeholder="canonical"></label>'+
    '<label>Классы задач (через запятую) <input id="can-classes" value="'+esc((e.task_classes||[]).join(', '))+'" placeholder="пусто = все"></label>'+
    '<label>Трафик, % <input id="can-pct" type="number" min="0" max="100" value="'+(e.traffic_pct!=null?e.traffic_pct:5)+'"></label>'+
    '<label>Длительность, мин <input id="can-ttl" type="number" min="1" value="'+(e.ttl_s?Math.round(e.ttl_s/60):60)+'"></label>'+
    '<label>Автооткат: ошибок > % <input id="can-err" type="number" min="0" max="100" value="'+(e.rollback_error_rate_pct!=null?e.rollback_error_rate_pct:20)+'"></label>'+
    '<label>Автооткат: TTFT p95 > мс <input id="can-ttft" type="number" min="0" value="'+(e.rollback_ttft_p95_ms!=null?e.rollback_ttft_p95_ms:15000)+'"></label>'+
    '<label class="row" style="gap:6px"><input type="checkbox" id="can-enabled" '+(e.enabled?'checked':'')+'> Включить после сохранения</label>'+
    '<div class="row" style="margin-top:10px;gap:8px">'+
    '<button class="primary" data-action="canSave">Сохранить</button>'+
    '<button class="ghost" data-action="modalClose">Отмена</button></div>');
};

ACTIONS.canSave = async () => {
  const classes = $('can-classes').value.split(',').map(x=>x.trim()).filter(Boolean);
  const body = {
    model: $('can-model').value.trim() || null,
    task_classes: classes,
    traffic_pct: parseInt($('can-pct').value) || 5,
    ttl_s: (parseInt($('can-ttl').value) || 60) * 60,
    rollback_error_rate_pct: parseFloat($('can-err').value) || 20,
    rollback_ttft_p95_ms: parseFloat($('can-ttft').value) || 15000,
    enabled: $('can-enabled').checked,
  };
  const r = await api('/admin/obs/canary', {method:'PUT', body: JSON.stringify(body)});
  if (r.ok) { toast('Канарейка сохранена'); closeModal(); render(); }
  else apiErr(r, 'obs/canary');
};

ACTIONS.canStop = async () => {
  const r = await api('/admin/obs/canary', {method:'PUT', body: JSON.stringify({enabled: false})});
  if (r.ok) { toast('Канарейка остановлена'); render(); }
  else apiErr(r, 'obs/canary stop');
};
