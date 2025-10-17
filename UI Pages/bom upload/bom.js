// BOM Results Page Logic

class BOMResultsPage {
  constructor() {
    this.token = null;
    this.results = null;
    this.activeFilters = new Set(['exact', 'close', 'alternate', 'none']);
    this.expandedRows = new Set();
    
    this.init();
  }
  
  init() {
    // Get token from URL
    const urlParams = new URLSearchParams(window.location.search);
    this.token = urlParams.get('token');
    
    if (!this.token) {
      this.showError('No BOM token provided');
      return;
    }
    
    // Setup filter checkboxes
    this.setupFilters();
    
    // Setup "Add All Exact" button
    document.getElementById('add-all-exact').addEventListener('click', () => {
      this.addAllExactToList();
    });
    
    // Load results
    this.loadResults();
  }
  
  async loadResults() {
    try {
      const response = await fetch(`/api/bom_results/${this.token}`);
      
      if (!response.ok) {
        const error = await response.json();
        this.showError(error.error || 'Failed to load BOM results');
        return;
      }
      
      this.results = await response.json();
      this.render();
      
    } catch (error) {
      console.error('Error loading BOM results:', error);
      this.showError('Network error loading results');
    }
  }
  
  showError(message) {
    document.getElementById('loading').style.display = 'none';
    const errorDiv = document.getElementById('error');
    errorDiv.style.display = 'block';
    errorDiv.querySelector('.error-message').textContent = message;
  }
  
  setupFilters() {
    const checkboxes = document.querySelectorAll('.match-filter');
    checkboxes.forEach(cb => {
      cb.addEventListener('change', (e) => {
        if (e.target.checked) {
          this.activeFilters.add(e.target.value);
        } else {
          this.activeFilters.delete(e.target.value);
        }
        this.applyFilters();
      });
    });
  }
  
  applyFilters() {
    const rows = document.querySelectorAll('.bom-row');
    rows.forEach(row => {
      const matchType = row.dataset.matchType;
      if (this.activeFilters.has(matchType)) {
        row.classList.remove('hidden');
      } else {
        row.classList.add('hidden');
      }
    });
  }
  
  render() {
    // Hide loading
    document.getElementById('loading').style.display = 'none';
    
    // Show results
    document.getElementById('results').style.display = 'block';
    
    // Render summary
    this.renderSummary();
    
    // Render table
    this.renderTable();
  }
  
  renderSummary() {
    // Filename
    document.getElementById('bom-filename').textContent = this.results.filename;
    
    // Calculate stats
    const stats = {
      total: this.results.results.length,
      exact: 0,
      close: 0,
      none: 0
    };
    
    this.results.results.forEach(item => {
      const matches = item.matches || [];
      if (matches.length === 0) {
        stats.none++;
      } else {
        const bestMatch = matches[0];
        if (bestMatch.match_type === 'exact') {
          stats.exact++;
        } else {
          stats.close++;
        }
      }
    });
    
    document.getElementById('stat-total').textContent = stats.total;
    document.getElementById('stat-exact').textContent = stats.exact;
    document.getElementById('stat-close').textContent = stats.close;
    document.getElementById('stat-none').textContent = stats.none;
    
    // Warnings
    if (this.results.warnings && this.results.warnings.length > 0) {
      const warningsDiv = document.getElementById('warnings');
      warningsDiv.style.display = 'block';
      const ul = warningsDiv.querySelector('ul');
      ul.innerHTML = '';
      this.results.warnings.forEach(warning => {
        const li = document.createElement('li');
        li.textContent = warning;
        ul.appendChild(li);
      });
    }
  }
  
