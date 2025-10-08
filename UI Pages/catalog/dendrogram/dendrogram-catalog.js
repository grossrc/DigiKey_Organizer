// Dynamic Hierarchical Category Browser
class TreeCatalog {
  constructor(){
    this.path = []; // array of selected category name segments (exact DB path names)
    this.columnsEl = document.getElementById('tree-columns');
    this.connectionsEl = document.getElementById('tree-connections');
    this.treeWrapper = document.getElementById('tree-wrapper');
    this.cache = new Map(); // key: depth|prefix.join('>') -> nodes array
    this.setupEvents();
    this.loadDepth(0); // initial root
  }

  setupEvents(){
    window.addEventListener('resize',()=>this.drawConnections());
  }

  // Build cache key
  _key(depth,prefix){return depth+'|'+prefix.join('>');}

  async loadDepth(depth){
    const prefix = this.path.slice(0, depth); // ancestors
    const key = this._key(depth,prefix);
    if(!this.cache.has(key)){
      const params = new URLSearchParams();
      params.set('depth', depth);
      prefix.forEach(p=>params.append('prefix', p));
      const resp = await fetch('/api/category_nodes?'+params.toString());
      const data = await resp.json();
      if(!data.ok){ console.error('Load failed', data.error); return; }
      this.cache.set(key, data.nodes);
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
        if(this.path[d] === node.name) div.classList.add('selected');
        if(this.path[d] && this.path[d] !== node.name) div.classList.add('disabled');
        div.dataset.name = node.name;
        div.dataset.depth = d;
        div.innerHTML = `<div class="node-title">${node.name}</div><div class="node-meta">${node.parts} parts • ${node.stock} in stock${node.final? ' • final':''}</div>`;
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
    if(node.final){
      if(node.category_id){
        window.location = `/catalog/${encodeURIComponent(node.category_id)}`;
      } else {
        // Fallback: navigate using deepest selected category name slug if needed
        console.warn('Final node missing category_id', node);
      }
    } else {
      await this.loadDepth(depth+1);
    }
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
}

document.addEventListener('DOMContentLoaded',()=>{ new TreeCatalog(); });