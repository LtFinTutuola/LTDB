const WELCOME_MESSAGE =
    "Ciao! Sono qui per aiutarti ad analizzare le vendite. Seleziona una vendita per riconciliarla con l'articolo venduto, oppure chiedimi qualcosa riguardo alla giornata di oggi.";

const LINE_HEIGHT_PX = 20;
const MAX_TEXTAREA_LINES = 10;
const MAX_TEXTAREA_HEIGHT = LINE_HEIGHT_PX * MAX_TEXTAREA_LINES; // 200px

function escapeHtml(raw) {
    return String(raw ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatInlineMarkdown(escapedLine) {
    // 1. Protect inline code spans `...` using a token free of markdown characters (_, *, `)
    const codeSpans = [];
    let processed = escapedLine.replace(/`([^`]+)`/g, (_, code) => {
        const token = `@@INLINECODE${codeSpans.length}@@`;
        codeSpans.push(`<code>${code}</code>`);
        return token;
    });

    // 2. Bold + Italic (***text***)
    processed = processed.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
    // 3. Bold (**text** or __text__)
    processed = processed.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    processed = processed.replace(/__(.+?)__/g, '<strong>$1</strong>');
    // 4. Italic (*text* or _text_ at non-word boundaries so snake_case is preserved)
    processed = processed.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, '$1<em>$2</em>');
    processed = processed.replace(/(^|[^\w_])_([^_]+)_(?![\w_])/g, '$1<em>$2</em>');

    // 5. Restore inline code spans
    codeSpans.forEach((html, idx) => {
        processed = processed.replace(`@@INLINECODE${idx}@@`, () => html);
    });

    return processed;
}

export function renderMarkdown(markdownText) {
    if (!markdownText) return '';
    const normalized = String(markdownText).replace(/\r\n/g, '\n');

    // Extract fenced code blocks ```...``` first
    const codeBlocks = [];
    const withBlockTokens = normalized.replace(/```(?:\w+)?\n?([\s\S]*?)```/g, (_, code) => {
        const token = `@@CODEBLOCK${codeBlocks.length}@@`;
        codeBlocks.push(`<pre><code>${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`);
        return `\n${token}\n`;
    });

    const lines = withBlockTokens.split('\n');
    const htmlParts = [];
    let inUl = false;
    let inOl = false;
    let paragraphLines = [];

    const flushParagraph = () => {
        if (paragraphLines.length > 0) {
            htmlParts.push(`<p>${paragraphLines.join('<br>')}</p>`);
            paragraphLines = [];
        }
    };

    const closeLists = () => {
        if (inUl) {
            htmlParts.push('</ul>');
            inUl = false;
        }
        if (inOl) {
            htmlParts.push('</ol>');
            inOl = false;
        }
    };

    for (const rawLine of lines) {
        const trimmed = rawLine.trim();

        // Empty line -> close current paragraph / lists
        if (!trimmed) {
            flushParagraph();
            closeLists();
            continue;
        }

        // Fenced code block token
        const blockMatch = trimmed.match(/^@@CODEBLOCK(\d+)@@$/);
        if (blockMatch) {
            flushParagraph();
            closeLists();
            htmlParts.push(codeBlocks[Number(blockMatch[1])]);
            continue;
        }

        const escaped = escapeHtml(rawLine);

        // Horizontal rule
        if (/^\s*([-*_])\s*\1\s*\1+\s*$/.test(rawLine)) {
            flushParagraph();
            closeLists();
            htmlParts.push('<hr>');
            continue;
        }

        // Headings (# .. ####)
        const headingMatch = escaped.match(/^\s*(#{1,4})\s+(.+)$/);
        if (headingMatch) {
            flushParagraph();
            closeLists();
            const level = headingMatch[1].length;
            htmlParts.push(`<h${level}>${formatInlineMarkdown(headingMatch[2].trim())}</h${level}>`);
            continue;
        }

        // Unordered list item (* or -)
        const ulMatch = escaped.match(/^\s*[*-]\s+(.+)$/);
        if (ulMatch) {
            flushParagraph();
            if (inOl) {
                htmlParts.push('</ol>');
                inOl = false;
            }
            if (!inUl) {
                htmlParts.push('<ul>');
                inUl = true;
            }
            htmlParts.push(`<li>${formatInlineMarkdown(ulMatch[1].trim())}</li>`);
            continue;
        }

        // Ordered list item (1. , 2. , ...)
        const olMatch = escaped.match(/^\s*\d+\.\s+(.+)$/);
        if (olMatch) {
            flushParagraph();
            if (inUl) {
                htmlParts.push('</ul>');
                inUl = false;
            }
            if (!inOl) {
                htmlParts.push('<ol>');
                inOl = true;
            }
            htmlParts.push(`<li>${formatInlineMarkdown(olMatch[1].trim())}</li>`);
            continue;
        }

        // Blockquote (&gt; ...)
        const quoteMatch = escaped.match(/^\s*&gt;\s?(.*)$/);
        if (quoteMatch) {
            flushParagraph();
            closeLists();
            htmlParts.push(`<blockquote>${formatInlineMarkdown(quoteMatch[1].trim())}</blockquote>`);
            continue;
        }

        // Regular paragraph line
        closeLists();
        paragraphLines.push(formatInlineMarkdown(escaped.trim()));
    }

    flushParagraph();
    closeLists();

    return htmlParts.join('');
}

export function createChatActionButtons(messageId = null, isReported = false, reportReason = null) {
    const actions = document.createElement('div');
    actions.className = 'chat-bubble-actions';

    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'chat-action-btn btn-chat-copy';
    copyBtn.title = 'Copia negli appunti';
    copyBtn.setAttribute('aria-label', 'Copia negli appunti');
    copyBtn.innerHTML = `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
        </svg>
    `;

    const reportBtn = document.createElement('button');
    reportBtn.type = 'button';
    reportBtn.className = 'chat-action-btn btn-chat-report';
    if (isReported) {
        reportBtn.classList.add('is-reported');
        reportBtn.title = `Messaggio già segnalato: ${reportReason || ''}`;
        reportBtn.setAttribute('aria-label', `Messaggio già segnalato: ${reportReason || ''}`);
    } else {
        reportBtn.title = 'Segnala messaggio';
        reportBtn.setAttribute('aria-label', 'Segnala messaggio');
    }
    if (messageId) {
        reportBtn.dataset.messageId = messageId;
    }
    reportBtn.innerHTML = `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"></path>
            <line x1="4" y1="22" x2="4" y2="15"></line>
        </svg>
    `;

    actions.appendChild(copyBtn);
    actions.appendChild(reportBtn);
    return actions;
}

export async function promptReportReason() {
    while (true) {
        const input = window.prompt("Indica il motivo per cui questo messaggio è errato:");
        if (input === null) {
            return null; // Operazione annullata dall'utente
        }
        const trimmed = input.trim();
        if (trimmed.length > 0) {
            return trimmed;
        }
        window.alert("Il motivo della segnalazione non può essere vuoto. Inserisci una motivazione prima di procedere.");
    }
}

export async function handleCopyMessage(text, copyBtn) {
    const cleanText = (text || '').trim();
    if (!cleanText) return;

    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(cleanText);
        } else {
            const textArea = document.createElement('textarea');
            textArea.value = cleanText;
            textArea.style.position = 'fixed';
            textArea.style.left = '-9999px';
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            document.execCommand('copy');
            document.body.removeChild(textArea);
        }

        if (copyBtn) {
            copyBtn.classList.add('is-copied');
            const originalTitle = copyBtn.title;
            const originalHTML = copyBtn.innerHTML;
            copyBtn.title = 'Copiato!';
            copyBtn.innerHTML = `
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <polyline points="20 6 9 17 4 12"></polyline>
                </svg>
            `;
            setTimeout(() => {
                copyBtn.classList.remove('is-copied');
                copyBtn.title = originalTitle || 'Copia negli appunti';
                copyBtn.innerHTML = originalHTML;
            }, 1800);
        }
    } catch (err) {
        console.error('Errore durante la copia del messaggio', err);
        window.alert('Impossibile copiare il messaggio negli appunti.');
    }
}

export async function handleReportMessage(messageId, reportBtn) {
    if (!messageId) {
        window.alert('Questo messaggio non può essere segnalato (ID non disponibile).');
        return;
    }

    const reason = await promptReportReason();
    if (!reason) {
        return; // Annullato dall'utente
    }

    try {
        const res = await fetch('/api/v1/sales/chat/report', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message_id: messageId,
                reason: reason,
            }),
        });

        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || 'Errore durante la segnalazione del messaggio.');
        }

        const data = await res.json();
        if (reportBtn) {
            reportBtn.classList.add('is-reported');
            reportBtn.title = `Messaggio segnalato: ${reason}`;
            reportBtn.setAttribute('aria-label', `Messaggio segnalato: ${reason}`);
        }
        window.alert('Messaggio segnalato come errato. Grazie per la segnalazione!');
        return data;
    } catch (err) {
        console.error('Errore durante la segnalazione', err);
        window.alert(`Impossibile inviare la segnalazione: ${err.message}`);
    }
}

export function simulateStreamingText(element, fullText, { onProgress = null, onComplete = null, intervalMs = 20, maxSteps = 70 } = {}) {
    if (!element) return null;
    const cleanText = fullText || '';
    if (!cleanText) {
        element.innerHTML = '';
        if (onComplete) onComplete();
        return null;
    }

    // Split into words and spacing tokens
    const tokens = cleanText.match(/\S+\s*|\s+/g) || [cleanText];
    const totalTokens = tokens.length;

    // Adaptive chunking: short messages typed word-by-word, long messages finish smoothly in ~1.5 - 2.2s
    const targetSteps = Math.min(Math.max(totalTokens, 15), maxSteps);
    const chunkSize = Math.max(1, Math.ceil(totalTokens / targetSteps));

    let index = 0;
    let currentText = '';

    const timer = setInterval(() => {
        if (!element.isConnected) {
            clearInterval(timer);
            return;
        }

        const nextChunk = tokens.slice(index, index + chunkSize).join('');
        currentText += nextChunk;
        index += chunkSize;

        element.innerHTML = renderMarkdown(currentText);

        if (onProgress) {
            onProgress(currentText);
        }

        if (index >= totalTokens) {
            clearInterval(timer);
            element.innerHTML = renderMarkdown(cleanText);
            if (onComplete) {
                onComplete();
            }
        }
    }, intervalMs);

    return timer;
}

export function initSalesChat(dom) {
    if (!dom.chatInput || !dom.btnChatSend || !dom.chatMessages) {
        console.warn("Elementi chat non trovati");
        return;
    }

    const sessionSelect = document.getElementById('chat-session-select');
    const btnNewChat = document.getElementById('btn-new-chat');
    const btnChatHistory = document.getElementById('btn-chat-history');
    const btnCloseChatHistory = document.getElementById('btn-close-chat-history');
    const historyPopup = document.getElementById('chat-history-popup');
    const historyList = document.getElementById('chat-history-list');

    let currentSessionId = null;
    let pastSessions = [];
    let isBusy = false;

    function openChatHistoryPopup() {
        if (!historyPopup) return;
        historyPopup.style.display = 'flex';
        renderChatHistoryList();
    }

    function closeChatHistoryPopup() {
        if (!historyPopup) return;
        historyPopup.style.display = 'none';
    }

    function toggleChatHistoryPopup() {
        if (!historyPopup) return;
        if (historyPopup.style.display === 'none' || !historyPopup.style.display) {
            openChatHistoryPopup();
        } else {
            closeChatHistoryPopup();
        }
    }

    function renderChatHistoryList() {
        if (!historyList) return;
        historyList.innerHTML = '';
        if (!pastSessions || pastSessions.length === 0) {
            historyList.innerHTML = '<div class="chat-history-empty">Nessuna conversazione precedente</div>';
            return;
        }

        pastSessions.forEach((s) => {
            const item = document.createElement('div');
            item.className = 'chat-history-item';
            if (s.id === currentSessionId) {
                item.classList.add('active');
            }
            item.dataset.sessionId = s.id;

            const titleEl = document.createElement('div');
            titleEl.className = 'chat-history-item__title';
            titleEl.textContent = s.title || 'Nuova conversazione';

            const metaEl = document.createElement('div');
            metaEl.className = 'chat-history-item__meta';
            const dateStr = s.updated_at
                ? new Date(s.updated_at).toLocaleString('it-IT', {
                    day: '2-digit',
                    month: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                })
                : '';
            metaEl.textContent = dateStr;

            item.appendChild(titleEl);
            if (dateStr) item.appendChild(metaEl);

            item.addEventListener('click', () => {
                closeChatHistoryPopup();
                loadChatSession(s.id);
            });

            historyList.appendChild(item);
        });
    }

    function scrollChatToBottom() {
        if (!dom.chatMessages) return;
        dom.chatMessages.scrollTop = dom.chatMessages.scrollHeight;
        requestAnimationFrame(() => {
            if (dom.chatMessages) {
                dom.chatMessages.scrollTop = dom.chatMessages.scrollHeight;
            }
        });
    }

    function autoResizeChatInput() {
        if (!dom.chatInput) return;
        dom.chatInput.style.height = 'auto';
        const scrollH = dom.chatInput.scrollHeight;
        const targetHeight = Math.min(Math.max(scrollH, LINE_HEIGHT_PX), MAX_TEXTAREA_HEIGHT);
        dom.chatInput.style.height = `${targetHeight}px`;
        dom.chatInput.style.overflowY = scrollH > MAX_TEXTAREA_HEIGHT ? 'auto' : 'hidden';
    }

    function setBubbleContent(bubble, text, sender) {
        if (sender === 'gemini') {
            bubble.innerHTML = renderMarkdown(text);
        } else {
            bubble.textContent = text;
        }
    }

    function appendMessage(text, sender, id = null, messageId = null, isReported = false, reportReason = null) {
        const group = document.createElement('div');
        group.className = `chat-msg-group chat-msg-group--${sender}`;
        if (messageId) {
            group.dataset.messageId = messageId;
        }
        group.dataset.rawText = text;

        const bubble = document.createElement('div');
        bubble.className = `chat-bubble chat-bubble--${sender}`;
        setBubbleContent(bubble, text, sender);
        if (id) {
            bubble.id = id;
        }
        group.appendChild(bubble);

        if (sender === 'gemini') {
            if (text !== '...') {
                group.appendChild(createChatActionButtons(messageId, isReported, reportReason));
            } else {
                const placeholder = document.createElement('div');
                placeholder.className = 'chat-bubble-actions';
                group.appendChild(placeholder);
            }
        }

        dom.chatMessages.appendChild(group);
        scrollChatToBottom();
        return group;
    }

    function resetChatUI() {
        isBusy = false;
        if (dom.btnChatSend) dom.btnChatSend.disabled = false;
        currentSessionId = null;
        if (sessionSelect) {
            sessionSelect.value = '';
        }
        dom.chatMessages.innerHTML = '';
        appendMessage(WELCOME_MESSAGE, 'gemini');
        dom.chatInput.value = '';
        autoResizeChatInput();
        closeChatHistoryPopup();
        renderChatHistoryList();
    }

    async function fetchChatSessions(selectedId = null) {
        try {
            const res = await fetch('/api/v1/sales/chats');
            if (!res.ok) return;
            pastSessions = await res.json();

            if (sessionSelect) {
                sessionSelect.innerHTML = '<option value="">Nuova conversazione</option>';
                pastSessions.forEach((s) => {
                    const opt = document.createElement('option');
                    opt.value = s.id;
                    const dateLabel = s.updated_at
                        ? new Date(s.updated_at).toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit' })
                        : '';
                    opt.textContent = dateLabel ? `[${dateLabel}] ${s.title}` : s.title;
                    sessionSelect.appendChild(opt);
                });

                if (selectedId) {
                    sessionSelect.value = selectedId;
                }
            }

            renderChatHistoryList();
        } catch (err) {
            console.error('Impossibile caricare la lista delle chat', err);
        }
    }

    async function loadChatSession(sessionId) {
        if (!sessionId) {
            resetChatUI();
            return;
        }
        try {
            const res = await fetch(`/api/v1/sales/chats/${encodeURIComponent(sessionId)}`);
            if (!res.ok) {
                console.error('Sessione chat non trovata');
                return;
            }
            const detail = await res.json();
            currentSessionId = detail.id;
            if (sessionSelect) {
                sessionSelect.value = currentSessionId;
            }
            renderChatHistoryList();
            closeChatHistoryPopup();
            dom.chatMessages.innerHTML = '';

            if (!detail.messages || detail.messages.length === 0) {
                appendMessage(WELCOME_MESSAGE, 'gemini');
            } else {
                detail.messages.forEach((m) => {
                    const sender = m.role === 'user' ? 'user' : 'gemini';
                    appendMessage(m.content, sender, null, m.id, m.is_reported, m.report_reason);
                });
            }
            scrollChatToBottom();
        } catch (err) {
            console.error('Errore nel caricamento della sessione chat', err);
        }
    }


    const sendMessage = async () => {
        const message = dom.chatInput.value.trim();
        if (!message || isBusy) return;

        isBusy = true;
        if (dom.btnChatSend) dom.btnChatSend.disabled = true;

        // Add user message
        appendMessage(message, 'user');
        dom.chatInput.value = '';
        autoResizeChatInput();

        // Add loading state
        const loadingId = 'loading-' + Date.now();
        appendMessage('...', 'gemini', loadingId);

        try {
            const contextDate =
                document.getElementById('date-picker')?.value ||
                new Date().toISOString().split('T')[0];
            const payload = {
                message: message,
                context_date: contextDate,
                session_id: currentSessionId,
            };

            const res = await fetch('/api/v1/sales/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            }).then((r) => r.json());

            const loadingEl = document.getElementById(loadingId);
            if (loadingEl) {
                const replyText = res.reply || 'Errore nella risposta.';
                const group = loadingEl.closest('.chat-msg-group');

                // Simulate typewriter live streaming generation of text
                simulateStreamingText(loadingEl, replyText, {
                    onProgress: () => {
                        scrollChatToBottom();
                    },
                    onComplete: () => {
                        if (group) {
                            group.dataset.rawText = replyText;
                            if (res.message_id) {
                                group.dataset.messageId = res.message_id;
                            }
                            const existingActions = group.querySelector('.chat-bubble-actions');
                            const newActions = createChatActionButtons(res.message_id, false, null);
                            if (existingActions) {
                                existingActions.replaceWith(newActions);
                            } else {
                                group.appendChild(newActions);
                            }
                        }
                        scrollChatToBottom();
                        isBusy = false;
                        if (dom.btnChatSend) dom.btnChatSend.disabled = false;
                    },
                });
            } else {
                isBusy = false;
                if (dom.btnChatSend) dom.btnChatSend.disabled = false;
            }

            if (res.session_id) {
                const isNewSession = currentSessionId !== res.session_id;
                currentSessionId = res.session_id;
                if (isNewSession) {
                    await fetchChatSessions(currentSessionId);
                }
            }
        } catch (err) {
            console.error('Chat error', err);
            const loadingEl = document.getElementById(loadingId);
            if (loadingEl) {
                setBubbleContent(loadingEl, 'Si è verificato un errore di connessione con Gemini.', 'gemini');
                scrollChatToBottom();
            }
            isBusy = false;
            if (dom.btnChatSend) dom.btnChatSend.disabled = false;
        }
    };

    dom.btnChatSend.addEventListener('click', sendMessage);

    dom.chatInput.addEventListener('input', autoResizeChatInput);

    dom.chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    if (btnNewChat) {
        btnNewChat.addEventListener('click', () => {
            resetChatUI();
        });
    }

    if (btnChatHistory) {
        btnChatHistory.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleChatHistoryPopup();
        });
    }

    if (btnCloseChatHistory) {
        btnCloseChatHistory.addEventListener('click', (e) => {
            e.stopPropagation();
            closeChatHistoryPopup();
        });
    }

    document.addEventListener('click', (e) => {
        if (historyPopup && historyPopup.style.display !== 'none') {
            if (!historyPopup.contains(e.target) && e.target !== btnChatHistory && !btnChatHistory?.contains(e.target)) {
                closeChatHistoryPopup();
            }
        }
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && historyPopup && historyPopup.style.display !== 'none') {
            closeChatHistoryPopup();
        }
    });

    if (sessionSelect) {
        sessionSelect.addEventListener('change', (e) => {
            loadChatSession(e.target.value);
        });
    }

    // Event delegation on chat messages for copy and report action buttons
    dom.chatMessages.addEventListener('click', (e) => {
        const copyBtn = e.target.closest('.btn-chat-copy');
        if (copyBtn) {
            e.preventDefault();
            const group = copyBtn.closest('.chat-msg-group') || copyBtn.closest('.chat-bubble--gemini')?.parentElement;
            const bubble = group ? group.querySelector('.chat-bubble--gemini') : null;
            const textToCopy = group?.dataset?.rawText || bubble?.innerText || bubble?.textContent || '';
            handleCopyMessage(textToCopy, copyBtn);
            return;
        }

        const reportBtn = e.target.closest('.btn-chat-report');
        if (reportBtn) {
            e.preventDefault();
            const group = reportBtn.closest('.chat-msg-group');
            const messageId = reportBtn.dataset?.messageId || group?.dataset?.messageId;
            handleReportMessage(messageId, reportBtn);
            return;
        }
    });


    // Horizontal drag-to-resize for the side panel
    const resizer = document.getElementById('work-panel-resizer');
    const salesPage = dom.page || document.querySelector('.sales-page');
    if (resizer && salesPage) {
        const MIN_PANEL_WIDTH = 300;
        const MAX_PANEL_RATIO = 0.75;

        let isResizing = false;

        const onMouseMove = (e) => {
            if (!isResizing) return;
            const pageRect = salesPage.getBoundingClientRect();
            const maxPanelWidth = Math.floor(pageRect.width * MAX_PANEL_RATIO);
            const desiredWidth = pageRect.right - e.clientX;
            const clampedWidth = Math.min(Math.max(MIN_PANEL_WIDTH, desiredWidth), maxPanelWidth);
            salesPage.style.setProperty('--work-panel-width', `${clampedWidth}px`);
        };

        const onMouseUp = () => {
            if (!isResizing) return;
            isResizing = false;
            resizer.classList.remove('is-resizing');
            salesPage.classList.remove('is-resizing-panel');
            window.removeEventListener('mousemove', onMouseMove);
            window.removeEventListener('mouseup', onMouseUp);
        };

        resizer.addEventListener('mousedown', (e) => {
            e.preventDefault();
            isResizing = true;
            resizer.classList.add('is-resizing');
            salesPage.classList.add('is-resizing-panel');
            window.addEventListener('mousemove', onMouseMove);
            window.addEventListener('mouseup', onMouseUp);
        });
    }

    fetchChatSessions();
}

