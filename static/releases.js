'use strict';
// «Выпуски» tab: list of registered releases, a release's file tree, zip, registration form.
// Relies on helpers from app.js: el, $, fmtDate, fmtDateTime, canMark, state.

const rel = { list: [], current: null, loaded: false };
// header filters of the releases list: column → Set of chosen keys (empty = all)
const relFilters = { project: new Set(), org: new Set(), stage: new Set(), month: new Set() };
const REL_COLS = {
  project: { title: 'Проект', keys: (r) => [projectText(r)] },
  org: { title: 'Кому', keys: (r) => (r.addressees.length ? r.addressees : ['—']) },
  stage: { title: 'Этап', keys: (r) => [r.stage ? 'Этап ' + r.stage : 'без этапа'] },
  month: { title: 'Дата', keys: (r) => [r.released_at ? r.released_at.slice(0, 7) : '—'],
           label: (k) => (k === '—' ? '—' : new Date(k + '-01').toLocaleDateString('ru-RU', { month: 'long', year: 'numeric' })) },
};

function relVisible(except = null) {
  return rel.list.filter((r) => Object.entries(relFilters).every(([col, set]) =>
    col === except || !set.size || REL_COLS[col].keys(r).some((k) => set.has(k))));
}

let relPopup = null;
function closeRelFilter() { if (relPopup) { relPopup.remove(); relPopup = null; } }
document.addEventListener('click', closeRelFilter);

function openRelFilter(col, anchor) {
  const wasOpen = relPopup && relPopup.dataset.col === col;
  closeRelFilter();
  if (wasOpen) return;
  const def = REL_COLS[col];
  const set = relFilters[col];
  const counts = new Map();
  for (const r of relVisible(col)) for (const k of def.keys(r)) counts.set(k, (counts.get(k) || 0) + 1);
  const all = [...new Set(rel.list.flatMap(def.keys))].sort((a, b) => (col === 'month' ? b.localeCompare(a) : a.localeCompare(b, 'ru')));
  const box = el('div', { class: 'colfilter' });
  box.dataset.col = col;
  box.addEventListener('click', (e) => e.stopPropagation());
  box.append(el('div', { class: 'cf-head', text: def.title }));
  const list = el('div', { class: 'cf-list' });
  for (const k of all) {
    const tick = el('input', { type: 'checkbox' });
    tick.checked = !set.size || set.has(k);
    tick.addEventListener('change', () => {
      if (!set.size) for (const x of all) set.add(x);
      if (tick.checked) set.add(k); else set.delete(k);
      if (set.size === all.length) set.clear();
      renderList();
    });
    list.append(el('label', { class: 'cf-item' + (counts.get(k) ? '' : ' cf-zero') }, [
      tick, el('span', { class: 'cf-label', text: def.label ? def.label(k) : k }),
      el('span', { class: 'cf-count', text: String(counts.get(k) || 0) })]));
  }
  const reset = el('button', { class: 'ghost', type: 'button', text: 'Сбросить' });
  reset.addEventListener('click', () => { set.clear(); closeRelFilter(); renderList(); });
  const done = el('button', { type: 'button', text: 'Готово' });
  done.addEventListener('click', closeRelFilter);
  box.append(list, el('div', { class: 'cf-foot' }, [reset, done]));
  document.body.appendChild(box);
  const rc = anchor.getBoundingClientRect();
  box.style.left = Math.max(8, Math.min(rc.left, window.innerWidth - box.offsetWidth - 8)) + window.scrollX + 'px';
  box.style.top = rc.bottom + 4 + window.scrollY + 'px';
  relPopup = box;
}

function relHead(col, extraClass = '') {
  const th = el('th', { class: 'plain rel-th ' + extraClass + (relFilters[col].size ? ' filtered' : '') }, [REL_COLS[col].title]);
  const btn = el('button', { class: 'th-filter', type: 'button', title: 'Фильтр', 'aria-label': 'Фильтр: ' + REL_COLS[col].title },
    [el('span', { class: 'mdi mdi-filter-variant' })]);
  btn.addEventListener('click', (e) => { e.stopPropagation(); openRelFilter(col, th); });
  th.append(btn);
  return th;
}

