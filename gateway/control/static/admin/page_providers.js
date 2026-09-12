// Pages: Providers — add/edit modal (UI spec §I), detail (§J), refresh (§K).
import { T } from './i18n.js';
import { RPAGES, POSTRENDER, api, apiErr, toast, tbl, badge, pct, ms, dt, usd6, UIS, esc, $, openModal, closeModal, openDrawer, closeDrawer, price1m, ctxFmt, confirmModal } from './core.js';
const ADAPTER_RU = {
  provider_a: 'Provider A — [OI] / Anthropic-совместимый маркетплейс',
  provider_b: 'Provider B — [OI]-совместимый маркетплейс',
  'openai-compatible': 'Универсальный [OI]-совместимый (кастомный)',
};

// original providers page logic (toggle/edit/delete) + new columns
RPAGES['Providers'] = async () => {
  const [pr, dt_] = await Promise.all([api('/admin/providers'), api('/admin/adapters')]);
  if(!pr.ok) return apiErr(pr, 'providers');
  const d = pr.data||{}, dts = (dt_.data&&dt_.data.types)||[];
  const list = Object.values(d.providers||{});
  const rows = list.map(p=>[
    '<b>'+esc(p.name)+'</b>'+(p.base_url?'<div class="muted small">'+esc(p.base_url)+'</div>':''),
    p.enabled?badge(T('enabled'),'ok'):badge(T('disabled'),'mut'),
    p.source==='control_db'?badge('control.db','acc'):badge(p.source||'config','mut'),
    esc(ADAPTER_RU[p.adapter_type] || p.adapter_type || '—'),
    '<button class="small" data-action="provDetail" data-provider="'+esc(p.name)+'">'+esc(T('details'))+'</button>',
    '<button class="ghost small" data-action="providerEdit" data-provider="'+esc(p.name)+'">'+esc(T('edit'))+'</button>',
    '<button class="'+(p.enabled?'warn':'ghost')+' small" data-action="providerToggle" data-provider="'+esc(p.name)+'" data-enabled="'+(p.enabled?'true':'false')+'">'+esc(p.enabled?T('off'):T('on'))+'</button>',
  ]);
  return '<div class="row" style="margin-bottom:10px">'+
    '<button data-action="providerAdd" data-adapters="'+esc(dts.join(','))+'">'+esc(T('addProvider'))+'</button>'+
    '<button class="ghost" data-action="refreshAllCatalogs">'+esc(T('refreshAllCatalogs'))+'</button>'+
    '<span id="prov-refresh-res" class="muted"></span></div>'+
    tbl([T('name'),T('providerStatus'),'source',T('adapter'),T('details'),T('edit'),T('actions')], rows);
};

// ── provider detail (§J) ─────────────────────────────────────────────────
async function provDetail(name){
  const r = await api('/admin/providers/'+encodeURIComponent(name)+'/detail');
  if(!r.ok){ toast(apiErrShort(r),'bad'); return; }
  const d = r.data, p = d.provider;
  const provRows = (d.models||[]).map(m=>[
    '<b>'+esc(m.canonical)+'</b>',
    UIS.showProviderId()?esc(m.provider_model_id):'<span class="muted small">'+esc(m.provider_model_id.split('/').pop())+'</span>',
    price1m(m.input_price, m.price_state),
    price1m(m.output_price, m.price_state),
    m.discount!=null?pct(m.discount):T('unknown'),
    ctxFmt(m.context),
    m.eligible?badge('ELIGIBLE','ok'):badge('BELOW_FLOOR','warn'),
    m.last_probe?(m.last_probe.ok?badge(T('available'),'ok')+' '+m.last_probe.latency_ms+' мс':badge(m.last_probe.error_code||T('unavailable'),'bad')):'<span class="muted">'+esc(T('notChecked'))+'</span>',
  ]);
  openDrawer('<div class="row" style="justify-content:space-between"><h3 style="margin:0">'+esc(p.name)+'</h3>'+
   '<button class="ghost small" data-action="closeDrawer">✕</button></div>'+
   '<dl class="kv">'+
   '<dt>'+esc(T('providerStatus'))+'</dt><dd>'+(p.enabled?badge(T('enabled'),'ok'):badge(T('disabled'),'mut'))+'</dd>'+
   '<dt>Base URL</dt><dd><code>'+esc(p.base_url||'—')+'</code></dd>'+
   '<dt>'+esc(T('adapter'))+'</dt><dd>'+esc(ADAPTER_RU[p.adapter_type] || p.adapter_type || '—')+'</dd>'+
   '<dt>Тип подключения</dt><dd class="muted small">'+esc(p.adapter_type||'—')+' (для разработчика)</dd>'+
   '<dt>'+esc(T('secretEnv'))+'</dt><dd><code>'+esc(p.secret_ref||'—')+'</code></dd>'+
   '<dt>'+esc(T('lastCheck'))+'</dt><dd>'+dt(d.last_check_ts)+'</dd>'+
   '<dt>'+esc(T('modelsFound'))+'</dt><dd>'+d.models_found+'</dd>'+
   '<dt>'+esc(T('eligibleRoutes'))+'</dt><dd>'+d.eligible_routes+'</dd>'+
   '</dl>'+
   '<div class="row" style="margin:10px 0">'+
   '<button class="small" data-action="probeProvider" data-provider="'+esc(p.name)+'">'+esc(T('testConnection'))+'</button>'+
   '<button class="ghost small" data-action="refreshProviderCatalog" data-provider="'+esc(p.name)+'">'+esc(T('refreshCatalog'))+'</button>'+
   '<button class="ghost small" data-action="providerEdit" data-provider="'+esc(p.name)+'">'+esc(T('edit'))+'</button>'+
   '</div><div id="prov-detail-msg"></div>'+
   '<h4>'+esc(T('models'))+'</h4>'+
   tbl([T('model'),'provider model',T('priceIn'),T('priceOut'),T('discount'),T('context'),'eligible',T('lastCheck')], provRows));
}

