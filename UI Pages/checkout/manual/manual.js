(function(){
  const $ = s => document.querySelector(s);
  const list = $('#list'), empty = $('#empty'), q = $('#q'), clearBtn = $('#clear');

  // Modal bits
  const modal = $('#modal'), mMPN = $('#mMPN'), mMfr = $('#mMfr'), mBin = $('#mBin'), mQty = $('#mQty');
  const mCancel = $('#mCancel'), mConfirm = $('#mConfirm');

  // Toast
  const toast = $('#toast');
  function showToast(msg){
    toast.textContent = msg;
    toast.classList.add('show'); toast.removeAttribute('hidden');
    setTimeout(()=>{ toast.classList.remove('show'); toast.setAttribute('hidden',''); }, 1600);
  }

  // State
  let rows = [];     // from backend
  let filtered = [];
  let pending = null;

  async function fetchAvailable(){
    const res = await fetch('/api/available_parts');
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'Failed to load');

    // Normalize keys defensively
    return (data.items || []).map(it => ({
      part_id:            it.part_id ?? it.id ?? null,
      mpn:                it.mpn ?? it.MPN ?? '',
      manufacturer:       it.manufacturer ?? it.mfr ?? '',
      position_code:      it.position_code ?? it.bin ?? it.location ?? '',
      qty_on_hand:        it.qty_on_hand ?? it.quantity ?? it.qty ?? 0
    }));
  }

  function render(){
    list.innerHTML = '';
    if (!filtered.length){ empty.removeAttribute('hidden'); return; }
    empty.setAttribute('hidden','');

    for (const it of filtered){
      const row = document.createElement('div');
      row.className = 'card-row';
      row.innerHTML = `
        <div>
          <div class="mpn">${it.mpn}</div>
          <div class="mfr">${it.manufacturer || ''}</div>
        </div>
        <div class="cell" style="text-align:center">
          <small>Bin</small>
          <div class="pill pill-blue">${it.position_code}</div>
        </div>
        <div class="cell" style="text-align:center">
          <small>Qty</small>
          <div class="qty">${it.qty_on_hand}</div>
        </div>
        <div style="display:flex;justify-content:flex-end">
          <button class="btn btn-primary" type="button">Checkout</button>
        </div>
      `;
      row.querySelector('button').addEventListener('click', () => openModal(it));
      list.appendChild(row);
    }
  }

  function applyFilter(){
    const term = (q.value || '').trim().toLowerCase();
    filtered = !term ? rows.slice()
                     : rows.filter(it =>
                         (it.mpn||'').toLowerCase().includes(term) ||
                         (it.manufacturer||'').toLowerCase().includes(term) ||
                         (it.position_code||'').toLowerCase().includes(term));
    render();
  }

  function openModal(it){
    pending = { ...it };                     // ensure not null
    mMPN.textContent = it.mpn || '';
    mMfr.textContent = it.manufacturer || '';
    mBin.textContent = it.position_code || '';
    mQty.textContent = it.qty_on_hand ?? '—';
    mConfirm.disabled = false;
    modal.removeAttribute('hidden');
  }
  function closeModal(){ modal.setAttribute('hidden',''); pending = null; }

  async function doCheckout(){
    // Guard
    if (!pending || !pending.part_id){
        alert('Nothing selected to checkout.'); return;
    }
    if (!pending.position_code){
        alert('Missing bin for this item.'); return;
    }

    // Make a stable copy BEFORE any mutation
    const snapshot = { ...pending };

    mConfirm.disabled = true;
    try {
        const res = await fetch('/api/checkout_part', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
            part_id: snapshot.part_id,
            position_code: snapshot.position_code
        })
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || 'Checkout failed');

        // Use the snapshot for messaging, THEN close & null out state
        showToast(`Checked out ${data.moved} from ${snapshot.position_code}`);
        closeModal(); // sets pending = null

        // Hard refresh so the list reflects the change immediately
        // (use reload(true) behavior across browsers)
        setTimeout(() => { window.location.reload(); }, 250);
    } catch (e) {
        alert(e.message);
        mConfirm.disabled = false;
    }
    }


  // Wire up
  mCancel.addEventListener('click', closeModal);
  mConfirm.addEventListener('click', doCheckout);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
  q.addEventListener('input', applyFilter);
  clearBtn.addEventListener('click', () => { q.value=''; applyFilter(); });

  // Init
  (async () => {
    try { rows = await fetchAvailable(); filtered = rows.slice(); render(); }
    catch (e) {
      empty.textContent = 'Error loading parts.'; empty.removeAttribute('hidden'); console.error(e);
    }
  })();
})();
