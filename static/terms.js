'use strict';
// «Сроки» tab: sendings to committees and their answers — «ушло → вернулось».
// Relies on helpers from app.js (el, $, fmtDate, canMark) and releases.js (api, modal, orgChips).

const terms = { data: null, project: '', query: '', mode: readStore('graddok.termsMode') || 'gantt' };

const STATE_CLASS = { approved: 'positive', refused: 'negative', waiting: 'waiting', withdrawn: 'neutral', unanswered: 'neutral' };

// short status words for the narrow matrix cells; the full one is in the tooltip
const SHORT_STATE = { unanswered: 'без ответа', withdrawn: 'отозвано' };

function shortDate(iso) {
  if (!iso) return '—';
  const [y, m, d] = iso.slice(0, 10).split('-');
  return `${d}-${m}` + (y !== String(new Date().getFullYear()) ? '-' + y : '');
}

function daysText(n) { return n == null ? '' : `${n} ${plural(n, 'день', 'дня', 'дней')}`; }

window.showTerms = async function showTerms() {
  $('terms-new').hidden = !canMark();
  const box = $('terms');
  if (!terms.data) {
    box.textContent = '';
    box.appendChild(el('div', { class: 'empty-state', text: 'Загрузка…' }));
  }
  try {
    terms.data = await api('/api/sendings');
  } catch (err) {
    box.textContent = '';
    box.appendChild(el('div', { class: 'empty-state', text: 'Ошибка: ' + err.message }));
    return;
  }
  fillProjects();
  renderTerms();
};

function projectLabel(p) { return [p.project_type, p.project_name].filter(Boolean).join(' · '); }

function fillProjects() {
  const sel = $('terms-project');
  const have = new Set(terms.data.sendings.map((s) => s.project_name));
  sel.textContent = '';
  sel.appendChild(el('option', { value: '', text: 'Все проекты' }));
  for (const p of terms.data.projects.filter((x) => have.has(x.project_name))) {
    sel.appendChild(el('option', { value: p.project_name, text: projectLabel(p) }));
  }
  sel.value = terms.project;
}

function cellFor(s) {
  const cls = 'term ' + (STATE_CLASS[s.state] || '') + (s.overdue ? ' overdue' : '');
  const lines = [
    el('div', { class: 'term-dates' }, [
      shortDate(s.sent_at), el('span', { class: 'arrow-r', text: ' → ' }),
      s.state === 'waiting' ? el('span', { class: 'sub', text: 'ждём' })
        : s.state === 'unanswered' ? el('span', { class: 'sub', text: 'нет ответа' }) : shortDate(s.answered_at),
    ]),
    el('div', { class: 'term-meta' }, [
      el('span', { class: 'badge ' + (STATE_CLASS[s.state] || 'neutral'), text: SHORT_STATE[s.state] || s.state_label }),
      el('span', { class: 'term-days', text: daysText(s.days) }),
    ]),
  ];
  if (s.state === 'waiting' && s.due) {
    lines.push(el('div', { class: 'sub term-due', text: (s.overdue ? 'просрочено, план ' : 'план ') + shortDate(s.due) }));
  }
  const title = [
    s.state_label,
    `Отправлено ${fmtDate(s.sent_at)}` + (s.application_no ? ` · заявление № ${s.application_no}` : ''),
    s.answer ? `Ответ: ${s.answer.file_name} (${fmtDate(s.answer.date)})${s.answer_auto ? ' — найден автоматически' : ''}` : 'Ответа нет',
    s.due ? `Ответ по плану: ${fmtDate(s.due)}` : '',
    s.note ? 'Примечание: ' + s.note : '',
  ].filter(Boolean).join('\n');
  const node = el(canMark() ? 'button' : 'div', { class: cls, type: canMark() ? 'button' : null, title }, lines);
  if (canMark()) node.addEventListener('click', () => editSending(s));
  return node;
}

