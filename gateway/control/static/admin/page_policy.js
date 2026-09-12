// R14: Router Control Center pages — Routing Policy (визуализатор + policy +
// распределение + последние решения), Task/Tier Policy (классы, тиры,
// качество, редактор), Overrides («Временные правила», wizard), Simulator
// (человеческий результат + кандидаты + trace), Audit / Revisions
// (человеческий журнал + ревизии в «Для разработчика»).
import { T } from './i18n.js';
import {
  RPAGES, POSTRENDER, ACTIONS, api, apiErr, toast, tbl, badge, usdFmt, ms, ago, dt, dtHMS, dtHM,
  UIS, esc, $, render, openDrawer, closeDrawer, openModal, closeModal, confirmModal,
} from './core.js';

const H = (s) => esc(s);
const time = () => Math.floor(Date.now() / 1000);
const kindRU = {
  FORCE_CANONICAL: 'Всегда использовать модель',
  FORCE_ROUTE: 'Зафиксировать маршрут',
  DISABLE_PROVIDER: 'Отключить провайдера',
  DISABLE_MODEL: 'Отключить модель',
  DISABLE_ROUTE: 'Отключить маршрут',
  PREFER_PROVIDER: 'Предпочитать провайдера',
};
const kindHint = {
  FORCE_CANONICAL: 'Router всегда выбирает указанную модель, пока правило действует. Применять для теста конкретной модели.',
  FORCE_ROUTE: 'Выбор жёстко зафиксирован на маршруте провайдер:модель. Только для диагностики одного маршрута.',
  DISABLE_PROVIDER: 'Все маршруты провайдера исключаются из выбора на время правила. Применять при сбоях провайдера.',
  DISABLE_MODEL: 'Модель исключается из выбора на время правила. Применять при плохом качестве/доступности модели.',
  DISABLE_ROUTE: 'Один маршрут исключается из выбора. Применять при сбоях конкретного маршрута.',
  PREFER_PROVIDER: 'Провайдер становится предпочтительным (не жёстким) выбором при прочих равных.',
};

// ═══════════ ROUTING POLICY (§9–§13, §38, §41) ═══════════════════════════

