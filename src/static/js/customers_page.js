import * as api from './api.js';
import { openImageModal } from './image_modal.js';

const EXCEL_COL_HEADERS = ['B', 'Sam/AT', 'V', 'PP', 'C', 'CAR', 'BORSELLI', 'O+A'];

const LEDGER_REASON_LABELS = {
    PURCHASE_EARN: 'Acquisto',
    BIRTHDAY_BONUS: '🎂 Bonus Compleanno (x2)',
    DISCOUNT_REDEMPTION: 'Riscatto Sconto Fedeltà',
    RETURN_REVERSAL: 'Storno per Reso / Cambio',
    PRICE_CORRECTION: 'Rettifica Variazione Prezzo',
    ADMIN_ADJUSTMENT: 'Rettifica Manuale Operatore',
};

const STATUS_META = {
    ACTIVE: { label: 'Attivo', badgeClass: 'crm-badge--active' },
    PENDING_ONBOARDING: { label: 'In attesa WhatsApp', badgeClass: 'crm-badge--pending' },
    INACTIVE_EXPIRED: { label: 'Inattivo (Scaduto)', badgeClass: 'crm-badge--inactive' },
    ANONYMIZED: { label: 'Anonimizzato', badgeClass: 'crm-badge--anonymized' },
};

const crmConfig = window.__LTDB_CRM_CONFIG__ || {
    whatsapp_provider: 'mock',
    onboarding_ttl_hours: 72,
    earning_euro_per_point: 1.0,
    birthday_multiplier: 2,
    birthday_window_days: 3,
    redemption_tiers: [],
};

const currentUser = window.__LTDB_USER__ || { role: 'base' };
const canManageCustomers = currentUser.role === 'cassa' || currentUser.role === 'admin';

const state = {
    customers: [],
    activeFilter: 'ALL',
    searchQuery: '',
    selectedCustomerId: null,
    selectedDetail: null,
    activeDetailTab: 'transactions',
    searchAbortController: null,
    conflictCustomerId: null,
    pendingIrreversibleAction: null,
};

