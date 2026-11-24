import * as api from './api.js';
import { openImageModal } from './image_modal.js';

function _esc(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Mobile layout applies to phones only (<= 768px); tablet uses the desktop layout.
const MOBILE_MAX_WIDTH = 768;
const isMobileLayout = () => window.innerWidth <= MOBILE_MAX_WIDTH;

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
    desktopCellMatrix: [],
    mobileCellMatrix: [],
};

export function computeTransactionSpans(sales) {
    const n = Array.isArray(sales) ? sales.length : 0;
    const spans = new Array(n);
    let i = 0;
    while (i < n) {
        const txId = sales[i] && sales[i].transaction_id ? String(sales[i].transaction_id) : null;
        if (!txId) {
            spans[i] = {
                spanCount: 1,
                groupRole: null,
                groupStartRow: i,
                groupEndRow: i,
            };
            i++;
            continue;
        }
        let j = i + 1;
        while (
            j < n &&
            sales[j] &&
            sales[j].transaction_id &&
            String(sales[j].transaction_id) === txId
        ) {
            j++;
        }
        const count = j - i;
        const endRow = j - 1;
        spans[i] = {
            spanCount: count,
            groupRole: count > 1 ? 'start' : 'single',
            groupStartRow: i,
            groupEndRow: endRow,
        };
        for (let k = i + 1; k < j; k++) {
            spans[k] = {
                spanCount: 0,
                groupRole: k === endRow ? 'end' : 'mid',
                groupStartRow: i,
                groupEndRow: endRow,
            };
        }
        i = j;
    }
    return spans;
}

function getLogicalColCount(tableEl) {
    if (tableEl && tableEl.tHead && tableEl.tHead.rows.length > 0) {
        return tableEl.tHead.rows[0].cells.length;
    }
    return 0;
}

function getLogicalColIndexByKey(tableEl, colKey) {
    if (!tableEl) return 0;
    // 1. Check rendered tbody cells carrying data-col-key / data-logical-col
    const firstTd = tableEl.querySelector(`tbody td[data-col-key="${colKey}"]`);
    if (firstTd && firstTd.dataset.logicalCol !== undefined) {
        const parsed = parseInt(firstTd.dataset.logicalCol, 10);
        if (!isNaN(parsed)) return parsed;
    }
    // 2. Fallback to thead header semantic classes
    if (tableEl.tHead && tableEl.tHead.rows.length > 0) {
        const ths = Array.from(tableEl.tHead.rows[0].cells);
        const found = ths.findIndex((th) => {
            if (th.dataset.colKey === colKey) return true;
            if (colKey === 'article') {
                return th.classList.contains('col-recon') || th.classList.contains('col-article');
            }
            if (colKey === 'customer') return th.classList.contains('col-customer');
            if (colKey === 'status') return th.classList.contains('col-status');
            return false;
        });
        if (found !== -1) return found;
    }
    return 0;
}

function registerLogicalCell(matrix, td, colKey, logicalCol, rowIdx, spanCount = 1) {
    td.dataset.colKey = colKey;
    td.dataset.logicalCol = String(logicalCol);
    td.dataset.spanStartRow = String(rowIdx);
    td.dataset.spanEndRow = String(rowIdx + spanCount - 1);
    if (spanCount > 1) {
        td.rowSpan = spanCount;
        td.classList.add('col-customer--spanned');
    }
    for (let r = rowIdx; r < rowIdx + spanCount; r++) {
        if (!matrix[r]) matrix[r] = [];
        matrix[r][logicalCol] = td;
    }
}

