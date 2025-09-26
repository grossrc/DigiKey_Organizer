(function(){
  // EL helpers
  const $ = id => document.getElementById(id);

  // Steps/containers
  const scanStep = $('scanStep');
  const preflightContainer = $('preflightContainer');
  const guideContainer = $('guideContainer');
  const doneContainer = $('doneContainer');

  // Preflight elements
  const countSummary = $('countSummary');
  const inStockPills = $('inStockPills');
  const missingPills = $('missingPills');
  const btnBegin = $('btnBegin');

  // Guide elements
  const prog = $('prog');
  const gImg = $('gImg');
  const gMPN = $('gMPN');
  const gMfr = $('gMfr');
  const gDesc = $('gDesc');
  const gBin = $('gBin');
  const gQty = $('gQty');
  const btnPulled = $('btnPulled');

  // Done elements
  const doneCount = $('doneCount');
  const doneMissingBlock = $('doneMissingBlock');
  const doneMissingPills = $('doneMissingPills');

  // Camera
  const video = $('video');
  let reader = null;
  let stream = null;

  // State
  let decodedTextChunks = [];
  let resolved = null; // {available:[], missing:[]}
  let queue = [];
  let idx = 0;
  let completed = [];

  // UI show/hide helpers
  function showScan() {
    scanStep.style.display = '';
    preflightContainer.style.display = 'none';
    guideContainer.style.display = 'none';
    doneContainer.style.display = 'none';
  }
  function showPreflight() {
    scanStep.style.display = 'none';
    preflightContainer.style.display = '';
    guideContainer.style.display = 'none';
    doneContainer.style.display = 'none';
  }
  function showGuide() {
    scanStep.style.display = 'none';
    preflightContainer.style.display = 'none';
    guideContainer.style.display = '';
    doneContainer.style.display = 'none';
  }
  function showDone() {
    scanStep.style.display = 'none';
    preflightContainer.style.display = 'none';
    guideContainer.style.display = 'none';
    doneContainer.style.display = '';
  }

  // Camera controls
  async function startCamera(){
    try {
      const ZX = window.ZXing;
      const hints = new Map();
      if (ZX.DecodeHintType && ZX.BarcodeFormat) {
        hints.set(ZX.DecodeHintType.POSSIBLE_FORMATS, [ZX.BarcodeFormat.QR_CODE]);
      }
      reader = new ZX.BrowserMultiFormatReader(hints);
      const devices = await reader.listVideoInputDevices();
      const deviceId = devices[0]?.deviceId || null;

      stream = await navigator.mediaDevices.getUserMedia({
        video: deviceId ? { deviceId: { exact: deviceId } } : { facingMode: 'environment' }
      });
      video.srcObject = stream;
      await video.play();

      reader.decodeFromVideoDevice(deviceId, video, (result, err) => {
        if (result) {
          stopCamera();
          handleDecodedPayload(result.getText());
        }
      });
    } catch (e) {
      alert('Camera error: ' + e.message);
    }
  }
  function stopCamera(){
    try { reader && reader.reset(); } catch {}
    reader = null;
    if (stream){ stream.getTracks().forEach(t => t.stop()); stream = null; }
  }

  // Decode handling
  function uniqLines(s){
    const set = new Set();
    (s || '').split(/[\r\n]+/).forEach(line => {
      const t = line.trim();
      if (t) set.add(t);
    });
    return Array.from(set);
  }
  async function handleDecodedPayload(text){
    decodedTextChunks.push(text || '');
    const allText = decodedTextChunks.join('\n');
    const mpns = uniqLines(allText);
    await resolveMPNs(mpns);
  }

  // Resolve MPNs with backend
  async function resolveMPNs(mpns){
    try {
      const res = await fetch('/api/resolve_mpns', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ mpns })
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'Resolve failed');
      resolved = { available: data.available || [], missing: data.missing || [] };
      queue = [...resolved.available];
      idx = 0;
      renderPreflight();
      showPreflight();
    } catch (e) {
      alert('Resolve error: ' + e.message);
      decodedTextChunks = [];
      showScan();
      startCamera();
    }
  }

  // Render preflight (compact lists, one-liner)
  function pill(parent, label, cls=''){
    const x = document.createElement('span');
    x.className = `pill ${cls}`;
    x.textContent = label;
    parent.appendChild(x);
    }
    function renderPreflight(){
    const total = (resolved.available?.length || 0) + (resolved.missing?.length || 0);
    countSummary.textContent = `${total} part${total===1?'':'s'} decoded`;
    inStockPills.innerHTML = '';
    missingPills.innerHTML = '';
    (resolved.available || []).forEach(it => pill(inStockPills, it.mpn, 'ok small'));
    (resolved.missing || []).forEach(it => pill(missingPills, it.mpn, 'err small'));
    }


  // Guided one-by-one
  let selectedBin = null;

