import { initWarehouse } from './warehouse.js?v=20261026_2';
import { renderForms } from './imports.js?v=20261026_2';
import * as staging from './staging.js?v=20261026_2';

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

export function ensureDetailPanelResizer() {
    const panel = document.getElementById('detail-panel');
    if (!panel) return;
    if (!document.getElementById('detail-panel-resizer')) {
        const resizer = document.createElement('div');
        resizer.className = 'detail-panel__resizer';
        resizer.id = 'detail-panel-resizer';
        resizer.title = 'Trascina il bordo per ridimensionare il pannello';
        panel.prepend(resizer);
    }
}

export function enterSplitScreen() {
    const canvas = document.getElementById('canvas');
    if (canvas) {
        if (window.innerWidth <= 1024) {
            canvas.classList.add('mobile-staging-open');
            canvas.style.gridTemplateColumns = '0px 1fr';
        } else {
            canvas.classList.remove('mobile-staging-open');
            const savedWidth = canvas.style.getPropertyValue('--staging-panel-width') || 'calc(100% - 550px)';
            canvas.style.gridTemplateColumns = `minmax(280px, 1fr) var(--staging-panel-width, ${savedWidth})`;
        }
    }
    ensureDetailPanelResizer();
}

export function exitSplitScreen() {
    const canvas = document.getElementById('canvas');
    if (canvas) {
        canvas.classList.remove('mobile-staging-open');
        canvas.style.gridTemplateColumns = '1fr 0px';
    }
}

export function initDetailPanelResizer() {
    const canvas = document.getElementById('canvas');
    const panel = document.getElementById('detail-panel');
    if (!canvas || !panel) return;

    ensureDetailPanelResizer();

    const observer = new MutationObserver(() => {
        if (!document.getElementById('detail-panel-resizer')) {
            ensureDetailPanelResizer();
        }
    });
    observer.observe(panel, { childList: true });

    const MIN_PANEL_WIDTH = 340;
    const MAX_PANEL_RATIO = 0.85;
    let isResizing = false;

    const onMouseMove = (e) => {
        if (!isResizing) return;
        const canvasRect = canvas.getBoundingClientRect();
        const maxPanelWidth = Math.floor(canvasRect.width * MAX_PANEL_RATIO);
        const desiredWidth = canvasRect.right - e.clientX;
        const clampedWidth = Math.min(Math.max(MIN_PANEL_WIDTH, desiredWidth), maxPanelWidth);
        canvas.style.setProperty('--staging-panel-width', `${clampedWidth}px`);
    };

    const onMouseUp = () => {
        if (!isResizing) return;
        isResizing = false;
        const resizer = document.getElementById('detail-panel-resizer');
        if (resizer) resizer.classList.remove('is-resizing');
        canvas.classList.remove('is-resizing-panel');
        window.removeEventListener('mousemove', onMouseMove);
        window.removeEventListener('mouseup', onMouseUp);
    };

    panel.addEventListener('mousedown', (e) => {
        if (e.target.closest('#detail-panel-resizer')) {
            e.preventDefault();
            isResizing = true;
            const resizer = document.getElementById('detail-panel-resizer');
            if (resizer) resizer.classList.add('is-resizing');
            canvas.classList.add('is-resizing-panel');
            window.addEventListener('mousemove', onMouseMove);
            window.addEventListener('mouseup', onMouseUp);
        }
    });
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
    initDetailPanelResizer();
    
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