RPAGES['Routing Policy'] = async () => {
  const [pipe, eff, dist, dec, eco] = await Promise.all([
    api('/admin/routing/pipeline'), api('/admin/routing/effective'),
    api('/admin/routing/distribution'), api('/admin/routing/decisions?limit=25'),
    api('/admin/insights/economics'),
  ]);
  if (!pipe.ok) return apiErr(pipe, 'routing/pipeline');
  let html = '';
  // R14 §17: активный черновик виден там, где его создали
  if (window._r14_draft) {
    html += '<div class="card" style="margin:8px 0;padding:10px"><b>' + H('Есть черновик политики: ') + '<code>' + H(window._r14_draft) + '</code></b> ' +
      '<button class="small" data-action="draftFlow" data-revision="' + H(window._r14_draft) + '">' + H('Проверить и применить') + '</button> ' +
      '<button class="ghost small" data-action="draftDrop">' + H('Отменить черновик') + '</button></div>';
  }

  // §9: визуальная схема (кликабельные блоки)
  const stages = (pipe.data.stages || []);
  html += '<h2>' + H('Как Router принимает решение') + '</h2>' +
    '<div class="pipeline">' + stages.map((s, i) =>
      '<div class="stage" data-action="pipeStage" data-i="' + i + '" title="' + H(s.detail || '') + '">' +
      '<span class="n">' + (i + 1) + '</span><span class="t">' + H(s.ru || s.title) + '</span></div>' +
      (i < stages.length - 1 ? '<span class="arrow">↓</span>' : '')).join('') + '</div>' +
    '<details class="raw"><summary>' + H(T('forDeveloper')) + ' — pipeline JSON</summary><pre>' +
    esc(JSON.stringify(pipe.data, null, 2)) + '</pre></details>';

  // §10/§39: текущая политика (значение/описание/источник/изменить)
  html += '<h2>' + H('Текущая политика') + '</h2>';
  if (eff.ok) {
    const rows = (eff.data.params || []).map(p => [
      H(p.ru || p.key),
      '<b>' + H(p.value_human || p.value) + '</b>',
      '<span class="muted small">' + H(p.hint || '') + '</span>',
      p.source === 'override' ? badge('переопределено', 'warn') : badge(p.source_ru || p.source, 'mut'),
      p.configurable === false ? '<span class="muted small">' + H('внутренний') + '</span>'
        : '<button class="ghost small" data-action="editParam" data-k="' + H(p.key) + '" data-v="' + H(p.value) + '">' + H('Изменить') + '</button>',
    ]);
    html += tbl(['Параметр', 'Значение', 'Описание', 'Источник', ''], rows) +
      '<p class="muted small">' + H('Порядок применения: сначала жёсткие фильтры (возможности → качество → контекст → доступность → скидка), затем ранжирование выживших (надёжность → экономика кэша → цена → задержка). Провайдер никогда не является фактором приоритета — Provider A и Provider B конкурируют на равных.') + '</p>';
  } else html += apiErr(eff, 'routing/effective');

  // §12: фактическое распределение (честный sample count)
  html += '<h2>' + H('Фактическое распределение запросов') + '</h2>';
  if (dist.ok) {
    const d = dist.data;
    if (!d.sample_count) {
      html += '<div class="empty">' + H('Пока нет журналируемых запросов за ' + d.hours + ' ч. Журналирование решений включено с версии R14 — статистика появится после первых запросов.') + '</div>';
    } else {
      const bar = (o) => '<div class="distrow"><span class="lbl">' + H(o.label) + '</span>' +
        '<span class="bar"><i style="width:' + Math.min(100, o.pct) + '%"></i></span>' +
        '<span class="val">' + o.requests + ' · ' + o.pct.toFixed(1) + '%</span></div>';
      html += '<h4>' + H('По уровню качества (Tier)') + '</h4>' +
        Object.entries(d.by_tier || {}).map(([t, v]) => bar({ label: t, ...v })).join('') +
        '<h4>' + H('По классу задачи') + '</h4>' +
        Object.entries(d.by_class || {}).map(([c, v]) => bar({ label: c, ...v })).join('') +
        '<h4>' + H('По модели') + '</h4>' +
        Object.entries(d.by_model || {}).map(([m, v]) => bar({ label: m, ...v })).join('') +
        '<h4>' + H('По провайдеру') + '</h4>' +
        Object.entries(d.by_provider || {}).map(([p, v]) => bar({ label: p, ...v })).join('') +
        '<p class="muted small">' + H('Выборка: ' + d.sample_count + ' запросов за ' + d.hours + ' ч. Это фактическое распределение, а не политика: Router выбирает по экономике и качеству, фиксированных долей провайдеров нет.') + '</p>';
    }
  } else html += apiErr(dist, 'routing/distribution');

  // §13: последние решения
  html += '<h2>' + H('Последние решения Router') + '</h2>';
  if (dec.ok) {
    const list = dec.data.decisions || [];
    if (!list.length) html += '<div class="empty">' + H('Журнал решений пуст. Журналирование включено с версии R14.') + '</div>';
    else {
      const rows = list.map(d => [
        H(dtHMS(d.ts)), H(d.task_class || '—'), H(d.tier || '—'),
        '<b>' + H(d.canonical || '—') + '</b>', H(d.provider || '—'),
        d.cost_usd != null ? usdFmt(d.cost_usd) : '<span class="muted">—</span>',
        d.latency_ms != null ? ms(d.latency_ms) : '<span class="muted">—</span>',
        '<span class="muted small">' + H(d.reason_ru || d.reason || '') + '</span>',
        '<button class="ghost small" data-action="decTrace" data-id="' + H(d.id) + '">' + H('Трейс') + '</button>',
      ]);
      html += tbl(['Время', 'Класс', 'Тир', 'Модель', 'Провайдер', 'Цена', 'Задержка', 'Причина', ''], rows) +
        '<p class="muted small">' + H('Без текста запросов — privacy-safe журнал (класс, маршрут, причина).') + '</p>';
    }
  } else html += apiErr(dec, 'routing/decisions');

  // Economics
  html += '<h2>' + H('Экономика') + '</h2>';
  if (eco.ok) {
    const a = eco.data.anomalies || [];
    html += a.length ? '<ul class="attention">' + a.map(x => '<li>' + H(x.note || JSON.stringify(x)) + '</li>').join('') + '</ul>'
      : '<div class="empty">' + H('Аномалий ценообразования не обнаружено.') + '</div>';
  } else html += apiErr(eco, 'insights/economics');
  return html;
};

ACTIONS.pipeStage = (el) => {
  // pipeline block detail — берём из кэша последнего рендера
  const t = el.title || el.getAttribute('title') || '';
  openModal('<h3>' + H(el.querySelector('.t').textContent) + '</h3><p class="small">' + H(t) + '</p>' +
    '<div class="row" style="justify-content:flex-end;margin-top:10px"><button class="ghost" data-action="closeModal">' + H(T('close')) + '</button></div>');
};

ACTIONS.decTrace = async (el) => {
  const r = await api('/admin/routing/decisions/' + encodeURIComponent(el.dataset.id));
  if (!r.ok) { toast(T('httpErr') + ' ' + r.status, 'bad'); return; }
  const d = r.data;
  openDrawer('<div class="drawer-head"><h3>' + H('Решение ' + dtHMS(d.ts)) + '</h3><span style="margin-left:auto"></span>' +
    '<button class="ghost small" data-action="closeDrawer">✕</button></div>' +
    '<dl class="kv">' +
    '<dt>' + H('Класс задачи') + '</dt><dd>' + H(d.task_class || '—') + ' → ' + H(d.tier || '—') + '</dd>' +
    '<dt>' + H('Выбрано') + '</dt><dd><b>' + H(d.canonical || '—') + '</b> · ' + H(d.provider || '—') + ' · <code>' + H(d.provider_model_id || '') + '</code></dd>' +
    '<dt>' + H('Причина') + '</dt><dd>' + H(d.reason_ru || d.reason || '') + '</dd>' +
    '</dl>' +
    (UIS.diag() || UIS.showRaw() ? '<details class="raw" open><summary>' + H(T('forDeveloper')) + '</summary><pre>' + esc(JSON.stringify(d, null, 2)) + '</pre></details>' : ''),
    'dectrace=' + encodeURIComponent(el.dataset.id));
};

