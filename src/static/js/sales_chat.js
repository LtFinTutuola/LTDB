export function initSalesChat(dom) {
    if (!dom.chatInput || !dom.btnChatSend || !dom.chatMessages) {
        console.warn("Elementi chat non trovati");
        return;
    }

    const sendMessage = async () => {
        const message = dom.chatInput.value.trim();
        if (!message) return;

        // Add user message
        appendMessage(message, 'user');
        dom.chatInput.value = '';

        // Add loading state
        const loadingId = 'loading-' + Date.now();
        appendMessage('...', 'gemini', loadingId);

        try {
            const contextDate = document.getElementById('date-picker')?.value || new Date().toISOString().split('T')[0];
            const payload = {
                message: message,
                context_date: contextDate
            };

            const res = await fetch('/api/v1/sales/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            }).then(r => r.json());

            const loadingEl = document.getElementById(loadingId);
            if (loadingEl) {
                loadingEl.textContent = res.reply || 'Errore nella risposta.';
            }

        } catch (err) {
            console.error('Chat error', err);
            const loadingEl = document.getElementById(loadingId);
            if (loadingEl) {
                loadingEl.textContent = 'Si è verificato un errore di connessione con Gemini.';
            }
        }
    };

    dom.btnChatSend.addEventListener('click', sendMessage);

    dom.chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    function appendMessage(text, sender, id = null) {
        const bubble = document.createElement('div');
        bubble.className = `chat-bubble chat-bubble--${sender}`;
        bubble.textContent = text;
        if (id) {
            bubble.id = id;
        }
        dom.chatMessages.appendChild(bubble);
        dom.chatMessages.scrollTop = dom.chatMessages.scrollHeight;
    }
}
