'use strict';

const MOBILE_QUERY = window.matchMedia('(max-width: 760px)');
const WIDTHS_KEY = 'graddok.columnWidths.v3';

const fileHref = (d) => '/file/' + d.id;

const state = {
  docs: [],
  meta: null,
  filters: emptyFilters(),
  sort: { key: 'mtime', dir: 'desc' },
  view: 'table',
  selected: new Set(),
  finalOrgs: new Set(),
  committees: [],
  groups: [],
  requirements: {},
};

const $ = (id) => document.getElementById(id);

// Excel-like column filters: an empty set means «all values».
function emptyFilters() {
  return {
    group: '', query: '', onlyNew: false, onlyUploaded: false, project: '',
    cols: { file: new Set(), org: new Set(), month: new Set(), decision: new Set(), project: new Set(), stage: new Set() },
  };
}

const MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август',
                'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];

// What each filterable column shows: the value key of a document and its label.
const COLUMN_FILTERS = {
  file: {
    title: 'Документ',
    key: (d) => d.file_name,
    label: (d) => d.file_name,
    hint: (d) => [d.subfolder, d.note].filter(Boolean).join(' · '),
  },
  org: {
    title: 'От кого',
    key: (d) => d.org_code || '__none__',
    label: (d) => (d.org_code ? d.org_code : '— не определён'),
    hint: (d) => d.org_name || '',
  },
  month: {
    title: 'Дата',
    key: (d) => (d.mtime || '').slice(0, 7) || '__none__',
    label: (d) => {
      const m = (d.mtime || '').slice(0, 7);
      return m ? MONTHS[Number(m.slice(5, 7)) - 1] + ' ' + m.slice(0, 4) : '— без даты';
    },
    order: 'desc',
  },
  decision: { title: 'Решение', key: (d) => d.decision_id, label: (d) => d.decision_label },
  project: {
    title: 'Проект',
    key: (d) => d.project_key,
    label: (d) => (d.project_type ? d.project_type + ' · ' : '') + d.project_name,
  },
  stage: {
    title: 'Этап',
    key: (d) => d.stage || '__none__',
    label: (d) => (d.stage ? d.stage + ' этап' : '— без этапа'),
  },
};

// ------------------------------------------------------------------ утилиты

// All dates are shown as ДД-ММ-ГГГГ (decision of the owner, 11.11).
const pad2 = (n) => String(n).padStart(2, '0');

function fmtDate(iso) {
  if (!iso) return '—';
  const s = String(iso);
  // a bare date: no time zone shift
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (m) return `${m[3]}-${m[2]}-${m[1]}`;
  const d = new Date(s);
  if (isNaN(d)) return s.slice(0, 10);
  return `${pad2(d.getDate())}-${pad2(d.getMonth() + 1)}-${d.getFullYear()}`;
}

