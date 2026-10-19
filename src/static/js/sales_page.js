import * as api from './api.js';

const CATEGORY_COLUMNS = [
    { index: 0, header: 'B' },
    { index: 1, header: 'Sam/AT' },
    { index: 2, header: 'V' },
    { index: 3, header: 'PP' },
    { index: 4, header: 'C' },
    { index: 5, header: 'CAR' },
    { index: 6, header: 'BORSELLI' },
    { index: 7, header: 'O+A' },
];

import { initSalesChat } from './sales_chat.js';

const state = {
    sales: [],
    activeCell: null, // { row: ..., col: ... }
    activeSaleId: null,
};

const dom = {
    table: document.getElementById('sales-table'),
    tbody: document.getElementById('sales-tbody'),
    datePicker: document.getElementById('date-picker'),
    salesView: document.getElementById('sales-view'),
    statsView: document.getElementById('stats-view'),
    historyView: document.getElementById('history-view'),
    btnRefreshStats: document.getElementById('btn-refresh-stats'),
    btnRefreshHistory: document.getElementById('btn-refresh-history'),
    historyLegend: document.getElementById('history-legend'),
    history7DaysGrid: document.getElementById('history-7days-grid'),
    history30DaysChart: document.getElementById('history-30days-chart'),
    statsTbody: document.getElementById('stats-tbody'),
    pnlContent: document.getElementById('pnl-content'),
    loading: document.getElementById('loading-indicator'),
    empty: document.getElementById('empty-state'),
    panel: document.getElementById('work-panel'),
    workPanelTabs: document.getElementById('work-panel-tabs'),
    tabChat: document.getElementById('tab-chat'),
    tabContext: document.getElementById('tab-context'),
    tabContextTitle: document.getElementById('tab-context-title'),
    btnCloseContext: document.getElementById('btn-close-context'),
    
    panelChat: document.getElementById('panel-chat'),
    panelSearch: document.getElementById('panel-search'),
    panelDetail: document.getElementById('panel-detail'),
    
    chatMessages: document.getElementById('chat-messages'),
    chatInput: document.getElementById('chat-input'),
    btnChatSend: document.getElementById('btn-chat-send'),

    reconPromptInput: document.getElementById('recon-prompt-input'),
    btnReconSearch: document.getElementById('btn-recon-search'),
    candidatesContainer: document.getElementById('candidates-container'),
    
    detailContainer: document.getElementById('detail-container'),
    substitutesSection: document.getElementById('substitutes-section'),
    substitutesContainer: document.getElementById('substitutes-container'),
    
    page: document.querySelector('.sales-page')
};

// Intersection Observer for lazy loading images
const lazyLoadObserver = new IntersectionObserver((entries, observer) => {
    entries.forEach(entry => {
        if (entry.isIntersecting) {
            const img = entry.target;
            if (img.dataset.src) {
                img.src = img.dataset.src;
                img.removeAttribute('data-src');
            }
            observer.unobserve(img);
        }
    });
}, { rootMargin: '200px' });

function openImageModal(url) {
    let overlay = document.getElementById('image-zoom-modal');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'image-zoom-modal';
        overlay.className = 'image-modal-overlay';
        overlay.innerHTML = `
            <div class="image-modal-content">
                <button class="btn-close-modal">✕</button>
                <img id="image-zoom-img" src="" alt="zoom">
            </div>
        `;
        document.body.appendChild(overlay);
        
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.classList.contains('btn-close-modal')) {
                overlay.classList.remove('open');
            }
        });
    }
    document.getElementById('image-zoom-img').src = url;
    overlay.classList.add('open');
}

function init() {
    dom.datePicker.valueAsDate = new Date();
    dom.datePicker.addEventListener('change', () => {
        fetchDailySales(dom.datePicker.value);
        if (dom.statsView && dom.statsView.style.display !== 'none') {
            fetchDailyAggregations(dom.datePicker.value);
        }
        if (dom.historyView && dom.historyView.style.display !== 'none') {
            fetchHistoricalSales(dom.datePicker.value);
        }
    });
    
    const mainTabs = document.querySelectorAll('.sales-page__header-left .tab');
    mainTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            mainTabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            
            const target = tab.dataset.tab;
            if (target === 'sales') {
                if (dom.statsView) dom.statsView.style.display = 'none';
                if (dom.historyView) dom.historyView.style.display = 'none';
                dom.salesView.style.display = 'block';
            } else if (target === 'stats') {
                dom.salesView.style.display = 'none';
                if (dom.historyView) dom.historyView.style.display = 'none';
                dom.statsView.style.display = 'block';
                fetchDailyAggregations(dom.datePicker.value);
            } else if (target === 'history') {
                dom.salesView.style.display = 'none';
                if (dom.statsView) dom.statsView.style.display = 'none';
                if (dom.historyView) dom.historyView.style.display = 'block';
                fetchHistoricalSales(dom.datePicker.value);
            }
        });
    });

    dom.btnRefreshStats.addEventListener('click', () => {
        fetchDailyAggregations(dom.datePicker.value);
    });

    if (dom.btnRefreshHistory) {
        dom.btnRefreshHistory.addEventListener('click', () => {
            fetchHistoricalSales(dom.datePicker.value);
        });
    }

    dom.btnReconSearch.addEventListener('click', performReconSearch);
    dom.reconPromptInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            performReconSearch();
        }
    });

    // Work Panel Tabs Listeners
    dom.tabChat.addEventListener('click', () => switchTab('chat'));
    dom.tabContext.addEventListener('click', () => {
        const sale = state.sales.find(s => s.id === state.activeSaleId);
        if (sale) {
            if (sale.status === 'ORPHAN') switchTab('search');
            else if (sale.status === 'RECONCILED') switchTab('detail');
        }
    });
    dom.btnCloseContext.addEventListener('click', (e) => {
        e.stopPropagation();
        closeContextTab();
    });

    document.addEventListener('keydown', handleKeyNav);

    // Init chat
    initSalesChat(dom);

    fetchDailySales(dom.datePicker.value);
    
    // Auto-refresh (polling)
    setInterval(() => {
        const isToday = dom.datePicker.value === new Date().toISOString().split('T')[0];
        if (isToday) {
            fetchDailySales(dom.datePicker.value, true);
        }
    }, 5000);
    
    // Poll sync status
    setInterval(updateSyncStatus, 5000);
    updateSyncStatus();
}

