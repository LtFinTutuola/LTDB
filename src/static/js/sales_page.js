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

const state = {
    sales: [],
    activeCell: null, // { row: ..., col: ... }
    activeSaleId: null,
};

const dom = {
    table: document.getElementById('sales-table'),
    tbody: document.getElementById('sales-tbody'),
    datePicker: document.getElementById('date-picker'),
    btnToday: document.getElementById('btn-today'),
    btnAskGemini: document.getElementById('btn-ask-gemini'),
    loading: document.getElementById('loading-indicator'),
    empty: document.getElementById('empty-state'),
    panel: document.getElementById('work-panel'),
    btnClosePanel: document.getElementById('btn-close-panel'),
    workPanelPrompt: document.getElementById('work-panel-prompt'),
    reconPromptInput: document.getElementById('recon-prompt-input'),
    btnReconSearch: document.getElementById('btn-recon-search'),
    candidatesContainer: document.getElementById('candidates-container'),
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
    dom.datePicker.addEventListener('change', () => fetchDailySales(dom.datePicker.value));
    
    dom.btnToday.addEventListener('click', () => {
        dom.datePicker.valueAsDate = new Date();
        fetchDailySales(dom.datePicker.value);
    });
    
    dom.btnAskGemini.addEventListener('click', () => {
        dom.page.classList.add('sales-page--panel-open');
        dom.panel.style.display = 'flex';
        // Clear candidates but focus prompt
        dom.candidatesContainer.innerHTML = '';
        dom.reconPromptInput.value = '';
        dom.reconPromptInput.focus();
    });

    dom.btnClosePanel.addEventListener('click', closeWorkPanel);

    dom.btnReconSearch.addEventListener('click', performReconSearch);
    dom.reconPromptInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            performReconSearch();
        }
    });

    document.addEventListener('keydown', handleKeyNav);

    fetchDailySales(dom.datePicker.value);
    
    // Auto-refresh (polling)
    setInterval(() => {
        const isToday = dom.datePicker.value === new Date().toISOString().split('T')[0];
        if (isToday) {
            fetchDailySales(dom.datePicker.value, true);
        }
    }, 5000);
}

async function fetchDailySales(dateStr, silent = false) {
    if (!dateStr) return;

    if (!silent) {
        dom.loading.style.display = 'block';
        dom.table.style.display = 'none';
        dom.empty.style.display = 'none';
        closeWorkPanel();
    }

    try {
        const url = `/api/v1/excel-synchronization/daily-sales?target_date=${dateStr}`;
        const res = await fetch(url).then(r => r.json());

        if (res.status === 'success') {
            const oldLength = state.sales.length;
            state.sales = res.data;
            renderTable();
            
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
                if (sale.is_exchange) tdCat.style.color = 'var(--error)';
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
                    setActiveCell(idx, colIdx);
                }
            });
        });

        dom.tbody.appendChild(tr);
    });

    // Restore or set initial active cell
    if (prevActiveId) {
        const newIdx = state.sales.findIndex(s => s.id === prevActiveId);
        if (newIdx !== -1) {
            setActiveCell(newIdx, state.activeCell.col);
        } else {
            setActiveCell(0, 0);
        }
    } else if (state.sales.length > 0) {
        setActiveCell(0, 0);
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

        container.onclick = (e) => {
            state.activeSaleId = sale.id;

            const tr = dom.tbody.querySelector(`tr[data-sale-id="${sale.id}"]`);
            if (tr) {
                const idx = parseInt(tr.dataset.rowIndex, 10);
                if (!isNaN(idx)) setActiveCell(idx, 10);
            }

            dom.page.classList.add('sales-page--panel-open');
            dom.panel.style.display = 'flex';
            dom.workPanelPrompt.style.display = 'flex';
            dom.candidatesContainer.innerHTML = '';
            dom.reconPromptInput.value = defaultVal;
            dom.reconPromptInput.focus();

            if (defaultVal) {
                performReconSearch();
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

    closeWorkPanel();

    // 3. Auto-scroll to next orphan
    const nextOrphanIdx = state.sales.findIndex(s => s.status === 'ORPHAN');
    if (nextOrphanIdx !== -1) {
        const nextTr = dom.tbody.children[nextOrphanIdx];
        if (nextTr) {
            nextTr.scrollIntoView({ behavior: 'smooth', block: 'center' });
            setActiveCell(nextOrphanIdx, 10); // focus the recon cell
        }
    }
}

function closeWorkPanel() {
    dom.page.classList.remove('sales-page--panel-open');
    dom.panel.style.display = 'none';
    state.activeSaleId = null;
}

function setActiveCell(rowIndex, colIndex) {
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

            // If it's a recon cell and it has an input, focus it
            if (colIndex === 10) {
                const input = newTd.querySelector('input');
                if (input) input.focus();
            } else {
                // Ensure document body has focus to intercept keys
                document.activeElement.blur();
            }
        }
    }
}

function handleKeyNav(e) {
    // Ignore if input is focused
    if (document.activeElement.tagName === 'INPUT') {
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
        setActiveCell(row, col);

        // Scroll into view if needed
        const tr = dom.tbody.children[row];
        if (tr) {
            tr.scrollIntoView({ behavior: 'auto', block: 'nearest' });
        }
    }
}

// Initialize on DOM load
document.addEventListener('DOMContentLoaded', init);
