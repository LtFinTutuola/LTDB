/**
 * main.js
 * Application entry point and orchestrator.
 * Manages the mode state machine, layout transitions, and event wiring.
 */
import * as chat from './chat.js';
import * as staging from './staging.js';
import * as search from './search.js';

// ---- Mode constants ----
export const Mode = {
    IDLE:        'idle',
    FORM_DDT:    'form-ddt',
    FORM_SINGLE: 'form-single',
    POLLING:     'polling',
    STAGING:     'staging',
    SEARCH:      'search',
};

// ---- App state ----
export const appState = {
    brands:     window.__LTDB_BRANDS__ || [],
    currentMode: Mode.IDLE,
    currentJobId: null,
    pollingTimer: null,
};

// ---- DOM references ----
const canvas      = document.getElementById('canvas');
const chatMsgs    = document.getElementById('chat-messages');
const formCont    = document.getElementById('import-form-container');
const detailPanel = document.getElementById('detail-panel');
const chatInput   = document.getElementById('chat-input');
const btnPlus     = document.getElementById('btn-plus');
const btnSend     = document.getElementById('btn-send');

// ---- Layout ----
export function enterSplitScreen() {
    canvas.classList.remove('canvas--chat');
    canvas.classList.add('canvas--split');
}

export function exitSplitScreen() {
    canvas.classList.add('canvas--chat');
    canvas.classList.remove('canvas--split');
    // Allow the grid transition to finish before clearing innerHTML
    setTimeout(() => { detailPanel.innerHTML = ''; }, 400);
}

// ---- Mode management ----
export function setMode(mode) {
    appState.currentMode = mode;
    _updateSendButton(mode);
}

function _updateSendButton(mode) {
    switch (mode) {
        case Mode.IDLE:
        case Mode.SEARCH:
            btnSend.textContent = 'Invia';
            btnSend.disabled = false;
            break;
        case Mode.FORM_DDT:
        case Mode.FORM_SINGLE:
            btnSend.textContent = 'Importa';
            btnSend.disabled = false;
            break;
        case Mode.POLLING:
            btnSend.textContent = 'Elaborazione...';
            btnSend.disabled = true;
            break;
        case Mode.STAGING:
            btnSend.textContent = 'Applica modifiche';
            btnSend.disabled = false;
            break;
    }
}

// ---- Chat helpers (exported for use by chat/staging/search modules) ----
export function addMessage(text, type = 'system') {
    const div = document.createElement('div');
    div.className = `chat-message chat-message--${type}`;
    div.textContent = text;
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
        btnPlus.textContent = '+';
        btnPlus.title = 'Azioni';
    } else {
        btnPlus.textContent = '←';
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

// ---- Init ----
function init() {
    chat.init();

    btnPlus.addEventListener('click', (e) => {
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