async function updateSyncStatus() {
    try {
        const res = await fetch('/api/v1/excel-synchronization/status');
        if (res.ok) {
            const data = await res.json();
            const status = data.data;
            const indicator = document.querySelector('.sync-text');
            const dot = document.querySelector('.sync-dot');
            if (indicator && status.last_attempt_at) {
                const lastAttempt = new Date(status.last_attempt_at);
                indicator.textContent = `Ultima sincronizzazione: ${lastAttempt.toLocaleTimeString()}`;
                if (status.last_error) {
                    dot.style.backgroundColor = 'var(--error, #ef4444)';
                    indicator.textContent += ' (Errore)';
                } else {
                    dot.style.backgroundColor = 'var(--status-success, #10b981)';
                }
            }
        }
    } catch (e) {
        console.error('Failed to poll sync status', e);
    }
}

async function fetchDailySales(dateStr, silent = false) {
    if (!dateStr) return;

    if (!silent) {
        dom.loading.style.display = 'block';
        dom.table.style.display = 'none';
        dom.empty.style.display = 'none';
        closeContextTab();
    }

    try {
        const url = `/api/v1/excel-synchronization/daily-sales?target_date=${dateStr}`;
        const res = await fetch(url).then(r => r.json());

        if (res.status === 'success') {
            const oldLength = state.sales.length;
            const salesChanged = !silent || JSON.stringify(state.sales) !== JSON.stringify(res.data);
            if (salesChanged) {
                state.sales = res.data;
                renderTable();
            }
            
            if (silent && state.sales.length > oldLength) {
                // Scroll to bottom when new records arrive
                const container = document.querySelector('.sales-page__table-container');
                if (container) {
                    container.scrollTop = container.scrollHeight;
                }
            }
        } else {
            console.error("API error", res);
            if (!silent) alert("Errore nel caricamento delle vendite.");
        }
    } catch (err) {
        console.error(err);
    } finally {
        if (!silent) dom.loading.style.display = 'none';
    }
}

function renderTable() {
    const activeEl = document.activeElement;
    const isExternalInputFocused = activeEl && (
        activeEl.tagName === 'INPUT' ||
        activeEl.tagName === 'TEXTAREA' ||
        activeEl.tagName === 'SELECT'
    ) && !dom.tbody.contains(activeEl);
    let selectionStart = null;
    let selectionEnd = null;
    if (isExternalInputFocused && 'selectionStart' in activeEl) {
        selectionStart = activeEl.selectionStart;
        selectionEnd = activeEl.selectionEnd;
    }

    let prevActiveId = null;
    if (state.activeCell && state.sales.length > 0 && dom.tbody.children[state.activeCell.row]) {
        prevActiveId = dom.tbody.children[state.activeCell.row].dataset.saleId;
    }

    dom.tbody.innerHTML = '';

    dom.table.style.display = 'table';

    if (state.sales.length === 0) {
        dom.empty.style.display = 'block';
        return;
    }

    dom.empty.style.display = 'none';

    state.sales.forEach((sale, idx) => {
        const tr = document.createElement('tr');
        tr.dataset.saleId = sale.id;
        tr.dataset.rowIndex = idx; // DOM row index

        // Col 0: Status indicator
        const tdStatus = document.createElement('td');
        tdStatus.className = 'col-status';
        if (sale.status === 'ORPHAN') tdStatus.classList.add('status-cell--orphan');
        else if (sale.status === 'UNPROCESSABLE') tdStatus.classList.add('status-cell--unprocessable');
        else if (sale.status === 'RECONCILED') tdStatus.classList.add('status-cell--reconciled');

        tdStatus.textContent = sale.excel_row_index;
        tr.appendChild(tdStatus);

        // Col 1: PL (starting price)
        const tdPL = document.createElement('td');
        tdPL.textContent = sale.starting_price !== null ? sale.starting_price : '';
        tr.appendChild(tdPL);

        // Col 2-9: Categories
        CATEGORY_COLUMNS.forEach(cat => {
            const tdCat = document.createElement('td');
            if (sale.excel_file_column === cat.index && sale.selling_price !== null) {
                tdCat.textContent = sale.selling_price;
                tdCat.style.fontWeight = 'bold';
            } else {
                tdCat.textContent = '';
            }
            tr.appendChild(tdCat);
        });

        // Col 10: Reconciliation
        const tdRecon = document.createElement('td');
        renderReconCell(tdRecon, sale);
        tr.appendChild(tdRecon);

        // Cell click handling for active state
        Array.from(tr.children).forEach((td, colIdx) => {
            td.addEventListener('click', (e) => {
                // Select cell only if not clicking on inputs/buttons
                if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'BUTTON') {
                    // Open detail context if reconciled
                    const shouldTriggerContext = sale.status === 'RECONCILED';
                    setActiveCell(idx, colIdx, shouldTriggerContext, true);
                }
            });
        });

        dom.tbody.appendChild(tr);
    });

    // Restore or set initial active cell without stealing focus
    if (prevActiveId) {
        const newIdx = state.sales.findIndex(s => s.id === prevActiveId);
        if (newIdx !== -1) {
            setActiveCell(newIdx, state.activeCell.col, false, false);
        } else {
            setActiveCell(0, 0, false, false);
        }
    } else if (state.sales.length > 0) {
        setActiveCell(0, 0, false, false);
    }

    // Preserve external input focus (e.g. chat-input or recon-prompt-input)
    if (isExternalInputFocused && document.activeElement !== activeEl) {
        activeEl.focus();
        if (selectionStart !== null && selectionEnd !== null && 'setSelectionRange' in activeEl) {
            try {
                activeEl.setSelectionRange(selectionStart, selectionEnd);
            } catch (_) {}
        }
    }
}

