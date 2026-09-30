/**
 * main.js
 * Application entry point and orchestrator.
 * Manages the mode state machine, layout transitions, and event wiring.
 */
import * as chat from './chat.js';
import * as staging from './staging.js';
import * as search from './search.js';
import * as api from './api.js';

// ---- Mode constants ----
export const Mode = {
    IDLE:        'idle',
    FORM_DDT:    'form-ddt',
    FORM_SINGLE: 'form-single',
    POLLING:     'polling',
    STAGING:     'staging',
    SEARCH:      'search',
    RECONCILIATION: 'reconciliation',
};

// ---- App state ----
export const appState = {
    brands:     window.__LTDB_BRANDS__ || [],
    currentMode: Mode.IDLE,
    currentJobId: null,
    pollingTimer: null,
    sidebarCollapsed: false,
};

// ---- DOM references ----
const canvas      = document.getElementById('canvas');
const chatMsgs    = document.getElementById('chat-messages');
const formCont    = document.getElementById('import-form-container');
const detailPanel = document.getElementById('detail-panel');
const chatInput   = document.getElementById('chat-input');
const btnPlus     = document.getElementById('btn-plus');
const btnSend     = document.getElementById('btn-send');
const btnSidebarToggle = document.getElementById('btn-sidebar-toggle');

// ---- Layout ----
let _clearPanelTimeout = null;

export function enterSplitScreen() {
    canvas.classList.add('canvas--split');
    if (_clearPanelTimeout) {
        clearTimeout(_clearPanelTimeout);
        _clearPanelTimeout = null;
    }
}

export function exitSplitScreen() {
    canvas.classList.remove('canvas--split');
    // Notifica l'artifact box che il panel è stato chiuso
    document.dispatchEvent(new CustomEvent('detail:closed'));
    // Allow the grid transition to finish before clearing innerHTML
    if (_clearPanelTimeout) {
        clearTimeout(_clearPanelTimeout);
    }
    _clearPanelTimeout = setTimeout(() => { 
        detailPanel.innerHTML = ''; 
        _clearPanelTimeout = null;
    }, 400);
}

export function toggleSidebar() {
    appState.sidebarCollapsed = !appState.sidebarCollapsed;
    if (appState.sidebarCollapsed) {
        canvas.classList.add('canvas--sidebar-collapsed');
        document.getElementById('sidebar').classList.add('sidebar--collapsed');
    } else {
        canvas.classList.remove('canvas--sidebar-collapsed');
        document.getElementById('sidebar').classList.remove('sidebar--collapsed');
    }
    localStorage.setItem('ltdb_sidebar_collapsed', appState.sidebarCollapsed);
}

// ---- Mode management ----
export function setMode(mode) {
    appState.currentMode = mode;
    _updateSendButton(mode);
}

function _updateSendButton(mode) {
    // The button now uses icons, we just update the title and disabled state
    switch (mode) {
        case Mode.IDLE:
        case Mode.SEARCH:
            btnSend.title = 'Invia';
            btnSend.disabled = false;
            break;
        case Mode.FORM_DDT:
        case Mode.FORM_SINGLE:
            btnSend.title = 'Importa';
            btnSend.disabled = false;
            break;
        case Mode.POLLING:
            btnSend.title = 'Elaborazione...';
            btnSend.disabled = true;
            break;
        case Mode.STAGING:
            btnSend.title = 'Applica modifiche';
            btnSend.disabled = false;
            break;
        case Mode.RECONCILIATION:
            btnSend.title = 'Riconciliazione in corso...';
            btnSend.disabled = true;
            break;
    }
}

// ---- Chat helpers (exported for use by chat/staging/search modules) ----
export function addMessage(text, type = 'system') {
    const div = document.createElement('div');
    div.className = `chat-message chat-message--${type}`;
    // If it's a DOM element (from artifact box), append it, else textContent
    if (typeof text === 'string') {
        div.innerHTML = text; // allowing HTML for spinners
    } else {
        div.appendChild(text);
    }
    chatMsgs.appendChild(div);
    chatMsgs.scrollTop = chatMsgs.scrollHeight;
    return div;
}

export function addChangeLogEntry(key, value, isDelete = false) {
    const entry = document.createElement('div');
    entry.className = `change-log-entry${isDelete ? ' change-log-entry--delete' : ''}`;
    entry.innerHTML =
        `<span class="change-key">${_esc(key)}</span>` +
        `<span class="change-sep">:</span>` +
        `<span class="change-val">${_esc(String(value))}</span>`;
    entry.dataset.key = key;
    chatMsgs.appendChild(entry);
    chatMsgs.scrollTop = chatMsgs.scrollHeight;
}

export function removeChangeLogEntriesByKey(key) {
    chatMsgs.querySelectorAll(`.change-log-entry[data-key="${CSS.escape(key)}"]`)
        .forEach(el => el.remove());
}