function renderTerms() {
  const box = $('terms');
  box.textContent = '';
  const q = terms.query.trim().toLowerCase();
  const archived = new Set(terms.data.archived || []);
  const folderOf = (s) => (s.project_type ? s.project_type + ' - ' : '') + s.project_name;
  const list = terms.data.sendings.filter((s) => !archived.has(folderOf(s)) && (!terms.project || s.project_name === terms.project)
    && (!q || (s.project_name + ' ' + (s.project_type || '')).toLowerCase().includes(q)));
  if (!list.length) {
    box.appendChild(el('div', { class: 'empty-state' }, [
      el('strong', { text: terms.data.sendings.length ? 'Ничего не найдено' : 'Отправок пока нет' }),
      canMark() ? 'Они появляются при регистрации выпуска; прошлые можно добавить кнопкой «Добавить отправку».' : '',
    ]));
    return;
  }
  // committees in the order of committees.yaml, the final body last
  const order = new Map(terms.data.committees.map((c, i) => [c.code, (c.final ? 1000 : 0) + i]));
  const blocks = new Map();
  for (const s of list) {
    const key = s.project_name + '|' + (s.stage || '');
    if (!blocks.has(key)) blocks.set(key, { name: projectLabel(s), stage: s.stage, items: [], ptype: s.project_type || '', pname: s.project_name });
    blocks.get(key).items.push(s);
  }
  const orderOf = new Map((terms.data.orders || []).map((o) => [(o.project_type || '') + '|' + o.project_name, o]));
  // the axis covers only the projects shown: one chosen project gets the whole width
  const shownOrders = [...new Set([...blocks.values()].map((b) => b.ptype + '|' + b.pname))].map((k) => orderOf.get(k)).filter(Boolean);
  const range = timeRange(list, shownOrders);
  if (terms.mode === 'gantt') box.appendChild(ganttLegend());
  for (const b of [...blocks.values()].sort((a, c) => a.name.localeCompare(c.name, 'ru') || String(a.stage).localeCompare(String(c.stage)))) {
    const count = (st) => b.items.filter((s) => s.state === st && isLast(s, b.items)).length;
    const lastOnes = b.items.filter((s) => isLast(s, b.items));
    const overdue = lastOnes.filter((s) => s.overdue).length;
    const summary = [
      `${lastOnes.length} ${plural(lastOnes.length, 'комитет', 'комитета', 'комитетов')}`,
      count('approved') ? `согласовано ${count('approved')}` : '',
      count('refused') ? `отказ ${count('refused')}` : '',
      count('waiting') ? `ждём ${count('waiting')}` + (overdue ? ` (просрочено ${overdue})` : '') : '',
    ].filter(Boolean).join(' · ');

    const rounds = Math.max(...b.items.map((s) => s.round));
    const byOrg = new Map();
    for (const s of b.items) {
      if (!byOrg.has(s.org_code)) byOrg.set(s.org_code, []);
      byOrg.get(s.org_code)[s.round - 1] = s;
    }
    const codes = [...byOrg.keys()].sort((a, c) => (order.get(a) ?? 500) - (order.get(c) ?? 500));
    const ord = orderOf.get(b.ptype + '|' + b.pname);
    if (terms.mode === 'gantt') {
      box.appendChild(el('section', { class: 'term-block' }, [
        el('div', { class: 'term-title' }, [
          el('b', { text: b.name + (b.stage ? ' · этап ' + b.stage : '') }), el('span', { class: 'sub', text: summary }),
          orderLabel(ord),
        ]),
        ganttFor(b.items, codes, range, ord && ord.valid_until),
      ]));
      continue;
    }
    const head = el('tr', {}, [el('th', { class: 'plain', text: 'Комитет' }),
      ...Array.from({ length: rounds }, (_, i) => el('th', { class: 'plain', text: rounds > 1 ? `Круг ${i + 1}` : 'Отправка → ответ' }))]);
    const tbody = el('tbody');
    for (const code of codes) {
      const cells = byOrg.get(code);
      tbody.appendChild(el('tr', {}, [
        el('td', { class: 'org-col' }, [el('b', { text: code })]),
        ...Array.from({ length: rounds }, (_, i) => el('td', {}, cells[i] ? [cellFor(cells[i])] : [])),
      ]));
    }
    box.appendChild(el('section', { class: 'term-block' }, [
      el('div', { class: 'term-title' }, [
        el('b', { text: b.name + (b.stage ? ' · этап ' + b.stage : '') }), el('span', { class: 'sub', text: summary }),
        orderLabel(ord),
      ]),
      el('div', { class: 'term-scroll' }, [el('table', { class: 'term-table' }, [el('thead', {}, [head]), tbody])]),
    ]));
  }
}