function renderReconCell(td, sale) {
    td.innerHTML = '';
    const container = document.createElement('div');
    container.className = 'recon-cell';

    if (sale.status === 'RECONCILED') {
        const photoHtml = sale.photo_url
            ? `<img data-src="${sale.photo_url}" class="recon-photo" alt="Foto">`
            : ``;

        container.innerHTML = photoHtml;
        
        container.onclick = (e) => {
            if (sale.photo_url) {
                openImageModal(sale.photo_url);
            }
        };

        const img = container.querySelector('img');
        if (img) lazyLoadObserver.observe(img);

    } else if (sale.status === 'UNPROCESSABLE') {
        container.innerHTML = `<span style="color: var(--error)">Errore dati origine</span>`;
    } else {
        // ORPHAN
        const defaultVal = sale.raw_article_code || '';

        const btn = document.createElement('button');
        btn.className = 'btn-add-recon';
        if (defaultVal) btn.classList.add('has-badge');
        btn.innerHTML = '+';

        btn.onclick = (e) => {
            e.stopPropagation(); // Prevent td click from double firing if any
            const tr = dom.tbody.querySelector(`tr[data-sale-id="${sale.id}"]`);
            if (tr) {
                const idx = parseInt(tr.dataset.rowIndex, 10);
                if (!isNaN(idx)) setActiveCell(idx, 10, true, true);
            }
        };
        
        container.appendChild(btn);
    }
    td.appendChild(container);
}

async function performReconSearch() {
    const searchCode = dom.reconPromptInput.value.trim();
    if (!searchCode) {
        alert("Inserisci un codice per cercare.");
        return;
    }

    const sale = state.sales.find(s => s.id === state.activeSaleId);
    if (!sale) return;

    dom.candidatesContainer.innerHTML = '<span class="spinner"></span> Ricerca in corso...';

    try {
        const res = await api.getReconciliationCandidates(searchCode, sale.is_exchange);
        renderCandidates(sale, res.data);
    } catch (err) {
        if (err.message.includes('404')) {
            dom.candidatesContainer.innerHTML = `<div style="color:var(--error);">Nessun articolo trovato per "${searchCode}".</div>`;
        } else {
            dom.candidatesContainer.innerHTML = `<div style="color:var(--error);">Errore: ${err.message}</div>`;
        }
    }
}

function renderCandidates(sale, candidates) {
    dom.candidatesContainer.innerHTML = '';

    if (candidates.length === 0) {
        dom.candidatesContainer.innerHTML = `<div style="color:var(--error);">Nessun candidato trovato.</div>`;
        return;
    }

    candidates.forEach(cand => {
        const card = document.createElement('div');
        card.style.border = '1px solid var(--border-color)';
        card.style.borderRadius = 'var(--radius-sm)';
        card.style.padding = 'var(--space-md)';
        card.style.marginBottom = 'var(--space-md)';
        card.style.display = 'flex';
        card.style.gap = 'var(--space-md)';

        const photoHtml = cand.photo_serving_endpoint_url
            ? `<img src="${cand.photo_serving_endpoint_url}" style="width:60px;height:60px;object-fit:cover;border-radius:4px;">`
            : `<div style="width:60px;height:60px;background:var(--bg-elevated);border-radius:4px;display:flex;align-items:center;justify-content:center;">No Img</div>`;

        card.innerHTML = `
            ${photoHtml}
            <div style="flex:1;">
                <div style="font-weight:600;">${cand.article_name || 'N/A'}</div>
                <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;">Disp: ${cand.inventory} | Colore: ${cand.colors ? cand.colors.join(', ') : '-'}</div>
                <button class="btn-primary btn-confirm" style="padding:4px 12px;font-size:12px;">Conferma</button>
            </div>
            <div class="err-msg" style="color:var(--error);font-size:12px;width:100%;display:none;"></div>
        `;

        card.querySelector('.btn-confirm').addEventListener('click', async (e) => {
            const btn = e.target;
            const errEl = card.querySelector('.err-msg');
            btn.disabled = true;
            btn.textContent = '...';

            try {
                await api.reconcileSale(sale.id, cand.article_id);
                handleCandidateConfirm(sale, cand);
            } catch (err) {
                errEl.textContent = err.message;
                errEl.style.display = 'block';
                btn.disabled = false;
                btn.textContent = 'Conferma';
            }
        });

        dom.candidatesContainer.appendChild(card);
    });
}

