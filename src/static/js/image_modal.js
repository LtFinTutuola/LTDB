/**
 * src/static/js/image_modal.js
 * Unified full-screen image preview / lightbox modal across LTDB:
 * - Warehouse catalog table, mobile panel, and article detail
 * - Staging area (blueprint cards, photo carousel, and mobile drawer)
 * - Oracle manual confirmation / HITL reconciliation cards
 * - Blueprint revision
 * - Sales page
 */

export function openImageModal(imageUrl, altText = '') {
    if (!imageUrl) return;

    let overlay = document.getElementById('image-zoom-modal');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'image-zoom-modal';
        overlay.className = 'image-modal-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-label', 'Visualizzazione foto a tutto schermo');

        overlay.innerHTML = `
            <button type="button" class="btn-close-modal" title="Chiudi (Esc)" aria-label="Chiudi foto">
                ✕
            </button>
            <div class="image-modal-content">
                <img id="image-zoom-img" src="" alt="Anteprima foto" onclick="event.stopPropagation()">
            </div>
        `;
        document.body.appendChild(overlay);

        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.closest('.btn-close-modal')) {
                closeImageModal();
            }
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && overlay.classList.contains('open')) {
                closeImageModal();
            }
        });
    }

    const imgEl = overlay.querySelector('#image-zoom-img');
    if (imgEl) {
        imgEl.src = imageUrl;
        imgEl.alt = altText || 'Foto articolo';
    }

    // Force display flex and add .open for smooth fade-in
    overlay.style.display = 'flex';
    void overlay.offsetWidth;
    overlay.classList.add('open');
}

export function closeImageModal() {
    const overlay = document.getElementById('image-zoom-modal');
    if (!overlay) return;
    overlay.classList.remove('open');
    setTimeout(() => {
        if (!overlay.classList.contains('open')) {
            overlay.style.display = 'none';
        }
    }, 200);
}

// Global aliases to ensure backwards compatibility across all inline handlers and modules
window._openImageModal = openImageModal;
window.openImageModal = openImageModal;
window.showImageModal = openImageModal;
window.showImageFullscreen = openImageModal;