// ------------------------------------------------------------ plan-graph

const DAY = 86400000;
const ms = (iso) => Date.parse(iso.slice(0, 10) + 'T00:00:00Z');

// One time axis for all blocks, so bars of different projects can be compared by eye.
function timeRange(list, orders = []) {
  const today = terms.data.today;
  let lo = Infinity, hi = ms(today);
  for (const s of list) {
    lo = Math.min(lo, ms(s.sent_at));
    for (const d of [s.answered_at, s.due]) if (d) hi = Math.max(hi, ms(d));
  }
  // the red line of an order is shown if it ends within two years from today (further — only the label)
  for (const o of orders) {
    // + 45 days so that the hatched «after» area is clearly visible
    if (o.valid_until && ms(o.valid_until) <= ms(today) + 730 * DAY) hi = Math.max(hi, ms(o.valid_until) + 45 * DAY);
  }
  // whole months, a little air on both sides
  const a = new Date(lo); const start = Date.UTC(a.getUTCFullYear(), a.getUTCMonth(), 1);
  const b = new Date(hi + 7 * DAY); const end = Date.UTC(b.getUTCFullYear(), b.getUTCMonth() + 1, 1);
  return { start, end, span: end - start, today: ms(today) };
}

const G_MONTHS = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

function ganttAxis(range) {
  const axis = el('div', { class: 'g-axis' });
  const d = new Date(range.start);
  while (d.getTime() < range.end) {
    const left = ((d.getTime() - range.start) / range.span) * 100;
    const m = d.getUTCMonth();
    axis.appendChild(el('span', { class: 'g-month' + (m === 0 ? ' year' : ''), style: `left:${left}%`,
      text: G_MONTHS[m] + (m === 0 || d.getTime() === range.start ? ' ' + d.getUTCFullYear() : '') }));
    d.setUTCMonth(m + 1);
  }
  return axis;
}

function pct(t, range) { return ((t - range.start) / range.span) * 100; }

// «Распоряжение № … до …» next to the block title: opens the order; the admin sets the date here.
// Red when it ends within 60 days or has ended.
function orderLabel(o) {
  if (!o) return canMark() ? el('span', { class: 'order-label unset', text: 'распоряжение в 01_КГА не найдено' }) : null;
  const left = o.valid_until ? Math.round((ms(o.valid_until) - ms(terms.data.today)) / DAY) : null;
  const cls = 'order-label' + (left == null ? ' unset' : left < 0 ? ' expired' : left <= ORDER_WARN_DAYS ? ' soon' : '');
  const box = el('span', { class: cls });
  box.append(el('a', { class: 'order-file', href: '/file/' + o.doc_id, target: '_blank', rel: 'noopener',
    title: `Распоряжение № ${o.number} — открыть`, 'aria-label': 'Открыть распоряжение ' + o.number },
    [el('span', { class: 'mdi mdi-file-document-outline' })]));
  const date = o.valid_until
    ? ' до ' + fmtDate(o.valid_until) + (left < 0 ? ' · истекло' : left <= ORDER_WARN_DAYS ? ` · осталось ${daysText(left)}` : '')
    : '';
  if (!canMark()) { if (date) box.append(date); return box; }
  const btn = el('button', { type: 'button', class: 'linkish', text: o.valid_until ? date.trim() : 'указать срок действия',
    title: 'До какого числа действует распоряжение' });
  btn.addEventListener('click', () => {
    const field = dateField(o.valid_until || '');
    const ok = el('button', { type: 'button', class: 'df-ok', text: 'OK' });
    const cancel = el('button', { type: 'button', class: 'df-cancel', title: 'Отмена', 'aria-label': 'Отмена' },
      [el('span', { class: 'mdi mdi-close' })]);
    const editor = el('span', { class: 'order-edit' }, [field, ok, cancel]);
    btn.replaceWith(editor);
    field.focus();
    const save = async () => {
      if (!field.valid()) { field.classList.add('invalid'); field.focus(); return; }
      try {
        const r = await api('/api/admin/documents/' + o.doc_id, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ valid_until: field.value || null }) });
        o.valid_until = r.valid_until;
      } catch (err) { alert('Не сохранилось: ' + err.message); return; }
      renderTerms();
    };
    ok.addEventListener('click', save);
    cancel.addEventListener('click', () => renderTerms());
    field.input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); save(); }
      if (e.key === 'Escape') renderTerms();
    });
  });
  box.append(' ', btn);
  return box;
}