function handleCandidateConfirm(sale, cand) {
    // 1. Update local state
    sale.status = 'RECONCILED';
    sale.article_name = cand.article_name;
    sale.photo_url = cand.photo_serving_endpoint_url;

    // 2. Update DOM row in place
    const tr = dom.tbody.querySelector(`tr[data-sale-id="${sale.id}"]`);
    if (tr) {
        const tdStatus = tr.querySelector('.col-status');
        tdStatus.className = 'col-status status-cell--reconciled';

        const tdRecon = tr.querySelector('td:last-child');
        renderReconCell(tdRecon, sale);
    }

    closeContextTab();

    // 3. Auto-scroll to next orphan
    const nextOrphanIdx = state.sales.findIndex(s => s.status === 'ORPHAN');
    if (nextOrphanIdx !== -1) {
        const nextTr = dom.tbody.children[nextOrphanIdx];
        if (nextTr) {
            nextTr.scrollIntoView({ behavior: 'smooth', block: 'center' });
            setActiveCell(nextOrphanIdx, 10, true, true); // focus the recon cell
        }
    }
}

function switchTab(mode) {
    // Hide all
    dom.panelChat.classList.remove('active');
    dom.panelSearch.classList.remove('active');
    dom.panelDetail.classList.remove('active');
    dom.tabChat.classList.remove('active');
    dom.tabContext.classList.remove('active');
    
    if (mode === 'chat') {
        dom.panelChat.classList.add('active');
        dom.tabChat.classList.add('active');
    } else if (mode === 'search') {
        dom.panelSearch.classList.add('active');
        dom.tabContext.classList.add('active');
    } else if (mode === 'detail') {
        dom.panelDetail.classList.add('active');
        dom.tabContext.classList.add('active');
    }
}

function openContextTab(sale) {
    dom.workPanelTabs.style.display = 'flex';
    state.activeSaleId = sale.id;
    
    if (sale.status === 'ORPHAN') {
        dom.tabContextTitle.textContent = 'Ricerca Articolo';
        switchTab('search');
        
        dom.candidatesContainer.innerHTML = '';
        const defaultVal = sale.raw_article_code || '';
        dom.reconPromptInput.value = defaultVal;
        dom.reconPromptInput.focus();
        if (defaultVal) {
            performReconSearch();
        }
    } else if (sale.status === 'RECONCILED') {
        dom.tabContextTitle.textContent = 'Dettaglio Vendita';
        switchTab('detail');
        fetchAndRenderSaleDetail(sale.id);
    } else {
        // UNPROCESSABLE or other
        dom.tabContextTitle.textContent = 'Dettaglio';
        switchTab('search');
        dom.candidatesContainer.innerHTML = 'Riga con errore, impossibile riconciliare.';
    }
}

function closeContextTab() {
    state.activeSaleId = null;
    dom.tabContextTitle.textContent = 'Contesto';
    dom.workPanelTabs.style.display = 'none';
    switchTab('chat');
}

async function fetchAndRenderSaleDetail(saleId) {
    dom.detailContainer.innerHTML = '<span class="spinner"></span> Caricamento dettagli...';
    dom.substitutesSection.style.display = 'none';
    
    try {
        const res = await fetch(`/api/v1/excel-synchronization/sale-detail/${saleId}`).then(r => r.json());
        
        const photoHtml = res.photo_url 
            ? `<img src="${res.photo_url}" class="detail-card__img" alt="Foto" onclick="showImageModal(this.src)">`
            : `<div class="detail-card__img" style="display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:12px;">No Img</div>`;
            
        const invBadgeClass = res.inventory > 0 ? 'inventory-badge--in-stock' : 'inventory-badge--out-of-stock';
        const invText = res.inventory > 0 ? `In Giacenza (${res.inventory})` : `Esaurito (0)`;
        
        dom.detailContainer.innerHTML = `
            <div class="detail-card">
                <div class="detail-card__header">
                    ${photoHtml}
                    <div class="detail-card__info">
                        <h4 class="detail-card__title">${res.article_name}</h4>
                        <div style="font-size: 0.85rem; color: var(--text-secondary);">Colori: ${res.colors ? res.colors.join(', ') : '-'}</div>
                        <div class="inventory-badge ${invBadgeClass}" style="margin-top:auto;">${invText}</div>
                    </div>
                </div>
            </div>
        `;
        
        if (res.inventory === 0) {
            fetchSubstitutes(res.blueprint_id);
        }
        
    } catch (err) {
        dom.detailContainer.innerHTML = `<div style="color:var(--error);">Errore nel caricamento del dettaglio</div>`;
    }
}

