'use strict';
// Uploading an approval by hand (e.g. from a phone) and resolving possible duplicates.
// Relies on app.js (el, $, canMark, load, fmtDate) and releases.js (api, modal).

// Marks under the file name of an uploaded document.
function uploadMarks(d) {
  const box = el('div', { class: 'up-marks' }, [
    el('span', { class: 'badge onserver', text: 'только на сервере',
      title: 'Загружен вручную, на ПК его ещё нет. Скачайте и сохраните в папку проекта на Диске — при следующем «Актуализировать» записи объединятся.' }),
  ]);
  if (d.similar && d.similar.length) {
    const dup = el(canMark() ? 'button' : 'span', { class: 'badge dup', type: canMark() ? 'button' : null,
      text: 'возможно, дубль', title: 'На ПК есть похожий документ: ' + d.similar.map((s) => s.file_name).join(', ') });
    if (canMark()) dup.addEventListener('click', (e) => { e.stopPropagation(); resolveDuplicate(d); });
    box.appendChild(dup);
  }
  return box;
}

function resolveDuplicate(d) {
  const m = modal('Возможно, это тот же документ');
  m.body.append(el('p', { class: 'hint', text: 'Загруженный вручную документ похож на найденный на ПК: тот же проект, этап, комитет и решение, близкая дата. Содержимое файлов разное.' }));
  m.body.append(el('div', {}, [el('span', { class: 'k', text: 'Загружен вручную: ' }),
    el('a', { href: '/file/' + d.id, target: '_blank', rel: 'noopener', text: d.file_name })]));
  for (const s of d.similar) {
    const same = el('button', { class: 'primary', type: 'button' }, [el('span', { class: 'mdi mdi-merge' }), 'Это тот же документ']);
    same.addEventListener('click', async () => {
      await api(`/api/admin/uploads/${d.id}/merge`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: s.id }) });
      m.back.remove(); load();
    });
    m.body.append(el('div', { class: 'modal-foot' }, [
      el('span', {}, ['На ПК: ', el('a', { href: '/file/' + s.id, target: '_blank', rel: 'noopener', text: s.file_name })]),
      el('span', { class: 'spacer' }), same]));
  }
  const distinct = el('button', { type: 'button', text: 'Разные документы — оставить оба' });
  distinct.addEventListener('click', async () => {
    await api(`/api/admin/uploads/${d.id}/distinct`, { method: 'POST' });
    m.back.remove(); load();
  });
  m.body.append(el('p', { class: 'sub', text: '«Это тот же документ» оставит файл с ПК, перенесёт на него привязки к отправкам и удалит загруженный.' }),
    el('div', { class: 'modal-foot' }, [el('span', { class: 'spacer' }), distinct]));
}

// ------------------------------------------------------------ upload form