// the number of days at the end of a bar; when an answer came, it opens the answer document
function daysMark(s) {
  const text = s.days != null ? String(s.days) : '';
  if (!s.answer || ['waiting', 'unanswered'].includes(s.state)) return el('span', { class: 'g-days', text });
  const link = el('span', { class: 'g-days link', role: 'link', tabindex: '0', text,
    title: `Открыть ответ: ${s.answer.file_name}` + (s.answer.decision_label ? ` — ${s.answer.decision_label}` : '') + `, ${fmtDate(s.answer.date)}` });
  const open = (e) => { e.stopPropagation(); e.preventDefault(); window.open('/file/' + s.answer.id, '_blank', 'noopener'); };
  link.addEventListener('click', open);
  link.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(e); });
  return link;
}

function ganttFor(items, codes, range, orderUntil = null) {
  const orderAt = orderUntil && ms(orderUntil) < range.end ? ms(orderUntil) : null;
  const wrap = el('div', { class: 'gantt' });
  const grid = el('div', { class: 'g-grid' }, [el('div', { class: 'g-label' }), ganttAxis(range)]);
  wrap.appendChild(grid);
  for (const code of codes) {
    const track = el('div', { class: 'g-track' });
    // month lines and today
    const d = new Date(range.start);
    while (d.getTime() < range.end) {
      track.appendChild(el('span', { class: 'g-line', style: `left:${pct(d.getTime(), range)}%` }));
      d.setUTCMonth(d.getUTCMonth() + 1);
    }
    track.appendChild(el('span', { class: 'g-today', style: `left:${pct(range.today, range)}%`, title: 'Сегодня' }));
    if (orderAt != null) {
      // hatched: the time after the order has ended
      track.appendChild(el('span', { class: 'g-order-after', style: `left:${pct(orderAt, range)}%`,
        title: 'После окончания распоряжения (' + fmtDate(orderUntil) + ')' }));
      track.appendChild(el('span', { class: 'g-order', style: `left:${pct(orderAt, range)}%`,
        title: 'Распоряжение действует до ' + fmtDate(orderUntil) }));
    }
    const rounds = items.filter((x) => x.org_code === code).sort((a, b) => a.round - b.round);
    for (const [i, s] of rounds.entries()) {
      const from = ms(s.sent_at);
      // an unanswered round ends where the next round starts (it was sent again)
      const next = rounds[i + 1];
      const to = !['waiting', 'unanswered'].includes(s.state) && s.answered_at ? ms(s.answered_at)
        : next ? Math.min(ms(next.sent_at), range.today) : range.today;
      const left = pct(from, range), width = Math.max(pct(to, range) - left, 0.6);
      const title = [`${code} · круг ${s.round}: ${s.state_label}`, `Отправлено ${fmtDate(s.sent_at)}`,
        s.state === 'waiting' ? `Ждём ${daysText(s.days)}` : s.state === 'unanswered' ? `Без ответа ${daysText(s.days)}, затем повторная отправка`
          : `Ответ ${fmtDate(s.answered_at)} · ${daysText(s.days)}`,
        s.due ? `Ответ по плану ${fmtDate(s.due)}` : ''].filter(Boolean).join('\n');
      // waiting for an answer planned after the order ends
      const late = orderUntil && s.state === 'waiting' && s.due && s.due.slice(0, 10) > orderUntil;
      const bar = el(canMark() ? 'button' : 'span', {
        class: 'g-bar ' + (STATE_CLASS[s.state] || '') + (s.overdue ? ' overdue' : '') + (late ? ' past-order' : ''),
        type: canMark() ? 'button' : null,
        style: `left:${left}%;width:${width}%`, title: title + (late ? '\nОтвет по плану позже окончания распоряжения' : ''),
      }, [daysMark(s)]);
      if (canMark()) bar.addEventListener('click', () => editSending(s));
      track.appendChild(bar);
      if (s.release_id) {
        const sq = el('button', { type: 'button', class: 'g-rel', style: `left:${left}%`,
          title: 'Открыть выпуск' + (s.release_label ? ': ' + s.release_label : ''), 'aria-label': 'Открыть выпуск' });
        sq.addEventListener('click', async (e) => {
          e.stopPropagation();
          await openRelease(s.release_id);
          state.showView('releases');
        });
        track.appendChild(sq);
      }
      if (s.due && s.state === 'waiting') {
        track.appendChild(el('span', { class: 'g-due' + (s.overdue ? ' overdue' : ''), style: `left:${pct(ms(s.due), range)}%`,
          title: `${code}: ответ по плану ${fmtDate(s.due)}` }));
      }
    }
    wrap.appendChild(el('div', { class: 'g-grid' }, [el('div', { class: 'g-label' }, [el('b', { text: code })]), track]));
  }
  return el('div', { class: 'term-scroll' }, [wrap]);
}

