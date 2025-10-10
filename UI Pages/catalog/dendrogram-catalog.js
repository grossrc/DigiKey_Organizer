// Dynamic Hierarchical Category Browser
class TreeCatalog {
  constructor(){
    this.path = []; // array of selected category name segments (exact DB path names)
    this.columnsEl = document.getElementById('tree-columns');
    this.connectionsEl = document.getElementById('tree-connections');
    this.treeWrapper = document.getElementById('tree-wrapper');
  this.cache = new Map(); // in-memory cache: key: depth|prefix.join('>') -> nodes array
  this.persistKeyPrefix = 'dendro_nodes_v1'; // bump version if response shape changes
  this.cacheTTLms = 1000 * 60 * 60 * 6; // 6 hours TTL
    // Search state
    this.searchInput = document.getElementById('dendro-search');
    this.btnPrev = document.getElementById('dendro-prev');
    this.btnNext = document.getElementById('dendro-next');
    this.btnClear = document.getElementById('dendro-clear');
    this.btnReset = document.getElementById('dendro-reset');
    this.matchCountEl = document.getElementById('dendro-match-count');
    this.searchMatches = []; // array of {path, category_id}
    this.matchIndex = -1;
    this.setupEvents();
    this.loadDepth(0); // initial root
    // Expand from URL if provided (?path=A>B>C)
    this.initFromURL();
  }

  setupEvents(){
    window.addEventListener('resize',()=>this.drawConnections());
    if(this.searchInput){
      let debounceTimer=null;
      this.searchInput.addEventListener('input', ()=>{
        clearTimeout(debounceTimer);
        debounceTimer=setTimeout(()=>this.performSearch(), 250);
      });
    }
    this.btnPrev && this.btnPrev.addEventListener('click', ()=>this.cycleMatch(-1));
    this.btnNext && this.btnNext.addEventListener('click', ()=>this.cycleMatch(1));
    this.btnClear && this.btnClear.addEventListener('click', ()=>this.clearSearch());
    this.btnReset && this.btnReset.addEventListener('click', ()=>this.resetAll());

    // Keyboard shortcuts: Esc clears search or resets; Left/Right cycles matches; Enter cycles forward
    document.addEventListener('keydown', (e)=>{
      const activeTag = (document.activeElement && document.activeElement.tagName || '').toLowerCase();
      const typing = activeTag === 'input' || activeTag === 'textarea';

      if(e.key === 'Escape'){
        if(this.searchInput && this.searchInput.value.trim() !== ''){
          this.clearSearch();
          e.preventDefault();
        } else if (this.path.length){
          this.resetAll();
          e.preventDefault();
        }
      }

      if(typing) return; // don't hijack arrows/enter while typing in inputs

      if(e.key === 'ArrowLeft'){
        if(this.searchMatches.length){ this.cycleMatch(-1); e.preventDefault(); }
      } else if(e.key === 'ArrowRight'){
        if(this.searchMatches.length){ this.cycleMatch(1); e.preventDefault(); }
      } else if(e.key === 'Enter'){
        if(this.searchMatches.length){ this.cycleMatch(1); e.preventDefault(); }
      }
    });
  }

  // Build cache key
  _key(depth,prefix){return depth+'|'+prefix.join('>');}
  _persistKey(depth,prefix){ return `${this.persistKeyPrefix}:${depth}:${encodeURIComponent(prefix.join('>'))}`; }

  async initFromURL(){
    try {
      const params = new URLSearchParams(window.location.search||'');
      const raw = params.get('path');
      if(!raw) return;
      const segs = String(raw).split('>').map(s=>s.trim()).filter(Boolean);
      if(!segs.length) return;
      this.path = [];
      for(let d=0; d<segs.length; d++){
        await this.loadDepth(d);
        const nodes = this.getNodesAt(d);
        const wanted = segs[d];
        if(!nodes.some(n=>n.name===wanted)) break;
        this.path[d] = wanted;
      }
      this.render();
      if(this.path.length){ this.highlightDeepest(this.path[this.path.length-1]); }
      this.updateSearchControls();
    } catch(e){ console.warn('initFromURL failed', e); }
  }

  async loadDepth(depth){
    const prefix = this.path.slice(0, depth); // ancestors
    const key = this._key(depth,prefix);
    if(!this.cache.has(key)){
      // Try localStorage first
      const pKey = this._persistKey(depth, prefix);
      try{
        const raw = localStorage.getItem(pKey);
        if(raw){
          const obj = JSON.parse(raw);
          if(obj && obj.t && Array.isArray(obj.nodes)){
            if(Date.now() - obj.t < this.cacheTTLms){
              this.cache.set(key, obj.nodes);
            } else {
              localStorage.removeItem(pKey);
            }
          }
        }
      } catch(_){/* ignore storage errors */}

      if(!this.cache.has(key)){
        const params = new URLSearchParams();
        params.set('depth', depth);
        prefix.forEach(p=>params.append('prefix', p));
        const resp = await fetch('/api/category_nodes?'+params.toString(), {cache:'no-store'});
        const data = await resp.json();
        if(!data.ok){ console.error('Load failed', data.error); return; }
        this.cache.set(key, data.nodes);
        // Persist with timestamp
        try { localStorage.setItem(this._persistKey(depth, prefix), JSON.stringify({t:Date.now(), nodes:data.nodes})); } catch(_){}
      }
    }
    this.render();
  }