const dom = {
    table: document.getElementById('sales-table'),
    tbody: document.getElementById('sales-tbody'),
    tableMobile: document.getElementById('sales-table-mobile'),
    tbodyMobile: document.getElementById('sales-tbody-mobile'),
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
    
    page: document.querySelector('.sales-page'),
    btnMobileOpenGemini: document.getElementById('btn-mobile-open-gemini'),
    btnMobileBackToTable: document.getElementById('btn-mobile-back-to-table')
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
            if (dom.page) dom.page.classList.remove('mobile-panel-open');
            
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

    if (dom.btnMobileOpenGemini) {
        dom.btnMobileOpenGemini.addEventListener('click', () => {
            switchTab('chat');
            if (dom.page) dom.page.classList.add('mobile-panel-open');
        });
    }

    if (dom.btnMobileBackToTable) {
        dom.btnMobileBackToTable.addEventListener('click', () => {
            if (dom.page) dom.page.classList.remove('mobile-panel-open');
        });
    }

    const mobileBackdrop = document.getElementById('mobile-panel-backdrop');
    if (mobileBackdrop) {
        mobileBackdrop.addEventListener('click', () => {
            if (dom.page) dom.page.classList.remove('mobile-panel-open');
        });
    }

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
            const container = document.getElementById('syncIndicator');
            const indicator = document.querySelector('.sync-text');
            const dot = document.querySelector('.sync-dot');
            if (status && status.last_attempt_at) {
                const lastAttempt = new Date(status.last_attempt_at);
                let titleText = `Ultima sincronizzazione: ${lastAttempt.toLocaleTimeString()}`;
                if (status.last_error) {
                    if (dot) dot.style.backgroundColor = 'var(--error, #ef4444)';
                    titleText += ' (Errore)';
                } else {
                    if (dot) dot.style.backgroundColor = 'var(--status-success, #10b981)';
                }
                if (container) container.setAttribute('title', titleText);
                if (dot) dot.setAttribute('title', titleText);
                if (indicator) indicator.textContent = titleText;
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
        if (dom.table) dom.table.style.display = 'none';
        if (dom.tableMobile) dom.tableMobile.style.display = 'none';
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

function renderCustomerPartitionCell(td, sale, spanInfo) {
    td.innerHTML = '';
    const tx = sale ? sale.transaction : null;
    if (!tx) {
        if (sale && sale.customer_phone) {
            td.innerHTML = `<span style="font-size: 11px; color: var(--text-muted);">${_esc(sale.customer_phone)}</span>`;
        } else {
            td.innerHTML = `<span style="color: var(--text-muted); font-size: 12px;">-</span>`;
        }
        return;
    }

    const cust = tx.customer;
    const isPendingId = tx.identification_status === 'IDENTIFICATION_PENDING';
    const isPendingOnb = cust && cust.status === 'PENDING_ONBOARDING';

    let customerHeaderHtml = '';
    if (cust && cust.id) {
        let label = cust.display_name || cust.short_name || cust.phone_number || 'Cliente';
        if (isPendingOnb && !cust.first_name && !cust.last_name) {
            label = `${cust.phone_number || ''} · Onboarding`.trim();
        }
        const bdayHtml = cust.is_birthday_window_active
            ? '<span title="Finestra compleanno attiva (Punti x2)">🎂</span>'
            : '';
        customerHeaderHtml = `
            <a href="/clienti?id=${encodeURIComponent(cust.id)}"
               class="tx-customer-link ${isPendingOnb ? 'tx-customer-link--pending' : ''}"
               title="Apri scheda cliente">
                <span style="overflow: hidden; text-overflow: ellipsis;">${_esc(label)}</span>
                ${bdayHtml}
            </a>
        `;
    } else if (sale && sale.customer_phone) {
        customerHeaderHtml = `<span style="font-size: 12px; font-weight: 600;">${_esc(sale.customer_phone)}</span>`;
    }

    const badges = [];
    if (isPendingId) {
        badges.push(
            `<span class="tx-mini-badge tx-mini-badge--pending" title="Salva una seconda volta in Excel per confermare la transazione">⏳ Conferma Excel</span>`
        );
    } else {
        const netPts = Number(tx.points_net_delta) || 0;
        if (netPts !== 0) {
            const sign = netPts > 0 ? '+' : '';
            const cls = netPts > 0 ? 'tx-mini-badge--pts-pos' : 'tx-mini-badge--pts-neg';
            badges.push(`<span class="tx-mini-badge ${cls}">${sign}${netPts} pt</span>`);
        } else if (cust) {
            badges.push(
                `<span class="tx-mini-badge" style="color: var(--text-muted); border: 1px solid var(--border-color);">0 pt</span>`
            );
        }

        const discAmt = Number(tx.loyalty_discount_amount) || 0;
        if (discAmt > 0) {
            badges.push(
                `<span class="tx-mini-badge tx-mini-badge--discount" title="Sconto punti spalmato">-€ ${discAmt.toFixed(2)}</span>`
            );
        }
    }

    const spanCount = spanInfo ? spanInfo.spanCount : 1;
    const captionHtml =
        spanCount > 1
            ? `<div class="tx-group-caption">${spanCount} righe · € ${Number(tx.net_payable || 0).toFixed(2)}</div>`
            : '';

    td.innerHTML = `
        <div class="tx-customer-card">
            ${customerHeaderHtml}
            ${badges.length ? `<div class="tx-customer-badges">${badges.join('')}</div>` : ''}
            ${captionHtml}
        </div>
    `;
}

function attachCellClickListener(td, tr) {
    td.addEventListener('click', (e) => {
        if (
            e.target.closest('a') ||
            e.target.closest('button') ||
            e.target.closest('input')
        ) {
            return;
        }
        const targetRow = parseInt(td.dataset.spanStartRow ?? tr.dataset.rowIndex, 10);
        const targetCol = parseInt(td.dataset.logicalCol, 10);
        const targetSale = state.sales[targetRow];
        if (!isNaN(targetRow) && !isNaN(targetCol) && targetSale) {
            const shouldTriggerContext =
                targetSale.status === 'RECONCILED' && td.dataset.colKey !== 'customer';
            setActiveCell(targetRow, targetCol, shouldTriggerContext, true);
        }
    });
}

function renderTable() {
    const activeEl = document.activeElement;
    const isExternalInputFocused = activeEl && (
        activeEl.tagName === 'INPUT' ||
        activeEl.tagName === 'TEXTAREA' ||
        activeEl.tagName === 'SELECT'
    ) && (!dom.tbody || !dom.tbody.contains(activeEl)) && (!dom.tbodyMobile || !dom.tbodyMobile.contains(activeEl));
    let selectionStart = null;
    let selectionEnd = null;
    if (isExternalInputFocused && 'selectionStart' in activeEl) {
        selectionStart = activeEl.selectionStart;
        selectionEnd = activeEl.selectionEnd;
    }

    let prevActiveId = null;
    if (state.activeCell && state.sales.length > 0) {
        if (dom.tbody && dom.tbody.children[state.activeCell.row]) {
            prevActiveId = dom.tbody.children[state.activeCell.row].dataset.saleId;
        } else if (dom.tbodyMobile && dom.tbodyMobile.children[state.activeCell.row]) {
            prevActiveId = dom.tbodyMobile.children[state.activeCell.row].dataset.saleId;
        }
    }

    if (dom.tbody) dom.tbody.innerHTML = '';
    if (dom.tbodyMobile) dom.tbodyMobile.innerHTML = '';
    state.desktopCellMatrix = [];
    state.mobileCellMatrix = [];

    if (state.sales.length === 0) {
        if (dom.table) dom.table.style.display = 'none';
        if (dom.tableMobile) dom.tableMobile.style.display = 'none';
        dom.empty.style.display = 'block';
        return;
    }

    dom.empty.style.display = 'none';
    if (dom.table) dom.table.style.display = '';
    if (dom.tableMobile) dom.tableMobile.style.display = '';

    const txSpans = computeTransactionSpans(state.sales);

    state.sales.forEach((sale, idx) => {
        const spanInfo = txSpans[idx] || {
            spanCount: 1,
            groupRole: null,
            groupStartRow: idx,
            groupEndRow: idx,
        };

        // --- 1. Desktop Row (Semantic columns + Transaction rowspan partition) ---
        if (dom.tbody) {
            const tr = document.createElement('tr');
            tr.dataset.saleId = sale.id;
            tr.dataset.rowIndex = idx;
            if (sale.transaction_id) {
                tr.dataset.transactionId = sale.transaction_id;
            }
            if (spanInfo.groupRole) {
                tr.classList.add(`tx-group-${spanInfo.groupRole}`);
            }

            let logicalCol = 0;

            // Col: Status indicator
            const tdStatus = document.createElement('td');
            tdStatus.className = 'col-status';
            if (sale.status === 'ORPHAN') tdStatus.classList.add('status-cell--orphan');
            else if (sale.status === 'UNPROCESSABLE') tdStatus.classList.add('status-cell--unprocessable');
            else if (sale.status === 'RECONCILED') tdStatus.classList.add('status-cell--reconciled');
            tdStatus.textContent = sale.excel_row_index !== null && sale.excel_row_index !== undefined ? sale.excel_row_index : (idx + 1);
            registerLogicalCell(state.desktopCellMatrix, tdStatus, 'status', logicalCol++, idx, 1);
            attachCellClickListener(tdStatus, tr);
            tr.appendChild(tdStatus);

            // Col: PL (starting price)
            const tdPL = document.createElement('td');
            tdPL.className = 'col-price';
            tdPL.textContent = sale.starting_price !== null && sale.starting_price !== undefined ? sale.starting_price : '';
            registerLogicalCell(state.desktopCellMatrix, tdPL, 'pl', logicalCol++, idx, 1);
            attachCellClickListener(tdPL, tr);
            tr.appendChild(tdPL);

            // Cols: Categories (B..O+A)
            CATEGORY_COLUMNS.forEach((cat) => {
                const tdCat = document.createElement('td');
                tdCat.className = 'col-cat';
                if (sale.excel_file_column === cat.index && sale.selling_price !== null && sale.selling_price !== undefined) {
                    tdCat.textContent = sale.selling_price;
                    tdCat.style.fontWeight = 'bold';
                } else {
                    tdCat.textContent = '';
                }
                registerLogicalCell(state.desktopCellMatrix, tdCat, `cat_${cat.index}`, logicalCol++, idx, 1);
                attachCellClickListener(tdCat, tr);
                tr.appendChild(tdCat);
            });

            // Col: Articolo (Reconciliation)
            const tdRecon = document.createElement('td');
            tdRecon.className = 'col-recon';
            renderDesktopReconCell(tdRecon, sale);
            registerLogicalCell(state.desktopCellMatrix, tdRecon, 'article', logicalCol++, idx, 1);
            attachCellClickListener(tdRecon, tr);
            tr.appendChild(tdRecon);

            // Col: Cliente (Spanned across contiguous Transaction rows via rowspan)
            if (spanInfo.spanCount >= 1) {
                const tdCust = document.createElement('td');
                tdCust.className = 'col-customer';
                renderCustomerPartitionCell(tdCust, sale, spanInfo);
                registerLogicalCell(state.desktopCellMatrix, tdCust, 'customer', logicalCol, idx, spanInfo.spanCount);
                attachCellClickListener(tdCust, tr);
                tr.appendChild(tdCust);
            }

            dom.tbody.appendChild(tr);
        }

        // --- 2. Mobile Row (Semantic columns + Transaction rowspan partition) ---
        if (dom.tbodyMobile) {
            const trMobile = document.createElement('tr');
            trMobile.dataset.saleId = sale.id;
            trMobile.dataset.rowIndex = idx;
            if (sale.transaction_id) {
                trMobile.dataset.transactionId = sale.transaction_id;
            }
            if (spanInfo.groupRole) {
                trMobile.classList.add(`tx-group-${spanInfo.groupRole}`);
            }
            if (sale.status === 'ORPHAN') trMobile.classList.add('status-row--orphan');
            else if (sale.status === 'UNPROCESSABLE') trMobile.classList.add('status-row--unprocessable');
            else if (sale.status === 'RECONCILED') trMobile.classList.add('status-row--reconciled');

            let mobCol = 0;

            // Col: Prezzo di listino
            const tdPriceList = document.createElement('td');
            tdPriceList.className = 'col-price-list';
            tdPriceList.textContent = sale.starting_price !== null && sale.starting_price !== undefined
                ? `€ ${Number(sale.starting_price).toFixed(2).replace(/\.00$/, '')}`
                : '-';
            registerLogicalCell(state.mobileCellMatrix, tdPriceList, 'price_list', mobCol++, idx, 1);
            attachCellClickListener(tdPriceList, trMobile);
            trMobile.appendChild(tdPriceList);

            // Col: Prezzo finale
            const tdPriceFinal = document.createElement('td');
            tdPriceFinal.className = 'col-price-final';
            if (sale.selling_price !== null && sale.selling_price !== undefined) {
                const formatted = `€ ${Number(sale.selling_price).toFixed(2).replace(/\.00$/, '')}`;
                if (sale.is_exchange) {
                    tdPriceFinal.innerHTML = `${formatted} <span class="chip chip--small chip--exchange" title="Reso">Reso</span>`;
                } else {
                    tdPriceFinal.textContent = formatted;
                }
            } else {
                tdPriceFinal.textContent = '-';
            }
            registerLogicalCell(state.mobileCellMatrix, tdPriceFinal, 'price_final', mobCol++, idx, 1);
            attachCellClickListener(tdPriceFinal, trMobile);
            trMobile.appendChild(tdPriceFinal);

            // Col: Colonna excel
            const tdExcelCol = document.createElement('td');
            tdExcelCol.className = 'col-excel-col';
            const colDef = CATEGORY_COLUMNS.find(c => c.index === sale.excel_file_column);
            const colLabel = colDef ? colDef.header : (sale.excel_file_column !== null && sale.excel_file_column !== undefined ? `Col ${sale.excel_file_column}` : '-');
            tdExcelCol.innerHTML = `<span class="chip chip--small" style="font-weight: 600;">${_esc(colLabel)}</span>`;
            registerLogicalCell(state.mobileCellMatrix, tdExcelCol, 'excel_col', mobCol++, idx, 1);
            attachCellClickListener(tdExcelCol, trMobile);
            trMobile.appendChild(tdExcelCol);

            // Col: Articolo
            const tdArticle = document.createElement('td');
            tdArticle.className = 'col-article';
            renderMobileReconCard(tdArticle, sale);
            registerLogicalCell(state.mobileCellMatrix, tdArticle, 'article', mobCol++, idx, 1);
            attachCellClickListener(tdArticle, trMobile);
            trMobile.appendChild(tdArticle);

            // Col: Cliente (Spanned across contiguous Transaction rows via rowspan)
            if (spanInfo.spanCount >= 1) {
                const tdCustMob = document.createElement('td');
                tdCustMob.className = 'col-customer';
                renderCustomerPartitionCell(tdCustMob, sale, spanInfo);
                registerLogicalCell(state.mobileCellMatrix, tdCustMob, 'customer', mobCol, idx, spanInfo.spanCount);
                attachCellClickListener(tdCustMob, trMobile);
                trMobile.appendChild(tdCustMob);
            }

            dom.tbodyMobile.appendChild(trMobile);
        }
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

function renderDesktopReconCell(td, sale) {
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
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-add-recon btn-add-recon--disabled';
        btn.disabled = true;
        btn.innerHTML = '+';
        btn.title = 'Non associabile (dati non validi)';
        container.appendChild(btn);
    } else {
        // ORPHAN
        const defaultVal = sale.raw_article_code || '';

        const btn = document.createElement('button');
        btn.className = 'btn-add-recon';
        if (defaultVal) btn.classList.add('has-badge');
        btn.innerHTML = '+';

        btn.onclick = (e) => {
            e.stopPropagation();
            const tr = dom.tbody ? dom.tbody.querySelector(`tr[data-sale-id="${sale.id}"]`) : null;
            if (tr) {
                const idx = parseInt(tr.dataset.rowIndex, 10);
                const articleCol = getLogicalColIndexByKey(dom.table, 'article');
                if (!isNaN(idx)) setActiveCell(idx, articleCol, true, true);
            }
        };
        
        container.appendChild(btn);
    }
    td.appendChild(container);
}

function renderMobileReconCard(td, sale) {
    td.innerHTML = '';
    const container = document.createElement('div');
    container.className = 'recon-cell';

    if (sale.status === 'RECONCILED') {
        const card = document.createElement('div');
        card.className = 'search-preview-card search-preview-card--sale';

        const photoHtml = sale.photo_url
            ? `<img data-src="${sale.photo_url}" src="${sale.photo_url}" alt="${_esc(sale.article_name || 'Articolo')}" onerror="this.parentElement.innerHTML='<span class=\\'preview-card__photo-placeholder\\'>🖼</span>'">`
            : `<span class="preview-card__photo-placeholder">🖼</span>`;

        card.innerHTML = `
            <div class="preview-card__photo">${photoHtml}</div>
            <div class="preview-card__info">
                <div class="preview-card__name" title="${_esc(sale.article_name || 'Articolo')}">${_esc(sale.article_name || 'Articolo')}</div>
                <div class="preview-card__meta">
                    ${sale.brand_name ? `<span class="preview-card__brand">${_esc(sale.brand_name)}</span>` : ''}
                    ${sale.category_name ? `<span class="preview-card__cat">${_esc(sale.category_name)}</span>` : ''}
                </div>
            </div>
            <span class="material-symbols-rounded preview-card__chevron">chevron_right</span>
        `;

        const photoEl = card.querySelector('.preview-card__photo');
        if (photoEl && sale.photo_url) {
            photoEl.addEventListener('click', (e) => {
                e.stopPropagation();
                openImageModal(sale.photo_url);
            });
        }

        card.addEventListener('click', (e) => {
            e.stopPropagation();
            openContextTab(sale);
        });

        const img = card.querySelector('img[data-src]');
        if (img) lazyLoadObserver.observe(img);

        container.appendChild(card);
    } else if (sale.status === 'UNPROCESSABLE') {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-add-recon-card btn-add-recon-card--disabled';
        btn.disabled = true;
        btn.innerHTML = `
            <span class="material-symbols-rounded" style="font-size: 18px;">add</span>
            <span>Non associabile</span>
        `;
        btn.title = 'Non associabile (dati non validi)';
        container.appendChild(btn);
    } else {
        // ORPHAN
        const defaultVal = sale.raw_article_code || '';

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-add-recon-card';
        btn.innerHTML = `
            <span class="material-symbols-rounded" style="font-size: 18px;">add</span>
            <span>Associa Articolo</span>
            ${defaultVal ? `<span class="chip chip--small" style="margin-left: 6px;">${_esc(defaultVal)}</span>` : ''}
        `;

        btn.onclick = (e) => {
            e.stopPropagation();
            const trMobile = dom.tbodyMobile ? dom.tbodyMobile.querySelector(`tr[data-sale-id="${sale.id}"]`) : null;
            if (trMobile) {
                const idx = parseInt(trMobile.dataset.rowIndex, 10);
                const articleCol = getLogicalColIndexByKey(dom.tableMobile, 'article');
                if (!isNaN(idx)) setActiveCell(idx, articleCol, true, true);
            }
        };

        container.appendChild(btn);
    }
    td.appendChild(container);
}

function renderReconCell(td, sale) {
    renderMobileReconCard(td, sale);
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
    sale.brand_name = cand.brand_name || sale.brand_name;
    sale.category_name = cand.category_name || sale.category_name;
    sale.photo_url = cand.photo_serving_endpoint_url;

    // 2. Update Desktop DOM row in place via semantic data-col-key
    if (dom.tbody) {
        const tr = dom.tbody.querySelector(`tr[data-sale-id="${sale.id}"]`);
        if (tr) {
            const tdStatus = tr.querySelector('[data-col-key="status"]') || tr.querySelector('.col-status');
            if (tdStatus) {
                tdStatus.className = 'col-status status-cell--reconciled';
            }
            const tdRecon = tr.querySelector('[data-col-key="article"]') || tr.querySelector('.col-recon');
            if (tdRecon) renderDesktopReconCell(tdRecon, sale);
        }
    }

    // 3. Update Mobile DOM row in place via semantic data-col-key
    if (dom.tbodyMobile) {
        const trMobile = dom.tbodyMobile.querySelector(`tr[data-sale-id="${sale.id}"]`);
        if (trMobile) {
            trMobile.classList.remove('status-row--orphan', 'status-row--unprocessable');
            trMobile.classList.add('status-row--reconciled');
            const tdArticle = trMobile.querySelector('[data-col-key="article"]') || trMobile.querySelector('.col-article');
            if (tdArticle) renderMobileReconCard(tdArticle, sale);
        }
    }

    closeContextTab();

    // 4. Auto-scroll to next orphan dynamically resolving the article column
    const nextOrphanIdx = state.sales.findIndex(s => s.status === 'ORPHAN');
    if (nextOrphanIdx !== -1) {
        const isMobile = isMobileLayout();
        const targetTable = isMobile ? dom.tableMobile : dom.table;
        const targetTbody = isMobile ? dom.tbodyMobile : dom.tbody;
        const targetCol = getLogicalColIndexByKey(targetTable, 'article');
        if (targetTbody && targetTbody.children[nextOrphanIdx]) {
            targetTbody.children[nextOrphanIdx].scrollIntoView({ behavior: 'smooth', block: 'center' });
            setActiveCell(nextOrphanIdx, targetCol, true, true);
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
    if (dom.page) dom.page.classList.add('mobile-panel-open');
    
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
    if (dom.page) dom.page.classList.remove('mobile-panel-open');
    switchTab('chat');
}

async function fetchAndRenderSaleDetail(saleId) {
    dom.detailContainer.innerHTML = '<span class="spinner"></span> Caricamento dettagli...';
    dom.substitutesSection.style.display = 'none';
    
    try {
        const res = await fetch(`/api/v1/excel-synchronization/sale-detail/${saleId}`).then(r => r.json());
        
        const photoHtml = res.photo_url 
            ? `<img src="${res.photo_url}" class="detail-card__img" alt="Foto" onclick="openImageModal(this.src)">`
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
    // Clear old active on desktop and mobile via logical cell matrices
    if (state.activeCell) {
        const oldRow = state.activeCell.row;
        const oldCol = state.activeCell.col;
        const oldDesktopTd = state.desktopCellMatrix[oldRow]
            ? state.desktopCellMatrix[oldRow][oldCol]
            : null;
        if (oldDesktopTd) oldDesktopTd.classList.remove('cell--active');

        const oldMobMaxCol = Math.max(0, getLogicalColCount(dom.tableMobile) - 1);
        const oldMobCol = Math.min(oldCol, oldMobMaxCol);
        const oldMobileTd = state.mobileCellMatrix[oldRow]
            ? state.mobileCellMatrix[oldRow][oldMobCol]
            : null;
        if (oldMobileTd) oldMobileTd.classList.remove('cell--active');
    }

    const desktopMaxCol = Math.max(0, getLogicalColCount(dom.table) - 1);
    const mobileMaxCol = Math.max(0, getLogicalColCount(dom.tableMobile) - 1);
    const activeMaxCol = isMobileLayout() ? mobileMaxCol : desktopMaxCol;
    const clampedCol = Math.max(0, Math.min(colIndex, activeMaxCol));

    state.activeCell = { row: rowIndex, col: clampedCol };

    const desktopCol = Math.min(clampedCol, desktopMaxCol);
    const mobileCol = Math.min(clampedCol, mobileMaxCol);

    // Set new active on desktop via logical matrix (handles rowspan seamlessly)
    const newTr = dom.tbody ? dom.tbody.children[rowIndex] : null;
    const newTd = state.desktopCellMatrix[rowIndex]
        ? state.desktopCellMatrix[rowIndex][desktopCol]
        : null;
    if (newTd) {
        newTd.classList.add('cell--active');
        if (triggerContext && newTr) {
            const saleId = newTr.dataset.saleId;
            const sale = state.sales.find(s => s.id === saleId);
            if (sale) {
                openContextTab(sale);
            }
        }
    }

    // Set new active on mobile via logical matrix
    const newTrMobile = dom.tbodyMobile ? dom.tbodyMobile.children[rowIndex] : null;
    const newTdMobile = state.mobileCellMatrix[rowIndex]
        ? state.mobileCellMatrix[rowIndex][mobileCol]
        : null;
    if (newTdMobile) {
        newTdMobile.classList.add('cell--active');
        if (triggerContext && !newTr && newTrMobile) {
            const saleId = newTrMobile.dataset.saleId;
            const sale = state.sales.find(s => s.id === saleId);
            if (sale) {
                openContextTab(sale);
            }
        }
    }

    // Only manage focus if explicitly requested (e.g. user clicked on cell or explicit navigation)
    if (shouldFocus) {
        const isMobile = isMobileLayout();
        const activeTd = isMobile ? newTdMobile : newTd;
        if (activeTd && activeTd.dataset.colKey === 'article') {
            const input = activeTd.querySelector('input');
            if (input) input.focus();
        } else {
            // Only blur if activeElement is an input inside the table body itself; never blur external textboxes (e.g. chat-input)
            if (document.activeElement && ((dom.tbody && dom.tbody.contains(document.activeElement)) || (dom.tbodyMobile && dom.tbodyMobile.contains(document.activeElement)))) {
                document.activeElement.blur();
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
    const isMobile = isMobileLayout();
    const activeTable = isMobile ? dom.tableMobile : dom.table;
    const activeMatrix = isMobile ? state.mobileCellMatrix : state.desktopCellMatrix;
    const maxCol = Math.max(0, getLogicalColCount(activeTable) - 1);

    const currentTd = activeMatrix[row] ? activeMatrix[row][col] : null;
    const spanStart = currentTd && currentTd.dataset.spanStartRow !== undefined
        ? parseInt(currentTd.dataset.spanStartRow, 10)
        : row;
    const spanEnd = currentTd && currentTd.dataset.spanEndRow !== undefined
        ? parseInt(currentTd.dataset.spanEndRow, 10)
        : row;

    let changed = false;

    switch (e.key) {
        case 'ArrowUp':
            if (spanStart > 0) {
                row = spanStart - 1;
                changed = true;
            }
            break;
        case 'ArrowDown':
            if (spanEnd < maxRow) {
                row = spanEnd + 1;
                changed = true;
            }
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
        const targetTbody = isMobile ? dom.tbodyMobile : dom.tbody;
        const tr = targetTbody ? targetTbody.children[row] : null;
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