function ganttLegend() {
  return el('div', { class: 'g-legend' }, [
    el('span', {}, [el('i', { class: 'g-sw positive' }), 'согласовано']),
    el('span', {}, [el('i', { class: 'g-sw negative' }), 'отказ']),
    el('span', {}, [el('i', { class: 'g-sw waiting' }), 'ждём']),
    el('span', {}, [el('i', { class: 'g-sw overdue' }), 'просрочено']),
    el('span', {}, [el('i', { class: 'g-sw neutral' }), 'нет ответа, отправлено повторно']),
    el('span', {}, [el('i', { class: 'g-due-sw' }), 'ответ по плану']),
    el('span', {}, [el('i', { class: 'g-today-sw' }), 'сегодня']),
    el('span', {}, [el('i', { class: 'g-rel-sw' }), 'выпуск']),
    el('span', { class: 'sub', text: 'подчёркнутое число — открыть ответ' }),
    el('span', {}, [el('i', { class: 'g-order-sw' }), el('i', { class: 'g-order-after-sw' }), 'после конца распоряжения']),
    el('span', { class: 'sub', text: 'число на полосе — дней' }),
  ]);
}

// the latest round of a committee decides the block summary
function isLast(s, items) {
  return !items.some((o) => o.org_code === s.org_code && o.round > s.round);
}

// ------------------------------------------------------------ editing

function field(label, input, hint) {
  return el('label', { class: 'field' }, [el('span', { class: 'k', text: label }), input,
    hint ? el('span', { class: 'sub', text: hint }) : null]);
}

function dateInput(v) { return dateField((v || '').slice(0, 10)); }