function _esc(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function parseUtcDateTime(isoStr) {
    if (!isoStr) return null;
    const str = String(isoStr).trim();
    const hasTz = str.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(str);
    const d = new Date(hasTz ? str : `${str}Z`);
    return Number.isNaN(d.getTime()) ? null : d;
}

function formatItalianDate(dateStr) {
    if (!dateStr) return '-';
    const clean = String(dateStr).split('T')[0];
    const parts = clean.split('-');
    if (parts.length !== 3) return _esc(dateStr);
    return `${parts[2]}/${parts[1]}/${parts[0]}`;
}

function formatItalianDateTime(isoStr) {
    const d = parseUtcDateTime(isoStr);
    if (!d) return '-';
    return d.toLocaleString('it-IT', {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
    });
}

function formatEuro(val) {
    const num = Number(val) || 0;
    return `€ ${num.toLocaleString('it-IT', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatPoints(pts) {
    const num = Number(pts) || 0;
    return `${num.toLocaleString('it-IT')} pt`;
}

function formatPhone(phone) {
    if (!phone) return '-';
    const s = String(phone).trim();
    if (s.length === 10 && s.startsWith('3')) {
        return `${s.slice(0, 3)} ${s.slice(3, 6)} ${s.slice(6)}`;
    }
    return s;
}

function formatRemainingCountdown(isoStr) {
    const expiresAt = parseUtcDateTime(isoStr);
    if (!expiresAt) return 'Scadenza 72h';
    const diffMs = expiresAt.getTime() - Date.now();
    if (diffMs <= 0) return 'Scaduto (in attesa pulizia)';
    const totalMinutes = Math.floor(diffMs / 60000);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return `Scade tra ${hours}h ${minutes}m`;
}

function showToast(message, type = 'success') {
    let container = document.getElementById('ltdb-crm-toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'ltdb-crm-toast-container';
        container.style.position = 'fixed';
        container.style.top = '16px';
        container.style.left = '50%';
        container.style.transform = 'translateX(-50%)';
        container.style.zIndex = '11000';
        container.style.display = 'flex';
        container.style.flexDirection = 'column';
        container.style.gap = '8px';
        container.style.pointerEvents = 'none';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.style.padding = '10px 16px';
    toast.style.borderRadius = '8px';
    toast.style.fontSize = '13px';
    toast.style.fontWeight = '600';
    toast.style.boxShadow = '0 8px 24px rgba(0,0,0,0.45)';
    toast.style.pointerEvents = 'none';
    toast.style.background = type === 'error' ? '#3b1818' : '#162c1e';
    toast.style.border = `1px solid ${type === 'error' ? 'var(--error)' : 'var(--success)'}`;
    toast.style.color = type === 'error' ? '#ff8a80' : '#6ee7b7';
    toast.textContent = message;

    container.appendChild(toast);
    setTimeout(() => {
        toast.remove();
    }, 3400);
}

const dom = {
    page: document.getElementById('customers-page'),
    countBadge: document.getElementById('customers-count-badge'),
    btnNewCustomer: document.getElementById('btn-new-customer-whatsapp'),
    searchInput: document.getElementById('customers-search-input'),
    btnClearSearch: document.getElementById('btn-clear-customer-search'),
    filterPills: document.getElementById('customers-filter-pills'),
    table: document.getElementById('customers-table'),
    tbody: document.getElementById('customers-tbody'),
    loading: document.getElementById('customers-loading'),
    empty: document.getElementById('customers-empty'),

    detailPanel: document.getElementById('customers-detail-panel'),
    detailResizer: document.getElementById('customers-detail-resizer'),
    btnMobileBack: document.getElementById('btn-mobile-back-customers'),
    detailEmpty: document.getElementById('customer-detail-empty'),
    detailContent: document.getElementById('customer-detail-content'),
    btnCloseDetail: document.getElementById('btn-close-customer-detail'),

    detailName: document.getElementById('detail-cust-name'),
    detailMeta: document.getElementById('detail-cust-meta'),
    detailBdayBanner: document.getElementById('detail-bday-banner'),
    kpiPoints: document.getElementById('kpi-points'),
    kpiTotalSpent: document.getElementById('kpi-total-spent'),
    kpiTxCount: document.getElementById('kpi-tx-count'),
    kpiAvgTicket: document.getElementById('kpi-avg-ticket'),
    contextualActions: document.getElementById('detail-contextual-actions'),

    detailTabs: document.getElementById('detail-tabs'),
    paneTransactions: document.getElementById('pane-transactions'),
    paneLedger: document.getElementById('pane-ledger'),
    paneManage: document.getElementById('pane-manage'),

    readonlyNotice: document.getElementById('manage-readonly-notice'),
    formProfile: document.getElementById('form-customer-profile'),
    editFirstName: document.getElementById('edit-first-name'),
    editLastName: document.getElementById('edit-last-name'),
    editPhone: document.getElementById('edit-phone'),
    editBirthDate: document.getElementById('edit-birth-date'),
    editAddress: document.getElementById('edit-address'),
    editConsentMkt: document.getElementById('edit-consent-marketing'),
    editConsentProf: document.getElementById('edit-consent-profiling'),
    editConsentTs: document.getElementById('edit-consent-timestamp'),
    editProfileActions: document.getElementById('edit-profile-actions'),
    btnSaveProfile: document.getElementById('btn-save-customer-profile'),

    advancedOps: document.getElementById('advanced-customer-ops'),
    inputPointsDelta: document.getElementById('input-points-delta'),
    btnApplyPointsAdj: document.getElementById('btn-apply-points-adj'),
    inputMergeSearch: document.getElementById('input-merge-search'),
    mergeCandidatesContainer: document.getElementById('merge-candidates-container'),
    btnOpenAnonymizeModal: document.getElementById('btn-open-anonymize-modal'),

    // Modals
    modalOnboarding: document.getElementById('modal-new-onboarding'),
    formOnboarding: document.getElementById('form-new-onboarding'),
    onboardingPhoneInput: document.getElementById('onboarding-phone-input'),
    onboardingError: document.getElementById('onboarding-modal-error'),
    onboardingConflictBox: document.getElementById('onboarding-conflict-box'),
    onboardingConflictMsg: document.getElementById('onboarding-conflict-msg'),
    btnOpenConflictCustomer: document.getElementById('btn-open-conflict-customer'),
    btnSubmitOnboarding: document.getElementById('btn-submit-onboarding'),

    modalMockFlow: document.getElementById('modal-mock-flow'),
    formMockFlow: document.getElementById('form-mock-flow'),
    mockFirstName: document.getElementById('mock-first-name'),
    mockLastName: document.getElementById('mock-last-name'),
    mockBirthDate: document.getElementById('mock-birth-date'),
    mockAddress: document.getElementById('mock-address'),
    mockTermsAccepted: document.getElementById('mock-terms-accepted'),
    mockConsentMkt: document.getElementById('mock-consent-marketing'),
    mockConsentProf: document.getElementById('mock-consent-profiling'),
    mockFlowError: document.getElementById('mock-flow-error'),
    btnSubmitMockFlow: document.getElementById('btn-submit-mock-flow'),

    modalConfirm: document.getElementById('modal-confirm-irreversible'),
    confirmTitle: document.getElementById('confirm-modal-title'),
    confirmBody: document.getElementById('confirm-modal-body'),
    confirmError: document.getElementById('confirm-modal-error'),
    btnConfirmIrreversible: document.getElementById('btn-confirm-irreversible'),
};

function openModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('open');
}

function closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('open');
}

function initResizer() {
    if (!dom.detailResizer || !dom.page) return;
    let isDragging = false;

    dom.detailResizer.addEventListener('mousedown', (e) => {
        e.preventDefault();
        isDragging = true;
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
    });

    document.addEventListener('mousemove', (e) => {
        if (!isDragging) return;
        const rect = dom.page.getBoundingClientRect();
        const newWidth = Math.min(Math.max(rect.right - e.clientX, 340), 740);
        dom.page.style.setProperty('--detail-panel-width', `${newWidth}px`);
    });

    document.addEventListener('mouseup', () => {
        if (isDragging) {
            isDragging = false;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        }
    });
}

function syncUrlCustomerId(customerId) {
    const url = new URL(window.location.href);
    if (customerId) {
        url.searchParams.set('id', customerId);
    } else {
        url.searchParams.delete('id');
    }
    window.history.replaceState({}, '', url.pathname + url.search);
}

async function fetchCustomersList() {
    if (state.searchAbortController) {
        state.searchAbortController.abort();
    }
    const controller = new AbortController();
    state.searchAbortController = controller;

    dom.loading.style.display = 'block';
    dom.empty.style.display = 'none';

    let apiStatus = '';
    let birthdayOnly = false;

    if (state.activeFilter === 'ACTIVE' || state.activeFilter === 'PENDING_ONBOARDING') {
        apiStatus = state.activeFilter;
    } else if (state.activeFilter === 'BIRTHDAY') {
        birthdayOnly = true;
    }

    try {
        let list = await api.getCustomers({
            q: state.searchQuery,
            status: apiStatus,
            birthdayThisWeek: birthdayOnly,
            signal: controller.signal,
        });

        if (state.activeFilter === 'INACTIVE') {
            list = list.filter(
                (c) => c.status === 'INACTIVE_EXPIRED' || c.status === 'ANONYMIZED'
            );
        }

        state.customers = list;
        renderCustomersTable();
    } catch (err) {
        if (err.name === 'AbortError') return;
        console.error('Failed to load customers:', err);
        showToast(err.message || 'Errore nel caricamento dei clienti.', 'error');
    } finally {
        if (state.searchAbortController === controller) {
            dom.loading.style.display = 'none';
        }
    }
}

function getCustomerDisplayName(cust) {
    if (cust.status === 'ANONYMIZED') return 'ANONIMO';
    const full = `${cust.first_name || ''} ${cust.last_name || ''}`.trim();
    return full || 'In attesa di compilazione...';
}

function renderCustomersTable() {
    dom.tbody.innerHTML = '';
    const count = state.customers.length;
    dom.countBadge.textContent = count === 1 ? '1 cliente' : `${count} clienti`;

    if (count === 0) {
        dom.table.style.display = 'none';
        dom.empty.style.display = 'block';
        return;
    }

    dom.table.style.display = '';
    dom.empty.style.display = 'none';

    state.customers.forEach((cust) => {
        const tr = document.createElement('tr');
        tr.dataset.customerId = cust.id;
        if (cust.id === state.selectedCustomerId) {
            tr.classList.add('row--selected');
        }

        const displayName = getCustomerDisplayName(cust);
        const nameExtraClass =
            cust.status === 'PENDING_ONBOARDING' && !cust.first_name && !cust.last_name
                ? 'cust-name-cell--pending'
                : cust.status === 'ANONYMIZED'
                  ? 'cust-name-cell--anon'
                  : '';

        const statusMeta = STATUS_META[cust.status] || {
            label: cust.status,
            badgeClass: 'crm-badge--inactive',
        };

        const pts = Number(cust.points_balance) || 0;
        const ptsClass =
            pts < 0
                ? 'points-pill--negative'
                : cust.status === 'PENDING_ONBOARDING' && pts > 0
                  ? 'points-pill--frozen'
                  : '';

        const bdayIcon = cust.is_birthday_window_active
            ? '<span title="Finestra compleanno attiva (Punti x2)">🎂</span>'
            : '';

        const frozenLabel =
            cust.status === 'PENDING_ONBOARDING' && pts > 0
                ? '<span style="font-size: 10px; font-weight: 500; opacity: 0.85;">(congelati)</span>'
                : '';

        tr.innerHTML = `
            <td>
                <div class="cust-name-cell ${nameExtraClass}">
                    <span>${_esc(displayName)}</span>
                    ${bdayIcon}
                </div>
            </td>
            <td><span class="cust-phone-text">${_esc(formatPhone(cust.phone_number))}</span></td>
            <td><span class="crm-badge ${statusMeta.badgeClass}">${_esc(statusMeta.label)}</span></td>
            <td>
                <span class="points-pill ${ptsClass}">
                    ${_esc(formatPoints(pts))}
                    ${frozenLabel}
                </span>
            </td>
            <td class="col-hide-mobile" style="color: var(--text-secondary); font-size: 12.5px;">
                ${_esc(formatItalianDate(cust.last_purchase_date))}
            </td>
            <td class="col-hide-mobile">
                <div class="consent-dots">
                    <span class="consent-tag ${cust.consent_marketing ? 'consent-tag--on' : ''}" title="Consenso Marketing">MKT</span>
                    <span class="consent-tag ${cust.consent_profiling ? 'consent-tag--on' : ''}" title="Consenso Profilazione">PROF</span>
                </div>
            </td>
        `;

        tr.addEventListener('click', () => {
            selectCustomer(cust.id);
        });

        dom.tbody.appendChild(tr);
    });
}

async function selectCustomer(customerId, keepActiveTab = false) {
    if (!customerId) {
        closeCustomerDetail();
        return;
    }

    state.selectedCustomerId = customerId;
    syncUrlCustomerId(customerId);

    dom.page.classList.remove('customers-page--no-selection');
    dom.page.classList.add('mobile-panel-open');

    // Highlight active row in master table
    Array.from(dom.tbody.children).forEach((tr) => {
        tr.classList.toggle('row--selected', tr.dataset.customerId === customerId);
    });

    if (!keepActiveTab) {
        switchDetailTab('transactions');
    }

    dom.detailEmpty.style.display = 'none';
    dom.detailContent.style.display = 'flex';

    try {
        const detail = await api.getCustomerDetail(customerId);
        if (state.selectedCustomerId !== customerId) return;
        state.selectedDetail = detail;
        renderCustomerDetail(detail);
    } catch (err) {
        console.error('Failed to fetch customer detail:', err);
        showToast(err.message || 'Impossibile caricare la scheda cliente.', 'error');
        closeCustomerDetail();
    }
}

function closeCustomerDetail() {
    state.selectedCustomerId = null;
    state.selectedDetail = null;
    syncUrlCustomerId(null);

    dom.page.classList.add('customers-page--no-selection');
    dom.page.classList.remove('mobile-panel-open');
    dom.detailContent.style.display = 'none';
    dom.detailEmpty.style.display = 'flex';

    Array.from(dom.tbody.children).forEach((tr) => tr.classList.remove('row--selected'));
}

function switchDetailTab(tabKey) {
    state.activeDetailTab = tabKey;
    dom.detailTabs.querySelectorAll('.tab').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.detailTab === tabKey);
    });
    dom.paneTransactions.classList.toggle('active', tabKey === 'transactions');
    dom.paneLedger.classList.toggle('active', tabKey === 'ledger');
    dom.paneManage.classList.toggle('active', tabKey === 'manage');
}

function renderCustomerDetail(detail) {
    const displayName = getCustomerDisplayName(detail);
    const statusMeta = STATUS_META[detail.status] || {
        label: detail.status,
        badgeClass: 'crm-badge--inactive',
    };

    dom.detailName.innerHTML = `
        <span>${_esc(displayName)}</span>
        <span class="crm-badge ${statusMeta.badgeClass}">${_esc(statusMeta.label)}</span>
    `;

    const metaParts = [];
    if (detail.phone_number) {
        metaParts.push(
            `<span><span class="material-symbols-rounded">call</span>${_esc(formatPhone(detail.phone_number))}</span>`
        );
    }
    if (detail.birth_date) {
        metaParts.push(
            `<span><span class="material-symbols-rounded">cake</span>${_esc(formatItalianDate(detail.birth_date))}</span>`
        );
    }
    if (detail.address) {
        metaParts.push(
            `<span><span class="material-symbols-rounded">location_on</span>${_esc(detail.address)}</span>`
        );
    }
    dom.detailMeta.innerHTML = metaParts.join('');

    dom.detailBdayBanner.style.display = detail.is_birthday_window_active ? 'flex' : 'none';

    const kpi = detail.kpi || {};
    const pts = Number(kpi.points_balance ?? detail.points_balance ?? 0);
    dom.kpiPoints.textContent = formatPoints(pts);
    dom.kpiPoints.classList.toggle('kpi-card__value--negative', pts < 0);
    dom.kpiPoints.classList.toggle('kpi-card__value--accent', pts >= 0);

    dom.kpiTotalSpent.textContent = formatEuro(kpi.total_spent);
    dom.kpiTxCount.textContent = String(kpi.transactions_count ?? 0);
    dom.kpiAvgTicket.textContent = formatEuro(kpi.avg_ticket);

    renderContextualBanner(detail);
    renderTransactionsTab(detail.transactions || []);
    renderLedgerTab(detail.loyalty_ledger || []);
    renderManageTab(detail);
}

function renderContextualBanner(detail) {
    dom.contextualActions.innerHTML = '';
    dom.contextualActions.style.display = 'none';

    if (!canManageCustomers) return;

    if (detail.status === 'PENDING_ONBOARDING') {
        dom.contextualActions.style.display = 'block';
        const countdownText = formatRemainingCountdown(detail.retention_expires_at);
        const simBtnHtml =
            crmConfig.whatsapp_provider === 'mock'
                ? `<button type="button" id="btn-trigger-mock-flow" class="btn-crm-sim">⚡ Simula Compilazione WhatsApp Flow</button>`
                : '';

        dom.contextualActions.innerHTML = `
            <div class="crm-action-banner crm-action-banner--pending">
                <div class="crm-action-banner__row">
                    <span style="font-size: 12px; font-weight: 600; color: #fcd34d;">
                        ⏳ In attesa completamento WhatsApp • ${_esc(countdownText)}
                    </span>
                </div>
                <div class="crm-action-banner__row">
                    <button type="button" id="btn-resend-whatsapp" class="btn-crm-secondary">
                        <span class="material-symbols-rounded" style="font-size: 16px;">forward_to_inbox</span>
                        <span>Re-invia Modulo WhatsApp</span>
                    </button>
                    ${simBtnHtml}
                </div>
            </div>
        `;

        const btnResend = document.getElementById('btn-resend-whatsapp');
        if (btnResend) {
            btnResend.addEventListener('click', handleResendWhatsapp);
        }
        const btnSimFlow = document.getElementById('btn-trigger-mock-flow');
        if (btnSimFlow) {
            btnSimFlow.addEventListener('click', () => {
                dom.formMockFlow.reset();
                dom.mockFlowError.style.display = 'none';
                openModal('modal-mock-flow');
            });
        }
        return;
    }

    if (detail.status === 'ACTIVE') {
        const tiers = Array.isArray(crmConfig.redemption_tiers) ? crmConfig.redemption_tiers : [];
        const hasActiveOtp = Boolean(detail.redemption_otp_code);
        const isMock = crmConfig.whatsapp_provider === 'mock';

        if (!hasActiveOtp && !isMock) return;

        dom.contextualActions.style.display = 'block';
        const pts = Number(detail.points_balance) || 0;

        const activeOtpHtml = hasActiveOtp
            ? `
                <div class="crm-action-banner__row">
                    <div style="font-size: 12px; color: var(--text-secondary);">
                        <div><strong style="color: var(--text-primary);">OTP Sconto Attivo</strong> (${_esc(detail.redemption_rule_code || '')})</div>
                        <div style="font-size: 11px; color: var(--text-muted);">Inserire in Colonna Q di Excel per riscattare lo sconto</div>
                    </div>
                    <span class="otp-code-chip" id="active-otp-chip" title="Clicca per copiare negli appunti">${_esc(detail.redemption_otp_code)}</span>
                </div>
            `
            : '';

        let mockOtpSimulatorHtml = '';
        if (isMock && tiers.length > 0) {
            const optionsHtml = tiers
                .map((t) => {
                    const reqPts = Number(t.points_required) || 0;
                    const unlocked = pts >= reqPts;
                    return `<option value="${_esc(t.code)}" ${unlocked ? '' : 'disabled'}>${_esc(t.label || t.code)}${unlocked ? '' : ` (Richiesti ${reqPts} pt)`}</option>`;
                })
                .join('');
            const anyUnlocked = tiers.some((t) => pts >= (Number(t.points_required) || 0));

            mockOtpSimulatorHtml = `
                <div class="crm-action-banner__row" style="${hasActiveOtp ? 'padding-top: 6px; border-top: 1px dashed var(--border-color);' : ''}">
                    <select id="select-mock-otp-tier" class="crm-input" style="flex: 1; min-width: 170px; font-size: 12px; padding: 6px 8px;" ${anyUnlocked ? '' : 'disabled'}>
                        ${optionsHtml}
                    </select>
                    <button type="button" id="btn-trigger-mock-otp" class="btn-crm-sim" ${anyUnlocked ? '' : 'disabled'} title="${anyUnlocked ? 'Simula tap cliente sul pulsante WhatsApp Quick Reply' : 'Saldo punti insufficiente per sbloccare soglie sconto'}">
                        ⚡ Simula Quick Reply OTP
                    </button>
                </div>
            `;
        }

        dom.contextualActions.innerHTML = `
            <div class="crm-action-banner crm-action-banner--otp">
                ${activeOtpHtml}
                ${mockOtpSimulatorHtml}
            </div>
        `;

        const otpChip = document.getElementById('active-otp-chip');
        if (otpChip) {
            otpChip.addEventListener('click', async () => {
                try {
                    await navigator.clipboard.writeText(detail.redemption_otp_code);
                    showToast(`OTP ${detail.redemption_otp_code} copiato negli appunti!`);
                } catch {
                    showToast(`Codice OTP: ${detail.redemption_otp_code}`);
                }
            });
        }

        const btnMockOtp = document.getElementById('btn-trigger-mock-otp');
        if (btnMockOtp) {
            btnMockOtp.addEventListener('click', handleSimulateQuickReplyOtp);
        }
    }
}

function renderTransactionsTab(transactions) {
    dom.paneTransactions.innerHTML = '';

    if (!transactions.length) {
        dom.paneTransactions.innerHTML = `
            <div style="padding: 28px 12px; text-align: center; color: var(--text-muted); font-size: 13px;">
                Nessuna transazione registrata per questo cliente.
            </div>
        `;
        return;
    }

    transactions.forEach((tx, idx) => {
        const card = document.createElement('div');
        card.className = `tx-accordion-card${idx === 0 ? ' expanded' : ''}`;

        const isPendingId = tx.identification_status === 'IDENTIFICATION_PENDING';
        const statusBadge = isPendingId
            ? `<span class="crm-badge crm-badge--pending">⏳ Conferma Excel</span>`
            : '';

        const loyaltyDisc = Number(tx.loyalty_discount_amount) || 0;
        const discBadge =
            loyaltyDisc > 0
                ? `<span class="crm-badge" style="background: rgba(168,199,250,0.14); color: var(--accent); border: 1px solid rgba(168,199,250,0.35);">Sconto -${_esc(formatEuro(loyaltyDisc))}</span>`
                : '';

        const netDelta = Number(tx.points_net_delta) || 0;
        const deltaSign = netDelta > 0 ? '+' : '';
        const deltaColor =
            netDelta > 0 ? '#3fb950' : netDelta < 0 ? '#f85149' : 'var(--text-muted)';

        const linesCount = Array.isArray(tx.items) ? tx.items.length : 0;

        const linesHtml = (tx.items || [])
            .map((line) => {
                const colHeader =
                    line.excel_file_column !== null && line.excel_file_column !== undefined
                        ? EXCEL_COL_HEADERS[line.excel_file_column] || `Col ${line.excel_file_column}`
                        : '-';
                const rowLabel =
                    line.excel_row_index !== null && line.excel_row_index !== undefined
                        ? `Riga ${line.excel_row_index}`
                        : 'LTDB';
                const title =
                    line.article_name ||
                    (line.raw_article_code ? `Codice: ${line.raw_article_code}` : 'Articolo non riconciliato');
                const metaParts = [line.brand_name, line.category_name, `${rowLabel} • ${colHeader}`]
                    .filter(Boolean)
                    .map(_esc)
                    .join(' · ');

                const thumbHtml = line.photo_url
                    ? `<img src="${_esc(line.photo_url)}" class="tx-line-thumb" alt="Foto articolo" data-photo-url="${_esc(line.photo_url)}">`
                    : `<div class="tx-line-thumb tx-line-thumb--placeholder">🖼</div>`;

                const badgeHtml = line.is_exchange
                    ? `<span class="crm-badge" style="background: var(--error-dim); color: var(--error); border: 1px solid rgba(248,81,73,0.3);">Reso</span>`
                    : `<span class="crm-badge crm-badge--active">Acquisto</span>`;

                return `
                    <div class="tx-line-item">
                        ${thumbHtml}
                        <div style="flex: 1; min-width: 0;">
                            <div style="font-size: 12.5px; font-weight: 600; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                                ${_esc(title)}
                            </div>
                            <div style="font-size: 11px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                                ${metaParts}
                            </div>
                        </div>
                        <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 3px; flex-shrink: 0;">
                            <span style="font-size: 12.5px; font-weight: 700; font-variant-numeric: tabular-nums;">
                                ${_esc(formatEuro(line.selling_price))}
                            </span>
                            ${badgeHtml}
                        </div>
                    </div>
                `;
            })
            .join('');

        card.innerHTML = `
            <button type="button" class="tx-accordion-header">
                <div style="display: flex; flex-direction: column; gap: 3px;">
                    <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                        <strong style="font-size: 13px;">${_esc(formatItalianDate(tx.date))}</strong>
                        <span style="font-size: 11.5px; color: var(--text-muted);">(${linesCount} ${linesCount === 1 ? 'riga' : 'righe'})</span>
                        ${statusBadge}
                        ${discBadge}
                    </div>
                </div>
                <div style="display: flex; align-items: center; gap: 10px; flex-shrink: 0;">
                    <span style="font-size: 12px; font-weight: 700; color: ${deltaColor}; font-variant-numeric: tabular-nums;">
                        ${deltaSign}${netDelta} pt
                    </span>
                    <strong style="font-size: 13.5px; font-variant-numeric: tabular-nums;">
                        ${_esc(formatEuro(tx.net_payable))}
                    </strong>
                    <span class="material-symbols-rounded" style="font-size: 18px; color: var(--text-muted);">expand_more</span>
                </div>
            </button>
            <div class="tx-accordion-body">
                <div class="tx-econ-summary">
                    <span>Listino netto: <strong>${_esc(formatEuro(tx.net_list_total))}</strong></span>
                    <span>Sconto cassa: <strong>${_esc(formatEuro(tx.base_cash_discount))}</strong></span>
                    <span>Sconto punti: <strong>${_esc(formatEuro(tx.loyalty_discount_amount))}</strong></span>
                    <span>Netto pagato: <strong style="color: var(--text-primary);">${_esc(formatEuro(tx.net_payable))}</strong></span>
                </div>
                <div style="display: flex; flex-direction: column; gap: 6px;">
                    ${linesHtml}
                </div>
            </div>
        `;

        const headerBtn = card.querySelector('.tx-accordion-header');
        headerBtn.addEventListener('click', () => {
            card.classList.toggle('expanded');
        });

        card.querySelectorAll('img[data-photo-url]').forEach((img) => {
            img.addEventListener('click', (e) => {
                e.stopPropagation();
                openImageModal(img.dataset.photoUrl);
            });
        });

        dom.paneTransactions.appendChild(card);
    });
}

function renderLedgerTab(entries) {
    dom.paneLedger.innerHTML = '';

    if (!entries.length) {
        dom.paneLedger.innerHTML = `
            <div style="padding: 28px 12px; text-align: center; color: var(--text-muted); font-size: 13px;">
                Nessun movimento presente nel libro mastro punti.
            </div>
        `;
        return;
    }

    entries.forEach((entry) => {
        const delta = Number(entry.points_delta) || 0;
        const sign = delta > 0 ? '+' : '';
        const deltaClass = delta >= 0 ? 'ledger-delta--pos' : 'ledger-delta--neg';
        const reasonLabel = LEDGER_REASON_LABELS[entry.reason] || entry.reason;

        const row = document.createElement('div');
        row.className = 'ledger-row';
        row.innerHTML = `
            <div class="ledger-row__left">
                <span class="ledger-row__reason">${_esc(reasonLabel)}</span>
                <span class="ledger-row__date">${_esc(formatItalianDateTime(entry.created_at))}</span>
            </div>
            <span class="ledger-delta ${deltaClass}">${sign}${delta.toLocaleString('it-IT')} pt</span>
        `;
        dom.paneLedger.appendChild(row);
    });
}

function renderManageTab(detail) {
    const isAnon = detail.status === 'ANONYMIZED';
    const isEditable = canManageCustomers && !isAnon;

    dom.editFirstName.value = detail.first_name || '';
    dom.editLastName.value = detail.last_name || '';
    dom.editPhone.value = detail.phone_number || '';
    dom.editBirthDate.value = detail.birth_date || '';
    dom.editAddress.value = detail.address || '';
    dom.editConsentMkt.checked = Boolean(detail.consent_marketing);
    dom.editConsentProf.checked = Boolean(detail.consent_profiling);

    dom.editConsentTs.textContent = detail.consent_updated_at
        ? `Ultimo aggiornamento consensi: ${formatItalianDateTime(detail.consent_updated_at)}`
        : 'Consensi non ancora confermati dal cliente.';

    [
        dom.editFirstName,
        dom.editLastName,
        dom.editPhone,
        dom.editBirthDate,
        dom.editAddress,
        dom.editConsentMkt,
        dom.editConsentProf,
    ].forEach((input) => {
        input.disabled = !isEditable;
    });

    if (!canManageCustomers) {
        dom.readonlyNotice.style.display = 'block';
        dom.readonlyNotice.textContent =
            'Modalità consultazione (sola lettura): la modifica dei clienti è riservata agli operatori di cassa e agli amministratori.';
        dom.editProfileActions.style.display = 'none';
        dom.advancedOps.style.display = 'none';
    } else if (isAnon) {
        dom.readonlyNotice.style.display = 'block';
        dom.readonlyNotice.textContent =
            'Questo profilo è stato anonimizzato ai sensi del GDPR e non può più essere modificato.';
        dom.editProfileActions.style.display = 'none';
        dom.advancedOps.style.display = 'none';
    } else {
        dom.readonlyNotice.style.display = 'none';
        dom.editProfileActions.style.display = 'flex';
        dom.advancedOps.style.display = 'flex';
        dom.inputPointsDelta.value = '';
        dom.inputMergeSearch.value = '';
        dom.mergeCandidatesContainer.innerHTML = '';
    }
}

// --- Action Handlers ---

async function handleNewOnboardingSubmit(e) {
    e.preventDefault();
    const rawPhone = dom.onboardingPhoneInput.value.trim();
    if (!rawPhone) return;

    dom.onboardingError.style.display = 'none';
    dom.onboardingConflictBox.style.display = 'none';
    state.conflictCustomerId = null;
    dom.btnSubmitOnboarding.disabled = true;

    try {
        const res = await api.startCustomerOnboarding(rawPhone);
        closeModal('modal-new-onboarding');
        dom.formOnboarding.reset();
        const msg = res.is_resent
            ? 'Modulo WhatsApp re-inviato e scadenza 72h aggiornata!'
            : 'Cliente creato in attesa di onboarding WhatsApp!';
        showToast(msg);
        await fetchCustomersList();
        if (res.customer && res.customer.id) {
            await selectCustomer(res.customer.id);
        }
    } catch (err) {
        if (err.status === 409 && err.detailData && err.detailData.existing_customer) {
            const existing = err.detailData.existing_customer;
            state.conflictCustomerId = existing.id;
            dom.onboardingConflictMsg.textContent =
                err.message || 'Esiste già un profilo associato a questo numero.';
            dom.onboardingConflictBox.style.display = 'flex';
        } else {
            dom.onboardingError.textContent = err.message || 'Errore durante l\'avvio dell\'onboarding.';
            dom.onboardingError.style.display = 'block';
        }
    } finally {
        dom.btnSubmitOnboarding.disabled = false;
    }
}

async function handleResendWhatsapp() {
    if (!state.selectedCustomerId) return;
    const btn = document.getElementById('btn-resend-whatsapp');
    if (btn) btn.disabled = true;
    try {
        await api.resendCustomerWhatsapp(state.selectedCustomerId);
        showToast('Modulo WhatsApp Flow re-inviato (+72h)!');
        await fetchCustomersList();
        await selectCustomer(state.selectedCustomerId, true);
    } catch (err) {
        showToast(err.message || 'Impossibile re-inviare il modulo WhatsApp.', 'error');
        if (btn) btn.disabled = false;
    }
}

async function handleMockFlowSubmit(e) {
    e.preventDefault();
    if (!state.selectedCustomerId) return;

    dom.mockFlowError.style.display = 'none';
    dom.btnSubmitMockFlow.disabled = true;

    try {
        await api.mockCompleteCustomerOnboarding(state.selectedCustomerId, {
            first_name: dom.mockFirstName.value.trim(),
            last_name: dom.mockLastName.value.trim(),
            birth_date: dom.mockBirthDate.value,
            address: dom.mockAddress.value.trim(),
            terms_accepted: dom.mockTermsAccepted.checked,
            consent_marketing: dom.mockConsentMkt.checked,
            consent_profiling: dom.mockConsentProf.checked,
        });
        closeModal('modal-mock-flow');
        showToast('Profilo cliente attivato con successo tramite WhatsApp Flow!');
        await fetchCustomersList();
        await selectCustomer(state.selectedCustomerId, true);
    } catch (err) {
        dom.mockFlowError.textContent = err.message || 'Validazione modulo WhatsApp fallita.';
        dom.mockFlowError.style.display = 'block';
    } finally {
        dom.btnSubmitMockFlow.disabled = false;
    }
}

async function handleSimulateQuickReplyOtp() {
    if (!state.selectedCustomerId) return;
    const sel = document.getElementById('select-mock-otp-tier');
    const btn = document.getElementById('btn-trigger-mock-otp');
    if (!sel || !sel.value) return;

    if (btn) btn.disabled = true;
    try {
        const res = await api.mockQuickReplyOtp(state.selectedCustomerId, sel.value);
        showToast(`OTP generato: ${res.redemption_otp_code} (da inserire in Colonna Q di Excel)`);
        await selectCustomer(state.selectedCustomerId, true);
    } catch (err) {
        showToast(err.message || 'Errore generazione OTP sconto.', 'error');
        if (btn) btn.disabled = false;
    }
}

async function handleProfileUpdateSubmit(e) {
    e.preventDefault();
    if (!state.selectedCustomerId || !canManageCustomers) return;

    const payload = {
        consent_marketing: dom.editConsentMkt.checked,
        consent_profiling: dom.editConsentProf.checked,
    };

    if (dom.editFirstName.value.trim()) payload.first_name = dom.editFirstName.value.trim();
    if (dom.editLastName.value.trim()) payload.last_name = dom.editLastName.value.trim();
    if (dom.editPhone.value.trim()) payload.phone_number = dom.editPhone.value.trim();
    if (dom.editBirthDate.value) payload.birth_date = dom.editBirthDate.value;
    if (dom.editAddress.value.trim()) payload.address = dom.editAddress.value.trim();

    dom.btnSaveProfile.disabled = true;
    try {
        const updated = await api.updateCustomer(state.selectedCustomerId, payload);
        state.selectedDetail = updated;
        renderCustomerDetail(updated);
        await fetchCustomersList();
        showToast('Anagrafica e consensi aggiornati!');
    } catch (err) {
        showToast(err.message || 'Errore durante il salvataggio.', 'error');
    } finally {
        dom.btnSaveProfile.disabled = false;
    }
}

async function handleApplyPointsAdjustment() {
    if (!state.selectedCustomerId || !canManageCustomers) return;
    const delta = parseInt(dom.inputPointsDelta.value, 10);
    if (Number.isNaN(delta) || delta === 0) {
        showToast('Inserisci un valore intero diverso da zero (es. +100 o -50).', 'error');
        return;
    }

    dom.btnApplyPointsAdj.disabled = true;
    try {
        const updated = await api.adjustCustomerPoints(state.selectedCustomerId, delta);
        state.selectedDetail = updated;
        renderCustomerDetail(updated);
        await fetchCustomersList();
        showToast(`Rettifica punti (${delta > 0 ? `+${delta}` : delta} pt) registrata!`);
    } catch (err) {
        showToast(err.message || 'Errore nella rettifica punti.', 'error');
    } finally {
        dom.btnApplyPointsAdj.disabled = false;
    }
}

let mergeSearchTimer = null;
function handleMergeSearchInput() {
    clearTimeout(mergeSearchTimer);
    const q = dom.inputMergeSearch.value.trim();
    if (!q) {
        dom.mergeCandidatesContainer.innerHTML = '';
        return;
    }

    mergeSearchTimer = setTimeout(async () => {
        try {
            const list = await api.getCustomers({ q });
            const candidates = list.filter(
                (c) => c.id !== state.selectedCustomerId && c.status !== 'ANONYMIZED'
            );
            renderMergeCandidates(candidates);
        } catch (err) {
            console.error('Merge search failed:', err);
        }
    }, 250);
}

function renderMergeCandidates(candidates) {
    dom.mergeCandidatesContainer.innerHTML = '';
    if (!candidates.length) {
        dom.mergeCandidatesContainer.innerHTML = `
            <div style="font-size: 12px; color: var(--text-muted); padding: 4px 2px;">
                Nessun altro profilo compatibile trovato.
            </div>
        `;
        return;
    }

    candidates.slice(0, 8).forEach((cand) => {
        const item = document.createElement('div');
        item.className = 'merge-candidate-item';
        const candName = getCustomerDisplayName(cand);
        item.innerHTML = `
            <div>
                <div style="font-weight: 600; color: var(--text-primary);">${_esc(candName)}</div>
                <div style="font-size: 11px; color: var(--text-muted);">
                    ${_esc(formatPhone(cand.phone_number))} · ${_esc(formatPoints(cand.points_balance))}
                </div>
            </div>
            <button type="button" class="btn-crm-secondary" style="padding: 4px 10px; font-size: 11.5px;">
                Unisci qui
            </button>
        `;

        item.querySelector('button').addEventListener('click', () => {
            openMergeConfirmation(cand);
        });

        dom.mergeCandidatesContainer.appendChild(item);
    });
}

function openMergeConfirmation(sourceCustomer) {
    if (!state.selectedDetail) return;
    const sourceLabel = `${getCustomerDisplayName(sourceCustomer)} (${formatPhone(sourceCustomer.phone_number)})`;
    const targetLabel = `${getCustomerDisplayName(state.selectedDetail)} (${formatPhone(state.selectedDetail.phone_number)})`;

    dom.confirmTitle.textContent = 'Conferma Unione Profili (Merge)';
    dom.confirmBody.textContent = `Tutte le transazioni e i movimenti punti di "${sourceLabel}" verranno spostati definitivamente in "${targetLabel}", e il profilo duplicato "${sourceLabel}" verrà eliminato. Vuoi procedere?`;
    dom.confirmError.style.display = 'none';

    state.pendingIrreversibleAction = async () => {
        const updated = await api.mergeCustomers(sourceCustomer.id, state.selectedDetail.id);
        state.selectedDetail = updated;
        renderCustomerDetail(updated);
        await fetchCustomersList();
        showToast('Profili uniti con successo!');
    };

    openModal('modal-confirm-irreversible');
}

function openAnonymizeConfirmation() {
    if (!state.selectedDetail) return;
    const targetLabel = `${getCustomerDisplayName(state.selectedDetail)} (${formatPhone(state.selectedDetail.phone_number)})`;

    dom.confirmTitle.textContent = 'Conferma Anonimizzazione GDPR';
    dom.confirmBody.textContent = `Stai per anonimizzare in modo irreversibile il cliente "${targetLabel}". Nome, cognome, cellulare, data di nascita e indirizzo verranno cancellati, i consensi revocati e l'eventuale saldo punti positivo azzerato. Le vendite storiche resteranno intatte in forma anonima.`;
    dom.confirmError.style.display = 'none';

    state.pendingIrreversibleAction = async () => {
        const updated = await api.anonymizeCustomer(state.selectedDetail.id);
        state.selectedDetail = updated;
        renderCustomerDetail(updated);
        await fetchCustomersList();
        showToast('Profilo cliente anonimizzato ai sensi del GDPR.');
    };

    openModal('modal-confirm-irreversible');
}

function init() {
    if (canManageCustomers && dom.btnNewCustomer) {
        dom.btnNewCustomer.style.display = 'inline-flex';
        dom.btnNewCustomer.addEventListener('click', () => {
            dom.formOnboarding.reset();
            dom.onboardingError.style.display = 'none';
            dom.onboardingConflictBox.style.display = 'none';
            openModal('modal-new-onboarding');
            setTimeout(() => dom.onboardingPhoneInput.focus(), 50);
        });
    }

    // Debounced search (250ms)
    let searchDebounce = null;
    dom.searchInput.addEventListener('input', () => {
        const val = dom.searchInput.value;
        dom.btnClearSearch.style.display = val ? 'block' : 'none';
        clearTimeout(searchDebounce);
        searchDebounce = setTimeout(() => {
            state.searchQuery = val.trim();
            fetchCustomersList();
        }, 250);
    });

    dom.btnClearSearch.addEventListener('click', () => {
        dom.searchInput.value = '';
        dom.btnClearSearch.style.display = 'none';
        state.searchQuery = '';
        fetchCustomersList();
    });

    // Filter pills
    dom.filterPills.querySelectorAll('.filter-pill').forEach((pill) => {
        pill.addEventListener('click', () => {
            dom.filterPills.querySelectorAll('.filter-pill').forEach((p) => p.classList.remove('active'));
            pill.classList.add('active');
            state.activeFilter = pill.dataset.filter || 'ALL';
            fetchCustomersList();
        });
    });

    // Detail Tabs
    dom.detailTabs.querySelectorAll('.tab').forEach((btn) => {
        btn.addEventListener('click', () => {
            switchDetailTab(btn.dataset.detailTab);
        });
    });

    // Detail Close / Mobile Back
    if (dom.btnCloseDetail) {
        dom.btnCloseDetail.addEventListener('click', closeCustomerDetail);
    }
    if (dom.btnMobileBack) {
        dom.btnMobileBack.addEventListener('click', () => {
            dom.page.classList.remove('mobile-panel-open');
        });
    }

    // Modal close buttons & backdrop clicks
    document.querySelectorAll('[data-close-modal]').forEach((btn) => {
        btn.addEventListener('click', () => closeModal(btn.dataset.closeModal));
    });
    document.querySelectorAll('.crm-modal-backdrop').forEach((backdrop) => {
        backdrop.addEventListener('click', (e) => {
            if (e.target === backdrop) backdrop.classList.remove('open');
        });
    });

    // Onboarding Modal & 409 Conflict jump
    dom.formOnboarding.addEventListener('submit', handleNewOnboardingSubmit);
    dom.btnOpenConflictCustomer.addEventListener('click', async () => {
        const cid = state.conflictCustomerId;
        closeModal('modal-new-onboarding');
        if (cid) {
            await selectCustomer(cid);
        }
    });

    // Mock Flow Modal
    dom.formMockFlow.addEventListener('submit', handleMockFlowSubmit);

    // Manage Tab handlers
    dom.formProfile.addEventListener('submit', handleProfileUpdateSubmit);
    dom.btnApplyPointsAdj.addEventListener('click', handleApplyPointsAdjustment);
    dom.inputMergeSearch.addEventListener('input', handleMergeSearchInput);
    dom.btnOpenAnonymizeModal.addEventListener('click', openAnonymizeConfirmation);

    // Irreversible confirmation modal execution
    dom.btnConfirmIrreversible.addEventListener('click', async () => {
        if (!state.pendingIrreversibleAction) return;
        dom.btnConfirmIrreversible.disabled = true;
        dom.confirmError.style.display = 'none';
        try {
            await state.pendingIrreversibleAction();
            closeModal('modal-confirm-irreversible');
            state.pendingIrreversibleAction = null;
        } catch (err) {
            dom.confirmError.textContent = err.message || 'Operazione non riuscita.';
            dom.confirmError.style.display = 'block';
        } finally {
            dom.btnConfirmIrreversible.disabled = false;
        }
    });

    initResizer();

    // Initial load + optional ?id=<uuid> deep link from /vendite
    const initialId = new URLSearchParams(window.location.search).get('id');
    fetchCustomersList().then(() => {
        if (initialId) {
            selectCustomer(initialId);
        }
    });
}

document.addEventListener('DOMContentLoaded', init);