// §10: изменение одного параметра политики → DRAFT → replay impact → Apply
ACTIONS.editParam = (el) => {
  const key = el.dataset.k, cur = el.dataset.v;
  openModal('<h3>' + H('Изменить параметр') + '</h3>' +
    '<p class="muted small">' + H('Создаётся черновик ревизии. Перед применением будет показана оценка влияния на последние решения (replay).') + '</p>' +
    '<label class="fld"><span><code>' + H(key) + '</code> — новое значение</span><input id="ep-val" value="' + H(cur) + '" class="mono"></label>' +
    '<div class="row" style="justify-content:flex-end;margin-top:10px">' +
    '<button class="ghost" data-action="closeModal">' + H(T('cancel')) + '</button>' +
    '<button id="ep-save">' + H('Создать черновик') + '</button></div>');
  $('ep-save').onclick = async () => {
    let v = $('ep-val').value.trim();
    if (v === 'true' || v === 'false') v = v === 'true';
    else if (!isNaN(Number(v)) && v !== '') v = Number(v);
    const r = await api('/admin/revisions', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'routing policy: ' + key + ' (admin UI)', patch: { [key]: v } }) });
    if (!r.ok) { toast(T('httpErr') + ' ' + r.status, 'bad'); return; }
    closeModal();
    window._r14_draft = r.data.revision.revision_id;
    render();
  };
};

// ═══════════ TASK / TIER POLICY (§14–§17) ════════════════════════════════

RPAGES['Task/Tier Policy'] = async () => {
  const [tc, qm] = await Promise.all([api('/admin/taCHANGE_ME'), api('/admin/quality/models')]);
  if (!tc.ok) return apiErr(tc, 'taCHANGE_ME');
  const d = tc.data;
  let html = '<div class="row" style="margin-bottom:10px"><button data-action="tcEdit">' + H('Изменить классы и уровни') + '</button>' +
    '<span class="muted small">' + H('Изменения проходят черновик → проверка → оценка влияния → применение.') + '</span></div>';

  // §14: классы задач (реальная классификация)
  const rows = (d.classes || []).map(c => [
    '<b>' + H(c.ru || c.class) + (c.enabled === false ? ' ' + badge('отключён', 'mut') : '') + '</b>' +
    (UIS.diag() ? '<br><code class="muted small">' + H(c.class) + '</code>' : ''),
    '<span class="small">' + H(c.description || '') + '</span>',
    c.source === 'config' ? badge(c.tier + ' ' + '(изменён)', 'warn') : badge(c.tier, 'acc'),
    H(c.min_quality != null ? c.min_quality : '—'),
    '<span class="muted small">' + H(c.capabilities && c.capabilities.join(', ') || 'text, streaming') + '</span>',
    c.requests_24h != null ? (c.requests_24h + ' · ' + (c.pct_24h || 0).toFixed(1) + '%') : '<span class="muted">—</span>',
  ]);
  html += '<h2>' + H('Классы задач') + '</h2>' +
    tbl(['Класс', 'Описание', 'Уровень', 'Мин. качество', 'Возможности', 'Запросов 24ч'], rows) +
    '<p class="muted small">' + H('Класс определяется детерминированным классификатором по тексту задачи (текст не сохраняется). Отключение класса означает обработку таких задач как простых (T1), а не отказ.') + '</p>';

  // §15: тиры
  html += '<h2>' + H('Уровни качества (Tier)') + '</h2><div class="cards">';
  for (const t of (d.tiers || [])) {
    html += '<div class="card"><div class="t">' + H(t.tier) + ' — ' + H(t.title) + '</div>' +
      '<div class="v" style="font-size:14px">' + H(t.what) + '</div>' +
      '<div class="s">' + H('Когда: ' + t.when) + '</div>' +
      '<div class="s">' + H('Мин. качество: ' + (t.quality_floor != null ? t.quality_floor : '—')) + '</div>' +
      '<div class="s">' + H('Кандидаты: ' + (t.candidates || []).slice(0, 5).join(', ') + (t.candidates && t.candidates.length > 5 ? ' …' : '')) + '</div></div>';
  }
  html += '</div><p class="muted small">' + H('Уровень определяет минимальное качество модели (quality floor). Кандидаты — модели, чей подтверждённый результат не ниже порога уровня.') + '</p>';

  // §16: качество моделей
  html += '<h2>' + H('Качество моделей') + '</h2>';
  if (qm.ok) {
    const rows = (qm.data.models || []).map(m => [
      '<b>' + H(m.canonical) + '</b>',
      m.confidence === 'VERIFIED' ? badge(m.confidence_ru, 'ok')
        : m.confidence === 'PROVISIONAL' ? badge(m.confidence_ru, 'acc')
        : m.confidence === 'INCOMPLETE' ? badge(m.confidence_ru, 'warn') : badge(m.confidence_ru, 'mut'),
      m.quality_score != null ? m.quality_score : '<span class="muted">—</span>',
      m.tier_eligibility ? badge(m.tier_eligibility, 'acc') : '<span class="muted small">' + H('не для T3/T4') + '</span>',
      '<span class="muted small">' + H(m.why || '') + '</span>',
    ]);
    html += tbl(['Модель', 'Статус', 'Результат', 'Допуск', 'Почему'], rows) +
      '<p class="muted small">' + H('Проверенное — калиброванный бенчмарк с полным покрытием. Предварительное — есть пробелы. Не оценено — нет калиброванных данных (не значит «слабая»: frontier-модели без бенчмарка допускаются только при явной политике).') + '</p>';
  } else html += apiErr(qm, 'quality/models');
  return html;
};