function openUpload() {
  const m = modal('Загрузить согласование');
  const input = el('input', { type: 'file', accept: 'application/pdf,.pdf', hidden: true });
  const drop = el('div', { class: 'up-drop' }, [el('span', { class: 'mdi mdi-file-upload-outline' }),
    ' Выберите PDF или перетащите его сюда']);
  drop.addEventListener('click', () => input.click());
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files[0]) form(e.dataTransfer.files[0]); });
  input.addEventListener('change', () => { if (input.files[0]) form(input.files[0]); });
  m.body.append(el('p', { class: 'hint', text: 'Файл сразу появится в каталоге с пометкой «только на сервере». Потом сохраните его в папку проекта на Диске — при «Актуализировать» записи объединятся.' }), drop, input);

  async function form(file) {
    if (!/\.pdf$/i.test(file.name)) { alert('Нужен PDF'); return; }
    let p;
    try {
      p = await api('/api/admin/upload/parse', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_name: file.name }) });
    } catch (e) { alert('Ошибка: ' + e.message); return; }
    m.body.textContent = '';
    const field = (label, inp, hint) => el('label', { class: 'field' }, [el('span', { class: 'k', text: label }), inp,
      hint ? el('span', { class: 'sub', text: hint }) : null]);

    const name = el('input', { type: 'text', value: p.suggested || file.name });
    const keep = el('button', { type: 'button', class: 'ghost', text: 'исходное' });
    keep.addEventListener('click', () => { name.value = file.name; });
    const proj = el('select', {}, [el('option', { value: '', text: '— выберите —' }),
      ...p.projects.map((x) => el('option', { value: x.project_key, text: [x.project_type, x.project_name].filter(Boolean).join(' · ') }))]);
    let lastProject = '';
    try { lastProject = localStorage.getItem('graddok.uploadProject') || ''; } catch (e) { /* private mode */ }
    if (p.projects.some((x) => x.project_key === lastProject)) proj.value = lastProject;
    const dec = el('select', {}, [el('option', { value: '', text: '—' }), ...p.decisions.map((d) => el('option', { value: d.id, text: d.label }))]);
    dec.value = p.decision_id || '';
    const org = el('select', {}, [el('option', { value: '', text: '—' }),
      ...[...p.committees].sort((a, b) => b.active - a.active).map((c) => el('option', { value: c.code, text: c.code, title: c.name }))]);
    org.value = p.org_code || '';
    const stage = el('input', { type: 'text', value: p.stage || '', placeholder: 'нет', style: 'width:80px' });
    const stageField = () => stage.closest('label');
    const syncStage = () => {
      const pr = p.projects.find((x) => x.project_key === proj.value);
      if (stageField()) stageField().hidden = !!pr && !pr.stages;
      if (pr && !pr.stages) stage.value = '';
    };
    proj.addEventListener('change', syncStage);
    const when = dateField(p.doc_date || new Date().toISOString().slice(0, 10));
    const note = el('input', { type: 'text', value: p.note || '' });
    const rename = () => {
      const d = p.decisions.find((x) => x.id === dec.value);
      const n = /[A-Za-zА-Яа-яЁё]{3}/.test(note.value) ? note.value.trim() : '';
      if (d && org.value) name.value = [d.label, org.value, n].filter(Boolean).join(' ') + '.pdf';
    };
    const suggestBtn = el('button', { type: 'button', class: 'ghost', text: 'по решению и комитету' });
    suggestBtn.addEventListener('click', rename);

    m.body.append(
      el('div', { class: 'stat-line' }, [el('span', { class: 'mdi mdi-file-pdf-box' }), ' ', el('b', { text: file.name }),
        el('span', { class: 'sub', text: ' · ' + sizeText(file.size) })]),
      el('div', { class: 'form-grid' }, [
        el('div', { class: 'field wide' }, [el('span', { class: 'k', text: 'Имя файла' }),
          el('div', { class: 'up-name' }, [name, suggestBtn, keep]),
          el('span', { class: 'sub', text: 'Так файл будет называться в каталоге — сохраните его на Диск под этим же именем.' })]),
        field('Проект', proj), field('Этап', stage),
        field('Решение', dec), field('От кого', org),
        field('Дата документа', when, 'по ней считается срок ответа'), field('Примечание', note),
      ]),
    );
    syncStage();
    const go = el('button', { class: 'primary', type: 'button' }, [el('span', { class: 'mdi mdi-upload' }), 'Загрузить']);
    const status = el('span', { class: 'sub' });
    m.body.append(el('div', { class: 'modal-foot' }, [status, el('span', { class: 'spacer' }), go]));

    go.addEventListener('click', async () => {
      if (!proj.value) { status.textContent = 'Выберите проект'; return; }
      try { when.value; } catch (e) { status.textContent = e.message; return; }
      go.disabled = true; status.textContent = 'Загружаю…';
      const fd = new FormData();
      fd.append('file', file);
      for (const [k, v] of Object.entries({ project_key: proj.value, file_name: name.value.trim() || file.name,
        decision_id: dec.value, org_code: org.value, stage: stage.value.trim(), doc_date: when.value, note: note.value.trim() })) fd.append(k, v);
      let res;
      try {
        res = await api('/api/admin/upload', { method: 'POST', body: fd });
      } catch (e) { status.textContent = 'Ошибка: ' + e.message; go.disabled = false; return; }
      try { localStorage.setItem('graddok.uploadProject', proj.value); } catch (e) { /* private mode */ }
      m.body.textContent = '';
      m.body.append(el('div', { class: 'up-done' }, [
        el('b', { text: 'Загружено.' }), ' Не забудьте сохранить файл на Диск',
        res.folder ? [' — в папку:', el('br'), el('code', { text: res.folder })] : ' — в папку согласований проекта.',
      ].flat()), el('div', { class: 'modal-foot' }, [
        el('a', { class: 'btn', href: '/download/' + res.id }, [el('span', { class: 'mdi mdi-download' }), 'Скачать файл']),
        el('span', { class: 'spacer' }),
        (() => { const b = el('button', { class: 'primary', type: 'button', text: 'Готово' }); b.addEventListener('click', () => m.back.remove()); return b; })(),
      ]));
      load();
    });
  }
}

$('upload-btn').addEventListener('click', openUpload);