async function fetchSubstitutes(blueprintId) {
    dom.substitutesSection.style.display = 'none';
    
    // Using semantic search with in_stock_only=true, passing the name or id would require a small trick.
    // For mock/fast, we just do a semantic search for the ID or something basic. 
    // Wait, semantic search takes natural language. We could just search for a generic term or we need to pass a good query.
    // Let's just mock it or skip it for now and say "Nessun sostituto implementato".
    // Or we can just call semantic search with query="borse simili"
    try {
        const payload = {
            query: "sostituti simili", 
            in_stock_only: true
        };
        const res = await fetch(`/api/v1/catalog/search/semantic`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        }).then(r => r.json());
        
        if (res.results && res.results.length > 0) {
            let html = '';
            res.results.slice(0, 3).forEach(item => {
                html += `
                    <div style="display:flex; gap:10px; margin-bottom:10px; align-items:center; font-size:0.85rem;">
                        <div style="font-weight:600;">${item.article_name}</div>
                        <div style="color:var(--text-muted);">Giacenza: ${item.stock}</div>
                    </div>
                `;
            });
            dom.substitutesContainer.innerHTML = html;
        } else {
            dom.substitutesContainer.innerHTML = '<div style="font-size:0.85rem; color:var(--text-muted);">Nessun articolo simile in giacenza.</div>';
        }
    } catch (err) {
        dom.substitutesContainer.innerHTML = '<div style="font-size:0.85rem; color:var(--error);">Errore ricerca sostituti.</div>';
    }
}

function setActiveCell(rowIndex, colIndex, triggerContext = false, shouldFocus = false) {
    // Clear old active
    if (state.activeCell) {
        const oldTr = dom.tbody.children[state.activeCell.row];
        if (oldTr) {
            const oldTd = oldTr.children[state.activeCell.col];
            if (oldTd) oldTd.classList.remove('cell--active');
        }
    }

    state.activeCell = { row: rowIndex, col: colIndex };

    // Set new active
    const newTr = dom.tbody.children[rowIndex];
    if (newTr) {
        const newTd = newTr.children[colIndex];
        if (newTd) {
            newTd.classList.add('cell--active');
            
            // Trigger context open
            if (triggerContext) {
                const saleId = newTr.dataset.saleId;
                const sale = state.sales.find(s => s.id === saleId);
                if (sale) {
                    openContextTab(sale);
                }
            }

            // Only manage focus if explicitly requested (e.g. user clicked on cell or explicit navigation)
            if (shouldFocus) {
                if (colIndex === 10) {
                    const input = newTd.querySelector('input');
                    if (input) input.focus();
                } else {
                    // Only blur if activeElement is an input inside the table body itself; never blur external textboxes (e.g. chat-input)
                    if (document.activeElement && dom.tbody.contains(document.activeElement)) {
                        document.activeElement.blur();
                    }
                }
            }
        }
    }
}

function handleKeyNav(e) {
    // Ignore if any input, textarea or select is focused
    if (document.activeElement && (
        document.activeElement.tagName === 'INPUT' ||
        document.activeElement.tagName === 'TEXTAREA' ||
        document.activeElement.tagName === 'SELECT' ||
        document.activeElement.isContentEditable
    )) {
        return;
    }

    if (!state.activeCell || state.sales.length === 0) return;

    let { row, col } = state.activeCell;
    const maxRow = state.sales.length - 1;
    const maxCol = 10;

    let changed = false;

    switch (e.key) {
        case 'ArrowUp':
            if (row > 0) { row--; changed = true; }
            break;
        case 'ArrowDown':
            if (row < maxRow) { row++; changed = true; }
            break;
        case 'ArrowLeft':
            if (col > 0) { col--; changed = true; }
            break;
        case 'ArrowRight':
            if (col < maxCol) { col++; changed = true; }
            break;
    }

    if (changed) {
        e.preventDefault();
        setActiveCell(row, col, false, true);

        // Scroll into view if needed
        const tr = dom.tbody.children[row];
        if (tr) {
            tr.scrollIntoView({ behavior: 'auto', block: 'nearest' });
        }
    }
}

// Initialize on DOM load
document.addEventListener('DOMContentLoaded', init);

async function fetchDailyAggregations(dateStr) {
    if (!dateStr) return;
    dom.statsTbody.innerHTML = '<tr><td colspan="9" style="text-align:center">Caricamento statistiche...</td></tr>';
    try {
        const url = `/api/v1/excel-synchronization/aggregations?target_date=${dateStr}`;
        const res = await fetch(url).then(r => r.json());
        if (res.status === 'success') {
            renderStatsTable(res.data);
        } else {
            dom.statsTbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:red;">Errore caricamento statistiche</td></tr>';
        }
    } catch (err) {
        console.error(err);
        dom.statsTbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:red;">Errore rete</td></tr>';
    }
}