// §17: редактор классов → DRAFT → VALIDATE → IMPACT → APPLY
let TCE = null; // taCHANGE_ME editor state
ACTIONS.tcEdit = async () => {
  const r = await api('/admin/taCHANGE_ME');
  if (!r.ok) { toast(T('httpErr'), 'bad'); return; }
  TCE = { classes: (r.data.classes || []).map(c => ({ class: c.class, tier: c.tier, enabled: c.enabled !== false })) };
  renderTcEditor();
};
function renderTcEditor() {
  const tierSel = (c) => '<select data-tc="' + H(c.class) + '"><option' + (c.tier === 'T1' ? ' selected' : '') + '>T1</option><option' + (c.tier === 'T2' ? ' selected' : '') + '>T2</option><option' + (c.tier === 'T3' ? ' selected' : '') + '>T3</option><option' + (c.tier === 'T4' ? ' selected' : '') + '>T4</option></select>';
  const rows = TCE.classes.map(c => [
    '<b>' + H(c.class) + '</b>',
    '<label class="chk"><input type="checkbox" data-tce="' + H(c.class) + '"' + (c.enabled ? ' checked' : '') + '></label>',
    tierSel(c),
  ]);
  openDrawer('<div class="drawer-head"><h3>' + H('Классы и уровни задач') + '</h3><span style="margin-left:auto"></span>' +
    '<button class="ghost small" data-action="closeDrawer">✕</button></div>' +
    '<p class="muted small">' + H('Изменения создают черновик ревизии. Перед применением покажем оценку влияния на последние решения.') + '</p>' +
    tbl(['Класс', 'Включён', 'Уровень'], rows) +
    '<div class="row" style="margin-top:12px"><button id="tce-save">' + H('Создать черновик') + '</button>' +
    '<button class="ghost" data-action="closeDrawer">' + H(T('cancel')) + '</button></div>', 'tcedit=1');
  $('tce-save').onclick = async () => {
    const patch = {};
    for (const c of TCE.classes) {
      const en = document.querySelector('[data-tce="' + CSS.escape(c.class) + '"]');
      const sel = document.querySelector('[data-tc="' + CSS.escape(c.class) + '"]');
      patch[c.class] = { enabled: en ? en.checked : c.enabled, tier: sel ? sel.value : c.tier };
    }
    const r = await api('/admin/revisions', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'task/tier policy edit (admin UI)', patch: { task_classes: patch } }) });
    if (!r.ok) { toast(T('httpErr') + ' ' + r.status, 'bad'); return; }
    closeDrawer();
    window._r14_draft = r.data.revision.revision_id;
    toast(T('draftSaved') + ' — ' + r.data.revision.revision_id, 'ok');
    render();
  };
}

// ═══════════ OVERRIDES → «Временные правила» (§19–§21) ═══════════════════

RPAGES['Overrides'] = async () => {
  const r = await api('/admin/overrides');
  if (!r.ok) return apiErr(r, 'overrides');
  const list = (r.data.overrides || []).filter(o => o.enabled);
  const rows = list.map(o => [
    '<b>' + H(kindRU[o.kind] || o.kind) + '</b>' +
    (UIS.diag() ? '<br><code class="muted small">' + H(o.kind) + '</code>' : ''),
    '<code class="small">' + H(o.target) + '</code>',
    o.expires_at ? H('до ' + dtHM(o.expires_at) + ' (' + ago(o.expires_at) + ')')
      : badge('без срока', 'warn'),
    '<span class="muted small">' + H(o.reason || '') + '</span>',
    '<button class="danger small" data-action="overrideDelete" data-override="' + H(o.override_id) + '">' + H(T('delete')) + '</button>',
  ]);
  return '<h2>' + H('Временные правила') + '</h2>' +
    '<p class="muted">' + H('Временное изменение поведения Router без изменения основной политики. Не нужно после обычного добавления модели: добавление модели в пул — это не правило, а часть конфигурации.') + '</p>' +
    '<div class="row" style="margin-bottom:10px"><button data-action="ovWizard">' + H('+ Добавить временное правило') + '</button></div>' +
    (rows.length ? tbl(['Что делает', 'Цель', 'Срок', 'Причина', ''], rows)
      : '<div class="empty">' + H('Временных правил нет — Router работает по основной политике.') + '</div>');
};

