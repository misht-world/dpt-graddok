'use strict';
// Sheet of answers to the remarks of one letter: items, answers, notes, statuses, pictures; Word export.
// Saved automatically 1.5 s after a change; another person's newer save is not overwritten (409).

const sid = Number(new URLSearchParams(location.search).get('id'));
const S = { picked: new Set(), sheet: null, doc: null, items: [], statuses: {}, canEdit: false, canDelete: false, version: null,
            dirty: false, timer: null, saving: false, lastRemark: null };
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, children = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v; else if (k === 'text') n.textContent = v; else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of [].concat(children)) if (c) n.append(typeof c === 'string' ? document.createTextNode(c) : c);
  return n;
}
const p2 = (n) => String(n).padStart(2, '0');
function fmtDate(iso) { const m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})/); return m ? `${m[3]}-${m[2]}-${m[1]}` : ''; }
function parseDate(text) {
  const m = String(text || '').trim().match(/^(\d{1,2})[-./](\d{1,2})[-./](\d{4})$/);
  if (!m) return null;
  const d = new Date(Date.UTC(+m[3], +m[2] - 1, +m[1]));
  return d.getUTCDate() === +m[1] && d.getUTCMonth() === +m[2] - 1 ? `${m[3]}-${p2(m[2])}-${p2(m[1])}` : null;
}

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  if (res.status === 401) { location.href = '/login'; throw new Error('нет входа'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.detail || 'Ошибка ' + res.status); e.status = res.status; throw e; }
  return data;
}

// ------------------------------------------------------------------ saving

function status(text, cls = '') {
  const s = $('rm-save');
  if (s) { s.textContent = text; s.className = 'sub rm-save ' + cls; }
}

function changed() {
  if (!S.canEdit) return;
  S.dirty = true;
  status('Есть несохранённые правки…');
  clearTimeout(S.timer);
  S.timer = setTimeout(save, 1500);
  renderCounts();
}

async function save() {
  if (S.saving) { clearTimeout(S.timer); S.timer = setTimeout(save, 1000); return; }   // a save is running: try after it
  if (!S.dirty) return;
  S.saving = true;
  S.dirty = false;
  status('Сохраняю…');
  const date = parseDate($('rm-date') ? $('rm-date').value : '');
  try {
    const r = await api('/api/remarks/' + sid, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version: S.version, letter_no: $('rm-no') ? $('rm-no').value : S.sheet.letter_no,
        letter_date: date || S.sheet.letter_date, items: S.items }) });
    S.version = r.version;
    status('Сохранено ' + new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }), 'ok');
  } catch (err) {
    S.dirty = true;
    if (err.status === 409) {
      const b = $('rm-banner');
      b.hidden = false;
      b.textContent = err.message;
      status('Не сохранено', 'bad');
      S.canEdit = false;          // stop: do not overwrite someone else's work
      return;
    }
    status('Не сохранено: ' + err.message + ' — повторю', 'bad');
    clearTimeout(S.timer);
    S.timer = setTimeout(save, 5000);
  } finally {
    S.saving = false;
  }
}

window.addEventListener('beforeunload', (e) => { if (S.dirty) { save(); e.preventDefault(); e.returnValue = ''; } });

// -------------------------------------------------------------- pictures

async function uploadImage(file) {
  const fd = new FormData();
  fd.append('file', file, file.name || 'picture.png');
  return api('/api/remarks-images', { method: 'POST', body: fd });
}

