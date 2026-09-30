import * as api from './api.js';
import {
    Mode, setMode,
    addMessage, enterSplitScreen, exitSplitScreen,
    getDetailPanel, _esc, showChatInput, forceOrphansRefresh
} from './main.js';

let _currentArtifactBox = null;
let _pendingSales = [];
let _expandedSaleId = null;

export async function startSession() {
    if (_currentArtifactBox && document.body.contains(_currentArtifactBox)) {
        if (_pendingSales.length > 0) {
            setMode(Mode.RECONCILIATION);
            showChatInput(false);
            enterSplitScreen();
            _renderList();
        }
        return;
    }

    setMode(Mode.RECONCILIATION);
    showChatInput(false);
    
    // Emit Artifact Box
    _currentArtifactBox = document.createElement('div');
    _currentArtifactBox.className = 'artifact-box';
    _currentArtifactBox.innerHTML = `
        <span class="material-symbols-rounded artifact-box__icon">point_of_sale</span>
        <div class="artifact-box__body">
            <span class="artifact-box__title">Riconciliazione Vendite</span>
            <span class="artifact-box__status" data-status-target><span class="spinner"></span>Caricamento in corso...</span>
        </div>
        <span class="material-symbols-rounded artifact-box__open-icon">open_in_full</span>
    `;
    addMessage(_currentArtifactBox, 'system');
    
    try {
        const res = await api.getPendingSales();
        _pendingSales = res.data.filter(s => s.status !== 'RECONCILED'); // we might still want to show UNPROCESSABLE
        
        _updateArtifactBox();
        
        _currentArtifactBox.classList.add('artifact-box--clickable');
        _currentArtifactBox.addEventListener('click', () => {
            enterSplitScreen();
            _renderList();
        });
        
        enterSplitScreen();
        _renderList();
        
    } catch (err) {
        _currentArtifactBox.querySelector('[data-status-target]').innerHTML = `Errore: ${err.message}`;
        _currentArtifactBox.querySelector('[data-status-target]').style.color = 'var(--error)';
        setMode(Mode.IDLE);
        showChatInput(true);
    }
}

function _updateArtifactBox() {
    if (!_currentArtifactBox) return;
    const orphans = _pendingSales.filter(s => s.status === 'ORPHAN');
    const statusEl = _currentArtifactBox.querySelector('[data-status-target]');
    if (statusEl) {
        if (orphans.length === 0) {
            statusEl.innerHTML = `Tutte le vendite riconciliate`;
            statusEl.style.color = 'var(--success)';
            _currentArtifactBox.classList.remove('artifact-box--clickable');
            const icon = _currentArtifactBox.querySelector('.artifact-box__open-icon');
            if (icon) icon.remove();
            
            // Reset state to allow new sessions
            _currentArtifactBox = null;
            _pendingSales = [];
        } else {
            statusEl.innerHTML = `${orphans.length} vendite in attesa di riconciliazione`;
            statusEl.style.color = 'inherit';
        }
    }
}

function _renderList() {
    const panel = getDetailPanel();
    
    // Header
    const header = document.createElement('div');
    header.className = 'detail-panel__header';
    header.innerHTML = `
        <div class="detail-panel__header-title-box">
            <h2 class="detail-panel__title">Riconciliazione Vendite</h2>
            <span style="font-size:12px;color:var(--text-muted)">${_pendingSales.length} record in sospeso</span>
        </div>
        <button class="btn-close-panel" title="Chiudi">✕</button>`;
    header.querySelector('.btn-close-panel').addEventListener('click', _closePanel);
    
    // Content
    const content = document.createElement('div');
    content.className = 'detail-panel__content';
    
    if (_pendingSales.length === 0) {
        content.innerHTML = `
            <div class="empty-state">
                <span class="empty-state__icon">✅</span>
                <p class="empty-state__text">Tutti i record sono stati elaborati.</p>
                <button class="btn-primary" style="margin-top: 1rem;" id="btn-close-session">Chiudi sessione</button>
            </div>`;
        setTimeout(() => {
            const btn = content.querySelector('#btn-close-session');
            if(btn) btn.addEventListener('click', _closePanel);
        }, 0);
    } else {
        const list = document.createElement('div');
        list.className = 'search-results-list'; // Reusing search list styles
        
        _pendingSales.forEach(sale => {
            const card = _buildSaleCard(sale);
            list.appendChild(card);
        });
        
        content.appendChild(list);
    }
    
    panel.innerHTML = '';
    panel.appendChild(header);
    panel.appendChild(content);
}