ACTIONS.overrideDelete = (el) => {
  confirmModal(T('delete') + '?', H('Правило будет отключено немедленно. Основная политика не меняется.'), async () => {
    const r = await api('/admin/overrides/' + encodeURIComponent(el.dataset.override), { method: 'DELETE' });
    if (r.ok) { toast(T('saved'), 'ok'); render(); }
    else toast(T('httpErr') + ' ' + r.status, 'bad');
  });
};

// §21: wizard временного правила
let OVW = null;
ACTIONS.ovWizard = () => {
  OVW = { step: 1, kind: 'DISABLE_MODEL', target: '', ttl: 3600, reason: '' };
  renderOvWizard();
};
function renderOvWizard() {
  if (OVW.step === 1) {
    const opts = [
      ['FORCE_CANONICAL', 'Временно использовать только определённую модель'],
      ['DISABLE_MODEL', 'Отключить проблемную модель'],
      ['DISABLE_PROVIDER', 'Отключить провайдера'],
      ['PREFER_PROVIDER', 'Предпочитать провайдера'],
      ['FORCE_ROUTE', 'Зафиксировать конкретный маршрут'],
    ];
    openDrawer('<div class="drawer-head"><h3>' + H('Новое временное правило') + '</h3><span style="margin-left:auto"></span>' +
      '<button class="ghost small" data-action="closeDrawer">✕</button></div>' +
      '<p>' + H('Что хотите сделать?') + '</p>' +
      opts.map(([k, lbl]) => '<label class="chk" style="margin:10px 0"><input type="radio" name="ovw-kind" value="' + k + '"' + (OVW.kind === k ? ' checked' : '') + '> <span>' + H(lbl) + '</span></label>').join('') +
      '<p class="muted small">' + H(kindHint[OVW.kind] || '') + '</p>' +
      '<div class="row" style="margin-top:12px"><button id="ovw-next">' + H(T('next')) + ' →</button></div>', 'ovw=1');
    $('ovw-next').onclick = () => {
      const sel = document.querySelector('input[name=ovw-kind]:checked');
      if (sel) OVW.kind = sel.value;
      OVW.step = 2; renderOvWizard();
    };
  } else {
    const targetPh = OVW.kind.includes('PROVIDER') ? 'provider_b' : (OVW.kind === 'FORCE_ROUTE' ? 'provider_a:cb/glm-5.3' : 'gpt-5.6-luna');
    openDrawer('<div class="drawer-head"><h3>' + H('Новое временное правило') + '</h3><span style="margin-left:auto"></span>' +
      '<button class="ghost small" data-action="closeDrawer">✕</button></div>' +
      '<dl class="kv"><dt>' + H('Действие') + '</dt><dd>' + H(kindRU[OVW.kind]) + '</dd></dl>' +
      '<label class="fld"><span>' + H('Цель (модель / провайдер / маршрут)') + '</span><input id="ovw-target" placeholder="' + H(targetPh) + '" class="mono"></label>' +
      '<label class="fld"><span>' + H('Срок действия (TTL)') + '</span><select id="ovw-ttl">' +
      [[900, '15 минут'], [3600, '1 час'], [21600, '6 часов'], [86400, '24 часа'], [0, 'Без срока (постоянное)']].map(([v, l]) =>
        '<option value="' + v + '"' + (OVW.ttl === v ? ' selected' : '') + '>' + H(l) + '</option>').join('') + '</select></label>' +
      '<label class="fld"><span>' + H('Причина') + '</span><input id="ovw-reason" placeholder="' + H('например: сбой продавца на Provider B') + '"></label>' +
      '<p class="muted small">' + H((kindHint[OVW.kind] || '') + ' Правило действует сразу после создания и автоматически истекает по TTL.') + '</p>' +
      '<div class="row" style="margin-top:12px"><button class="ghost" data-action="ovwBack">← ' + H(T('back')) + '</button>' +
      '<button id="ovw-save">' + H('Создать правило') + '</button></div>', 'ovw=2');
    $('ovw-back') || null;
    document.querySelector('[data-action=ovwBack]').onclick = () => { OVW.step = 1; renderOvWizard(); };
    $('ovw-save').onclick = async () => {
      OVW.target = $('ovw-target').value.trim();
      OVW.ttl = parseInt($('ovw-ttl').value) || 0;
      OVW.reason = $('ovw-reason').value.trim();
      if (!OVW.target) { $('ovw-target').classList.add('invalid'); return; }
      const body = { kind: OVW.kind, target: OVW.target, reason: OVW.reason || 'admin UI' };
      if (OVW.ttl > 0) body.ttl_s = OVW.ttl; else body.persistent = true;
      const r = await api('/admin/overrides', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (!r.ok) { toast(T('httpErr') + ' ' + r.status, 'bad'); return; }
      closeDrawer(); toast(H('Правило создано') + (OVW.ttl > 0 ? ' — ' + H('истекает ' + dtHM(time() + OVW.ttl)) : ''), 'ok'); render();
    };
  }
}

// ═══════════ SIMULATOR (§22–§24) ═════════════════════════════════════════

RPAGES['Simulator'] = async () => {
  const tc = await api('/admin/taCHANGE_ME');
  const classes = (tc.ok && tc.data.classes) || [];
  return '<h2>' + H('Проверить, что выберет Router') + '</h2>' +
    '<p class="muted small">' + H('Симуляция выполняется офлайн на текущем состоянии реестра: без запросов к провайдерам и без сохранения текста.') + '</p>' +
    '<div class="row">' +
    '<label class="fld"><span>' + H('Тип задачи (класс)') + '</span><select id="sim-class">' +
    classes.map(c => '<option value="' + H(c.class) + '"' + (c.class === 'NORMAL_CODING' ? ' selected' : '') + '>' + H(c.ru || c.class) + ' (' + H(c.class) + ')</option>').join('') + '</select></label>' +
    '<label class="fld"><span>' + H('Входные токены') + '</span><input id="sim-tokens" type="number" value="50000" style="width:120px"></label>' +
    '<label class="fld"><span>' + H('Требуемые возможности') + '</span><select id="sim-caps"><option value="text,streaming">текст + стриминг</option><option value="text,streaming,tool_call">текст + стриминг + вызов инструментов</option></select></label>' +
    '</div>' +
    '<div class="row" style="margin-top:8px"><button id="sim-run">' + H('Смоделировать выбор') + '</button></div>' +
    '<div id="sim-res" style="margin-top:12px"></div>';
};

POSTRENDER['Simulator'] = () => {
  $('sim-run').onclick = async () => {
    $('sim-res').innerHTML = '<span class="spin"></span>';
    const caps = $('sim-caps').value.split(',');
    const r = await api('/admin/simulate', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_class: $('sim-class').value, context_tokens: parseInt($('sim-tokens').value) || 0, capabilities: caps }) });
    if (!r.ok) { $('sim-res').innerHTML = apiErr(r, 'simulate'); return; }
    const d = r.data;
    const w = d.winner;
    // верхняя карточка победителя
    let html = '<div class="card" style="border-color:var(--ok)"><div class="t">' + H('Router выбрал') + '</div>' +
      '<div class="v">' + (w ? H(w.canonical) : H('ничего — нет допустимого маршрута')) + '</div>' +
      (w ? '<div class="s">' + H('Провайдер: ') + '<b>' + H(w.provider) + '</b> · ' + H('Маршрут: ') + '<code class="small">' + H(w.provider_model_id) + '</code></div>' +
        '<div class="s">' + H('Причина: ') + H(w.reason_ru || w.reason || '') + '</div>' : '') +
      '</div>';
    // кандидаты: eligible + rejected
    const cand = (d.canonical_candidates || []).map(c => (typeof c === 'string' ? { canonical: c } : c));
    const rejC = (d.rejected_canonicals || []);
    const gates = (d.hard_gates || []);
    const candRows = [
      ...cand.map(c => {
        const rr = (d.plan || []).find(p => p.canonical === (c.canonical || c)) || {};
        return ['<b>' + H(c.canonical || c) + '</b>',
          rr.provider ? H(rr.provider) : '—',
          badge('Допущена', 'ok'),
          '<span class="muted small">' + H((w && w.canonical === (c.canonical || c)) ? 'выбрана' : (rr.reason_ru || rr.reason || 'допущена, но дороже/хуже выбранной')) + '</span>'];
      }),
      ...rejC.map(c => ['<b>' + H(c.canonical) + '</b>', '—', badge('Нет', 'bad'),
        '<span class="muted small">' + H(c.reason_ru || c.reason || c.code || '') + '</span>']),
    ];
    html += '<h4>' + H('Кандидаты') + '</h4>' +
      (candRows.length ? tbl(['Модель', 'Провайдер', 'Допущена?', 'Причина'], candRows)
        : '<div class="empty">' + H('Нет кандидатов') + '</div>');
    if (gates.length && UIS.diag()) {
      html += '<h4>' + H('Отклонённые маршруты (gates)') + '</h4>' +
        tbl(['Маршрут', 'Причина'], gates.map(g => ['<code class="small">' + H(g.route) + '</code>', H(g.reason_ru || g.reason || '')]));
    }
    // §24: decision trace по шагам
    html += '<details class="raw"><summary>' + H('Пошаговый разбор решения') + '</summary><ol class="small">' +
      '<li>' + H('Кандидаты по качеству и возможностям: ' + cand.length) + '</li>' +
      '<li>' + H('Отклонено на этапе качества: ' + rejC.length) + '</li>' +
      '<li>' + H('Отклонено маршрутных фильтров (скидка/контекст/доступность): ' + gates.length) + '</li>' +
      '<li>' + H('Экономика кэша: ' + ((d.cache_decision || {}).reason_code || '—')) + '</li>' +
      '<li>' + H('Выбрано: ' + (w ? w.canonical + ' @ ' + w.provider : 'ничего')) + '</li>' +
      '</ol></details>';
    if (UIS.diag() || UIS.showRaw()) html += '<details class="raw"><summary>' + H('Raw JSON (' + H(T('forDeveloper')) + ')') + '</summary><pre>' + esc(JSON.stringify(d, null, 2)) + '</pre></details>';
    $('sim-res').innerHTML = html;
  };
};