async function editSending(s) {
  const m = modal(`${s.org_code} · ${s.project_name}${s.stage ? ' · этап ' + s.stage : ''} · круг ${s.round}`);
  const status = el('select', {}, [el('option', { value: '', text: 'автоматически (' + (s.answer ? s.answer.decision_label || 'по ответу' : 'ждём') + ')' }),
    ...Object.entries(terms.data.statuses).map(([k, v]) => el('option', { value: k, text: v }))]);
  status.value = s.status || '';
  const sent = dateInput(s.sent_at);
  const due = dateInput(s.due);
  const answered = dateInput(s.answer_date);
  const appNo = el('input', { type: 'text', value: s.application_no || '' });
  const note = el('input', { type: 'text', value: s.note || '' });
  const answer = el('select', {}, [el('option', { value: 'auto', text: 'Найти автоматически' }),
    el('option', { value: 'none', text: 'Нет ответа (отвязать)' })]);
  answer.value = s.answer_mode === 'none' ? 'none' : 'auto';
  m.body.append(el('p', { class: 'hint', text: s.answer
    ? `Ответ: ${s.answer.file_name} от ${fmtDate(s.answer.date)}` + (s.answer_auto ? ' — найден автоматически.' : ' — привязан вручную.')
    : 'Ответ не найден.' }));
  m.body.append(el('div', { class: 'form-grid' }, [
    field('Статус', status), field('Ответ', answer),
    field('Дата отправки', sent), field('Ответ по плану', due, `по умолчанию +${terms.data.default_days} дней`),
    field('Дата ответа', answered, 'пусто — дата документа'), field('№ заявления', appNo),
    el('div', { class: 'field wide' }, [field('Примечание', note)]),
  ]));
  // candidates: documents of this committee for the project
  api(`/api/admin/sendings/${s.id}/candidates`).then((c) => {
    for (const d of c.candidates) {
      answer.appendChild(el('option', { value: 'doc:' + d.id,
        text: `${fmtDate(d.date)} · ${d.file_name}` + (d.stage ? ` · этап ${d.stage}` : '') }));
    }
    if (s.answer_mode === 'manual' && s.answer_doc_id) answer.value = 'doc:' + s.answer_doc_id;
  }).catch(() => {});
  const save = el('button', { class: 'primary', type: 'button' }, [el('span', { class: 'mdi mdi-content-save-outline' }), 'Сохранить']);
  const del = el('button', { class: 'danger', type: 'button' }, [el('span', { class: 'mdi mdi-delete-outline' }), 'Удалить отправку']);
  const err = el('span', { class: 'sub' });
  m.body.append(el('div', { class: 'modal-foot' }, [del, err, el('span', { class: 'spacer' }), save]));
  save.addEventListener('click', async () => {
    const a = answer.value;
    try {
      await api('/api/admin/sendings/' + s.id, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          status: status.value || null, sent_at: sent.value, due: due.value || null, answer_date: answered.value || null,
          application_no: appNo.value.trim() || null, note: note.value.trim() || null,
          answer_mode: a === 'auto' ? null : a === 'none' ? 'none' : 'manual',
          answer_doc_id: a.startsWith('doc:') ? Number(a.slice(4)) : null,
        }),
      });
    } catch (e) { err.textContent = 'Ошибка: ' + e.message; return; }
    m.back.remove();
    afterChange();
  });
  del.addEventListener('click', async () => {
    if (!confirm(`Удалить отправку в ${s.org_code} от ${fmtDate(s.sent_at)}?`)) return;
    await api('/api/admin/sendings/' + s.id, { method: 'DELETE' });
    m.back.remove();
    afterChange();
  });
}

