'use strict';
// Quick look at Word and Excel files of a release, rendered in the browser.
// Layout of complex Word documents is approximate; the «Скачать» button gives the original.
// Usage: /static/viewer.html?src=/release/<id>/file/<fid>&name=<file name>

const MAX_ROWS = 3000;     // a huge sheet would freeze a phone; the rest is in the file

const params = new URLSearchParams(location.search);
const src = params.get('src') || '';
const name = params.get('name') || 'документ';
const msg = document.getElementById('msg');
const out = document.getElementById('out');

function el(tag, attrs = {}, children = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'text') n.textContent = v; else if (k === 'class') n.className = v; else n.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c) n.append(c);
  return n;
}

function fail(text) { msg.hidden = false; msg.textContent = text; }

async function main() {
  // only files of this site: the page never fetches anything else
  if (!/^\/release\/\d+\/file\/\d+$/.test(src)) { fail('Неверная ссылка на файл.'); return; }
  document.title = name + ' — ГРАДДОК';
  document.getElementById('name').textContent = name;
  document.getElementById('dl').href = src + '?dl=1';
  const ext = (name.match(/\.([^.]+)$/) || [, ''])[1].toLowerCase();
  document.getElementById('kind').textContent = ext === 'docx' ? 'Word, просмотр приблизительный' : 'Excel';

  const res = await fetch(src + '?dl=1');
  if (res.status === 401) { location.href = '/login'; return; }
  if (!res.ok) { fail('Файл не получен (ошибка ' + res.status + ').'); return; }
  const data = await res.arrayBuffer();

  if (ext === 'docx') {
    if (!window.docx) { fail('Не загрузилась библиотека просмотра Word — проверьте интернет или скачайте файл.'); return; }
    const box = el('div', { id: 'doc' });
    out.append(box);
    await window.docx.renderAsync(data, box, null, { inWrapper: true, ignoreLastRenderedPageBreak: true, experimental: true });
    msg.hidden = true;
    return;
  }

  if (!window.XLSX) { fail('Не загрузилась библиотека просмотра Excel — проверьте интернет или скачайте файл.'); return; }
  const wb = window.XLSX.read(data, { type: 'array', cellDates: true });
  const tabs = el('div', { class: 'tabs', role: 'tablist' });
  const note = el('div', { class: 'note', hidden: '' });
  const sheet = el('div', { id: 'sheet' });
  out.append(tabs, note, sheet);
  const show = (sheetName, btn) => {
    for (const b of tabs.children) b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
    const ws = wb.Sheets[sheetName];
    const range = window.XLSX.utils.decode_range(ws['!ref'] || 'A1:A1');
    const rows = range.e.r - range.s.r + 1;
    if (rows > MAX_ROWS) {
      range.e.r = range.s.r + MAX_ROWS - 1;
      ws['!ref'] = window.XLSX.utils.encode_range(range);
      note.hidden = false;
      note.textContent = `Показаны первые ${MAX_ROWS} строк из ${rows} — остальное в файле.`;
    } else {
      note.hidden = true;
    }
    // sheet_to_html escapes cell text; the result goes into our own container only
    sheet.innerHTML = window.XLSX.utils.sheet_to_html(ws, { header: '', footer: '' });
  };
  wb.SheetNames.forEach((sn, i) => {
    const b = el('button', { type: 'button', role: 'tab', text: sn });
    b.addEventListener('click', () => show(sn, b));
    tabs.append(b);
    if (i === 0) show(sn, b);
  });
  if (wb.SheetNames.length < 2) tabs.hidden = true;
  msg.hidden = true;
}

main().catch((e) => fail('Не удалось показать файл: ' + e.message + '. Скачайте его кнопкой вверху.'));