function apiErrShort(r){ return r.status+' '+JSON.stringify(r.data&&r.data.detail||'').slice(0,140); }

// ── provider add/edit modal (§I, blocker H fix verified here) ────────────
function provAddForm(adapters, existing){
  const isEdit = !!existing;
  const a = existing ? existing.adapter_type : '';
  const sel = '<select id="pf-adapter">'+
    (adapters||['provider_a','provider_b','openai-compatible']).map(t=>'<option value="'+esc(t)+'"'+(t===a?' selected':'')+'>'+esc(t)+'</option>').join('')+
    '</select>';
  const sw = (id, on) => '<label class="switch"><input type="checkbox" id="'+id+'"'+(on?' checked':'')+'><i></i></label>';
  openModal(
   '<div class="row" style="justify-content:space-between"><h3 style="margin:0">'+esc(isEdit?('Edit: '+existing.name):T('addProvider'))+'</h3>'+
   '<button class="ghost small" data-action="closeModal">✕</button></div>'+
   '<label class="fld"><span>'+esc(T('name'))+'</span><input id="pf-name" value="'+esc(existing?existing.name:'')+'"'+(isEdit?' disabled':'')+'></label>'+
   '<label class="fld"><span>'+esc(T('providerId'))+'</span><input id="pf-id" value="'+esc(existing?existing.name:'')+'" placeholder="provider_a"'+(isEdit?' disabled':'')+'></label>'+
   '<label class="fld"><span>'+esc(T('adapter'))+'</span>'+sel+'</label>'+
   '<label class="fld"><span>'+esc(T('baseUrl'))+'</span><input id="pf-baseurl" value="'+esc(existing?existing.base_url||'':'')+'" placeholder="https://api.example.com/v1"></label>'+
   '<label class="fld"><span>'+esc(T('secretEnv'))+'</span><input id="pf-secret" value="'+esc(existing?existing.secret_ref||'':'')+'" placeholder="MY_PROVIDER_API_KEY"></label>'+
   '<label class="fld chk"><input type="checkbox" id="pf-enabled"'+(isEdit?(existing.enabled?' checked':''):' checked')+'> <span>'+esc(T('enabled'))+'</span></label>'+
   '<div class="row" style="margin-top:12px">'+
   '<button id="pf-test" class="ghost">'+esc(T('testConnection'))+'</button>'+
   (isEdit?'<button id="pf-save">'+esc(T('save'))+'</button>'
          :'<button id="pf-save">'+esc(T('saveDraft'))+'</button>')+
   '<button class="ghost" data-action="closeModal">'+esc(T('cancel'))+'</button>'+
   '</div><div id="pf-test-res" style="margin-top:10px"></div>');
  $('pf-test').onclick = async ()=>{
    $('pf-test-res').innerHTML = '<span class="spin"></span> '+esc(T('probing'))+'…';
    const r = await api('/admin/providers/test-connection', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({base_url: $('pf-baseurl').value.trim(), secret_ref: $('pf-secret').value.trim()})});
    if(!r.ok){ $('pf-test-res').innerHTML = apiErr(r, T('testConnection')); return; }
    const d = r.data;
    $('pf-test-res').innerHTML =
      '<div class="row" style="gap:6px">'+
      (d.api_reachable?badge(T('apiReachable'),'ok'):badge(T('apiReachable'),'bad'))+
      (d.auth_ok?badge(T('authOk'),'ok'):badge(T('authOk'),'bad'))+
      (d.models_endpoint_ok?badge(T('modelsEndpointOk'),'ok'):badge(T('modelsEndpointOk'),'bad'))+
      '</div>'+
      '<div class="muted" style="margin-top:6px">'+
      esc(T('modelsFound'))+': <b>'+d.models_found+'</b> · '+
      esc(T('pricingFound'))+': <b>'+esc(d.pricing_found)+'</b> · '+
      esc(T('latency'))+': <b>'+d.latency_ms+' мс</b>'+
      (d.error?' · '+esc(d.error):'')+'</div>';
  };
  $('pf-save').onclick = async ()=>{
    const body = {name: ($('pf-id').value.trim()||$('pf-name').value.trim()),
      display_name: $('pf-name').value.trim(),
      adapter_type: $('pf-adapter').value,
      base_url: $('pf-baseurl').value.trim(),
      secret_ref: $('pf-secret').value.trim(),
      enabled: $('pf-enabled').checked};
    const r = await api('/admin/providers', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if(!r.ok){ $('pf-test-res').innerHTML = apiErr(r, T('save')); return; }
    closeModal(); toast(isEdit?T('saved'):T('draftSaved'),'ok'); window.__render();
  };
}