function _buildSaleCard(sale) {
    const wrapper = document.createElement('div');
    wrapper.className = 'search-preview-card'; // Reusing styles, maybe add a new class if needed
    wrapper.style.display = 'block'; // override flex
    wrapper.dataset.id = sale.id;
    
    const isUnprocessable = sale.status === 'UNPROCESSABLE';
    const defaultCode = sale.raw_article_code || '';
    
    let html = `
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <div>
                <strong>Riga ${sale.excel_row_index}</strong> 
                <span class="stock-badge stock-badge--${isUnprocessable ? 'zero' : 'low'}" style="margin-left: 8px;">${isUnprocessable ? 'NON PROCESSABILE' : sale.status}</span>
            </div>
            <div style="font-weight: bold;">
                ${sale.is_exchange ? 'Reso' : 'Vendita'}: €${sale.selling_price || '-'}
            </div>
        </div>
    `;
    
    if (!isUnprocessable) {
        html += `
            <div class="reconciliation-search-box" style="display:flex; gap:8px; align-items: center; margin-top: 12px;">
                <input type="text" class="form-control" placeholder="Codice Fornitore o EAN" value="${_esc(defaultCode)}" ${defaultCode ? '' : ''}>
                <button class="btn-primary btn-search-candidates">Cerca candidati</button>
            </div>
            <div class="candidates-container" style="margin-top: 12px; display: none;"></div>
        `;
    } else {
        html += `<div style="color: var(--error); margin-top: 8px; font-size: var(--font-size-sm);">Questa riga non può essere elaborata (es. prezzo mancante/invalido).</div>`;
    }
    
    wrapper.innerHTML = html;
    
    if (!isUnprocessable) {
        const inputCode = wrapper.querySelector('input');
        const btnSearch = wrapper.querySelector('.btn-search-candidates');
        const candContainer = wrapper.querySelector('.candidates-container');
        
        const doSearch = async () => {
            const code = inputCode.value.trim();
            if (!code) return;
            
            candContainer.style.display = 'block';
            candContainer.innerHTML = '<span class="spinner"></span> Ricerca...';
            btnSearch.disabled = true;
            
            try {
                const res = await api.getReconciliationCandidates(code, sale.is_exchange);
                _renderCandidates(sale.id, res.data, candContainer);
            } catch (err) {
                if (err.message.includes('404')) {
                    candContainer.innerHTML = `<div style="color:var(--error); font-size:14px;">Nessun articolo trovato per questo codice.</div>`;
                } else {
                    candContainer.innerHTML = `<div style="color:var(--error); font-size:14px;">Errore: ${err.message}</div>`;
                }
            } finally {
                btnSearch.disabled = false;
            }
        };
        
        btnSearch.addEventListener('click', doSearch);
        inputCode.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') doSearch();
        });
        
        // Auto-search if code is pre-filled
        if (defaultCode) {
            setTimeout(doSearch, 100);
        }
    }
    
    return wrapper;
}

function _renderCandidates(saleId, candidates, container) {
    container.innerHTML = '';
    
    if (candidates.length === 0) {
        container.innerHTML = `<div style="color:var(--error); font-size:14px;">Nessun candidato restituito.</div>`;
        return;
    }
    
    candidates.forEach(cand => {
        const candEl = document.createElement('div');
        candEl.className = 'search-expanded-card'; // Reuse
        candEl.style.marginTop = '8px';
        candEl.style.border = '1px solid var(--border)';
        
        const photoHtml = cand.photo_serving_endpoint_url
            ? `<img src="${cand.photo_serving_endpoint_url}" alt="${_esc(cand.article_name)}" style="cursor:zoom-in;" onclick="if(window._openImageModal) window._openImageModal(this.src)">`
            : `<div class="expanded-card__photo-placeholder">🖼</div>`;
            
        candEl.innerHTML = `
            <div class="expanded-card__body" style="padding: 12px;">
                <div class="expanded-card__photo" style="width: 60px; height: 60px;">${photoHtml}</div>
                <div class="expanded-card__info">
                    <div class="expanded-card__name" style="font-size: 14px;">${_esc(cand.article_name)}</div>
                    ${cand.article_description ? `<div class="expanded-card__desc" style="font-size: 12px; margin-bottom: 4px;">${_esc(cand.article_description)}</div>` : ''}
                    <div class="expanded-card__meta">
                        <span class="stock-badge stock-badge--available">Disp: ${cand.inventory}</span>
                        ${cand.colors ? `<span class="chip chip--small" style="margin-left: 8px;">Colore: ${_esc(cand.colors.join(', '))}</span>` : ''}
                    </div>
                </div>
                <div style="display:flex; align-items:center;">
                    <button class="btn-primary btn-confirm-reconciliation">Conferma</button>
                </div>
            </div>
            <div class="cand-error" style="color:var(--error); font-size: 12px; padding: 0 12px 12px; display:none;"></div>
        `;
        
        candEl.querySelector('.btn-confirm-reconciliation').addEventListener('click', async (e) => {
            const btn = e.target;
            const errEl = candEl.querySelector('.cand-error');
            btn.disabled = true;
            btn.textContent = '...';
            errEl.style.display = 'none';
            
            try {
                await api.reconcileSale(saleId, cand.article_id);
                // Success!
                // Remove the sale from list and re-render
                _pendingSales = _pendingSales.filter(s => s.id !== saleId);
                _updateArtifactBox();
                _renderList();
                forceOrphansRefresh();
                
                // Add inline chat message
                addMessage(`✅ Riga riconciliata con successo (${_esc(cand.article_name)}).`, 'success');
            } catch (err) {
                errEl.textContent = err.message;
                errEl.style.display = 'block';
                btn.disabled = false;
                btn.textContent = 'Conferma';
            }
        });
        
        container.appendChild(candEl);
    });
}

function _closePanel() {
    exitSplitScreen();
    setMode(Mode.IDLE);
    showChatInput(true);
}