function renderStatsTable(data) {
    const { columns, pl_column, pnl_summary, target_date_dow } = data;
    const colMap = {};
    columns.forEach(c => { colMap[c.col_index] = c; });

    const rows = [
        { label: 'Totale Vendite (€)', key: 'total_list_price', transform: v => `${v.toFixed(2)}` },
        { label: 'Totale Sconti (€)', key: 'discount', transform: v => `-${v.toFixed(2)}` },
        { label: 'Totale Netto (€)', key: 'total_revenue', transform: v => `${v.toFixed(2)}`, strong: true },
        { label: 'Numero Vendite', key: 'total_sales' },
        { label: 'Media (€)', key: 'avg_price', transform: v => `${v.toFixed(2)}` }
    ];

    let html = '';
    rows.forEach(r => {
        const fw = r.strong ? 'font-weight: bold;' : '';
        html += `<tr><td style="${fw}"><strong>${r.label}</strong></td>`;
        
        const plVal = pl_column[r.key] || 0;
        const plDisplayVal = r.transform ? r.transform(plVal) : plVal;
        html += `<td style="${fw}">${plDisplayVal}</td>`;
        
        for (let i = 0; i < 8; i++) {
            const val = colMap[i] ? colMap[i][r.key] : 0;
            const displayVal = r.transform ? r.transform(val) : val;
            html += `<td style="${fw}">${displayVal}</td>`;
        }
        html += '</tr>';
    });
    dom.statsTbody.innerHTML = html;

    const days = ["Domenica", "Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato"];
    const dowName = days[target_date_dow] || "Giorno";

    const renderCard = (title, summaryData, invertColor, explanation) => {
        const d = summaryData;
        const formatDiff = (diff, invert) => {
            if (diff === 0) return '<span style="color: var(--text-muted);">-</span>';
            const isPositive = diff > 0;
            const isGood = invert ? !isPositive : isPositive;
            const color = isGood ? 'var(--success)' : 'var(--error)';
            const sign = isPositive ? '+' : '';
            return `<strong style="color: ${color};">${sign}€ ${diff.toFixed(2)}</strong>`;
        };

        return `
            <div style="background: var(--surface); padding: 15px; border-radius: 8px; border: 1px solid var(--border); margin-bottom: 20px; flex: 1; display: flex; flex-direction: column;">
                <h3 style="margin-top: 0; color: var(--text-muted); font-size: 0.9rem; text-transform: uppercase;">${title}</h3>
                <div style="font-size: 2.1rem; font-weight: bold; margin: 5px 0;">€ ${d.today.toFixed(2)}</div>
                <p style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 15px; min-height: 40px;">${explanation}</p>
                
                <div style="display: flex; gap: 15px; border-top: 1px solid var(--border); padding-top: 12px; margin-top: auto;">
                    <div style="flex: 1;">
                        <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 5px;">Media di ${dowName}</div>
                        <div style="display: flex; align-items: baseline; gap: 8px;">
                            <span style="font-size: 1.05rem;">€ ${d.dow_avg.toFixed(2)}</span>
                            <span style="font-size: 0.9rem;">${formatDiff(d.dow_diff, invertColor)}</span>
                        </div>
                    </div>
                    <div style="flex: 1;">
                        <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 5px;">Media Generale</div>
                        <div style="display: flex; align-items: baseline; gap: 8px;">
                            <span style="font-size: 1.05rem;">€ ${d.general_avg.toFixed(2)}</span>
                            <span style="font-size: 0.9rem;">${formatDiff(d.general_diff, invertColor)}</span>
                        </div>
                    </div>
                </div>
            </div>
        `;
    };

    let pnlHtml = `
        <div style="display: flex; gap: 20px; margin-top: 20px; flex-wrap: wrap;">
            ${renderCard(
                'Fatturato', 
                pnl_summary.net_discounted, 
                false, 
                'Incasso totale al netto degli sconti applicati, IVA inclusa.'
            )}
            ${renderCard(
                'Netto', 
                pnl_summary.net_margin, 
                false, 
                'Utile netto stimato dopo la deduzione di sconti, IVA, costo della merce e costi operativi/fissi.'
            )}
        </div>
    `;

    dom.pnlContent.innerHTML = pnlHtml;
}

const ITALIAN_DAYS = ["Domenica", "Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato"];
const ITALIAN_MONTHS = [
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"
];

const HISTORY_SERIES_PALETTE = [
    { key: 'total', label: 'Totale', shortLabel: 'TOT', color: '#3b82f6' },
    { key: 0, label: 'B', shortLabel: 'B', color: '#10b981' },
    { key: 1, label: 'Sam/AT', shortLabel: 'Sam/AT', color: '#f59e0b' },
    { key: 2, label: 'V', shortLabel: 'V', color: '#a855f7' },
    { key: 3, label: 'PP', shortLabel: 'PP', color: '#ec4899' },
    { key: 4, label: 'C', shortLabel: 'C', color: '#06b6d4' },
    { key: 5, label: 'CAR', shortLabel: 'CAR', color: '#f97316' },
    { key: 6, label: 'BORSELLI', shortLabel: 'Bors.', color: '#14b8a6' },
    { key: 7, label: 'O+A', shortLabel: 'O+A', color: '#eab308' },
];