  getNodesAt(depth){
    const prefix = this.path.slice(0, depth);
    return this.cache.get(this._key(depth,prefix)) || [];
  }

  render(){
    const depthToRender = this.path.length + 1; // current path + one lookahead column
    const existing=[...this.columnsEl.querySelectorAll('.tree-column')];
    while(existing.length > depthToRender){ existing.pop().remove(); }

    for(let d=0; d<depthToRender; d++){
      const col = existing[d] || this._makeColumn();
      if(!existing[d]) this.columnsEl.appendChild(col);
      col.classList.add('no-anim');
      col.innerHTML='';
      const nodesWrap = document.createElement('div'); nodesWrap.className='tree-nodes';
      const nodes = this.getNodesAt(d);
      nodes.forEach(node=>{
        const div = document.createElement('div');
        div.className='tree-node';
        if(node.final) div.classList.add('final');
        if(node.terminates_here && node.terminates_here > 0) div.classList.add('terminates');
        if(this.path[d] === node.name) div.classList.add('selected');
        if(this.path[d] && this.path[d] !== node.name) div.classList.add('disabled');
        div.dataset.name = node.name;
        div.dataset.depth = d;
        div.innerHTML = `<div class="node-title">${node.name}</div><div class="node-meta">${node.parts} parts</div>`;
        div.addEventListener('click',()=>this.handleNodeClick(node, d));
        nodesWrap.appendChild(div);
      });
      col.appendChild(nodesWrap);
    }
    requestAnimationFrame(()=>this.drawConnections());
  }

  _makeColumn(){
    const c=document.createElement('div');
    c.className='tree-column fade-in';
    return c;
  }

  async handleNodeClick(node, depth){
    // Trim path to this depth, then set selection
    this.path = this.path.slice(0, depth);
    this.path[depth] = node.name;
    // Navigate to list page if this node itself terminates (has direct parts)
    if(node.terminates_here && node.category_id){
      window.location = `/catalog/${encodeURIComponent(node.category_id)}`;
      return;
    }
    // Otherwise if not final, load deeper
    if(!node.final){
      await this.loadDepth(depth+1);
    } else if(node.final && node.category_id){
      // edge case: final leaf also has list page (should always have category_id)
      window.location = `/catalog/${encodeURIComponent(node.category_id)}`;
    }
    // After any node click, re-evaluate reset availability
    this.updateSearchControls();
  }

  drawConnections(){
    this.connectionsEl.innerHTML='';
    const cols=[...this.columnsEl.querySelectorAll('.tree-column')];
    if(cols.length<2) return;
    const wrapperRect=this.treeWrapper.getBoundingClientRect();
    this.connectionsEl.setAttribute('width', wrapperRect.width);
    this.connectionsEl.setAttribute('height', wrapperRect.height);
    for(let i=0;i<cols.length-1;i++){
      const leftNodes=[...cols[i].querySelectorAll('.tree-node')];
      const rightNodes=[...cols[i+1].querySelectorAll('.tree-node')];
      if(!leftNodes.length || !rightNodes.length) continue;
      const selectedLeft=leftNodes.find(n=>n.classList.contains('selected')) || leftNodes[0];
      const origins= this.path[i]? [selectedLeft]: leftNodes;
      origins.forEach(origin=>{
        rightNodes.forEach(target=>{
          if(this.path[i] && origin!==selectedLeft) return;
            this.drawEdge(origin,target);
        });
      });
    }
  }

  drawEdge(fromEl,toEl){
    const svgRect=this.treeWrapper.getBoundingClientRect();
    const a=fromEl.getBoundingClientRect();
    const b=toEl.getBoundingClientRect();
    const startX=a.right - svgRect.left + 4;
    const startY=a.top + a.height/2 - svgRect.top;
    const endX=b.left - svgRect.left - 4;
    const endY=b.top + b.height/2 - svgRect.top;
    const midX=startX + (endX-startX)*0.4;
    const path=`M ${startX} ${startY} C ${midX} ${startY}, ${endX - (endX-startX)*0.4} ${endY}, ${endX} ${endY}`;
    const p=document.createElementNS('http://www.w3.org/2000/svg','path');
    p.setAttribute('d', path);
    p.classList.add('tree-edge');
    if(fromEl.classList.contains('selected')) p.classList.add('active');
    this.connectionsEl.appendChild(p);
  }