  renderTable() {
    const tbody = document.getElementById('bom-tbody');
    tbody.innerHTML = '';
    
    this.results.results.forEach((item, index) => {
      const bom_line = item.bom_line;
      const matches = item.matches || [];
      
      // Determine match type for filtering
      let matchType = 'none';
      let bestMatchBadge = this.createMatchBadge('none', 'No Match');
      
      if (matches.length > 0) {
        const bestMatch = matches[0];
        matchType = bestMatch.match_type;
        const confidence = Math.round(bestMatch.confidence * 100);
        bestMatchBadge = this.createMatchBadge(bestMatch.match_type, `${bestMatch.match_type} (${confidence}%)`);
      }
      
      // Main row
      const tr = document.createElement('tr');
      tr.className = 'bom-row';
      tr.dataset.matchType = matchType;
      tr.dataset.index = index;
      
      tr.innerHTML = `
        <td>${index + 1}</td>
        <td>${this.renderDesignators(bom_line.designator)}</td>
        <td>${bom_line.quantity || 1}</td>
        <td>${this.escapeHtml(bom_line.value || '')}</td>
        <td>${this.escapeHtml(bom_line.footprint || '')}</td>
        <td>${this.escapeHtml(bom_line.mpn || '')}</td>
        <td>${bestMatchBadge}</td>
        <td>
          ${matches.length > 0 
            ? `<button class="expand-btn" data-index="${index}">View ${matches.length} Match${matches.length > 1 ? 'es' : ''}</button>`
            : '<span style="color: var(--muted);">—</span>'
          }
        </td>
      `;
      
      tbody.appendChild(tr);
      
      // Expanded row (if there are matches)
      if (matches.length > 0) {
        const expandedTr = document.createElement('tr');
        expandedTr.className = 'expanded-row';
        expandedTr.dataset.index = index;
        
        expandedTr.innerHTML = `
          <td colspan="8" class="expanded-cell">
            <div class="expanded-content">
              ${this.renderMatches(matches, index)}
            </div>
          </td>
        `;
        
        tbody.appendChild(expandedTr);
      }
    });
    
    // Attach event listeners for expand buttons
    document.querySelectorAll('.expand-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const index = e.target.dataset.index;
        this.toggleExpanded(index);
      });
    });
    
    // Attach event listeners for "Add to List" buttons
    document.querySelectorAll('.add-to-list-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const index = e.target.dataset.index;
        const matchIndex = e.target.dataset.matchIndex;
        this.addToList(index, matchIndex);
      });
    });
  }
  
  renderDesignators(designatorStr) {
    if (!designatorStr) return '—';
    
    const designators = designatorStr.split(/[,\s]+/).filter(d => d.trim());
    if (designators.length === 0) return '—';
    
    return `<div class="designators">
      ${designators.slice(0, 5).map(d => `<span class="designator-pill">${this.escapeHtml(d)}</span>`).join('')}
      ${designators.length > 5 ? `<span class="designator-pill">+${designators.length - 5} more</span>` : ''}
    </div>`;
  }
  
  renderMatches(matches, bomIndex) {
    return `<div class="match-details">
      ${matches.map((match, i) => this.renderMatchItem(match, bomIndex, i)).join('')}
    </div>`;
  }
  
  renderMatchItem(match, bomIndex, matchIndex) {
    const confidence = Math.round(match.confidence * 100);
    
    return `
      <div class="match-item">
        <div class="match-header">
          <div>
            <span class="match-mpn">${this.escapeHtml(match.mpn)}</span>
            ${this.createMatchBadge(match.match_type, match.match_type)}
          </div>
          <span class="match-confidence">${confidence}% match</span>
        </div>
        <div class="match-meta">
          <strong>Mfr:</strong> ${this.escapeHtml(match.manufacturer)} | 
          <strong>Value:</strong> ${this.escapeHtml(match.value || '—')} | 
          <strong>Footprint:</strong> ${this.escapeHtml(match.footprint || '—')}
        </div>
        ${match.reason ? `<div class="match-reason">${this.escapeHtml(match.reason)}</div>` : ''}
        ${match.bins && match.bins.length > 0 ? `
          <div class="match-bins">
            ${match.bins.map(bin => `<span class="bin-pill-small">${this.escapeHtml(bin)}</span>`).join('')}
          </div>
        ` : ''}
        <div class="match-actions">
          <button class="btn-small primary add-to-list-btn" data-index="${bomIndex}" data-match-index="${matchIndex}">
            Add to My List
          </button>
        </div>
      </div>
    `;
  }
  
  createMatchBadge(type, text) {
    return `<span class="match-badge ${type}">${this.escapeHtml(text)}</span>`;
  }
  
  toggleExpanded(index) {
    const expandedRow = document.querySelector(`.expanded-row[data-index="${index}"]`);
    const btn = document.querySelector(`.expand-btn[data-index="${index}"]`);
    
    if (!expandedRow) return;
    
    if (this.expandedRows.has(index)) {
      expandedRow.classList.remove('show');
      this.expandedRows.delete(index);
      btn.textContent = btn.textContent.replace('Hide', 'View');
    } else {
      expandedRow.classList.add('show');
      this.expandedRows.add(index);
      btn.textContent = btn.textContent.replace('View', 'Hide');
    }
  }
  
  addToList(bomIndex, matchIndex) {
    const item = this.results.results[bomIndex];
    const match = item.matches[matchIndex];
    
    // Get or create My List from localStorage
    let myList = [];
    try {
      const stored = localStorage.getItem('myList');
      if (stored) {
        myList = JSON.parse(stored);
      }
    } catch (e) {
      console.error('Error reading myList from localStorage:', e);
    }
    
    // Check if already in list
    const exists = myList.some(p => p.part_id === match.part_id);
    if (exists) {
      alert('This part is already in your list!');
      return;
    }
    
    // Add to list
    const listItem = {
      part_id: match.part_id,
      mpn: match.mpn,
      manufacturer: match.manufacturer,
      description: match.description || '',
      value: match.value || '',
      footprint: match.footprint || '',
      bins: match.bins || [],
      quantity: item.bom_line.quantity || 1
    };
    
    myList.push(listItem);
    
    // Save back to localStorage
    try {
      localStorage.setItem('myList', JSON.stringify(myList));
      alert(`Added ${match.mpn} to My List!`);
    } catch (e) {
      console.error('Error saving to localStorage:', e);
      alert('Failed to add to list (storage error)');
    }
  }
  
  addAllExactToList() {
    let added = 0;
    let skipped = 0;
    
    // Get current list
    let myList = [];
    try {
      const stored = localStorage.getItem('myList');
      if (stored) {
        myList = JSON.parse(stored);
      }
    } catch (e) {
      console.error('Error reading myList:', e);
      alert('Failed to read My List from storage');
      return;
    }
    
    // Find all exact matches
    this.results.results.forEach(item => {
      const matches = item.matches || [];
      if (matches.length > 0) {
        const bestMatch = matches[0];
        if (bestMatch.match_type === 'exact') {
          // Check if already in list
          const exists = myList.some(p => p.part_id === bestMatch.part_id);
          if (!exists) {
            const listItem = {
              part_id: bestMatch.part_id,
              mpn: bestMatch.mpn,
              manufacturer: bestMatch.manufacturer,
              description: bestMatch.description || '',
              value: bestMatch.value || '',
              footprint: bestMatch.footprint || '',
              bins: bestMatch.bins || [],
              quantity: item.bom_line.quantity || 1
            };
            myList.push(listItem);
            added++;
          } else {
            skipped++;
          }
        }
      }
    });
    
    // Save
    if (added > 0) {
      try {
        localStorage.setItem('myList', JSON.stringify(myList));
        alert(`Added ${added} exact match${added > 1 ? 'es' : ''} to My List!${skipped > 0 ? ` (${skipped} already in list)` : ''}`);
      } catch (e) {
        console.error('Error saving to localStorage:', e);
        alert('Failed to save to My List');
      }
    } else {
      alert(skipped > 0 ? 'All exact matches are already in your list!' : 'No exact matches found.');
    }
  }
  
  escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
  new BOMResultsPage();
});