function sizeText(n) {
  if (n == null) return '—';
  const units = ['Б', 'КБ', 'МБ', 'ГБ'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return (i === 0 ? String(n) : n.toFixed(n < 10 ? 1 : 0).replace('.', ',')) + ' ' + units[i];
}

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

function orgChips(codes) {
  const box = el('div', { class: 'org-chips' });
  for (const code of codes) box.appendChild(el('span', { class: 'org-chip', text: code }));
  if (!codes.length) box.appendChild(el('span', { class: 'sub', text: '—' }));
  return box;
}

function projectText(r) {
  return [r.project_type, r.project_name].filter(Boolean).join(' · ') || '—';
}

function stageText(stage) { return stage ? 'Этап ' + stage : '—'; }

// Short title of a portal service
function serviceText(s) {
  if (!s) return 'Заявление';
  if (/^проведение проверки документации/i.test(s)) return 'Проверка документации';
  return s.replace(/документации ППТ/i, '').replace(/\s+/g, ' ').trim();
}

const STALE_DAYS = 3;
function isStale(app, releasedAt) {
  if (!app.filed || !releasedAt) return false;
  const d = new Date(releasedAt.slice(0, 10));
  d.setDate(d.getDate() - STALE_DAYS);
  return app.filed < d.toISOString().slice(0, 10);
}

// «Заявление» blocks of a release card; two of them are normal (different committees)
function applicationsBlock(r) {
  const apps = r.applications || [];
  if (!apps.length) return null;
  const box = el('div', { class: 'apps' });
  for (const a of apps) {
    const stale = isStale(a, r.released_at);
    const head = el('div', { class: 'app-head' }, [
      a.file_id ? iconLink('mdi-eye-outline', 'Открыть заявление', `/release/${r.id}/file/${a.file_id}`, { target: '_blank', rel: 'noopener' }) : null,
      el('b', { text: a.number ? 'Заявление № ' + a.number : a.file }),
      el('span', { class: 'sub', text: serviceText(a.service) }),
      stale ? el('span', { class: 'badge warn', text: 'возможно, старое', title: 'Подано намного раньше даты выпуска — может быть, осталось от прошлой отправки' }) : null,
    ]);
    const facts = el('div', { class: 'app-facts' }, [
      el('span', {}, [el('span', { class: 'k', text: 'Кому' }), orgChips(a.committees)]),
      el('span', {}, [el('span', { class: 'k', text: 'Подано' }), a.filed ? fmtDate(a.filed) : '—']),
      el('span', {}, [el('span', { class: 'k', text: 'Ответ по плану' }), el('b', { text: a.due ? fmtDate(a.due) : '—' })]),
      a.stage_text ? el('span', {}, [el('span', { class: 'k', text: 'Этап' }), a.stage_text]) : null,
      a.order ? el('span', {}, [el('span', { class: 'k', text: 'Распоряжение' }), a.order]) : null,
    ]);
    const extra = [];
    if (a.unknown && a.unknown.length) extra.push(el('div', { class: 'sub', text: 'Не нашёл в справочнике: ' + a.unknown.join('; ') }));
    if (a.error) extra.push(el('div', { class: 'warn-line', text: a.error }));
    box.appendChild(el('div', { class: 'app' + (stale ? ' stale' : '') }, [head, facts, ...extra]));
  }
  return box;
}

// What opens in the browser: PDF, images and text as they are; Word and Excel in the viewer page.
const INLINE_EXT = /\.(pdf|png|jpe?g|gif|webp|txt)$/i;
const VIEWER_EXT = /\.(docx|xlsx|xlsm|xls|csv)$/i;

function viewLink(releaseId, f) {
  const name = f.rel_path.split('/').pop();
  const src = `/release/${releaseId}/file/${f.id}`;
  if (INLINE_EXT.test(name)) return iconLink('mdi-eye-outline', 'Открыть', src, { target: '_blank', rel: 'noopener' });
  if (VIEWER_EXT.test(name)) {
    return iconLink('mdi-eye-outline', 'Посмотреть (Word и Excel — в упрощённом виде)',
      `/static/viewer.html?src=${encodeURIComponent(src)}&name=${encodeURIComponent(name)}`, { target: '_blank', rel: 'noopener' });
  }
  return null;
}

function iconLink(icon, title, href, attrs = {}) {
  return el('a', { class: 'btn icon-btn', title, href, ...attrs }, [el('span', { class: 'mdi ' + icon })]);
}

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  if (res.status === 401) { location.href = '/login'; throw new Error('нужен вход'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || ('ошибка ' + res.status));
  return data;
}

// ------------------------------------------------------------------ list

window.showReleases = async function showReleases() {
  $('rel-new').hidden = !canMark();
  if (rel.current) { renderRelease(); return; }
  await loadReleases();
};

async function loadReleases() {
  const box = $('releases');
  box.textContent = '';
  box.appendChild(el('div', { class: 'empty-state', text: 'Загрузка…' }));
  try {
    rel.list = (await api('/api/releases')).releases;
  } catch (err) {
    box.textContent = '';
    box.appendChild(el('div', { class: 'empty-state', text: 'Ошибка: ' + err.message }));
    return;
  }
  renderList();
}

function renderList() {
  rel.current = null;
  const crumbs = $('rel-crumbs');
  crumbs.textContent = '';
  crumbs.append(el('b', { text: 'Выпуски' }),
                el('span', { class: 'sub', text: rel.list.length ? '  ' + rel.list.length : '' }));
  const box = $('releases');
  box.textContent = '';
  if (!rel.list.length) {
    box.appendChild(el('div', { class: 'empty-state' }, [
      el('strong', { text: 'Выпусков пока нет' }),
      canMark() ? 'Нажмите «Зарегистрировать выпуск» и выберите папку выпуска в срезе.' : 'Администратор ещё не зарегистрировал ни одного выпуска.',
    ]));
    return;
  }
  const shown = relVisible();
  const filtered = Object.values(relFilters).some((s) => s.size);
  if (filtered) {
    const reset = el('a', { href: '#', text: 'сбросить фильтры' });
    reset.addEventListener('click', (e) => { e.preventDefault(); for (const s of Object.values(relFilters)) s.clear(); renderList(); });
    crumbs.append(el('span', { class: 'sub', text: `  · показано ${shown.length} · ` }), reset);
  }
  const table = el('table', { class: 'rel-table' });
  table.appendChild(el('thead', {}, [el('tr', {}, [
    el('th', { class: 'plain act' }), relHead('project'), relHead('org'), relHead('stage'), relHead('month'),
    el('th', { class: 'plain num', text: 'Файлов' }), el('th', { class: 'plain num', text: 'Объём' }),
  ])]));
  const tbody = el('tbody');
  for (const r of shown) {
    const open = el('button', { class: 'icon-btn', type: 'button', title: 'Открыть выпуск' },
                    [el('span', { class: 'mdi mdi-folder-open-outline' })]);
    open.addEventListener('click', () => openRelease(r.id));
    const actions = el('div', { class: 'actions' }, [
      open, iconLink('mdi-folder-zip-outline', 'Скачать архивом', `/release/${r.id}/zip`),
    ]);
    const title = el('div', { class: 'cell-body' }, [
      el('a', { class: 'fname rel-link', href: '#', text: projectText(r) }),
      r.status !== 'ready' ? el('span', { class: 'badge ' + (r.status === 'failed' ? 'negative' : 'neutral'),
                                          text: r.status === 'failed' ? 'ошибка копирования' : 'копируется' }) : null,
      r.note ? el('div', { class: 'sub', text: r.note }) : null,
    ]);
    title.querySelector('a').addEventListener('click', (e) => { e.preventDefault(); openRelease(r.id); });
    const tr = el('tr', {}, [
      el('td', { class: 'act' }, [actions]),
      el('td', { class: 'doc' }, [title]),
      el('td', { class: 'orgs-cell' }, [orgChips(r.addressees)]),
      el('td', { class: 'nowrap', text: stageText(r.stage) }),
      el('td', { class: 'nowrap', text: r.released_at ? fmtDate(r.released_at) : '—' }),
      el('td', { class: 'num', text: String(r.files_count) }),
      el('td', { class: 'num nowrap', text: sizeText(r.size_total) }),
    ]);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  box.appendChild(table);
}

// --------------------------------------------------------------- one release

async function openRelease(id) {
  try {
    rel.current = await api('/api/releases/' + id);
  } catch (err) {
    alert('Не удалось открыть выпуск: ' + err.message);
    return;
  }
  renderRelease();
}

function buildTree(files) {
  const root = { name: '', dirs: new Map(), files: [], size: 0, count: 0 };
  for (const f of files) {
    const parts = f.rel_path.split('/');
    let node = root;
    node.size += f.size; node.count += 1;
    for (const p of parts.slice(0, -1)) {
      if (!node.dirs.has(p)) node.dirs.set(p, { name: p, dirs: new Map(), files: [], size: 0, count: 0 });
      node = node.dirs.get(p);
      node.size += f.size; node.count += 1;
    }
    node.files.push({ ...f, name: parts[parts.length - 1] });
  }
  return root;
}

function renderRelease() {
  const { release: r, files } = rel.current;
  const crumbs = $('rel-crumbs');
  crumbs.textContent = '';
  const back = el('a', { href: '#', text: 'Выпуски' });
  back.addEventListener('click', (e) => { e.preventDefault(); rel.current = null; loadReleases(); });
  crumbs.append(back, el('span', { class: 'sep', text: '/' }),
                el('b', { text: projectText(r) + (r.stage ? ' · этап ' + r.stage : '') }));

  const box = $('releases');
  box.textContent = '';

  const info = el('div', { class: 'rel-info' }, [
    el('div', {}, [el('span', { class: 'k', text: 'Проект' }), el('b', { text: projectText(r) })]),
    el('div', {}, [el('span', { class: 'k', text: 'Кому' }), orgChips(r.addressees)]),
    el('div', {}, [el('span', { class: 'k', text: 'Этап' }), r.stage || '—']),
    el('div', {}, [el('span', { class: 'k', text: 'Дата' }), r.released_at ? fmtDateTime(r.released_at) : '—']),
    el('div', {}, [el('span', { class: 'k', text: 'Состав' }),
                   `${r.files_count} ${plural(r.files_count, 'файл', 'файла', 'файлов')} · ${sizeText(r.size_total)}`]),
    r.note ? el('div', { class: 'wide' }, [el('span', { class: 'k', text: 'Примечание' }), r.note]) : null,
    // only the admin gets the source path from the server
    r.source_path ? el('div', { class: 'wide' }, [el('span', { class: 'k', text: 'Откуда' }), el('span', { class: 'path', text: r.source_path })]) : null,
  ]);
  const tools = el('div', { class: 'rel-tools' }, [
    el('a', { class: 'btn primary', href: `/release/${r.id}/zip` },
       [el('span', { class: 'mdi mdi-folder-zip-outline' }), 'Скачать весь выпуск архивом']),
    el('span', { class: 'sub', text: 'папки внутри — отдельными zip' }),
  ]);
  const others = rel.list.filter((x) => x.id !== r.id && x.status === 'ready');
  if (others.length) {
    const cmp = el('button', { type: 'button' }, [el('span', { class: 'mdi mdi-compare-horizontal' }), 'Сравнить']);
    cmp.addEventListener('click', () => openCompare(r, others));
    tools.append(cmp);
  }
  if (canMark()) {
    if (r.status === 'failed') {
      const again = el('button', { class: 'primary', type: 'button', title: 'Копирование оборвалось (например, выключился ПК) — докопировать недостающие файлы' },
        [el('span', { class: 'mdi mdi-refresh' }), 'Докопировать']);
      again.addEventListener('click', async () => {
        again.disabled = true;
        let job;
        try { job = await api(`/api/admin/releases/${r.id}/resume`, { method: 'POST' }); }
        catch (e) { alert('Не получилось: ' + e.message); again.disabled = false; return; }
        const tick = async () => {
          const j = await api('/api/admin/releases/jobs/' + job.id);
          again.lastChild.textContent = j.state === 'copying' ? `Копирую ${j.done} из ${j.files}…` : 'Докопировать';
          if (j.state === 'copying') setTimeout(tick, 1000); else openRelease(r.id);
        };
        tick();
      });
      tools.append(again);
    }
    const edit = el('button', { type: 'button' }, [el('span', { class: 'mdi mdi-pencil-outline' }), 'Изменить']);
    edit.addEventListener('click', () => openEditor(r));
    const del = el('button', { class: 'danger', type: 'button' }, [el('span', { class: 'mdi mdi-delete-outline' }), 'Удалить выпуск']);
    del.addEventListener('click', async () => {
      if (!confirm('Удалить выпуск «' + projectText(r) + (r.stage ? ', этап ' + r.stage : '') + '» из ГРАДДОКа?\n\nЕго отправки уйдут из «Сроков». Копии его файлов удалятся с сервера (кроме тех, что нужны другим выпускам или каталогу). Срез на диске E: не трогается.')) return;
      await api('/api/admin/releases/' + r.id, { method: 'DELETE' });
      rel.current = null; loadReleases();
    });
    tools.append(el('span', { class: 'spacer' }), edit, del);
  }
  box.append(info);
  const apps = applicationsBlock(r);
  if (apps) box.append(apps);
  const sendBox = el('div');
  box.append(sendBox, tools);
  if (window.releaseSendings) window.releaseSendings(r, sendBox);

  const tree = el('div', { class: 'tree' });
  const tbody = el('tbody');
  const closed = new Set();
  // a row is hidden when any folder above it is folded
  const refresh = () => {
    for (const row of tbody.children) {
      const path = row.dataset.path;
      row.hidden = [...closed].some((c) => path !== c && path.startsWith(c + '/'));
    }
  };
  const addDir = (node, path, depth) => {
    const dirs = [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name, 'ru'));
    for (const d of dirs) {
      const p = path ? path + '/' + d.name : d.name;
      const row = el('tr', { class: 'dir' }, [
        el('td', { class: 'act' }, [el('div', { class: 'actions' }, [
          iconLink('mdi-folder-zip-outline', 'Скачать папку архивом', `/release/${r.id}/zip?prefix=${encodeURIComponent(p)}`),
        ])]),
        el('td', { class: 'doc' }, [el('div', { class: 'tree-name', style: `padding-left:${depth * 18}px` }, [
          el('span', { class: 'mdi mdi-chevron-down twist' }), el('span', { class: 'mdi mdi-folder-outline' }),
          el('b', { text: d.name })])]),
        el('td', { class: 'num nowrap sub', text: `${d.count} ${plural(d.count, 'файл', 'файла', 'файлов')}` }),
        el('td', { class: 'num nowrap sub', text: sizeText(d.size) }),
      ]);
      row.dataset.path = p;
      row.addEventListener('click', (e) => {
        if (e.target.closest('a')) return;
        if (row.classList.toggle('closed')) closed.add(p); else closed.delete(p);
        refresh();
      });
      tbody.appendChild(row);
      addDir(d, p, depth + 1);
    }
    for (const f of node.files.sort((a, b) => a.name.localeCompare(b.name, 'ru', { numeric: true }))) {
      tbody.appendChild(el('tr', { 'data-path': f.rel_path }, [
        el('td', { class: 'act' }, [el('div', { class: 'actions' }, [
          // download first: it lines up with the folder archive button above
          iconLink('mdi-download', 'Скачать', `/release/${r.id}/file/${f.id}?dl=1`),
          viewLink(r.id, f) || el('span', { class: 'icon-btn icon-gap', title: 'Этот тип файла только скачивается' }),
        ])]),
        el('td', { class: 'doc' }, [el('div', { class: 'tree-name', style: `padding-left:${depth * 18 + 22}px` }, [
          (() => {
            // the file name itself opens the preview (where the browser can show the file)
            const v = viewLink(r.id, f);
            return v
              ? el('a', { class: 'fname rel-file', href: v.getAttribute('href'), target: '_blank', rel: 'noopener', text: f.name, title: 'Открыть: ' + f.rel_path })
              : el('span', { class: 'fname', text: f.name, title: f.rel_path + ' — только скачать' });
          })()])]),
        el('td', { class: 'num sub', text: '' }),
        el('td', { class: 'num nowrap sub', text: sizeText(f.size) }),
      ]));
    }
  };
  addDir(buildTree(files), '', 0);
  // top-level folders open, deeper ones folded: the parts of the release are visible at once
  for (const row of tbody.querySelectorAll('tr.dir')) {
    if (!row.dataset.path.includes('/')) continue;
    row.classList.add('closed'); closed.add(row.dataset.path);
  }
  refresh();
  const table = el('table', { class: 'rel-table tree-table' }, [tbody]);
  tree.appendChild(table);
  box.appendChild(tree);
}

// ------------------------------------------------------------- comparison

function releaseName(x) {
  return `${fmtDate(x.released_at)} · ${projectText(x)}${x.stage ? ' · этап ' + x.stage : ''} · ${x.addressees.join(', ') || '—'}`;
}

// The other release to compare with: the latest earlier one of the same project and stage, else of the project.
function defaultOther(r, others) {
  const earlier = others.filter((x) => (x.released_at || '') < (r.released_at || '')
    && x.project_name === r.project_name).sort((a, b) => (b.released_at || '').localeCompare(a.released_at || ''));
  return earlier.find((x) => x.stage === r.stage) || earlier[0] || others[0];
}

function openCompare(r, others) {
  const m = modal('Сравнение выпусков');
  const pick = el('select', {}, others.map((x) => el('option', { value: String(x.id), text: releaseName(x) })));
  pick.value = String(defaultOther(r, others).id);
  const out = el('div', { class: 'cmp' });
  m.body.append(
    el('div', { class: 'cmp-head' }, [
      el('span', { class: 'k', text: 'Этот выпуск' }), el('b', { text: releaseName(r) }),
      el('span', { class: 'k', text: 'Сравнить с' }), pick,
    ]),
    out,
  );
  const fileLink = (rid, f, text) => el('a', { href: `/release/${rid}/file/${f.id}`, target: '_blank', rel: 'noopener', text: text || f.rel_path });
  const section = (title, cls, items, render) => {
    if (!items.length) return null;
    const list = el('div', { class: 'cmp-list' }, items.map(render));
    const head = el('button', { type: 'button', class: 'cmp-sec-head ' + cls }, [
      el('span', { class: 'mdi mdi-chevron-down twist' }), el('b', { text: title }), el('span', { class: 'sub', text: String(items.length) })]);
    const sec = el('div', { class: 'cmp-sec' }, [head, list]);
    if (items.length > 12) sec.classList.add('closed');
    head.addEventListener('click', () => sec.classList.toggle('closed'));
    return sec;
  };
  async function run() {
    out.textContent = 'Сравниваю…';
    let d;
    try { d = await api(`/api/releases/${r.id}/compare/${pick.value}`); } catch (e) { out.textContent = 'Ошибка: ' + e.message; return; }
    const older = (d.old.released_at || '') <= (d.new.released_at || '');
    out.textContent = '';
    out.append(el('p', { class: 'hint', text: older
      ? 'Что изменилось в этом выпуске по сравнению с выбранным (по содержимому файлов).'
      : 'Выбранный выпуск новее этого — списки показывают, чем этот отличается от него.' }));
    out.append(el('div', { class: 'cmp-sum' }, [
      el('span', { class: 'cmp-n added' }, [el('b', { text: String(d.added.length) }), ' добавлено']),
      el('span', { class: 'cmp-n changed' }, [el('b', { text: String(d.changed.length) }), ' изменено']),
      el('span', { class: 'cmp-n moved' }, [el('b', { text: String(d.moved.length) }), ' перемещено']),
      el('span', { class: 'cmp-n removed' }, [el('b', { text: String(d.removed.length) }), ' убрано']),
      el('span', { class: 'cmp-n' }, [el('b', { text: String(d.same) }), ' без изменений']),
    ]));
    const rows = [
      section('Добавлено', 'added', d.added, (f) => el('div', {}, [fileLink(r.id, f), el('span', { class: 'sub', text: ' ' + sizeText(f.size) })])),
      section('Изменено', 'changed', d.changed, (x) => el('div', {}, [fileLink(r.id, x.new), el('span', { class: 'sub' }, [
        ' · ', fileLink(d.old.id, x.old, 'прежний'), ` · ${sizeText(x.old.size)} → ${sizeText(x.new.size)}`])])),
      section('Перемещено или переименовано', 'moved', d.moved, (x) => el('div', {}, [
        el('span', { class: 'sub', text: x.old.rel_path + ' → ' }), fileLink(r.id, x.new)])),
      section('Убрано', 'removed', d.removed, (f) => el('div', {}, [fileLink(d.old.id, f)])),
    ].filter(Boolean);
    if (!rows.length) out.append(el('p', { class: 'hint', text: 'Состав выпусков совпадает полностью.' }));
    out.append(...rows);
  }
  pick.addEventListener('change', run);
  run();
}

// ------------------------------------------------------------- registration

function modal(title) {
  const back = el('div', { class: 'modal-back' });
  const box = el('div', { class: 'modal', role: 'dialog', 'aria-label': title });
  const close = el('button', { class: 'ghost icon-btn', type: 'button', title: 'Закрыть' }, [el('span', { class: 'mdi mdi-close' })]);
  close.addEventListener('click', () => back.remove());
  const body = el('div', { class: 'modal-body' });
  box.append(el('div', { class: 'modal-head' }, [el('b', { text: title }), el('span', { class: 'spacer' }), close]), body);
  back.appendChild(box);
  back.addEventListener('click', (e) => { if (e.target === back) back.remove(); });
  document.body.appendChild(back);
  return { back, body };
}

async function browseStep(m, startPath) {
  m.body.textContent = '';
  m.body.append(el('p', { class: 'hint', text: 'Выберите папку выпуска внутри среза — ту, что реально ушла. Например: срез → 09_Выпуск → Этап 02 - 02_КГА → ДПТ-Далта_Эт2_КГА.' }));
  const head = el('div', { class: 'browser-head' });
  const list = el('div', { class: 'browser-list' });
  const choose = el('button', { class: 'primary', type: 'button', disabled: true }, [el('span', { class: 'mdi mdi-check' }), 'Выбрать эту папку']);
  const foot = el('div', { class: 'browser-foot' }, [choose, el('span', { class: 'sub', text: '' })]);
  m.body.append(el('div', { class: 'browser' }, [head, list, foot]));
  let current = '';

  async function go(path) {
    list.textContent = '';
    list.appendChild(el('div', { class: 'browser-item sub', text: 'Загрузка…' }));
    let data;
    try {
      data = await api('/api/admin/browse?path=' + encodeURIComponent(path || ''));
    } catch (err) {
      list.textContent = '';
      list.appendChild(el('div', { class: 'browser-item sub', text: 'Ошибка: ' + err.message }));
      return;
    }
    current = data.current;
    head.textContent = data.current || 'Подключённые папки';
    choose.disabled = !current;
    list.textContent = '';
    if (data.parent !== null && data.current) {
      const up = el('button', { class: 'browser-item', type: 'button' }, [el('span', { class: 'mdi mdi-arrow-up' }), ' ..']);
      up.addEventListener('click', () => go(data.parent));
      list.appendChild(up);
    }
    for (const d of data.dirs) {
      const b = el('button', { class: 'browser-item', type: 'button' }, [el('span', { class: 'mdi mdi-folder-outline' }), ' ' + d.name]);
      b.addEventListener('click', () => go(d.path));
      list.appendChild(b);
    }
    if (!data.dirs.length) list.appendChild(el('div', { class: 'browser-item sub', text: 'Вложенных папок нет' }));
  }
  choose.addEventListener('click', () => formStep(m, current));
  go(startPath || '');
}

async function formStep(m, path) {
  m.body.textContent = '';
  m.body.appendChild(el('div', { class: 'empty-state', text: 'Читаю папку…' }));
  let p;
  try {
    p = await api('/api/admin/releases/preview?path=' + encodeURIComponent(path));
  } catch (err) {
    m.body.textContent = '';
    m.body.appendChild(el('p', { class: 'error', text: 'Ошибка: ' + err.message }));
    return;
  }
  m.body.textContent = '';
  const field = (label, input, hint) => el('label', { class: 'field' }, [
    el('span', { class: 'k', text: label }), input, hint ? el('span', { class: 'sub', text: hint }) : null]);
  const title = el('input', { type: 'text', value: p.title || '' });
  const when = dateField(p.released_at || '', { time: true });
  const ptype = el('select', {}, [el('option', { value: '', text: '—' }),
    ...p.project_types.map((t) => el('option', { value: t, text: t }))]);
  ptype.value = p.project_type || '';
  const pname = el('input', { type: 'text', value: p.project_name || '' });
  const stage = el('input', { type: 'text', value: p.stage || '', placeholder: 'нет', style: 'width:80px' });
  const note = el('input', { type: 'text', placeholder: 'необязательно' });

  const chosen = new Set(p.addressees);
  const orgs = el('div', { class: 'org-pick' });
  const committees = [...p.committees].sort((a, b) => (b.active - a.active));
  for (const c of committees) {
    const box = el('input', { type: 'checkbox' });
    box.checked = chosen.has(c.code);
    box.addEventListener('change', () => { if (box.checked) chosen.add(c.code); else chosen.delete(c.code); });
    orgs.appendChild(el('label', { class: 'org-opt' + (c.active ? '' : ' off'), title: c.name }, [box, c.code]));
  }

  m.body.append(
    el('div', { class: 'path-line' }, [el('span', { class: 'mdi mdi-folder-outline' }), el('span', { class: 'path', text: p.path })]),
    el('div', { class: 'stat-line' }, [
      el('b', { text: `${p.files} ${plural(p.files, 'файл', 'файла', 'файлов')} · ${sizeText(p.size)}` }),
      p.skipped ? el('span', { class: 'sub', text: ` · не берём zip, sig и служебные (.bak и т. п.): ${p.skipped}` }) : null,
    ]),
    ...p.notes.map((n) => el('p', { class: 'warn-line', text: n })),
    ...(p.applications.length ? [applicationsBlock({ applications: p.applications, released_at: p.released_at })] : []),
    ...(p.already.length ? [el('p', { class: 'warn-line', text: 'Эта папка уже зарегистрирована: ' + p.already.map((a) => a.title).join(', ') })] : []),
    el('div', { class: 'form-grid' }, [
      field('Дата отправки', when, p.snapshot ? 'из среза ' + p.snapshot : ''),
      field('Проект', el('div', { class: 'inline' }, [ptype, pname])),
      field('Этап', stage),
      el('div', { class: 'field wide' }, [
        el('span', { class: 'k', text: 'Кому отправлен' + (p.iogv ? ' (ИОГВ раскрыт в список комитетов)' : '') }), orgs]),
      field('Примечание', note),
    ]),
  );
  const go = el('button', { class: 'primary', type: 'button' }, [el('span', { class: 'mdi mdi-content-save-outline' }), 'Зарегистрировать выпуск']);
  const back = el('button', { type: 'button', text: 'Другая папка' });
  back.addEventListener('click', () => browseStep(m, p.path));
  const status = el('div', { class: 'progress', hidden: true }, [el('div', { class: 'bar' }), el('span', { class: 'sub' })]);
  m.body.append(el('div', { class: 'modal-foot' }, [back, el('span', { class: 'spacer' }), go]), status);

  go.addEventListener('click', async () => {
    let releasedAt;
    try { releasedAt = when.value || null; } catch (e) { alert(e.message); return; }
    go.disabled = true; back.disabled = true;
    const type = ptype.value, name = pname.value.trim();
    const payload = {
      path: p.path, title: title.value.trim(), released_at: releasedAt,
      project_type: type || null, project_name: name || null,
      project_folder: type && name ? `${type} - ${name}` : (p.project_folder || name || null),
      stage: stage.value.trim(), label: p.label, note: note.value, addressees: [...chosen],
    };
    let job;
    try {
      job = await api('/api/admin/releases', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
    } catch (err) {
      status.hidden = false; status.querySelector('.sub').textContent = 'Ошибка: ' + err.message;
      go.disabled = false; back.disabled = false;
      return;
    }
    status.hidden = false;
    const bar = status.querySelector('.bar'), txt = status.querySelector('.sub');
    const tick = async () => {
      const j = await api('/api/admin/releases/jobs/' + job.id);
      const pct = j.bytes ? Math.round((100 * j.bytes_done) / j.bytes) : 100;
      bar.style.width = pct + '%';
      txt.textContent = `Копирую: ${j.done} из ${j.files} файлов · ${sizeText(j.bytes_done)} из ${sizeText(j.bytes)}`
        + (j.bytes_done ? ` · новых данных ${sizeText(j.new_bytes)}` : '');
      if (j.state === 'copying') { setTimeout(tick, 700); return; }
      if (j.state === 'failed') { txt.textContent = 'Ошибка копирования: ' + j.error; return; }
      txt.textContent = `Готово: ${j.files} файлов, новых данных ${sizeText(j.new_bytes)} (остальное уже было в хранилище).`;
      setTimeout(() => { m.back.remove(); openRelease(j.release_id); }, 900);
    };
    tick();
  });
}

function openEditor(r) {
  const m = modal('Изменить выпуск');
  const title = el('input', { type: 'text', value: r.title });
  const when = dateField((r.released_at || '').slice(0, 16), { time: true });
  const stage = el('input', { type: 'text', value: r.stage || '', style: 'width:80px' });
  const pname = el('input', { type: 'text', value: r.project_name || '' });
  const addr = el('input', { type: 'text', value: r.addressees.join(', ') });
  const note = el('input', { type: 'text', value: r.note || '' });
  const field = (label, input, hint) => el('label', { class: 'field' }, [
    el('span', { class: 'k', text: label }), input, hint ? el('span', { class: 'sub', text: hint }) : null]);
  const save = el('button', { class: 'primary', type: 'button' }, [el('span', { class: 'mdi mdi-content-save-outline' }), 'Сохранить']);
  m.body.append(el('div', { class: 'form-grid' }, [
    field('Дата отправки', when), field('Проект', pname), field('Этап', stage),
    field('Кому', addr, 'сокращения через запятую'), field('Примечание', note),
  ]), el('div', { class: 'modal-foot' }, [el('span', { class: 'spacer' }), save]));
  save.addEventListener('click', async () => {
    try { when.value; } catch (e) { alert(e.message); return; }
    await api('/api/admin/releases/' + r.id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: title.value.trim(), released_at: when.value || null, stage: stage.value.trim() || null,
        project_name: pname.value.trim() || null, note: note.value.trim() || null,
        addressees: addr.value.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean),
      }),
    });
    m.back.remove();
    openRelease(r.id);
  });
}

$('rel-new').addEventListener('click', () => {
  const m = modal('Регистрация выпуска');
  browseStep(m, '');
});