// ═══════════ AUDIT / REVISIONS (§25–§27, §43) ════════════════════════════

RPAGES['Audit / Revisions'] = async () => {
  const [au, rv] = await Promise.all([api('/admin/audit?limit=200'), api('/admin/revisions')]);
  if (!au.ok) return apiErr(au, 'audit');
  const entries = (au.data.audit || []);
  const rows = entries.map(a => [
    H(a.iso || dtHM(a.ts)),
    '<span class="small">' + H(a.text) + '</span>',
    '<code class="small">' + H(a.entity || a.action) + '</code>',
    H(a.actor),
    a.status_ru ? badge(a.status_ru, 'ok') : badge('Применено', 'ok'),
    '<button class="ghost small" data-action="auDetail" data-i="' + entries.indexOf(a) + '">' + H('Подробнее') + '</button>',
  ]);
  let html = '<h2>' + H('История изменений') + '</h2>' +
    (rows.length ? tbl(['Дата', 'Изменение', 'Объект', 'Автор', 'Статус', ''], rows)
      : '<div class="empty">' + H('Журнал пуст. Все административные действия (модели, сопоставления, политика, провайдеры, обновления, точки восстановления, временные правила) записываются с версии R12; решения маршрутизации — с R14.') + '</div>') +
    '<p class="muted small">' + H('Журналирование всех административных действий включено с версии R14 (частично — с R12). Ранние действия модели/политики, выполненные до этого, восстановить из журнала невозможно.') + '</p>';
  // ревизии — «Для разработчика»
  html += '<details class="raw"><summary>' + H(T('forDeveloper')) + ' — ревизии конфигурации</summary>';
  if (rv.ok) {
    const rr = ((rv.data.revisions) || []).map(r => [
      '<code>' + H(r.revision_id.slice(0, 12)) + '</code>', H(dt(r.created_at)), H(r.actor), H(r.reason),
      r.status === 'APPLIED' ? badge('APPLIED', 'ok') : r.status === 'DRAFT' ? badge('DRAFT', 'mut') : badge(r.status, 'warn'),
      '<button class="ghost small" data-action="revValidate" data-revision="' + H(r.revision_id) + '">Validate</button> ' +
      '<button class="ghost small" data-action="revImpact" data-revision="' + H(r.revision_id) + '">Impact</button> ' +
      (r.status === 'DRAFT' ? '<button class="small" data-action="revApply" data-revision="' + H(r.revision_id) + '">Apply</button>' : '') +
      ' <button class="ghost small" data-action="revDiff" data-revision="' + H(r.revision_id) + '">Diff</button>' +
      (r.status === 'APPLIED' ? ' <button class="warn small" data-action="revRollback" data-revision="' + H(r.revision_id) + '">Rollback</button>' : ''),
    ]);
    html += tbl(['ID', 'Создана', 'Автор', 'Причина', 'Статус', ''], rr);
  } else html += apiErr(rv, 'revisions');
  html += '</details>';
  // черновик из Routing/Task editor: панель «Проверить и применить»
  if (window._r14_draft) {
    html += '<div class="errbox" style="border-color:var(--warn)">' +
      '<b>' + H('Есть черновик: ') + '<code>' + H(window._r14_draft) + '</code></b> ' +
      '<button class="small" data-action="draftFlow" data-revision="' + H(window._r14_draft) + '">' + H('Проверить и применить') + '</button> ' +
      '<button class="ghost small" data-action="draftDrop">' + H('Отменить черновик') + '</button></div>';
  }
  return html;
};