function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return `${fmtDate(iso)} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

// Date field typed as ДД-ММ-ГГГГ (dashes are added while typing) with a calendar button.
// .value is ISO like the native input: «2027-06-01» (or «2027-06-01T14:30» with {time: true}); '' when empty.
// .valid() — false when something is typed but it is not a date.
function dateField(iso, opts = {}) {
  const time = !!opts.time;
  const hint = time ? 'ДД-ММ-ГГГГ чч:мм' : 'ДД-ММ-ГГГГ';
  const text = el('input', { type: 'text', class: 'df-text', inputmode: 'numeric', placeholder: hint,
    autocomplete: 'off', spellcheck: 'false', maxlength: time ? '16' : '10', 'aria-label': hint });
  const picker = el('input', { type: time ? 'datetime-local' : 'date', class: 'df-picker', tabindex: '-1', 'aria-hidden': 'true' });
  const btn = el('button', { type: 'button', class: 'df-btn', title: 'Календарь', 'aria-label': 'Календарь' },
    [el('span', { class: 'mdi mdi-calendar-month-outline' })]);
  const box = el('span', { class: 'datefield' + (time ? ' with-time' : '') }, [text, btn, picker]);

  const toText = (v) => {
    if (!v) return '';
    const [day, t] = String(v).split('T');
    const [y, m, d] = day.split('-');
    return `${d}-${m}-${y}` + (time && t ? ' ' + t.slice(0, 5) : '');
  };
  const parse = (s) => {
    s = s.trim();
    if (!s) return '';
    const m = s.match(/^(\d{1,2})[-./ ](\d{1,2})[-./ ](\d{4})(?:\s+(\d{1,2})[:.](\d{2}))?$/);
    if (!m) return null;
    const [, d, mo, y, hh, mi] = m;
    const dt = new Date(Date.UTC(+y, +mo - 1, +d));
    if (dt.getUTCDate() !== +d || dt.getUTCMonth() !== +mo - 1) return null;
    let out = `${y}-${pad2(mo)}-${pad2(d)}`;
    if (time) {
      if (hh && (+hh > 23 || +mi > 59)) return null;
      out += 'T' + (hh ? pad2(hh) + ':' + mi : '00:00');
    }
    return out;
  };
  // typing digits builds «ДД-ММ-ГГГГ чч:мм» by itself; pasted «01.09.2027» works too
  text.addEventListener('input', (e) => {
    if (e.inputType && !e.inputType.startsWith('insert')) return;   // erasing: leave as is
    const digits = text.value.replace(/\D/g, '').slice(0, time ? 12 : 8);
    let v = digits.slice(0, 2);
    if (digits.length >= 2) v += '-' + digits.slice(2, 4);
    if (digits.length >= 4) v += '-' + digits.slice(4, 8);
    if (time && digits.length >= 8) v += ' ' + digits.slice(8, 10);
    if (time && digits.length >= 10) v += ':' + digits.slice(10, 12);
    text.value = v.replace(/[- :]$/, (x) => (digits.length === 2 || digits.length === 4 || digits.length === 8 || digits.length === 10 ? x : ''));
    box.classList.remove('invalid');
  });
  text.addEventListener('blur', () => {
    const v = parse(text.value);
    box.classList.toggle('invalid', v === null);
    if (v) text.value = toText(v);
  });
  btn.addEventListener('click', () => {
    picker.value = parse(text.value) || '';
    try { picker.showPicker(); } catch (e) { picker.focus(); }
  });
  picker.addEventListener('change', () => {
    text.value = toText(picker.value);
    box.classList.remove('invalid');
  });
  Object.defineProperty(box, 'value', {
    // a typed non-date must not silently become «no date»: saving stops with a message
    get: () => {
      const v = parse(text.value);
      if (v === null) {
        box.classList.add('invalid');
        throw new Error(`Дата «${text.value.trim()}» не распознана — нужно ${hint}`);
      }
      return v;
    },
    set: (v) => { text.value = toText(v ? String(v).slice(0, time ? 16 : 10) : ''); },
  });
  box.valid = () => parse(text.value) !== null;
  box.focus = () => text.focus();
  box.input = text;
  box.value = iso || '';
  return box;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const child of [].concat(children)) {
    if (child) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

// localStorage может быть недоступен (приватный режим) — не роняем страницу
function readStore(key) {
  try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { return null; }
}
function writeStore(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* не критично */ }
}

// ----------------------------------------------------------------- загрузка

async function loadPayload() {
  const [metaRes, docsRes] = await Promise.all([fetch('/api/meta'), fetch('/api/documents')]);
  if (metaRes.status === 401 || docsRes.status === 401) { location.href = '/login'; return null; }
  state.meta = await metaRes.json();
  return docsRes.json();
}

async function load() {
  const payload = await loadPayload();
  if (!payload) return;
  state.docs = payload.documents;
  state.finalOrgs = new Set(payload.final_orgs || []);
  state.committees = payload.committees || [];
  state.groups = payload.groups || [];
  state.requirements = payload.requirements || {};
  state.defaultOrgs = payload.default_orgs || [];
  state.archived = new Set(payload.archived || []);

  const adminLink = $('admin-link');
  if (adminLink && state.meta.role === 'admin') adminLink.hidden = false;
  if ($('logout') && state.meta.user) $('logout').title = 'Вы вошли как ' + state.meta.user;

  const pub = payload.publication;
  $('pub-info').textContent = pub
    ? `${state.docs.length} док. · обновлено ${fmtDateTime(pub.created_at)}`
    : 'каталог пуст';

  buildFilters();
  render();
}

// ----------------------------------------------------------------- фильтры

// «6:ДПТ - Далта» → «ДПТ - Далта»: one project, whatever source folder it came from
function projectFolder(d) { return String(d.project_key || '').replace(/^\d+:/, ''); }

function buildFilters() {
  // Project list (archived projects are left out)
  const byProject = new Map();
  for (const d of state.docs) {
    const k = projectFolder(d);
    if (state.archived && state.archived.has(k)) continue;
    const cur = byProject.get(k) || { label: [d.project_type, d.project_name].filter(Boolean).join(' · '), n: 0 };
    cur.n += 1;
    byProject.set(k, cur);
  }
  const ps = $('project-filter');
  ps.textContent = '';
  ps.appendChild(el('option', { value: '', text: 'Все проекты' }));
  for (const [k, v] of [...byProject].sort((a, b) => a[1].label.localeCompare(b[1].label, 'ru'))) {
    ps.appendChild(el('option', { value: k, text: `${v.label} (${v.n})` }));
  }
  if (!byProject.has(state.filters.project)) state.filters.project = '';
  ps.value = state.filters.project;

  // Groups (approvals, applications…) — a list only when there is more than one.
  const byGroup = new Map();
  for (const d of state.docs) byGroup.set(d.group_id, (byGroup.get(d.group_id) || 0) + 1);
  const groupSelect = $('group');
  const keepGroup = state.filters.group;
  groupSelect.textContent = '';
  groupSelect.appendChild(el('option', { value: '', text: `Все документы (${state.docs.length})` }));
  const titles = new Map(state.groups.map((g) => [g.id, g.title]));
  for (const [id, count] of byGroup) {
    groupSelect.appendChild(el('option', { value: id, text: `${titles.get(id) || id} (${count})` }));
  }
  groupSelect.hidden = byGroup.size < 2;
  groupSelect.value = byGroup.has(keepGroup) ? keepGroup : '';
  state.filters.group = groupSelect.value;

  const newCount = state.docs.filter((d) => d.is_new || d.is_changed).length;
  const onlyNew = $('only-new');
  onlyNew.textContent = '';
  onlyNew.append('Только новые', el('span', { class: 'count', text: String(newCount) }));
  onlyNew.hidden = newCount === 0 && !state.filters.onlyNew;

  // uploaded by hand and not found on the PC yet — a reminder to save them to the Disk
  const upCount = state.docs.filter((d) => d.uploaded).length;
  const onlyUp = $('only-uploaded');
  onlyUp.textContent = '';
  onlyUp.append(el('span', { class: 'mdi mdi-cloud-alert-outline' }), 'Только на сервере',
                el('span', { class: 'count', text: String(upCount) }));
  onlyUp.hidden = upCount === 0 && !state.filters.onlyUploaded;
  $('upload-btn').hidden = !canMark();

  // On a phone the table head is hidden — the same filters as buttons.
  const mob = $('mobile-filters');
  mob.textContent = '';
  for (const [col, def] of Object.entries(COLUMN_FILTERS)) {
    const btn = el('button', { class: 'chip', type: 'button', 'data-filter-btn': col },
                   [def.title, el('span', { class: 'mdi mdi-chevron-down' })]);
    btn.addEventListener('click', (e) => { e.stopPropagation(); openColumnFilter(col, btn); });
    mob.appendChild(btn);
  }
}

// Documents passing every filter except, optionally, one column —
// that is what a column's list shows, like Excel does.
function visibleDocs(exceptCol = null) {
  const f = state.filters;
  const q = f.query.trim().toLowerCase();
  return state.docs.filter((d) => {
    if (f.group && d.group_id !== f.group) return false;
    if (f.onlyNew && !(d.is_new || d.is_changed)) return false;
    if (f.onlyUploaded && !d.uploaded) return false;
    if (f.project && projectFolder(d) !== f.project) return false;
    for (const [col, set] of Object.entries(f.cols)) {
      if (col === exceptCol || !set.size) continue;
      if (!set.has(COLUMN_FILTERS[col].key(d))) return false;
    }
    if (q) {
      const hay = [d.project_name, d.project_type, d.org_code, d.org_name, d.org_detail,
                   d.file_name, d.note, d.subfolder, d.decision_label].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function activeFilterCount() {
  const f = state.filters;
  let n = Object.values(f.cols).filter((set) => set.size).length;
  if (f.query.trim()) n += 1;
  if (f.onlyNew) n += 1;
  if (f.onlyUploaded) n += 1;
  if (f.project) n += 1;
  if (f.group) n += 1;
  return n;
}

// ------------------------------------------------- фильтр в заголовке столбца

const colFilter = { box: null, col: null };
const NOTHING = '__nothing__';   // marker: every value unticked

function columnValues(col) {
  const def = COLUMN_FILTERS[col];
  const values = new Map();
  for (const d of state.docs) {
    const key = def.key(d);
    if (!values.has(key)) {
      values.set(key, { key, label: def.label(d), hint: def.hint ? def.hint(d) : '', count: 0 });
    }
  }
  for (const d of visibleDocs(col)) values.get(def.key(d)).count += 1;
  const list = [...values.values()];
  list.sort((a, b) => {
    const none = (a.key === '__none__') - (b.key === '__none__');
    if (none) return none;
    return def.order === 'desc' ? String(b.key).localeCompare(String(a.key))
      : a.label.localeCompare(b.label, 'ru', { numeric: true });
  });
  return list;
}

function closeColumnFilter() {
  if (colFilter.box) colFilter.box.remove();
  colFilter.box = null;
  colFilter.col = null;
}

function openColumnFilter(col, anchor) {
  if (colFilter.col === col) { closeColumnFilter(); return; }
  closeColumnFilter();
  const def = COLUMN_FILTERS[col];
  const set = state.filters.cols[col];
  const values = columnValues(col);

  const box = el('div', { class: 'colfilter', role: 'dialog', 'aria-label': 'Фильтр: ' + def.title });
  box.addEventListener('click', (e) => e.stopPropagation());
  const list = el('div', { class: 'cf-list' });
  const search = values.length > 8
    ? el('input', { type: 'text', class: 'cf-search', placeholder: 'Найти…', autocomplete: 'off' })
    : null;

  const apply = () => {
    if (set.size === values.length) set.clear();   // all ticked again → no filter
    render();
    paint();
  };

  const allBox = el('input', { type: 'checkbox' });
  const allRow = el('label', { class: 'cf-item cf-all' }, [allBox, el('span', { text: 'Выделить все' })]);
  allBox.addEventListener('change', () => {
    set.clear();
    if (!allBox.checked) set.add(NOTHING);
    apply();
  });

  const rows = values.map((v) => {
    const tick = el('input', { type: 'checkbox' });
    tick.addEventListener('change', () => {
      if (!set.size) for (const x of values) set.add(x.key);   // from «all» to an explicit list
      set.delete(NOTHING);
      if (tick.checked) set.add(v.key); else set.delete(v.key);
      if (!set.size) set.add(NOTHING);
      apply();
    });
    const row = el('label', { class: 'cf-item' + (v.count ? '' : ' cf-zero'), title: v.hint || null }, [
      tick, el('span', { class: 'cf-label', text: v.label }), el('span', { class: 'cf-count', text: String(v.count) }),
    ]);
    row.dataset.text = (v.label + ' ' + v.hint).toLowerCase();
    list.appendChild(row);
    return { row, tick, key: v.key };
  });

  function paint() {
    for (const r of rows) r.tick.checked = !set.has(NOTHING) && (!set.size || set.has(r.key));
    const ticked = rows.filter((r) => r.tick.checked).length;
    allBox.checked = ticked === rows.length;
    allBox.indeterminate = ticked > 0 && ticked < rows.length;
  }

  if (search) {
    search.addEventListener('input', () => {
      const q = search.value.trim().toLowerCase();
      for (const r of rows) r.row.hidden = !!q && !r.row.dataset.text.includes(q);
    });
  }

  const resetBtn = el('button', { class: 'ghost', type: 'button', text: 'Сбросить' });
  resetBtn.addEventListener('click', () => { set.clear(); apply(); });
  const doneBtn = el('button', { type: 'button', text: 'Готово' });
  doneBtn.addEventListener('click', closeColumnFilter);

  box.append(el('div', { class: 'cf-head', text: def.title }));
  if (search) box.append(search);
  box.append(allRow, list, el('div', { class: 'cf-foot' }, [resetBtn, doneBtn]));
  document.body.appendChild(box);
  paint();

  const r = anchor.getBoundingClientRect();
  const w = box.offsetWidth;
  box.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + window.scrollX + 'px';
  box.style.top = r.bottom + 4 + window.scrollY + 'px';
  colFilter.box = box;
  colFilter.col = col;
  // On a phone focusing the search pops the keyboard up — that resizes the window
  // and used to close the popup at once. Focus only where there is a real mouse.
  if (search && window.matchMedia('(hover: hover) and (pointer: fine)').matches) search.focus();
}

document.addEventListener('click', closeColumnFilter);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeColumnFilter(); });
// Close on a real width change only: the on-screen keyboard changes just the height.
let lastWidth = window.innerWidth;
window.addEventListener('resize', () => {
  if (window.innerWidth !== lastWidth) { lastWidth = window.innerWidth; closeColumnFilter(); }
});

function setupColumnFilters() {
  for (const th of document.querySelectorAll('#head-row th[data-filter]')) {
    const col = th.dataset.filter;
    const btn = el('button', {
      class: 'th-filter', type: 'button', title: 'Фильтр',
      'aria-label': 'Фильтр: ' + COLUMN_FILTERS[col].title,
    }, [el('span', { class: 'mdi mdi-filter-variant' })]);
    btn.addEventListener('click', (e) => { e.stopPropagation(); openColumnFilter(col, th); });
    th.appendChild(btn);
  }
}

// Funnel highlight, «Сбросить фильтры» and short tags of what is filtered.
function paintFilterState() {
  for (const [col, set] of Object.entries(state.filters.cols)) {
    for (const node of document.querySelectorAll(`[data-filter="${col}"], [data-filter-btn="${col}"]`)) {
      node.classList.toggle('filtered', set.size > 0);
    }
  }
  $('reset').hidden = activeFilterCount() === 0;
  const box = $('active-filters');
  box.textContent = '';
  for (const [col, set] of Object.entries(state.filters.cols)) {
    if (!set.size) continue;
    const def = COLUMN_FILTERS[col];
    const labels = columnValues(col).filter((v) => set.has(v.key)).map((v) => v.label);
    const text = !labels.length ? 'ничего'
      : labels.length > 2 ? labels.slice(0, 2).join(', ') + ' +' + (labels.length - 2) : labels.join(', ');
    const tag = el('button', { class: 'ftag', type: 'button', title: 'Убрать этот фильтр' },
                   [el('b', { text: def.title + ':' }), ' ' + text, el('span', { class: 'mdi mdi-close' })]);
    tag.addEventListener('click', (e) => { e.stopPropagation(); set.clear(); render(); });
    box.appendChild(tag);
  }
}

function sortDocs(docs) {
  const { key, dir } = state.sort;
  const sign = dir === 'asc' ? 1 : -1;
  return [...docs].sort((a, b) => {
    let x = a[key], y = b[key];
    x = x === null || x === undefined ? '' : String(x);
    y = y === null || y === undefined ? '' : String(y);
    if (x !== y) {
      if (x === '') return 1;
      if (y === '') return -1;
      return x.localeCompare(y, 'ru', { numeric: true }) * sign;
    }
    // Одинаковое значение — сверху более свежий файл.
    const ta = a.mtime || '', tb = b.mtime || '';
    if (ta !== tb) return ta < tb ? 1 : -1;
    return a.file_name.localeCompare(b.file_name, 'ru');
  });
}

// ------------------------------------------------------------------- вывод

function render() {
  if (state.view === 'releases') return;
  const docs = sortDocs(visibleDocs());
  if (state.view === 'table') renderTable(docs); else renderMatrix(docs);
  paintFilterState();

  for (const th of document.querySelectorAll('#head-row th[data-sort]')) {
    const arrow = th.querySelector('.arrow');
    if (th.dataset.sort === state.sort.key) {
      th.setAttribute('aria-sort', state.sort.dir === 'asc' ? 'ascending' : 'descending');
      arrow.textContent = state.sort.dir === 'asc' ? '▲' : '▼';
    } else {
      th.removeAttribute('aria-sort');
      arrow.textContent = '▼';
    }
  }
}

// Ячейка таблицы. Содержимое всегда лежит в одном контейнере — на телефоне
// строка превращается в карточку «подпись | значение», и без обёртки вторая
// и последующие строки съезжали бы в колонку подписи.
function cell(label, children, opts = {}) {
  const body = el('div', { class: 'cell-body' }, children);
  return el('td', {
    'data-label': label,
    class: [opts.nowrap ? 'nowrap' : null, opts.dim ? 'dim' : null].filter(Boolean).join(' ') || null,
  }, [body]);
}

function renderTable(docs) {
  const tbody = $('rows');
  tbody.textContent = '';
  showEmptyState(docs.length);

  for (const d of docs) {
    const tr = el('tr', { class: d.is_new ? 'is-new' : null });

    const pick = el('input', { type: 'checkbox', 'aria-label': 'Выбрать документ' });
    pick.checked = state.selected.has(d.id);
    pick.addEventListener('change', () => {
      if (pick.checked) state.selected.add(d.id); else state.selected.delete(d.id);
      tr.classList.toggle('picked', pick.checked);
      updateSelection();
    });
    if (pick.checked) tr.classList.add('picked');
    const pickCell = cell('Выбрать', [pick]);
    pickCell.classList.add('pick');
    tr.appendChild(pickCell);

    // Open / download come first: it is what people come here for.
    const actCell = cell('', [
      el('div', { class: 'actions' }, [
            el('a', { class: 'btn icon-btn', href: fileHref(d), target: '_blank', rel: 'noopener', title: 'Открыть' },
               [el('span', { class: 'mdi mdi-eye-outline' }), el('span', { class: 'lbl', text: 'Открыть' })]),
            el('a', {
              class: 'btn icon-btn', title: 'Скачать',
              href: '/download/' + d.id,
            }, [el('span', { class: 'mdi mdi-download' }), el('span', { class: 'lbl', text: 'Скачать' })]),
            canMark() && window.shareButton ? shareButton('doc', d.id) : null,
          ]),
    ]);
    actCell.classList.add('act');
    tr.appendChild(actCell);

    // One muted detail next to the file name: the row stays one line high.
    const docSub = [
      d.subfolder,
      d.doc_date ? 'в имени ' + fmtDate(d.doc_date) : null,
      d.note ? 'прим. ' + d.note : null,
    ].filter(Boolean).join(' · ');
    const docCell = cell('Документ', [
      el('div', { class: 'fname', text: d.file_name, title: d.file_name }),
      d.uploaded ? uploadMarks(d) : null,
      docSub ? el('div', { class: 'sub', text: docSub, title: docSub }) : null,
    ]);
    docCell.classList.add('doc');
    tr.appendChild(docCell);

    const orgSub = [d.org_detail, d.org_parent ? 'при ' + d.org_parent : null].filter(Boolean).join(' · ');
    tr.appendChild(cell('От кого', [
      el('div', { class: 'org', text: d.org_code || '—', title: d.org_name || '' }),
      orgSub ? el('div', { class: 'sub', text: orgSub }) : null,
    ], { dim: !d.org_code }));

    const seen = d.first_seen_at ? 'В каталоге с ' + fmtDate(d.first_seen_at) : '';
    tr.appendChild(cell('Дата', [
      el('div', { text: fmtDate(d.uploaded && d.doc_date ? d.doc_date : d.mtime), title: seen }),
      newMark(d, seen),
    ], { nowrap: true }));

    tr.appendChild(cell('Решение', [decisionBadge(d)]));

    tr.appendChild(cell('Проект', [
      el('div', { class: 'proj' }, [
        d.project_type ? el('span', { class: 'ptype', text: d.project_type }) : null,
        d.project_name,
      ]),
    ]));

    tr.appendChild(cell('Этап', [el('div', { text: d.stage ? d.stage + ' эт.' : '—' })],
                        { nowrap: true, dim: !d.stage }));

    tbody.appendChild(tr);
  }
  updateSelection();
}

// Пустая таблица бывает по двум причинам: каталог ещё не наполнен
// или условия отбора ничего не находят — подсказки должны отличаться.
function showEmptyState(visibleCount) {
  const box = $('empty');
  box.hidden = visibleCount > 0;
  if (visibleCount > 0) return;

  box.textContent = '';
  if (!state.docs.length) {
    box.append(el('strong', { text: 'Каталог пуст' }));
    box.append(state.meta && state.meta.role === 'admin'
      ? 'Откройте панель управления, укажите папки с проектами и нажмите «Актуализировать».'
      : 'Документы ещё не опубликованы.');
  } else {
    box.append(el('strong', { text: 'Ничего не найдено' }));
    box.append('Измените условия отбора или сбросьте фильтры.');
  }
}

const ORDER_WARN_DAYS = 60;

// «Замечания» / «Отказ»: the sheet of answers — progress, or «Разобрать» for those who answer
function canAnswer() {
  return state.meta && (state.meta.role === 'admin' || state.meta.role === 'editor');
}

// The decision badge. «Замечания» / «Отказ» is a button: it opens the sheet of answers,
// or (for those who answer) cuts the letter into items first. A small icon marks it.
function decisionBadge(d) {
  const plain = el('span', { class: 'badge ' + d.decision_kind, text: d.decision_label });
  const remarkable = ['negative', 'neutral'].includes(d.decision_kind);
  if (!d.remarks && !(remarkable && canAnswer())) return plain;
  const b = el('button', { type: 'button', class: 'badge badge-btn ' + d.decision_kind,
    title: d.remarks ? 'Открыть лист ответов на замечания' : 'Разобрать письмо на пункты и писать ответы' },
    [d.decision_label, el('span', { class: 'mdi ' + (d.remarks ? 'mdi-format-list-checks' : 'mdi-format-list-numbered') })]);
  b.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (d.remarks) { window.open('/remarks?id=' + d.remarks.sheet_id, '_blank', 'noopener'); return; }
    // open the tab at once (a pop-up opened after a wait would be blocked), then point it to the sheet
    const tab = window.open('about:blank', '_blank');
    b.disabled = true;
    try {
      const res = await fetch('/api/remarks', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ doc_id: d.id }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'ошибка');
      d.remarks = { sheet_id: data.id, total: data.count || 0, done: 0 };
      if (tab) tab.location = '/remarks?id=' + data.id; else location.href = '/remarks?id=' + data.id;
      b.replaceWith(decisionBadge(d));
    } catch (err) {
      if (tab) tab.close();
      alert('Не получилось разобрать: ' + err.message);
      b.disabled = false;
    }
  });
  return b;
}

function canMark() {
  return state.meta && state.meta.role === 'admin';
}

// «New» is shown only as a light tint of the row (tr.is-new); no badge.
function newMark(d, seen) {
  if (d.is_changed && !d.is_new) return el('span', { class: 'badge neutral', text: 'обновлён', title: seen });
  return null;
}

async function markSelected(value) {
  const ids = [...state.selected];
  if (!ids.length) return;
  const status = $('sel-status');
  status.textContent = 'Сохраняю…';
  try {
    const res = await fetch('/api/admin/documents', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, manual_new: value }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'не удалось сохранить');
    // Обновляем строки на месте, не перезагружая весь каталог.
    const byId = new Map(data.documents.map((x) => [x.id, x]));
    for (const d of state.docs) {
      const fresh = byId.get(d.id);
      if (fresh) Object.assign(d, fresh);
    }
    const label = value === true ? 'помечены новыми'
      : value === false ? 'сняты с новых' : 'возвращены к автоматическому расчёту';
    status.textContent = `Готово: ${data.documents.length} док. ${label}`;
    render();
  } catch (err) {
    status.textContent = 'Ошибка: ' + err.message;
  }
}

async function deleteSelected() {
  const ids = [...state.selected];
  if (!ids.length) return;
  const ok = window.confirm(
    'Удалить из каталога: ' + ids.length + ' док.?'
    + String.fromCharCode(10) + String.fromCharCode(10)
    + 'Документы пропадут из таблицы и матрицы, их копии удалятся с сервера. '
    + 'Файлы на ПК не трогаются. Вернуть можно в панели управления '
    + '(копия появится снова при следующем «Актуализировать»).',
  );
  if (!ok) return;
  const status = $('sel-status');
  status.textContent = 'Удаляю…';
  try {
    const res = await fetch('/api/admin/documents', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, hidden: true }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'не удалось удалить');
    const gone = new Set(data.documents.map((x) => x.id));
    state.docs = state.docs.filter((d) => !gone.has(d.id));
    state.selected.clear();
    status.textContent = 'Удалено: ' + gone.size
      + ' док. Вернуть можно в панели управления.';
    render();
  } catch (err) {
    status.textContent = 'Ошибка: ' + err.message;
  }
}

const KIND_SIGN = { positive: '✓', negative: '✕', neutral: '!', unknown: '?' };

function renderMatrix(allDocs) {
  const container = $('matrix');
  container.textContent = '';
  // В матрицу идут только согласования: заявления и прочие группы
  // к комитетам не привязаны.
  const matrixGroups = new Set(state.groups.filter((g) => g.matrix).map((g) => g.id));
  const docs = matrixGroups.size
    ? allDocs.filter((d) => matrixGroups.has(d.group_id))
    : allDocs;
  if (!docs.length) {
    container.appendChild(el('div', { class: 'empty-state', text: 'Ничего не найдено.' }));
    return;
  }

  const rowKeys = [];
  const rowLabels = new Map();
  const orgs = new Map();
  const cells = new Map();
  // Строки с согласованием завершающего органа (по умолчанию КГА).
  const finished = new Map();

  for (const d of docs) {
    // archived projects stay out of the matrix (their documents are still in the table)
    if (state.archived && state.archived.has(String(d.project_key).replace(/^\d+:/, ''))) continue;
    const rowKey = d.project_key + '||' + (d.stage || '');
    if (!rowLabels.has(rowKey)) {
      rowKeys.push(rowKey);
      rowLabels.set(rowKey, {
        project: (d.project_type ? d.project_type + ' · ' : '') + d.project_name,
        stage: d.stage ? d.stage + ' этап' : '',
      });
    }
    const org = d.org_code || '—';
    if (!orgs.has(org)) orgs.set(org, d.org_name || org);
    // Завершает только положительное решение: отказ проект не закрывает.
    if (state.finalOrgs.has(org) && d.decision_kind === 'positive') {
      finished.set(rowKey, org);
    }
    const cellKey = rowKey + '||' + org;
    const list = cells.get(cellKey) || [];
    list.push(d);
    cells.set(cellKey, list);
  }

  // Columns: organizations some shown project needs (its own list, or the standard set)
  // plus anyone who has actually sent a document. An organization nobody needs and
  // nobody heard from gets no column.
  // What a project needs: its own list from «Комитеты по проектам», else the standard set.
  const requiredFor = (rowKey) => {
    const codes = state.requirements[rowKey.split('||')[0]];
    return new Set(Array.isArray(codes) ? codes : state.defaultOrgs);
  };
  const names = new Map(state.committees.map((c) => [c.code, c.name]));
  for (const rowKey of rowKeys) {
    for (const code of requiredFor(rowKey)) if (!orgs.has(code)) orgs.set(code, names.get(code) || code);
  }
  // Порядок столбцов повторяет порядок записей в справочнике: так его можно
  // менять, просто переставив комитет в committees.yaml или в панели.
  // Органы, которых в справочнике нет (встретились только в именах файлов),
  // идут после известных. Завершающий орган — всегда последний.
  const rank = new Map(state.committees.map((c, i) => [c.code, i]));
  const orgList = [...orgs.keys()].sort((a, b) => {
    const fa = state.finalOrgs.has(a) ? 1 : 0;
    const fb = state.finalOrgs.has(b) ? 1 : 0;
    if (fa !== fb) return fa - fb;
    const ra = rank.has(a) ? rank.get(a) : Number.MAX_SAFE_INTEGER;
    const rb = rank.has(b) ? rank.get(b) : Number.MAX_SAFE_INTEGER;
    if (ra !== rb) return ra - rb;
    return a.localeCompare(b, 'ru');
  });
  const rowTitle = (k) => rowLabels.get(k).project + ' ' + rowLabels.get(k).stage;
  rowKeys.sort((a, b) => {
    // Завершённые — в конец, остальные по названию проекта и этапу.
    const fa = finished.has(a) ? 1 : 0;
    const fb = finished.has(b) ? 1 : 0;
    if (fa !== fb) return fa - fb;
    return rowTitle(a).localeCompare(rowTitle(b), 'ru', { numeric: true });
  });

  const table = el('table', { class: 'matrix' });
  const headRow = el('tr', {}, [el('th', { class: 'row plain', text: 'Проект / этап' })]);
  // a column nobody shown here needs (it is in only because a document came) is marked
  const neededByAny = new Set();
  for (const rowKey of rowKeys) for (const code of requiredFor(rowKey)) neededByAny.add(code);
  for (const org of orgList) {
    const unused = !neededByAny.has(org) && org !== '—';
    headRow.appendChild(el('th', {
      class: 'col plain' + (state.finalOrgs.has(org) ? ' final' : '') + (unused ? ' unused' : ''),
      title: (orgs.get(org) || org) + (unused ? ' — не отмечен ни у одного проекта, но от него есть документ' : ''),
    }, [el('span', { class: 'vt', text: org })]));
  }
  table.appendChild(el('thead', {}, [headRow]));

  const tbody = el('tbody');
  for (const rowKey of rowKeys) {
    const info = rowLabels.get(rowKey);
    const finalOrg = finished.get(rowKey);
    const tr = el('tr', { class: finalOrg ? 'done' : null }, [
      el('th', { class: 'row' }, [
        el('div', { text: info.project }),
        info.stage ? el('div', { class: 'sub', text: info.stage }) : null,
        finalOrg ? el('div', { class: 'sub done-mark', text: 'завершён · ' + finalOrg }) : null,
      ]),
    ]);
    for (const org of orgList) {
      const list = cells.get(rowKey + '||' + org);
      const td = el('td');
      if (!list) {
        // Если для проекта задан список согласований по ТЗ, всё остальное —
        // серая заглушка «не требуется», а не пустое ожидание.
        const notNeeded = !requiredFor(rowKey).has(org);
        td.appendChild(el('span', {
          class: 'cell ' + (notNeeded ? 'skip' : 'empty'),
          text: notNeeded ? '–' : '·',
          title: notNeeded ? 'Этому проекту не требуется' : 'Согласования ещё нет',
        }));
        tr.appendChild(td);
        continue;
      }

      // Документов в ячейке может быть несколько — это нормальный случай для
      // районных администраций: один проект попадает в два-три района.
      // Показываем сводное состояние, а подробности — в подсказке.
      const negative = list.filter((d) => d.decision_kind === 'negative');
      const doc = negative[0] || list[0];
      const isNew = list.some((d) => d.is_new || d.is_changed);

      const lines = list.map((d) => {
        const who = d.org_detail ? d.org_detail + ' — ' : '';
        return '• ' + who + d.decision_label + ': ' + d.file_name;
      });
      if (list.length > 1) {
        lines.unshift('Документов: ' + list.length
          + (negative.length ? ', из них отказов: ' + negative.length : ''), '');
      }

      const cell = el('a', {
            class: 'cell ' + doc.decision_kind + (isNew ? ' is-new' : ''),
            href: fileHref(doc), target: '_blank', rel: 'noopener',
            title: lines.join(String.fromCharCode(10)),
            text: KIND_SIGN[doc.decision_kind] || '?',
          });
      td.appendChild(cell);
      if (list.length > 1) {
        // Счётчик подсказывает, что за значком скрыто больше одного документа.
        td.appendChild(el('span', { class: 'cell-count', text: String(list.length) }));
      }
      tr.appendChild(td);
    }

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  container.appendChild(table);
}

// ----------------------------------------------------------- выбор строк

function visibleIds() {
  return sortDocs(visibleDocs()).map((d) => d.id);
}

function buildMarkTools() {
  const box = $('mark-tools');
  if (!canMark()) { box.hidden = true; return; }
  if (box.childElementCount) return;   // already built
  box.hidden = false;
  for (const [icon, label, value, title] of [
    ['mdi-star-four-points-outline', 'Отметить новым', true, 'Пометить выбранные как новые'],
    ['mdi-star-off-outline', 'Снять «новое»', false, 'Снять пометку «новое» у выбранных'],
    ['mdi-autorenew', 'Авто', null, 'Вернуть автоматическую пометку «новое»'],
  ]) {
    const btn = el('button', { type: 'button', title }, [el('span', { class: 'mdi ' + icon }), label]);
    btn.addEventListener('click', () => markSelected(value));
    box.appendChild(btn);
  }
  const drop = el('button', {
    class: 'danger', type: 'button',
    title: 'Убрать лишний документ из каталога. Файл на диске остаётся; вернуть можно в панели управления',
  }, [el('span', { class: 'mdi mdi-delete-outline' }), 'Удалить из каталога']);
  drop.addEventListener('click', deleteSelected);
  box.appendChild(drop);
}

function updateSelection() {
  const count = state.selected.size;
  buildMarkTools();
  const bar = $('selbar');
  // The admin sees the action bar all the time (buttons wait for a selection);
  // a viewer sees it only when something is selected.
  bar.hidden = state.view !== 'table' || (count === 0 && !canMark());
  bar.classList.toggle('has-selection', count > 0);
  $('sel-count').textContent = 'Выбрано: ' + count;
  for (const btn of bar.querySelectorAll('#download-selected, #mark-tools button, #select-none')) {
    btn.disabled = count === 0;
  }

  // The header checkbox reflects only the rows visible now.
  const ids = visibleIds();
  const picked = ids.filter((id) => state.selected.has(id)).length;
  const all = $('pick-all');
  all.checked = ids.length > 0 && picked === ids.length;
  all.indeterminate = picked > 0 && picked < ids.length;
}

function setSelection(ids, on) {
  for (const id of ids) {
    if (on) state.selected.add(id); else state.selected.delete(id);
  }
  render();
}

async function downloadSelected() {
  const btn = $('download-selected');
  const status = $('sel-status');
  btn.disabled = true;
  status.textContent = 'Собираю архив…';
  try {
    const res = await fetch('/api/zip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [...state.selected] }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'Не удалось собрать архив');
    status.textContent = `Готово: ${data.count} док., ${fmtSize(data.size)}`;
    // Ссылку открывает браузер — это обычная загрузка с индикатором прогресса.
    location.href = '/zip/' + data.token;
  } catch (err) {
    status.textContent = 'Ошибка: ' + err.message;
  } finally {
    btn.disabled = state.selected.size === 0;
  }
}

function fmtSize(bytes) {
  if (!bytes) return '0';
  if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' КБ';
  return (bytes / 1024 / 1024).toFixed(1).replace('.', ',') + ' МБ';
}

// --------------------------------------------------- ширина столбцов мышью

const columns = {
  table: $('table'),
  headers: [...document.querySelectorAll('#head-row th')],
  active: null,
};

// По умолчанию таблица раскладывается браузером: подстраивается под ширину
// окна и масштаб страницы. Фиксированные ширины включаются только тогда,
// когда пользователь сам потянул границу столбца.
function savedWidths() {
  return readStore(WIDTHS_KEY) || {};
}

function hasCustomWidths() {
  return Object.keys(savedWidths()).length > 0;
}

function autoLayout() {
  columns.table.classList.remove('fixed');
  columns.table.style.minWidth = '';
  for (const th of columns.headers) th.style.width = '';
}

function applyStoredWidths() {
  if (MOBILE_QUERY.matches || !hasCustomWidths()) {
    autoLayout();
    return;
  }
  const saved = savedWidths();
  // Столбцы без своей сохранённой ширины оставляем такими, какими их
  // только что разложил браузер.
  autoLayout();
  const measured = columns.headers.map((th) => th.offsetWidth);
  columns.headers.forEach((th, i) => {
    th.style.width = (saved[th.dataset.col] || measured[i]) + 'px';
  });
  columns.table.classList.add('fixed');
}

// Первое перетаскивание: замораживаем текущую раскладку, дальше двигаем её.
function freezeWidths() {
  if (columns.table.classList.contains('fixed')) return;
  const measured = columns.headers.map((th) => th.offsetWidth);
  columns.headers.forEach((th, i) => { th.style.width = measured[i] + 'px'; });
  columns.table.classList.add('fixed');
}

function saveWidths() {
  const saved = {};
  for (const th of columns.headers) {
    if (th.style.width) saved[th.dataset.col] = parseInt(th.style.width, 10);
  }
  writeStore(WIDTHS_KEY, saved);
}

function resetWidths() {
  writeStore(WIDTHS_KEY, {});
  autoLayout();
}

function setupResizers() {
  for (const th of columns.headers) {
    if (th.dataset.col === 'pick' || th.dataset.col === 'actions') continue;
    th.classList.add('resizable');
    const handle = el('span', {
      class: 'resizer',
      title: 'Потяните, чтобы изменить ширину. Двойной щелчок — вернуть автоматическую.',
    });

    handle.addEventListener('pointerdown', (e) => {
      if (MOBILE_QUERY.matches) return;
      e.preventDefault();
      e.stopPropagation();
      freezeWidths();
      handle.setPointerCapture(e.pointerId);
      handle.classList.add('dragging');
      document.body.classList.add('resizing');
      columns.active = { th, startX: e.clientX, startWidth: th.offsetWidth };
    });

    handle.addEventListener('pointermove', (e) => {
      if (!columns.active || columns.active.th !== th) return;
      const delta = e.clientX - columns.active.startX;
      th.style.width = Math.max(60, columns.active.startWidth + delta) + 'px';
    });

    const finish = (e) => {
      if (!columns.active || columns.active.th !== th) return;
      columns.active = null;
      handle.classList.remove('dragging');
      document.body.classList.remove('resizing');
      try { handle.releasePointerCapture(e.pointerId); } catch (err) { /* уже отпущен */ }
      saveWidths();
    };
    handle.addEventListener('pointerup', finish);
    handle.addEventListener('pointercancel', finish);

    handle.addEventListener('dblclick', (e) => {
      e.stopPropagation();
      resetWidths();
    });

    th.appendChild(handle);
  }
}

function applyLayoutMode() {
  columns.table.classList.toggle('cards', MOBILE_QUERY.matches);
  applyStoredWidths();
}

// ------------------------------------------------------------------ события

function bind() {
  let timer;
  $('search').addEventListener('input', (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.filters.query = e.target.value; render(); }, 150);
  });

  $('group').addEventListener('change', (e) => { state.filters.group = e.target.value; render(); });

  $('only-new').addEventListener('click', () => {
    state.filters.onlyNew = !state.filters.onlyNew;
    $('only-new').setAttribute('aria-pressed', state.filters.onlyNew ? 'true' : 'false');
    render();
  });

  $('only-uploaded').addEventListener('click', () => {
    state.filters.onlyUploaded = !state.filters.onlyUploaded;
    $('only-uploaded').setAttribute('aria-pressed', state.filters.onlyUploaded ? 'true' : 'false');
    render();
  });

  $('project-filter').addEventListener('change', (e) => { state.filters.project = e.target.value; render(); });

  $('reset').addEventListener('click', () => {
    $('only-uploaded').setAttribute('aria-pressed', 'false');
    state.filters = emptyFilters();
    $('project-filter').value = '';
    $('search').value = '';
    $('group').value = '';
    $('only-new').setAttribute('aria-pressed', 'false');
    closeColumnFilter();
    render();
  });

  $('reset-widths').addEventListener('click', resetWidths);

  $('pick-all').addEventListener('change', (e) => setSelection(visibleIds(), e.target.checked));
  $('select-visible').addEventListener('click', () => setSelection(visibleIds(), true));
  $('select-none').addEventListener('click', () => {
    state.selected.clear();
    $('sel-status').textContent = '';
    render();
  });
  $('download-selected').addEventListener('click', downloadSelected);

  for (const th of document.querySelectorAll('#head-row th[data-sort]')) {
    th.addEventListener('click', (e) => {
      if (e.target.classList.contains('resizer')) return;
      const key = th.dataset.sort;
      if (state.sort.key === key) {
        state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        const descFirst = ['mtime', 'doc_date'];
        state.sort = { key, dir: descFirst.includes(key) ? 'desc' : 'asc' };
      }
      render();
    });
  }

  // «Письма» is one tab with two views: the list (table) and the matrix.
  const tabs = { table: $('tab-table'), releases: $('tab-releases'), terms: $('tab-terms'), remarks: $('tab-remarks') };
  const views = { table: $('view-table'), matrix: $('view-matrix'), releases: $('view-releases'), terms: $('view-terms'),
                  remarks: $('view-remarks') };
  state.lettersMode = readStore('graddok.lettersMode') === 'matrix' ? 'matrix' : 'table';
  const show = (view) => {
    state.view = view;
    const tabName = view === 'matrix' ? 'table' : view;
    for (const [other, node] of Object.entries(tabs)) node.setAttribute('aria-selected', other === tabName ? 'true' : 'false');
    for (const [other, node] of Object.entries(views)) node.hidden = other !== view;
    for (const b of document.querySelectorAll('#letters-mode button')) {
      b.setAttribute('aria-pressed', b.dataset.mode === view ? 'true' : 'false');
    }
    // the catalog filters belong to the letters, not to releases or terms
    document.querySelector('.filters').hidden = view === 'releases' || view === 'terms' || view === 'remarks';
    if (view === 'remarks') { $('selbar').hidden = true; closeColumnFilter(); window.showRemarks(); return; }
    if (view === 'releases') { $('selbar').hidden = true; closeColumnFilter(); window.showReleases(); return; }
    if (view === 'terms') { $('selbar').hidden = true; closeColumnFilter(); window.showTerms(); return; }
    render();
    updateSelection();
    if (view === 'table') applyLayoutMode();
  };
  for (const [name, tab] of Object.entries(tabs)) {
    tab.addEventListener('click', () => show(name === 'table' ? state.lettersMode : name));
  }
  for (const b of document.querySelectorAll('#letters-mode button')) {
    b.addEventListener('click', () => {
      state.lettersMode = b.dataset.mode;
      writeStore('graddok.lettersMode', state.lettersMode);
      show(state.lettersMode);
    });
  }
  state.showView = show;

  const logout = $('logout');
  if (logout) {
    logout.addEventListener('click', async () => {
      await fetch('/api/logout', { method: 'POST' });
      location.href = '/login';
    });
  }

  MOBILE_QUERY.addEventListener('change', applyLayoutMode);
}

bind();
setupColumnFilters();
setupResizers();
// a share link opened by someone with an account lands here: «/#doc=12», «/#release=5&file=40»
async function followLink() {
  const h = new URLSearchParams(location.hash.slice(1));
  const doc = Number(h.get('doc')), release = Number(h.get('release')), file = Number(h.get('file'));
  // «/#tab=remarks»: back from a sheet of answers to the list of remarks
  const tab = h.get('tab');
  if (tab && ['releases', 'terms', 'remarks'].includes(tab)) {
    history.replaceState(null, '', location.pathname);
    state.showView(tab);
    return true;
  }
  if (!doc && !release) return false;
  history.replaceState(null, '', location.pathname);
  if (release) {
    await openRelease(release);
    state.showView('releases');
    if (file && rel.current) {
      const f = (rel.current.files || []).find((x) => x.id === file);
      if (f) {
        // open the folders on the way, then show the file
        for (const row of document.querySelectorAll('#releases tr.dir.closed')) {
          if (f.rel_path.startsWith(row.dataset.path + '/')) row.click();
        }
        const row = [...document.querySelectorAll('#releases tr[data-path]')].find((x) => x.dataset.path === f.rel_path && !x.classList.contains('dir'));
        if (row) { row.classList.add('flash'); row.scrollIntoView({ block: 'center' }); }
      }
    }
    return true;
  }
  const d = state.docs.find((x) => x.id === doc);
  state.lettersMode = 'table';
  if (d) {
    state.filters = emptyFilters();
    state.filters.query = d.file_name;
    $('search').value = d.file_name;
  }
  state.showView('table');
  return true;
}

// a link to the same page changes only the part after «#»
window.addEventListener('hashchange', () => { if (state.docs) followLink(); });

load()
  .then(async () => { if (!(await followLink())) state.showView(state.lettersMode); })
  .catch(() => { $('pub-info').textContent = 'Ошибка загрузки данных'; });