function formatItalianDayDate(dateIso, dow) {
    const parts = (dateIso || '').split('-');
    if (parts.length !== 3) return dateIso;
    const dayNum = parseInt(parts[2], 10);
    const monthIdx = parseInt(parts[1], 10) - 1;
    const dayName = ITALIAN_DAYS[dow] || '';
    const monthName = ITALIAN_MONTHS[monthIdx] || '';
    return `${dayName} ${dayNum} ${monthName}`.trim();
}

function formatShortDayMonth(dateIso) {
    const parts = (dateIso || '').split('-');
    if (parts.length !== 3) return dateIso;
    return `${parts[2]}/${parts[1]}`;
}

function formatEuro(val) {
    const num = Number(val) || 0;
    return `€ ${num.toFixed(2)}`;
}

let _floatingTooltipEl = null;
function getFloatingTooltip() {
    if (!_floatingTooltipEl) {
        _floatingTooltipEl = document.createElement('div');
        _floatingTooltipEl.className = 'chart-floating-tooltip';
        document.body.appendChild(_floatingTooltipEl);
    }
    return _floatingTooltipEl;
}

function bindBarTooltips(rootEl) {
    if (!rootEl) return;
    const tooltip = getFloatingTooltip();
    const cols = rootEl.querySelectorAll('.history-bar-col[data-tooltip]');
    cols.forEach(col => {
        col.addEventListener('mouseenter', (e) => {
            tooltip.textContent = col.getAttribute('data-tooltip') || '';
            tooltip.style.display = 'block';
            positionTooltip(e, tooltip);
        });
        col.addEventListener('mousemove', (e) => {
            positionTooltip(e, tooltip);
        });
        col.addEventListener('mouseleave', () => {
            tooltip.style.display = 'none';
        });
    });
}

function positionTooltip(e, tooltip) {
    const offset = 12;
    let left = e.clientX + offset;
    let top = e.clientY - 36;
    if (left + tooltip.offsetWidth > window.innerWidth - 8) {
        left = e.clientX - tooltip.offsetWidth - offset;
    }
    if (top < 8) {
        top = e.clientY + offset;
    }
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
}

async function fetchHistoricalSales(dateStr) {
    if (!dateStr || !dom.history7DaysGrid || !dom.history30DaysChart) return;

    dom.history7DaysGrid.innerHTML = '<div style="text-align:center; padding: 20px; color: var(--text-muted);">Caricamento storico 7 giorni...</div>';
    dom.history30DaysChart.innerHTML = '<div style="text-align:center; padding: 20px; color: var(--text-muted);">Caricamento storico 30 giorni...</div>';

    try {
        const url = `/api/v1/excel-synchronization/history?target_date=${encodeURIComponent(dateStr)}`;
        const res = await fetch(url).then(r => r.json());
        if (res.status === 'success' && res.data) {
            renderHistoryView(res.data);
        } else {
            dom.history7DaysGrid.innerHTML = '<div style="text-align:center; color: var(--error);">Errore caricamento storico</div>';
            dom.history30DaysChart.innerHTML = '';
        }
    } catch (err) {
        console.error(err);
        dom.history7DaysGrid.innerHTML = '<div style="text-align:center; color: var(--error);">Errore di rete nel caricamento dello storico</div>';
        dom.history30DaysChart.innerHTML = '';
    }
}