export function clearChangeLogEntries() {
    chatMsgs.querySelectorAll('.change-log-entry').forEach(el => el.remove());
}

export function _esc(str) {
    return String(str)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;')
        .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ---- Input area helpers ----
export function showChatInput(show) {
    chatInput.style.display = show ? '' : 'none';
    formCont.style.display = show ? 'none' : '';
    
    if (show) {
        btnPlus.innerHTML = '<span class="material-symbols-rounded">add_circle</span>';
        btnPlus.title = 'Azioni';
    } else {
        btnPlus.innerHTML = '<span class="material-symbols-rounded">arrow_back</span>';
        btnPlus.title = 'Indietro';
    }
}

export function setChatInputLocked(locked) {
    chatInput.disabled = locked;
    btnPlus.disabled   = locked;
    btnSend.disabled   = locked;
}

export function clearFormContainer() {
    formCont.innerHTML = '';
}

export function getFormContainer() { return formCont; }
export function getDetailPanel()   { return detailPanel; }

// ---- Send button handler ----
async function handleSend() {
    const mode = appState.currentMode;

    if (mode === Mode.IDLE || mode === Mode.SEARCH) {
        const query = chatInput.value.trim();
        if (!query) return;
        addMessage(query, 'user');
        chatInput.value = '';
        chatInput.style.height = 'auto'; // reset height
        setChatInputLocked(true);
        await search.executeSearch(query);
        setChatInputLocked(false);
        return;
    }

    if (mode === Mode.FORM_DDT) {
        await chat.submitDDTForm();
        return;
    }

    if (mode === Mode.FORM_SINGLE) {
        await chat.submitSingleItemForm();
        return;
    }

    if (mode === Mode.STAGING) {
        await staging.submitRevisions();
        return;
    }
}

// ---- Background Polling per Orfani ----
let _lastOrphanCount = 0;
let _isFirstPoll = true;
async function _pollOrphans() {
    try {
        const res = await api.getPendingSales();
        const orphans = res.data.filter(r => r.status === 'ORPHAN');
        const count = orphans.length;
        if (count > _lastOrphanCount || (_isFirstPoll && count > 0)) {
            const diff = count > _lastOrphanCount ? count - _lastOrphanCount : count;
            if (Notification.permission === 'granted' && appState.currentMode !== Mode.RECONCILIATION) {
                new Notification(`Ci sono ${diff} nuove vendite da riconciliare!`, {
                    body: `Totale vendite in sospeso: ${count}`
                });
            }
            btnPlus.classList.add('has-badge'); // CSS da aggiungere
        } else if (count === 0) {
            btnPlus.classList.remove('has-badge');
        }
        _lastOrphanCount = count;
        _isFirstPoll = false;
        
        // Se siamo in RECONCILIATION, aggiorniamo il badge per sicurezza
        if (appState.currentMode === Mode.RECONCILIATION && count === 0) {
            btnPlus.classList.remove('has-badge');
        }
    } catch (e) {
        console.warn('Errore polling orfani:', e);
    }
}

export function getOrphanCount() {
    return _lastOrphanCount;
}

export function forceOrphansRefresh() {
    _pollOrphans();
}

async function startOrphansPollingDaemon() {
    _pollOrphans(); // first check
    setInterval(_pollOrphans, 60000); // every 60s
}

// ---- Init ----
function init() {
    startOrphansPollingDaemon();
    chat.init();

    // Restore sidebar state
    if (localStorage.getItem('ltdb_sidebar_collapsed') === 'true') {
        toggleSidebar(); // it starts false, so this makes it true
    }
    
    if (btnSidebarToggle) {
        btnSidebarToggle.addEventListener('click', toggleSidebar);
    }

    btnPlus.addEventListener('click', async (e) => {
        if ('Notification' in window && Notification.permission === 'default') {
            await Notification.requestPermission();
        }
        e.stopPropagation();
        if (appState.currentMode === Mode.FORM_DDT || appState.currentMode === Mode.FORM_SINGLE) {
            showChatInput(true);
            clearFormContainer();
            setMode(Mode.IDLE);
        } else {
            chat.togglePlusMenu();
        }
    });

    document.addEventListener('click', () => chat.closePlusMenu());

    btnSend.addEventListener('click', handleSend);
    chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
    });
    chatInput.addEventListener('input', () => {
        chatInput.style.height = 'auto';
        chatInput.style.height = (chatInput.scrollHeight) + 'px';
    });

    // Restore to idle after ingestion confirmation
    document.addEventListener('ingestionDone', () => {
        exitSplitScreen();
        showChatInput(true);
        clearFormContainer();
        clearChangeLogEntries();
        setMode(Mode.IDLE);
    });
}

init();