ACTIONS.auDetail = () => { /* filled by POSTRENDER (data not in DOM for size) */ };

POSTRENDER['Audit / Revisions'] = () => {
  const acts = {
    revValidate: id => api('/admin/revisions/' + encodeURIComponent(id) + '/validate', { method: 'POST' }).then(showRev),
    revImpact: id => api('/admin/revisions/' + encodeURIComponent(id) + '/impact', { method: 'POST' }).then(showRev),
    revApply: id => confirmModal('Применить?', id.slice(0, 12), async () => {
      const r = await api('/admin/revisions/' + encodeURIComponent(id) + '/apply', { method: 'POST' });
      showRev(r); window._r14_draft = null; window.__render();
    }),
    revDiff: id => api('/admin/revisions/' + encodeURIComponent(id) + '/diff').then(showRev),
    revRollback: id => confirmModal('Откатить к ревизии?', id.slice(0, 12), async () => {
      const r = await api('/admin/revisions/' + encodeURIComponent(id) + '/rollback', { method: 'POST' });
      showRev(r); window.__render();
    }),
  };
  Object.entries(acts).forEach(([name, fn]) => {
    document.querySelectorAll('[data-action=' + name + ']').forEach(b => { b.onclick = () => fn(b.dataset.revision); });
  });
  function showRev(r) {
    if (!r.ok) { toast(T('httpErr') + ' ' + r.status, 'bad'); return; }
    openModal('<div class="row" style="justify-content:space-between"><h3 style="margin:0">' + H('Результат') + '</h3><button class="ghost small" data-action="closeModal">✕</button></div>' +
      '<pre style="max-height:60vh">' + esc(JSON.stringify(r.data, null, 2)) + '</pre>');
  }
};