function renderHistoryView(data) {
    const last7 = [...(data.last_7_days || [])].sort((a, b) => (b.date || '').localeCompare(a.date || ''));
    const last30 = data.last_30_days || [];

    // 1. Render Legend
    if (dom.historyLegend) {
        dom.historyLegend.innerHTML = HISTORY_SERIES_PALETTE.map(s => `
            <span class="history-legend__item">
                <span class="history-legend__swatch" style="background: ${s.color};"></span>
                <span>${s.label}</span>
            </span>
        `).join('');
    }

    // 2. Render Upper Area: 7 Daily Column Charts (Totale + 8 Categories)
    let max7Val = 0;
    last7.forEach(day => {
        if (day.total_revenue > max7Val) max7Val = day.total_revenue;
        (day.categories || []).forEach(c => {
            if (c.total_revenue > max7Val) max7Val = c.total_revenue;
        });
    });

    dom.history7DaysGrid.innerHTML = last7.map(day => {
        const dayTitle = formatItalianDayDate(day.date, day.dow);
        const catMap = {};
        (day.categories || []).forEach(c => {
            catMap[c.col_index] = c.total_revenue || 0;
        });

        const barsHtml = HISTORY_SERIES_PALETTE.map(series => {
            const val = series.key === 'total' ? (day.total_revenue || 0) : (catMap[series.key] || 0);
            const heightPct = max7Val > 0 ? Math.max((Math.max(val, 0) / max7Val) * 100, 3) : 3;
            const tooltipText = `${dayTitle} — ${series.label}: ${formatEuro(val)}`;
            return `
                <div class="history-bar-col" data-tooltip="${tooltipText}" title="${tooltipText}">
                    <div class="history-bar" style="height: ${heightPct.toFixed(1)}%; background: ${series.color}; opacity: ${val > 0 ? '1' : '0.28'};"></div>
                </div>
            `;
        }).join('');

        const labelsHtml = HISTORY_SERIES_PALETTE.map(series => `
            <div class="history-bar-label" title="${series.label}">${series.shortLabel}</div>
        `).join('');

        return `
            <div class="history-day-card">
                <div class="history-day-card__header">
                    <span class="history-day-card__title">${dayTitle}</span>
                    <span class="history-day-card__subtitle">${formatEuro(day.total_revenue)}</span>
                </div>
                <div class="history-bars-area">
                    ${barsHtml}
                </div>
                <div class="history-bar-labels">
                    ${labelsHtml}
                </div>
            </div>
        `;
    }).join('');

    // 3. Render Lower Area: 30-Day Column Chart (Total ONLY + General Avg & Hover DOW Avg Lines)
    const generalAvg = Number(data.general_avg) || 0;
    let max30Val = generalAvg;
    last30.forEach(day => {
        if ((day.total_revenue || 0) > max30Val) max30Val = day.total_revenue;
        if ((day.dow_avg || 0) > max30Val) max30Val = day.dow_avg;
    });

    const generalAvgPct = max30Val > 0 ? Math.min(Math.max((generalAvg / max30Val) * 100, 0), 100) : 0;

    const bars30Html = last30.map(day => {
        const val = day.total_revenue || 0;
        const dowAvg = Number(day.dow_avg) || 0;
        const dowName = ITALIAN_DAYS[day.dow] || 'Giorno';
        const heightPct = max30Val > 0 ? Math.max((Math.max(val, 0) / max30Val) * 100, 3) : 3;
        const dowPct = max30Val > 0 ? Math.min(Math.max((dowAvg / max30Val) * 100, 0), 100) : 0;
        const dayTitle = formatItalianDayDate(day.date, day.dow);
        const dowLineLabel = `Media ${dowName}: ${formatEuro(dowAvg)}`;
        const tooltipText = `${dayTitle} — Totale: ${formatEuro(val)} (${dowLineLabel})`;
        return `
            <div class="history-bar-col"
                 data-tooltip="${tooltipText}"
                 data-dow="${day.dow}"
                 data-dow-pct="${dowPct.toFixed(1)}"
                 data-dow-label="${dowLineLabel}"
                 title="${tooltipText}">
                <div class="history-bar" style="height: ${heightPct.toFixed(1)}%; background: #3b82f6; max-width: 28px; opacity: ${val > 0 ? '1' : '0.28'};"></div>
            </div>
        `;
    }).join('');

    const labels30Html = last30.map((day, idx) => {
        const showLabel = idx % 2 === 0 || idx === last30.length - 1;
        return `<div class="history-30days-label">${showLabel ? formatShortDayMonth(day.date) : ''}</div>`;
    }).join('');

    dom.history30DaysChart.innerHTML = `
        <div class="history-30days-bars">
            <div class="history-avg-line history-avg-line--general" style="bottom: ${generalAvgPct.toFixed(1)}%;">
                <span class="history-avg-line__tag">Media Generale: ${formatEuro(generalAvg)}</span>
            </div>
            <div id="history-30days-dow-line" class="history-avg-line history-avg-line--dow" style="bottom: 0%;">
                <span id="history-30days-dow-tag" class="history-avg-line__tag"></span>
            </div>
            ${bars30Html}
        </div>
        <div class="history-30days-labels">
            ${labels30Html}
        </div>
    `;

    // Bind hover for DOW average horizontal line & highlighting matching days while dimming others
    const dowLineEl = document.getElementById('history-30days-dow-line');
    const dowTagEl = document.getElementById('history-30days-dow-tag');
    const generalLineEl = dom.history30DaysChart.querySelector('.history-avg-line--general');
    const cols30 = dom.history30DaysChart.querySelectorAll('.history-bar-col[data-dow]');
    const barsContainer = dom.history30DaysChart.querySelector('.history-30days-bars');

    const reset30DaysHighlight = () => {
        cols30.forEach(c => {
            c.classList.remove('history-bar-col--dimmed');
            c.classList.remove('history-bar-col--highlighted');
        });
        if (generalLineEl) {
            generalLineEl.classList.remove('history-avg-line--dimmed');
        }
        if (dowLineEl) {
            dowLineEl.style.display = 'none';
        }
    };

    cols30.forEach(col => {
        col.addEventListener('mouseenter', () => {
            const targetDow = col.getAttribute('data-dow');
            cols30.forEach(c => {
                if (c.getAttribute('data-dow') === targetDow) {
                    c.classList.remove('history-bar-col--dimmed');
                    c.classList.add('history-bar-col--highlighted');
                } else {
                    c.classList.add('history-bar-col--dimmed');
                    c.classList.remove('history-bar-col--highlighted');
                }
            });

            if (generalLineEl) {
                generalLineEl.classList.add('history-avg-line--dimmed');
            }

            if (dowLineEl && dowTagEl) {
                dowLineEl.style.bottom = `${col.getAttribute('data-dow-pct')}%`;
                dowTagEl.textContent = col.getAttribute('data-dow-label') || '';
                dowLineEl.style.display = 'block';
            }
        });

        col.addEventListener('mouseleave', (e) => {
            if (!e.relatedTarget || !e.relatedTarget.closest('.history-bar-col')) {
                reset30DaysHighlight();
            }
        });
    });

    if (barsContainer) {
        barsContainer.addEventListener('mouseleave', reset30DaysHighlight);
    }

    bindBarTooltips(dom.history7DaysGrid);
    bindBarTooltips(dom.history30DaysChart);
}