async function addImages(item, key, files, rerender) {
  for (const f of files) {
    if (!/^image\//.test(f.type)) continue;
    status('Загружаю картинку…');
    try { item[key].push(await uploadImage(f)); } catch (err) { alert('Картинка не добавлена: ' + err.message); }
  }
  rerender();
  changed();
}

function imagesBox(item, key) {
  const box = el('div', { class: 'rm-images' });
  const draw = () => {
    box.textContent = '';
    for (const [i, img] of item[key].entries()) {
      const a = el('a', { href: '/remarks-image/' + img.sha, target: '_blank', rel: 'noopener', title: 'Открыть картинку' },
        [el('img', { src: '/remarks-image/' + img.sha, alt: '' })]);
      const wrap = el('span', { class: 'rm-img' }, [a]);
      if (S.canEdit) {
        const x = el('button', { type: 'button', class: 'rm-img-x', title: 'Убрать картинку', 'aria-label': 'Убрать картинку' },
          [el('span', { class: 'mdi mdi-close' })]);
        x.addEventListener('click', () => { item[key].splice(i, 1); draw(); changed(); });
        wrap.append(x);
      }
      box.append(wrap);
    }
  };
  draw();
  box.redraw = draw;
  return box;
}

// ------------------------------------------------------------------ items

function grow(t) { t.style.height = 'auto'; t.style.height = t.scrollHeight + 2 + 'px'; }

function field(item, key, imgKey, placeholder) {
  const t = el('textarea', { rows: '2', placeholder: S.canEdit ? placeholder : '', readonly: !S.canEdit });
  t.value = item[key] || '';
  t.addEventListener('input', () => { item[key] = t.value; grow(t); changed(); });
  requestAnimationFrame(() => grow(t));
  if (!imgKey) return el('div', { class: 'rm-field' }, [t]);
  const pics = imagesBox(item, imgKey);
  if (S.canEdit) {
    // Ctrl+V of a screenshot, or dropping a picture file, adds it to this cell
    t.addEventListener('paste', (e) => {
      const files = [...(e.clipboardData ? e.clipboardData.files : [])].filter((f) => /^image\//.test(f.type));
      if (files.length) { e.preventDefault(); addImages(item, imgKey, files, pics.redraw); }
    });
    t.addEventListener('dragover', (e) => e.preventDefault());
    t.addEventListener('drop', (e) => {
      const files = [...e.dataTransfer.files].filter((f) => /^image\//.test(f.type));
      if (files.length) { e.preventDefault(); addImages(item, imgKey, files, pics.redraw); }
    });
    const pick = el('input', { type: 'file', accept: 'image/png,image/jpeg,image/gif', multiple: true, hidden: true });
    pick.addEventListener('change', () => { addImages(item, imgKey, [...pick.files], pics.redraw); pick.value = ''; });
    const btn = el('button', { type: 'button', class: 'linkish rm-addpic', title: 'Или вставьте картинку в поле (Ctrl+V) / перетащите файл' },
      [el('span', { class: 'mdi mdi-image-plus-outline' }), ' картинка']);
    btn.addEventListener('click', () => pick.click());
    return el('div', { class: 'rm-field' }, [t, pics, btn, pick]);
  }
  return el('div', { class: 'rm-field' }, [t, pics]);
}

// ------------------------------------------------------- note: text + pictures

// The note is written like a small document: text, a picture where the cursor is, text again.
// Kept as [{t:'text', v}, {t:'img', sha, type}] — the same order goes to Word.
function blocksFromDom(root) {
  const out = [];
  let text = '';
  const flush = () => { if (text) { out.push({ t: 'text', v: text }); text = ''; } };
  const walk = (node) => {
    for (const n of node.childNodes) {
      if (n.nodeType === 3) { text += n.nodeValue; continue; }
      if (n.nodeType !== 1) continue;
      if (n.tagName === 'IMG' && n.dataset.sha) { flush(); out.push({ t: 'img', sha: n.dataset.sha, type: n.dataset.type || null }); continue; }
      if (n.tagName === 'BR') { text += '\n'; continue; }
      const block = /^(DIV|P)$/.test(n.tagName);
      if (block && (text || out.length) && !text.endsWith('\n')) text += '\n';
      walk(n);
    }
  };
  walk(root);
  flush();
  return out;
}

function renderBlocks(box, blocks) {
  box.textContent = '';
  for (const b of blocks || []) {
    if (b.t === 'img') {
      box.append(el('img', { src: '/remarks-image/' + b.sha, 'data-sha': b.sha, 'data-type': b.type || '', alt: 'картинка', class: 'rm-inline' }));
    } else {
      box.append(document.createTextNode(b.v));
    }
  }
}

function insertAtCursor(box, node) {
  const sel = window.getSelection();
  if (sel && sel.rangeCount && box.contains(sel.getRangeAt(0).startContainer)) {
    const r = sel.getRangeAt(0);
    r.deleteContents();
    r.insertNode(node);
    r.setStartAfter(node);
    r.collapse(true);
    sel.removeAllRanges();
    sel.addRange(r);
  } else {
    box.append(node);
  }
}

function richNote(item) {
  const box = el('div', { class: 'rm-rich', contenteditable: S.canEdit ? 'true' : 'false', role: 'textbox', 'aria-multiline': 'true',
    'data-placeholder': S.canEdit ? 'Примечание — текст и картинки (Ctrl+V) вперемешку' : '' });
  renderBlocks(box, item.note_blocks);
  const sync = () => {
    item.note_blocks = blocksFromDom(box);
    item.note = item.note_blocks.filter((b) => b.t === 'text').map((b) => b.v).join('\n').trim();
    item.note_images = item.note_blocks.filter((b) => b.t === 'img').map((b) => ({ sha: b.sha, type: b.type }));
    box.classList.toggle('empty', !item.note_blocks.length);
    changed();
  };
  box.classList.toggle('empty', !(item.note_blocks || []).length);
  if (!S.canEdit) return el('div', { class: 'rm-field' }, [box]);

  const addFiles = async (files) => {
    for (const f of files) {
      if (!/^image\//.test(f.type)) continue;
      status('Загружаю картинку…');
      try {
        const img = await uploadImage(f);
        const node = el('img', { src: '/remarks-image/' + img.sha, 'data-sha': img.sha, 'data-type': img.type, alt: 'картинка', class: 'rm-inline' });
        insertAtCursor(box, node);
      } catch (err) { alert('Картинка не добавлена: ' + err.message); }
    }
    sync();
  };
  box.addEventListener('input', sync);
  box.addEventListener('paste', (e) => {
    const files = [...(e.clipboardData ? e.clipboardData.files : [])].filter((f) => /^image\//.test(f.type));
    e.preventDefault();
    if (files.length) { addFiles(files); return; }
    // only plain text: formatting from Word or a browser would not go to the table anyway
    const text = e.clipboardData ? e.clipboardData.getData('text/plain') : '';
    if (text) { if (!document.execCommand('insertText', false, text)) insertAtCursor(box, document.createTextNode(text)); sync(); }
  });
  box.addEventListener('dragover', (e) => e.preventDefault());
  box.addEventListener('drop', (e) => {
    const files = [...e.dataTransfer.files].filter((f) => /^image\//.test(f.type));
    if (files.length) { e.preventDefault(); box.focus(); addFiles(files); }
  });
  box.addEventListener('keydown', (e) => {
    // Enter: a plain line break (not a new <div>) so that the text stays simple
    if (e.key === 'Enter') {
      e.preventDefault();
      if (!document.execCommand('insertLineBreak')) insertAtCursor(box, el('br'));
      sync();
    }
  });
  const pick = el('input', { type: 'file', accept: 'image/png,image/jpeg,image/gif', multiple: true, hidden: true });
  pick.addEventListener('change', () => { box.focus(); addFiles([...pick.files]); pick.value = ''; });
  const btn = el('button', { type: 'button', class: 'linkish rm-addpic', title: 'Картинка встанет туда, где курсор. Удалить — Backspace/Delete' },
    [el('span', { class: 'mdi mdi-image-plus-outline' }), ' картинка в текст']);
  btn.addEventListener('mousedown', (e) => e.preventDefault());   // keep the cursor where it was
  btn.addEventListener('click', () => pick.click());
  return el('div', { class: 'rm-field' }, [box, btn, pick]);
}

function iconBtn(icon, title, onClick, disabled = false) {
  const b = el('button', { type: 'button', class: 'icon-btn', title, 'aria-label': title, disabled }, [el('span', { class: 'mdi ' + icon })]);
  b.addEventListener('click', onClick);
  return b;
}

function blank() { return { remark: '', answer: '', note: '', status: 'work', answer_images: [], note_images: [], note_blocks: [], repeat: null }; }

// «повторное»: the same remark came in an earlier letter of this committee
function repeatMark(item) {
  const rp = item.repeat;
  if (!rp) return null;
  const when = [rp.letter_no ? '№ ' + rp.letter_no : '', rp.letter_date ? 'от ' + fmtDate(rp.letter_date) : ''].filter(Boolean).join(' ');
  return el('a', { class: 'rm-repeat', href: '/remarks?id=' + rp.sheet_id, target: '_blank', rel: 'noopener',
    title: `Было в письме ${when} (совпадение ${Math.round((rp.similarity || 0) * 100)}%):\n${rp.remark}`
      + (rp.answer ? `\n\nПрошлый ответ:\n${rp.answer}` : '\n\nПрошлого ответа нет') },
    [el('span', { class: 'mdi mdi-repeat' }), ` повторное — было в письме ${when}`]);
}

function renderItems() {
  const box = $('rm-items');
  box.textContent = '';
  if (!S.items.length) {
    box.append(el('div', { class: 'empty-state' }, [el('strong', { text: 'Пунктов нет' }),
      S.sheet.text_ok ? 'Добавьте пункты кнопкой «Добавить пункт».' : 'В письме нет текстового слоя (скан). Добавьте пункты и вставьте текст вручную.']));
    return;
  }
  const head = el('div', { class: 'rm-row rm-headrow' }, ['№', 'Замечание', 'Ответ', 'Примечание', 'Статус'].map((x) => el('div', { text: x })));
  box.append(head);
  S.items.forEach((item, i) => {
    const remark = field(item, 'remark', null, 'Текст замечания');
    const rmark = repeatMark(item);
    if (rmark) remark.append(rmark);
    remark.querySelector('textarea').addEventListener('focus', (e) => { S.lastRemark = { i, t: e.target }; });
    const st = el('select', { disabled: !S.canEdit, 'aria-label': 'Статус' },
      Object.entries(S.statuses).map(([k, v]) => el('option', { value: k, text: v, selected: item.status === k })));
    st.addEventListener('change', () => { item.status = st.value; row.dataset.status = st.value; changed(); });
    const acts = S.canEdit ? el('div', { class: 'rm-acts' }, [
      iconBtn('mdi-arrow-up', 'Выше', () => move(i, -1), i === 0),
      iconBtn('mdi-arrow-down', 'Ниже', () => move(i, 1), i === S.items.length - 1),
      iconBtn('mdi-format-page-split', 'Разделить замечание там, где стоит курсор', () => split(i)),
      iconBtn('mdi-plus', 'Вставить пустой пункт ниже', () => { S.items.splice(i + 1, 0, blank()); renderItems(); changed(); }),
      iconBtn('mdi-delete-outline', 'Удалить пункт', () => { if (confirm(`Удалить пункт ${i + 1}?`)) { S.items.splice(i, 1); renderItems(); changed(); } }),
    ]) : null;
    const row = el('div', { class: 'rm-row' }, [
      el('div', { class: 'rm-no' }, [
        S.canEdit ? (() => {
          const tick = el('input', { type: 'checkbox', title: 'Отметить пункт (объединить / удалить отмеченные)', 'aria-label': `Отметить пункт ${i + 1}` });
          tick.checked = S.picked.has(item);
          tick.addEventListener('change', () => { if (tick.checked) S.picked.add(item); else S.picked.delete(item); renderPicked(); });
          return tick;
        })() : null,
        el('span', { text: String(i + 1) })]),
      remark, field(item, 'answer', 'answer_images', 'Ответ'), richNote(item),
      el('div', { class: 'rm-st' }, [st, acts]),
    ]);
    row.dataset.status = item.status;
    box.append(row);
  });
}

function move(i, d) { const [x] = S.items.splice(i, 1); S.items.splice(i + d, 0, x); renderItems(); changed(); }

// ticks: merge the picked items into the first of them (in their order), or delete them
function renderPicked() {
  const n = S.picked.size;
  const m = $('rm-merge'), d = $('rm-delsel');
  if (m) { m.disabled = n < 2; m.lastChild.textContent = n >= 2 ? `Объединить отмеченные (${n})` : 'Объединить отмеченные'; }
  if (d) { d.disabled = n < 1; d.lastChild.textContent = n ? `Удалить отмеченные (${n})` : 'Удалить отмеченные'; }
}

function mergePicked() {
  const picked = S.items.filter((x) => S.picked.has(x));
  if (picked.length < 2) return;
  const nums = picked.map((x) => S.items.indexOf(x) + 1).join(', ');
  if (!confirm(`Объединить пункты ${nums} в один?`)) return;
  const [first, ...rest] = picked;
  for (const k of ['remark', 'answer', 'note']) first[k] = picked.map((x) => x[k]).filter((x) => x && x.trim()).join('\n');
  first.note_blocks = picked.flatMap((x, i) => (i && x.note_blocks.length ? [{ t: 'text', v: '\n' }] : []).concat(x.note_blocks));
  first.answer_images = picked.flatMap((x) => x.answer_images);
  first.note_images = picked.flatMap((x) => x.note_images);
  first.repeat = first.repeat || (rest.find((x) => x.repeat) || {}).repeat || null;
  S.items = S.items.filter((x) => !rest.includes(x));
  S.picked.clear();
  renderItems();
  renderPicked();
  changed();
}

function deletePicked() {
  const picked = S.items.filter((x) => S.picked.has(x));
  if (!picked.length) return;
  if (!confirm(`Удалить пункты ${picked.map((x) => S.items.indexOf(x) + 1).join(', ')}?`)) return;
  S.items = S.items.filter((x) => !S.picked.has(x));
  S.picked.clear();
  renderItems();
  renderPicked();
  changed();
}

function split(i) {
  const cur = S.lastRemark;
  if (!cur || cur.i !== i) { alert('Поставьте курсор в тексте замечания туда, где его нужно разделить, и нажмите снова.'); return; }
  const pos = cur.t.selectionStart;
  const text = S.items[i].remark;
  if (pos <= 0 || pos >= text.length) { alert('Курсор в начале или в конце текста — делить нечего.'); return; }
  S.items.splice(i + 1, 0, { ...blank(), remark: text.slice(pos).trim() });
  S.items[i].remark = text.slice(0, pos).trim();
  S.lastRemark = null;
  renderItems();
  changed();
}

// ------------------------------------------------------------- head & tools

function renderCounts() {
  const c = $('rm-counts');
  if (!c) return;
  const n = (k) => S.items.filter((x) => x.status === k).length;
  const rep = S.items.filter((x) => x.repeat).length;
  c.textContent = `Пунктов ${S.items.length}: в работе ${n('work')}, учтено ${n('done')}, не согласны ${n('disagree')}`
    + (rep ? `; повторных ${rep}` : '');
}

// which sending this letter answers (from «Сроки») and whether the answer has gone back
function roundLine() {
  const s = S.sending;
  if (!s || !s.round) return el('p', { class: 'sub', text: 'В «Сроках» нет отправки, на которую это письмо — ответ.' });
  const parts = [`Ответ на отправку ${fmtDate(s.sent_at)} (круг ${s.round})`];
  parts.push(s.next_sent_at ? `ответ на замечания отправлен ${fmtDate(s.next_sent_at)} (круг ${s.round + 1})`
    : 'повторной отправки пока нет — после ответа зарегистрируйте выпуск, появится круг ' + (s.round + 1));
  return el('p', { class: 'rm-round' + (s.next_sent_at ? ' sent' : '') }, [el('span', { class: 'mdi mdi-timer-sand' }), ' ' + parts.join(' · ')]);
}

function renderHead() {
  const d = S.doc;
  document.title = 'Замечания: ' + d.file_name + ' — ГРАДДОК';
  const project = [d.project_type, d.project_name].filter(Boolean).join(' · ') + (d.stage ? ', этап ' + d.stage : '');
  const no = el('input', { type: 'text', id: 'rm-no', value: S.sheet.letter_no || '', placeholder: 'номер письма', readonly: !S.canEdit });
  const date = el('input', { type: 'text', id: 'rm-date', value: fmtDate(S.sheet.letter_date), placeholder: 'ДД-ММ-ГГГГ',
    readonly: !S.canEdit, inputmode: 'numeric', maxlength: '10' });
  no.addEventListener('input', changed);
  date.addEventListener('input', () => { date.classList.toggle('bad', !!date.value && !parseDate(date.value)); if (parseDate(date.value)) changed(); });
  $('rm-head').replaceChildren(
    el('h2', { class: 'rm-title' }, [
      el('a', { href: '/file/' + d.id, target: '_blank', rel: 'noopener', title: 'Открыть письмо' },
        [el('span', { class: 'mdi mdi-file-document-outline' }), ' ', d.file_name])]),
    el('div', { class: 'rm-meta' }, [
      el('span', {}, [el('span', { class: 'k', text: 'Проект' }), project || '—']),
      el('span', {}, [el('span', { class: 'k', text: 'От кого' }), el('b', { text: d.org_code || '—' }), d.org_name ? ' ' + d.org_name : '']),
      el('span', {}, [el('span', { class: 'k', text: '№' }), no]),
      el('span', {}, [el('span', { class: 'k', text: 'Дата' }), date]),
    ]),
    roundLine(),
    el('p', { class: 'sub', id: 'rm-counts' }));
  renderCounts();
}

function renderTools() {
  const word = (notes) => el('a', { class: 'btn' + (notes ? ' primary' : ''), href: `/remarks/${sid}/word?notes=${notes ? 1 : 0}` },
    [el('span', { class: 'mdi mdi-file-word-outline' }), notes ? 'Word с примечаниями' : 'Word без примечаний']);
  const tools = [word(true), word(false)];
  if (S.canEdit) {
    const add = el('button', { type: 'button' }, [el('span', { class: 'mdi mdi-plus' }), 'Добавить пункт']);
    add.addEventListener('click', () => { S.items.push(blank()); renderItems(); changed(); });
    const again = el('button', { type: 'button', title: 'Разрезать письмо заново: ответы будут потеряны' },
      [el('span', { class: 'mdi mdi-refresh' }), 'Разобрать заново']);
    again.addEventListener('click', async () => {
      if (!confirm('Разобрать письмо заново? Все пункты, ответы и картинки этого листа будут заменены.')) return;
      try { load(await api(`/api/remarks/${sid}/reparse`, { method: 'POST' })); } catch (err) { alert(err.message); }
    });
    const rep = el('button', { type: 'button', title: 'Сравнить пункты с прошлыми письмами этого комитета по проекту' },
      [el('span', { class: 'mdi mdi-repeat' }), 'Найти повторы']);
    rep.addEventListener('click', async () => {
      if (S.dirty) await save();
      try {
        const r = await api(`/api/remarks/${sid}/repeats`, { method: 'POST' });
        load(r);
        status(r.found ? `Повторных: ${r.found}. Пустые ответы заполнены прошлыми — проверьте.` : 'Повторов не найдено', 'ok');
      } catch (err) { alert(err.message); }
    });
    const merge = el('button', { type: 'button', id: 'rm-merge', disabled: true }, [el('span', { class: 'mdi mdi-call-merge' }), el('span', { text: 'Объединить отмеченные' })]);
    merge.addEventListener('click', mergePicked);
    const delSel = el('button', { type: 'button', id: 'rm-delsel', disabled: true }, [el('span', { class: 'mdi mdi-delete-outline' }), el('span', { text: 'Удалить отмеченные' })]);
    delSel.addEventListener('click', deletePicked);
    tools.push(add, merge, delSel, rep, again);
  }
  if (S.canDelete) {
    const del = el('button', { type: 'button', class: 'danger' }, [el('span', { class: 'mdi mdi-delete-outline' }), 'Удалить лист']);
    del.addEventListener('click', async () => {
      if (!confirm('Удалить лист ответов целиком? Письмо в каталоге останется.')) return;
      await api('/api/remarks/' + sid, { method: 'DELETE' });
      S.dirty = false;
      location.href = '/';
    });
    tools.push(del);
  }
  tools.push(el('span', { class: 'spacer' }), el('span', { class: 'sub rm-save', id: 'rm-save',
    text: S.canEdit ? 'Правки сохраняются сами' : 'Только просмотр' }));
  $('rm-tools').replaceChildren(...tools);
  $('rm-foot').replaceChildren(S.canEdit ? el('p', { class: 'sub',
    text: 'Картинку в ответ или примечание: скопируйте её и нажмите Ctrl+V в поле, перетащите файл в поле или нажмите «картинка». PNG, JPEG, GIF до 10 МБ.' }) : '');
}

function load(data) {
  S.picked = new Set();
  S.sheet = data.sheet;
  S.doc = data.doc;
  S.items = data.items.map((x) => ({ remark: x.remark, answer: x.answer, note: x.note, status: x.status,
    answer_images: x.answer_images || [], note_images: x.note_images || [], repeat: x.repeat || null,
    note_blocks: x.note_blocks || [] }));
  S.sending = data.sending || {};
  S.statuses = data.statuses;
  S.canEdit = data.can_edit;
  S.canDelete = data.can_delete;
  S.version = data.sheet.updated_at;
  S.dirty = false;
  renderHead();
  renderTools();
  renderItems();
}

(async () => {
  if (!sid) { $('rm-head').textContent = 'Не указан лист.'; return; }
  try { load(await api('/api/remarks/' + sid)); } catch (err) { $('rm-head').textContent = 'Не открылся лист: ' + err.message; }
})();