function addSending(preset = {}) {
  const m = modal('Добавить отправку');
  const proj = el('select', {}, terms.data.projects.map((p) =>
    el('option', { value: JSON.stringify([p.project_type, p.project_name]), text: projectLabel(p) })));
  if (preset.project_name) proj.value = JSON.stringify([preset.project_type || null, preset.project_name]);
  else if (terms.project) {
    const p = terms.data.projects.find((x) => x.project_name === terms.project);
    if (p) proj.value = JSON.stringify([p.project_type, p.project_name]);
  }
  const stage = el('input', { type: 'text', value: preset.stage || '', placeholder: 'нет', style: 'width:80px' });
  const sent = dateInput(new Date().toISOString());
  const due = dateInput('');
  const appNo = el('input', { type: 'text' });
  const note = el('input', { type: 'text', placeholder: 'например: бумагой, письмом' });
  const chosen = new Set();
  const orgs = el('div', { class: 'org-pick' });
  for (const c of [...terms.data.committees].sort((a, b) => b.active - a.active)) {
    const box = el('input', { type: 'checkbox' });
    box.addEventListener('change', () => { if (box.checked) chosen.add(c.code); else chosen.delete(c.code); });
    orgs.appendChild(el('label', { class: 'org-opt' + (c.active ? '' : ' off'), title: c.name }, [box, c.code]));
  }
  m.body.append(el('p', { class: 'hint', text: 'Для отправок без выпуска (бумагой, письмом, через портал без среза). На каждый отмеченный комитет — своя отправка; ответ найдётся автоматически.' }));
  m.body.append(el('div', { class: 'form-grid' }, [
    field('Проект', proj), field('Этап', stage),
    field('Дата отправки', sent), field('Ответ по плану', due, 'пусто — по сроку организации (у Заказчика — без срока)'),
    field('№ заявления', appNo), field('Примечание', note),
    el('div', { class: 'field wide' }, [el('span', { class: 'k', text: 'Кому' }), orgs]),
  ]));
  const save = el('button', { class: 'primary', type: 'button' }, [el('span', { class: 'mdi mdi-plus' }), 'Добавить']);
  const err = el('span', { class: 'sub' });
  m.body.append(el('div', { class: 'modal-foot' }, [err, el('span', { class: 'spacer' }), save]));
  save.addEventListener('click', async () => {
    const [ptype, pname] = JSON.parse(proj.value || '[null,""]');
    try {
      await api('/api/admin/sendings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_type: ptype, project_name: pname, stage: stage.value.trim() || null,
          sent_at: sent.value, due: due.value || null, application_no: appNo.value.trim() || null,
          note: note.value.trim() || null, org_codes: [...chosen] }),
      });
    } catch (e) { err.textContent = 'Ошибка: ' + e.message; return; }
    m.back.remove();
    afterChange();
  });
}

function afterChange() {
  if (state.view === 'terms') window.showTerms();
  else if (rel.current) openRelease(rel.current.release.id);
}

// sendings of one release — a block in its card
window.releaseSendings = async function releaseSendings(r, box) {
  let data;
  try { data = await api(`/api/releases/${r.id}/sendings`); } catch (e) { return; }
  if (!terms.data) { try { terms.data = await api('/api/sendings'); } catch (e) { return; } }
  if (!data.sendings.length) return;
  const wrap = el('div', { class: 'rel-sendings' }, [el('span', { class: 'k', text: 'Отправки' }),
    ...data.sendings.map(pillFor)]);
  box.appendChild(wrap);
};

// one-line sending: «КГА  ждём · 5 дн. · план 03.12»
function pillFor(s) {
  const tail = s.state === 'waiting'
    ? `${s.days} дн. · ${s.overdue ? 'просрочено, ' : ''}план ${shortDate(s.due)}`
    : s.state === 'unanswered' ? `${s.days} дн. до повторной` : `${shortDate(s.answered_at)} · ${s.days} дн.`;
  const node = el(canMark() ? 'button' : 'span', {
    class: 'pill ' + (STATE_CLASS[s.state] || '') + (s.overdue ? ' overdue' : ''), type: canMark() ? 'button' : null,
    title: `Отправлено ${fmtDate(s.sent_at)}` + (s.answer ? `\nОтвет: ${s.answer.file_name}` : ''),
  }, [el('b', { text: s.org_code }), el('span', { class: 'pill-state', text: SHORT_STATE[s.state] || s.state_label }), el('span', { class: 'sub', text: tail })]);
  if (canMark()) node.addEventListener('click', () => editSending(s));
  return node;
}

$('terms-new').addEventListener('click', () => addSending());
for (const b of document.querySelectorAll('#terms-mode button')) {
  b.setAttribute('aria-pressed', b.dataset.mode === terms.mode ? 'true' : 'false');
  b.addEventListener('click', () => {
    terms.mode = b.dataset.mode;
    writeStore('graddok.termsMode', terms.mode);
    for (const o of document.querySelectorAll('#terms-mode button')) o.setAttribute('aria-pressed', o === b ? 'true' : 'false');
    renderTerms();
  });
}
$('terms-project').addEventListener('change', (e) => { terms.project = e.target.value; renderTerms(); });
$('terms-search').addEventListener('input', (e) => { terms.query = e.target.value; renderTerms(); });