  // ------------------ Search Logic ------------------
  async performSearch(){
    const q=this.searchInput.value.trim();
    this.clearMatchFocus();
    if(!q){
      this.searchMatches=[];this.matchIndex=-1;
      this.updateSearchControls();
      return;
    }
    try {
      const resp = await fetch('/api/category_search?q='+encodeURIComponent(q));
      const data = await resp.json();
      if(!data.ok){throw new Error(data.error||'search failed');}
      // Build matches for ALL segments that contain the query (parent and child),
      // but collapse paths to the matched segment (no auto-expansion).
      const ql = q.toLowerCase();
      const seen = new Set();
      const collapsed = [];
      (data.matches||[]).forEach(m=>{
        const path = Array.isArray(m.path)? m.path : [];
        path.forEach((seg, idx)=>{
          if(((seg||'').toLowerCase()).includes(ql)){
            const prefix = path.slice(0, idx+1);
            const key = prefix.join('>');
            if(!seen.has(key)){
              seen.add(key);
              collapsed.push({ path: prefix, category_id: m.category_id });
            }
          }
        });
      });
      this.searchMatches = collapsed;
      this.matchIndex = this.searchMatches.length? 0 : -1;
      this.updateSearchControls();
      if(this.matchIndex>=0){
        await this.showMatch(this.matchIndex);
      }
    } catch(err){
      console.error(err);
      this.searchMatches=[];this.matchIndex=-1;this.updateSearchControls();
    }
  }

  updateSearchControls(){
    const n=this.searchMatches.length;
    if(this.matchCountEl) this.matchCountEl.textContent = n? `${n} match${n!==1?'es':''}` : '0 matches';
    const disabled = n===0;
    [this.btnPrev,this.btnNext,this.btnClear].forEach(b=>{ if(b) b.disabled = disabled && b!==this.btnClear; });
    if(this.btnClear) this.btnClear.disabled = this.searchInput.value.trim()==='';
    if(this.btnReset) this.btnReset.disabled = this.path.length===0 && this.searchInput.value.trim()==='';
  }

  async cycleMatch(delta){
    if(!this.searchMatches.length) return;
    this.matchIndex = (this.matchIndex + delta + this.searchMatches.length) % this.searchMatches.length;
    await this.showMatch(this.matchIndex);
  }

  async showMatch(index){
    const match = this.searchMatches[index];
    if(!match) return;
    const path = match.path; // array of names top->deep
    // Expand path progressively
    this.path = [];
    for(let d=0; d<path.length; d++){
      // ensure parent depth loaded
      await this.loadDepth(d);
      // set selection for this depth
      this.path[d] = path[d];
    }
  // Do not auto-open the next depth when navigating search matches;
  // keep children hidden until user clicks to expand.
    this.render();
    // Highlight last node (deepest in path)
    this.highlightDeepest(path[path.length-1]);
    // Scroll into view
    const lastCol = this.columnsEl.querySelector('.tree-column:last-child');
    if(lastCol){ lastCol.scrollIntoView({behavior:'smooth', inline:'end'}); }
    this.updateSearchControls();
  }

  clearSearch(){
    this.searchInput.value='';
    this.searchMatches=[];this.matchIndex=-1;this.updateSearchControls();
    this.clearMatchFocus();
  }

  resetAll(){
    // Clear search and fully reset path + columns and cache
    this.clearSearch();
    this.path = [];
    this.columnsEl.innerHTML='';
    this.cache.clear();
    // Also clear persisted cache keys for safety (only our versioned namespace)
    try{
      const toDelete=[];
      for(let i=0;i<localStorage.length;i++){
        const k=localStorage.key(i);
        if(k && k.startsWith(this.persistKeyPrefix+':')) toDelete.push(k);
      }
      toDelete.forEach(k=>localStorage.removeItem(k));
    }catch(_){/* ignore */}
    this.loadDepth(0);
    if(this.btnReset) this.btnReset.disabled = true;
  }

  clearMatchFocus(){
    this.columnsEl.querySelectorAll('.tree-node.match-focus').forEach(n=>n.classList.remove('match-focus'));
  }

  highlightDeepest(name){
    this.clearMatchFocus();
    // deepest column = last with a selected match of that name
    const cols=[...this.columnsEl.querySelectorAll('.tree-column')];
    for(const col of cols.reverse()){
      const candidate=[...col.querySelectorAll('.tree-node')].find(n=>n.dataset.name===name && n.classList.contains('selected'));
      if(candidate){ candidate.classList.add('match-focus'); break; }
    }
  }
}

document.addEventListener('DOMContentLoaded',()=>{ new TreeCatalog(); });