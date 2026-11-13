'use strict';
// «Замечания» tab: sheets of answers to committee remarks; ticks → summary table / chronology.
// Relies on app.js (el, $, fmtDate) and releases.js (api).

const rmList = { picked: new Set() };

window.showRemarks = async function showRemarks() {
  const box = $('remarks');
  box.textContent = '';
  let data;
  try { data = await api('/api/remarks'); } catch (err) {
    box.appendChild(el('div', { class: 'empty-state', text: 'Ошибка: ' + err.message }));
    return;
  }
  if (!data.sheets.length) {
    box.appendChild(el('div', { class: 'empty-state' }, [el('strong', { text: 'Листов пока нет' }),
      data.can_edit ? 'В «Письмах» нажмите на метку «Замечания» или «Отказ» у письма.' : '']));
    return;
  }
  const known = new Set(data.sheets.map((s) => s.sheet_id));
  for (const id of [...rmList.picked]) if (!known.has(id)) rmList.picked.delete(id);

  const go = el('button', { type: 'button', class: 'primary' }, [el('span', { class: 'mdi mdi-table-merge-cells' }), 'Сводная таблица']);
  const note = el('span', { class: 'sub' });
  const bar = el('div', { class: 'rm-listbar' }, [go, note]);
  const refresh = () => {
    go.disabled = rmList.picked.size < 2;
    note.textContent = rmList.picked.size < 2
      ? 'Отметьте два листа или больше: разные комитеты — сводная таблица, один комитет в разное время — хронология.'
      : `Отмечено: ${rmList.picked.size}`;
  };
  go.addEventListener('click', () => window.open('/remarks-summary?ids=' + [...rmList.picked].join(','), '_blank', 'noopener'));

  // grouped by project; inside — newest letters first
  const groups = new Map();
  for (const s of data.sheets) {
    const d = s.doc;
    const key = [d.project_type, d.project_name].filter(Boolean).join(' · ') || '— без проекта';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  const tbody = el('tbody');
  for (const key of [...groups.keys()].sort((a, b) => a.localeCompare(b, 'ru'))) {
    const list = groups.get(key);
    const done = list.reduce((a, x) => a + x.done, 0), total = list.reduce((a, x) => a + x.total, 0);
    tbody.appendChild(el('tr', { class: 'rm-group' }, [el('td', { colspan: '6' }, [
      el('b', { text: key }), el('span', { class: 'sub', text: `  ${list.length} ${list.length === 1 ? 'письмо' : list.length < 5 ? 'письма' : 'писем'} · ответы ${done} из ${total}` })])]));
    for (const s of list) {
      const d = s.doc;
      const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
      const tick = el('input', { type: 'checkbox', 'aria-label': 'Отметить лист' });
      tick.checked = rmList.picked.has(s.sheet_id);
      tick.addEventListener('change', () => { if (tick.checked) rmList.picked.add(s.sheet_id); else rmList.picked.delete(s.sheet_id); refresh(); });
      // the letter's name opens the letter (PDF) in a new tab; the rest of the row — the sheet of answers
      const file = el('a', { href: '/file/' + d.id, target: '_blank', rel: 'noopener', class: 'fname', text: d.file_name, title: 'Открыть письмо' });
      const tr = el('tr', { class: 'rm-row-link', title: 'Открыть лист ответов' }, [
        el('td', { class: 'pick' }, [tick]),
        el('td', {}, [file, s.text_ok ? null : el('div', { class: 'sub', text: 'скан — пункты вносятся вручную' })]),
        el('td', {}, [el('b', { text: d.org_code || '—' })]),
        el('td', { class: 'nowrap', text: d.stage ? 'этап ' + d.stage : '' }),
        el('td', { class: 'nowrap', text: (s.letter_date ? fmtDate(s.letter_date) : '—') + (s.letter_no ? ' · № ' + s.letter_no : '') }),
        el('td', { class: 'nowrap' }, [el('span', { class: 'rm-progress' }, [el('i', { style: `width:${pct}%` })]), `${s.done} из ${s.total}`]),
      ]);
      tr.addEventListener('click', (e) => {
        if (e.target.closest('a, input, button')) return;
        location.href = '/remarks?id=' + s.sheet_id;
      });
      tbody.appendChild(tr);
    }
  }
  box.append(bar, el('table', { class: 'rel-table rm-table' }, [
    el('thead', {}, [el('tr', {}, ['', 'Письмо', 'Кто', 'Этап', 'Дата', 'Ответы'].map((t) => el('th', { class: 'plain', text: t })))]),
    tbody]));
  refresh();
};