function renderCurrent(){
    const it = queue[idx];
    if (!it) { renderDone(); return; }

    // Header progress at top-right
    prog.textContent = `Item ${idx+1} of ${queue.length}`;

    // Part details
    gImg.src = it.image_url || '';
    gImg.alt = it.mpn ? `Image of ${it.mpn}` : 'Part image';
    gMPN.textContent = it.mpn || '(no MPN)';
    gMfr.textContent = it.manufacturer || '';
    gDesc.textContent = it.description || '';

    // Bin chips (no quantities, prominent)
    const bins = Array.isArray(it.bins) ? it.bins.slice() : [];
    // default order: by qty desc then code; but we don't show qty
    bins.sort((a,b) => (b.qty||0)-(a.qty||0) || String(a.position).localeCompare(String(b.position)));
    const gBinList = document.getElementById('gBinList');
    gBinList.innerHTML = '';

    if (!bins.length){
        const none = document.createElement('div');
        none.className = 'muted';
        none.textContent = 'No bin on record';
        gBinList.appendChild(none);
        selectedBin = null;
        // Disable pulled button since no source bin
        document.getElementById('btnPulled').setAttribute('aria-disabled','true');
        document.getElementById('btnPulled').classList.add('disabled');
    } else {
        // enable pull
        const pulled = document.getElementById('btnPulled');
        pulled.removeAttribute('aria-disabled');
        pulled.classList.remove('disabled');

        bins.forEach((b, i) => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'bin-chip';
        chip.textContent = b.position; // no qty display
        chip.addEventListener('click', () => {
            // set active chip
            Array.from(gBinList.children).forEach(c => c.classList.remove('active'));
            chip.classList.add('active');
            selectedBin = b.position;
        });
        if (i === 0) {
            chip.classList.add('active');
            selectedBin = b.position;
        }
        gBinList.appendChild(chip);
        });
    }

    showGuide();
    }


  async function confirmPulled(){
    const it = queue[idx];
    if (!it) return;

    const position = selectedBin || null;
    if (!position) { alert('No bin selected.'); return; }

    try {
        const res = await fetch('/api/checkout_part', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ part_id: it.part_id, position_code: position })
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || 'Checkout failed');

        completed.push({ part_id: it.part_id, mpn: it.mpn, qty: data.moved, position: data.from });
        idx += 1;
        renderCurrent();
    } catch (e) {
        alert('Checkout error: ' + e.message);
    }
    }


  // Done
  function renderDone(){
    const okCount = completed.length;
    doneCount.textContent = `${okCount} item${okCount===1?'':'s'} checked out successfully.`;
    doneMissingPills.innerHTML = '';
    const miss = resolved?.missing || [];
    if (miss.length){
      miss.forEach(it => pill(doneMissingPills, it.mpn, 'err'));
      doneMissingBlock.style.display = '';
    } else {
      doneMissingBlock.style.display = 'none';
    }
    showDone();
  }

  // Events
  btnBegin.addEventListener('click', () => {
    if (!queue.length) renderDone(); else renderCurrent();
  });
  btnPulled.addEventListener('click', confirmPulled);

  // Start scanning immediately on load
  showScan();
  startCamera();
  window.addEventListener('beforeunload', () => { try { stopCamera(); } catch {} });
})();