ACTIONS.draftDrop = () => { window._r14_draft = null; render(); };
// §41: DRAFT → validate → replay impact → apply
ACTIONS.draftFlow = async (el) => {
  const id = el.dataset.revision;
  const v = await api('/admin/revisions/' + encodeURIComponent(id) + '/validate', { method: 'POST' });
  if (!v.ok) { toast(H('Валидация не прошла') + ' HTTP ' + v.status, 'bad'); showRevLocal(v); return; }
  const errs = (((v.data.revision || {}).validation) || {}).errors || (v.data.revision || {}).errors;
  if (errs && errs.length) { toast(H('Черновик невалиден') + ': ' + errs[0], 'bad'); return; }
  const imp = await api('/admin/routing/replay-impact', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision_id: id }) });
  let impactText = H('Оценка влияния недоступна (журнал решений пуст).');
  if (imp.ok && imp.data.replayed) {
    const x = imp.data;
    const ec = x.estimated_cost || {};
    impactText = H('Из ' + x.replayed + ' последних решений: ' + (x.same ?? 0) + ' не изменятся · ' +
      (x.provider_changed ?? 0) + ' сменят провайдера · ' + (x.model_changed ?? 0) + ' сменят модель · ' +
      (x.no_eligible_route ?? 0) + ' останутся без маршрута') +
      (ec.before_index != null ? H(' · Индекс стоимости: ' + ec.before_index + ' → ' + (ec.after_index ?? '?') + ' (прокси, не точный счёт)') : '');
  }
  confirmModal(H('Применить черновик?'), impactText, async () => {
    const r = await api('/admin/revisions/' + encodeURIComponent(id) + '/apply', { method: 'POST' });
    if (r.ok) { window._r14_draft = null; toast(H('Применено'), 'ok'); render(); }
    else toast(T('httpErr') + ' ' + r.status, 'bad');
  }, H('Применить'));
  function showRevLocal(r) {
    openModal('<pre>' + esc(JSON.stringify(r.data, null, 2)) + '</pre>');
  }
};

// ═══════════ «КАК РАБОТАЕТ ROUTER» (§37) ═════════════════════════════════

RPAGES['How it works'] = async () => {
  const r = await api('/admin/routing/pipeline');
  const stages = (r.ok && r.data.stages) || [];
  const short = [
    ['1', 'Запрос', 'Приходит запрос: модель (алиас, конкретная модель или main-auto), текст задачи, размер контекста.'],
    ['2', 'Класс задачи', 'Детерминированный классификатор определяет класс (простая задача, кодирование, исследование…) — по тексту, без сохранения текста.'],
    ['3', 'Уровень качества (Tier)', 'Класс задаёт уровень T1–T4. Уровень определяет минимальное качество модели.'],
    ['4', 'Кандидаты', 'Отбираются модели, чьи подтверждённые результаты не ниже порога уровня и у которых есть нужные возможности.'],
    ['5', 'Фильтры маршрутов', 'Каждый маршрут (модель у провайдера) проходит жёсткие фильтры: здоровье, скидка ≥ минимума, контекст, сертификация.'],
    ['6', 'Ранжирование', 'Выжившие сортируются: надёжность → экономика кэша → цена за успех → задержка. Провайдер не является фактором приоритета.'],
    ['7', 'Выбор + план отказа', 'Победитель получает запрос; на случай сбоя заранее построен план: другой маршрут той же модели, затем другая модель.'],
  ];
  return '<h2>' + H('Как работает Router') + '</h2>' +
    '<p class="muted">' + H('Кратко: запрос → класс → уровень → кандидаты → фильтры → цена/здоровье/кэш → победитель. Ниже — каждый шаг подробнее; полная схема с кликабельными блоками — на странице «Политика маршрутизации».') + '</p>' +
    short.map(([n, t, d]) => '<div class="card" style="margin:8px 0"><div class="row"><span class="badge acc">' + n + '</span> <b>' + H(t) + '</b></div><div class="s" style="margin-top:4px">' + H(d) + '</div></div>').join('') +
    '<h3>' + H('Частые вопросы') + '</h3>' +
    '<dl class="kv">' +
    '<dt>' + H('Почему выбирается конкретная модель') + '</dt><dd>' + H('Модель выигрывает конкуренцию: прошла фильтры качества/скидки/контекста и оказалась лучшей по цене за успешный запрос среди надёжных маршрутов. Точную причину последнего решения см. «Политика маршрутизации» → «Последние решения».') + '</dd>' +
    '<dt>' + H('Распределяет ли Router запросы по провайдерам в долях') + '</dt><dd>' + H('Нет. Долей вида «30% Provider A / 70% Provider B» не существует: каждый запрос выбирается заново по экономике. Статистика распределения — фактическая, а не плановая.') + '</dd>' +
    '<dt>' + H('Что делать, если модель ведёт себя плохо') + '</dt><dd>' + H('Временное правило «Отключить модель» на странице «Временные правила» (с TTL или без). Основная политика не меняется; после снятия правила всё вернётся.') + '</dd>' +
    '<dt>' + H('Чем отличается добавление модели от временного правила') + '</dt><dd>' + H('Добавление модели в пул — постоянная часть конфигурации (с журналом и ревизией). Временное правило — быстрый способ изменить поведение на время без правки политики.') + '</dd>' +
    '</dl>';
};
