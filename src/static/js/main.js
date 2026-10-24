import { initWarehouse } from './warehouse.js?v=20261023_7';
import { renderForms } from './imports.js?v=20261023_7';
import * as staging from './staging.js?v=20261023_7';

export function _esc(str) {
    if (typeof str !== 'string') return '';
    return str.replace(/[&<>"']/g, m => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;'
    })[m]);
}

export function addMessage(text, type = 'system') {
    const toast = document.createElement('div');
    toast.style.position = 'fixed';
    toast.style.bottom = '20px';
    toast.style.right = '20px';
    toast.style.padding = '12px 24px';
    toast.style.borderRadius = 'var(--radius-md)';
    toast.style.background = type === 'error' ? 'var(--error)' : 'var(--bg-surface)';
    toast.style.color = type === 'error' ? '#fff' : 'var(--text-primary)';
    toast.style.boxShadow = '0 4px 6px rgba(0,0,0,0.1)';
    toast.style.zIndex = '9999';
    toast.style.transition = 'opacity 0.3s';
    
    if (typeof text === 'string') {
        toast.innerHTML = text;
    } else {
        toast.appendChild(text);
    }
    
    document.body.appendChild(toast);
    
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
    return toast;
}

export function enterSplitScreen() {
    const canvas = document.getElementById('canvas');
    if (canvas) {
        if (window.innerWidth <= 1024) {
            canvas.classList.add('mobile-staging-open');
            canvas.style.gridTemplateColumns = '0px 1fr';
        } else {
            canvas.classList.remove('mobile-staging-open');
            canvas.style.gridTemplateColumns = '550px 1fr';
        }
    }
}

export function exitSplitScreen() {
    const canvas = document.getElementById('canvas');
    if (canvas) {
        canvas.classList.remove('mobile-staging-open');
        canvas.style.gridTemplateColumns = '1fr 0px';
    }
}

export function getDetailPanel() {
    return document.getElementById('detail-panel');
}

export function addChangeLogEntry(key, value, isDelete = false) {
    const entry = document.createElement('div');
    entry.className = `change-log-entry${isDelete ? ' change-log-entry--delete' : ''}`;
    entry.innerHTML =
        `<span class="change-key">${_esc(key)}</span>` +
        `<span class="change-sep">:</span>` +
        `<span class="change-val">${_esc(String(value))}</span>`;
    return entry;
}

// Keep a minimal Mode enum for staging compatibility
export const Mode = {
    IDLE: 'idle',
    STAGING: 'staging'
};

export function setMode(mode) {
    // No-op for now, just to keep staging.js happy
}

export const appState = {
    currentJobId: null
};

// Initialize Tabs
function initTabs() {
    const tabs = document.querySelectorAll('#magazzino-tabs .tab');
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            tabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            
            const target = tab.dataset.tab;
            document.querySelectorAll('.tab-pane').forEach(pane => {
                pane.classList.remove('active');
            });
            document.getElementById(`tab-${target}`).classList.add('active');
            
            if (target === 'catalog') {
                exitSplitScreen();
                initWarehouse();
            } else if (target === 'import-ddt') {
                // If there's a staging panel, we show it (enterSplitScreen does this if needed)
            } else if (target === 'import-article') {
                exitSplitScreen();
            }
        });
    });
}

document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initWarehouse();
    renderForms();
    
    // Check if there is an action button for staging that requires main.js hook
    document.addEventListener('click', async (e) => {
        if (e.target.closest('#btn-confirm-staging')) {
            await staging.submitRevisions();
        }
        if (e.target.closest('#btn-cancel-staging')) {
            staging.abortStaging();
            exitSplitScreen();
        }
    });
});
