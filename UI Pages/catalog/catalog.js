(function(){
  // ===== Filtering =====
  const q = document.getElementById('freetext');
  const table = document.getElementById('partsTable');
  const rows = table ? Array.from(table.querySelectorAll('tbody tr.part-row')) : [];
  const norm = s => (s || '').toString().toLowerCase();

  function match(row, needle){
    if (!needle) return true;
    const mpn = norm(row.dataset.mpn);
    const mfr = norm(row.dataset.mfr);
    const attrs = norm(row.dataset.attrs);
    return mpn.includes(needle) || mfr.includes(needle) || attrs.includes(needle);
  }
  function applyFilter() {
    const needle = norm(q?.value?.trim());
    rows.forEach(r => { r.style.display = match(r, needle) ? '' : 'none'; });
  }
  if (q) q.addEventListener('input', applyFilter);

  // ===== LocalStorage List (unique items, no qty) =====
  const LS_KEY = 'catalogList';
  const listItems = document.getElementById('listItems');
  const listCount = document.getElementById('listCount');

  function loadList(){
    try { return JSON.parse(localStorage.getItem(LS_KEY) || '[]'); }
    catch { return []; }
  }
  function saveList(list){ localStorage.setItem(LS_KEY, JSON.stringify(list)); }
  function hasPart(list, part_id){ return list.some(x => String(x.part_id) === String(part_id)); }

  function renderList(){
    const list = loadList();
    if (!listItems) return;
    listItems.innerHTML = '';
    if (!list.length){
      listItems.classList.add('empty-state');
      listItems.textContent = 'No items yet. Click “Add to List”.';
      if (listCount) listCount.textContent = '(0)';
      return;
    }
    listItems.classList.remove('empty-state');
    list.forEach(item => {
      const row = document.createElement('div');
      row.className = 'list-row';
      row.innerHTML = `
        <div class="mpn" title="${item.description || ''}">${item.mpn}</div>
        <button class="remove" title="Remove">✕</button>
      `;
      row.querySelector('.remove').addEventListener('click', () => {
        const next = loadList().filter(x => String(x.part_id) !== String(item.part_id));
        saveList(next);
        renderList();
      });
      listItems.appendChild(row);
    });
    if (listCount) listCount.textContent = `(${list.length})`;
  }

  // Add to list button (blue)
  rows.forEach(r => {
    const btn = r.querySelector('.add-to-list');
    const payloadRaw = r.getAttribute('data-part');
    if (!btn || !payloadRaw) return;
    const data = JSON.parse(payloadRaw);

    btn.addEventListener('click', () => {
      const list = loadList();
      if (!hasPart(list, data.part_id)){
        list.push({
          part_id: data.part_id,
          mpn: data.mpn,
          manufacturer: data.manufacturer || '',
          description: data.description || '',
          category_id: data.category_id || '',
          image_url: data.image_url || '',
          datasheet_url: data.datasheet_url || '',
          product_url: data.product_url || ''
        });
        saveList(list);
        renderList();
        btn.textContent = 'Added ✓';
        btn.disabled = true;
        setTimeout(() => { btn.textContent = 'Add to List'; btn.disabled = false; }, 800);
      } else {
        btn.textContent = 'Already Added';
        btn.disabled = true;
        setTimeout(() => { btn.textContent = 'Add to List'; btn.disabled = false; }, 800);
      }
    });
  });

  // Copy single MPN (📋 icon)
  rows.forEach(r => {
    const btnCopy = r.querySelector('.copy-mpn');
    if (!btnCopy) return;
    const mpn = r.dataset.mpn || '';
    btnCopy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(mpn);
        btnCopy.textContent = '✓';
        setTimeout(()=> btnCopy.textContent = '📋', 600);
      } catch { alert('Copy failed'); }
    });
  });

  // ===== CSV export =====
  function listToCSV(list){
    const header = ['MPN','Manufacturer','Description','DatasheetURL','ProductURL'];
    const lines = [header.join(',')];
    list.forEach(it => {
      const row = [
        csvEscape(it.mpn),
        csvEscape(it.manufacturer || ''),
        csvEscape(it.description || ''),
        csvEscape(it.datasheet_url || ''),
        csvEscape(it.product_url || '')
      ];
      lines.push(row.join(','));
    });
    return lines.join('\n');
  }
  function csvEscape(s){
    if (s == null) return '';
    const t = String(s);
    if (/[",\n]/.test(t)) return `"${t.replace(/"/g,'""')}"`;
    return t;
  }

  const btnCopyCSV = document.getElementById('btnCopyCSV');
  const btnDownloadCSV = document.getElementById('btnDownloadCSV');
  const btnClearList = document.getElementById('btnClearList');

  if (btnCopyCSV) btnCopyCSV.addEventListener('click', async () => {
    const list = loadList();
    if (!list.length) return;
    const csv = listToCSV(list);
    try {
      await navigator.clipboard.writeText(csv);
      btnCopyCSV.textContent = 'Copied!';
      setTimeout(()=> btnCopyCSV.textContent = 'Copy CSV', 900);
    } catch {
      alert('Clipboard blocked—download instead.');
    }
  });

  if (btnDownloadCSV) btnDownloadCSV.addEventListener('click', () => {
    const list = loadList();
    if (!list.length) return;
    const csv = listToCSV(list);
    const blob = new Blob([csv], {type:'text/csv;charset=utf-8;'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const ts = new Date().toISOString().slice(0,19).replace(/[:T]/g,'-');
    a.href = url;
    a.download = `parts-list-${ts}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  });

  if (btnClearList) btnClearList.addEventListener('click', () => {
    if (!confirm('Clear the entire list?')) return;
    saveList([]);
    renderList();
  });


  // ===== QR download =====
  const btnDownloadQR = document.getElementById('btnDownloadQR');

  /** Build plain text payload: newline-separated MPNs (easy to parse with splitlines()). */
  function buildQRText(list){
    const mpns = list.map(x => (x.mpn || '').trim()).filter(Boolean);
    return mpns.join('\n');
  }

  /** Split payload into chunks to keep each QR small for reliable scanning. */
  function chunkByBytes(str, maxBytes=1800){
    const enc = new TextEncoder();
    const lines = str.split('\n');
    const chunks = [];
    let current = [];
    let size = 0;

    for (const line of lines){
      const candidate = (current.length ? '\n' : '') + line;
      const bytes = enc.encode(candidate).length;
      if (size + bytes > maxBytes){
        if (current.length) chunks.push(current.join('\n'));
        current = [line];
        size = enc.encode(line).length;
      } else {
        current.push(line);
        size += bytes;
      }
    }
    if (current.length) chunks.push(current.join('\n'));
    return chunks;
  }

  /**
   * Generate a PNG data URL using either:
   *  - qrcode (https://github.com/soldair/node-qrcode): QRCode.toDataURL(...)
   *  - qrcodejs (https://github.com/davidshimjs/qrcodejs): new QRCode(el, {...}) then read canvas/img
   */
  async function payloadToDataURL(payload){
    // Library A: soldair/node-qrcode → has QRCode.toDataURL
    if (window.QRCode && typeof window.QRCode.toDataURL === 'function'){
      return await window.QRCode.toDataURL(payload, { errorCorrectionLevel: 'M', margin: 2, scale: 8 });
    }

    // Library B: davidshimjs/qrcodejs → constructor-based
    if (window.QRCode){
      const tmp = document.createElement('div');
      tmp.style.position = 'fixed';
      tmp.style.left = '-9999px';
      document.body.appendChild(tmp);

      // correctLevel constant if available; fallback to 1 (=L)
      const level = window.QRCode.CorrectLevel ? window.QRCode.CorrectLevel.M : 1;

      // Create QR into hidden container
      // eslint-disable-next-line no-new
      new window.QRCode(tmp, {
        text: payload,
        width: 512,
        height: 512,
        correctLevel: level
      });

      // Prefer canvas → dataURL; else image src
      let dataUrl = null;
      const canvas = tmp.querySelector('canvas');
      if (canvas && canvas.toDataURL) {
        dataUrl = canvas.toDataURL('image/png');
      } else {
        const img = tmp.querySelector('img');
        dataUrl = img ? img.src : null;
      }
      tmp.remove();
      if (dataUrl) return dataUrl;
    }

    // Neither library is present/usable
    return null;
  }

  async function downloadQRFromText(text, baseName='parts-list-qr'){
    const ts = new Date().toISOString().slice(0,19).replace(/[:T]/g,'-');
    const chunks = chunkByBytes(text, 1800); // conservative for reliability
    let idx = 1;

    for (const payload of chunks){
      const dataUrl = await payloadToDataURL(payload);
      if (!dataUrl) { alert('QR library not loaded.'); return; }

      const a = document.createElement('a');
      a.href = dataUrl;
      a.download = `${baseName}-${ts}${chunks.length>1?`-${idx}`:''}.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      idx++;
    }
  }

  if (btnDownloadQR) btnDownloadQR.addEventListener('click', async () => {
    const list = (function(){
      try { return JSON.parse(localStorage.getItem('catalogList') || '[]'); } catch { return []; }
    })();
    if (!list.length) return;
    const text = buildQRText(list);
    await downloadQRFromText(text);
  });



  // initial render
  renderList();
})();