async function provEdit(name){
  const r = await api('/admin/providers');
  if(!r.ok){ toast(apiErrShort(r),'bad'); return; }
  const list = Object.values((r.data||{}).providers||{});
  const p = list.find(x=>x.name===name);
  if(!p){ toast('provider not found','bad'); return; }
  const ad = await api('/admin/adapters');
  provAddForm((ad.data&&ad.data.types)||[], p);
}

// ── refresh actions (§K) ─────────────────────────────────────────────────
function diffReport(d){
  return esc(T('modelsFound'))+': <b>'+d.models_found+'</b> · новых: <b>'+d.new+'</b> · исчезло: <b>'+d.disappeared+'</b> · '+
    esc('цены изменились')+': <b>'+d.price_changed+'</b> · '+esc('неизвестных цен')+': <b>'+d.unknown_prices+'</b> · '+
    esc('ошибки')+': <b>'+d.errors+'</b> · '+dt(d.updated_at);
}

async function refreshAllCatalogs(){
  const el = $('prov-refresh-res'); if(el) el.innerHTML = '<span class="spin"></span> '+esc(T('probing'))+'…';
  const r = await api('/admin/catalog/refresh', {method:'POST'});
  if(!r.ok){ if(el) el.innerHTML=''; toast(apiErrShort(r),'bad'); return; }
  if(el) el.innerHTML = diffReport(r.data.diff||{});
  toast(T('refreshCatalog')+' OK','ok');
}
async function refreshProviderCatalog(name){
  const r = await api('/admin/catalog/refresh/'+encodeURIComponent(name), {method:'POST'});
  if(!r.ok){ toast(apiErrShort(r),'bad'); return; }
  const msg = $('prov-detail-msg');
  if(msg) msg.innerHTML = '<div class="muted" style="margin:8px 0">'+diffReport(r.data.diff||{})+'</div>';
  toast(T('refreshCatalog')+' '+name+' OK','ok');
}
async function probeProvider(name){
  const msg = $('prov-detail-msg') || {innerHTML:''};
  const prev = msg.innerHTML; msg.innerHTML = '<span class="spin"></span> '+esc(T('probing'))+'…';
  const r = await api('/admin/probe/provider/'+encodeURIComponent(name), {method:'POST'});
  if(!r.ok){ msg.innerHTML = prev; toast(apiErrShort(r),'bad'); return; }
  msg.innerHTML = prev + '<div class="muted" style="margin:8px 0">'+esc(T('progress'))+': '+r.data.ok+' / '+r.data.total+' OK</div>';
  toast(name+': '+r.data.ok+'/'+r.data.total+' OK', r.data.ok?'ok':'warn');
}

Object.assign(globalThis.MR_ACTIONS, globalThis.MR_ACTIONS||{}, {
  providerAdd: el=>provAddForm((el.dataset.adapters||'').split(',').filter(Boolean), null),
  providerEdit: el=>provEdit(el.dataset.provider),
  provDetail: el=>provDetail(el.dataset.provider),
  probeProvider: el=>probeProvider(el.dataset.provider),
  refreshProviderCatalog: el=>refreshProviderCatalog(el.dataset.provider),
  refreshAllCatalogs: ()=>refreshAllCatalogs(),
});
