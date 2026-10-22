/**
 * src/static/js/photo_upload_helper.js
 * ------------------------------------
 * Responsive helper for capturing or selecting photos with client-side compression.
 * - Desktop (> 1024px): Directly opens OS file picker.
 * - Mobile & Tablet (<= 1024px): Presents a native-feeling dialog to choose between
 *   "Scatta Foto" (camera with capture="environment") and "Scegli da File / Galleria".
 */

/**
 * Compresses an image File/Blob using an HTML canvas, keeping aspect ratio.
 * Maximum dimension: 1600px, JPEG quality: 0.85.
 * @param {File|Blob} file
 * @param {number} maxDim
 * @param {number} quality
 * @returns {Promise<Blob>}
 */
export async function compressImage(file, maxDim = 1600, quality = 0.85) {
    if (!file || !file.type.startsWith('image/')) {
        return file;
    }

    // SVG or animated GIF can bypass canvas
    if (file.type === 'image/svg+xml' || file.type === 'image/gif') {
        return file;
    }

    return new Promise((resolve) => {
        const img = new Image();
        const url = URL.createObjectURL(file);

        img.onload = () => {
            URL.revokeObjectURL(url);
            let w = img.naturalWidth || img.width;
            let h = img.naturalHeight || img.height;

            if (w <= maxDim && h <= maxDim && file.size < 500 * 1024) {
                // Already small enough
                return resolve(file);
            }

            if (Math.max(w, h) > maxDim) {
                const scale = maxDim / Math.max(w, h);
                w = Math.round(w * scale);
                h = Math.round(h * scale);
            }

            const canvas = document.createElement('canvas');
            canvas.width = w;
            canvas.height = h;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, w, h);

            canvas.toBlob(
                (blob) => {
                    if (blob && blob.size < file.size) {
                        resolve(blob);
                    } else {
                        resolve(file);
                    }
                },
                'image/jpeg',
                quality
            );
        };

        img.onerror = () => {
            URL.revokeObjectURL(url);
            resolve(file);
        };

        img.src = url;
    });
}

/**
 * Creates hidden file inputs for desktop/gallery and camera capture if not existing.
 */
function _getOrCreateInputs() {
    let fileInput = document.getElementById('global-photo-file-input');
    if (!fileInput) {
        fileInput = document.createElement('input');
        fileInput.type = 'file';
        fileInput.id = 'global-photo-file-input';
        fileInput.accept = 'image/*';
        fileInput.style.display = 'none';
        document.body.appendChild(fileInput);
    }

    let cameraInput = document.getElementById('global-photo-camera-input');
    if (!cameraInput) {
        cameraInput = document.createElement('input');
        cameraInput.type = 'file';
        cameraInput.id = 'global-photo-camera-input';
        cameraInput.accept = 'image/*';
        cameraInput.setAttribute('capture', 'environment');
        cameraInput.style.display = 'none';
        document.body.appendChild(cameraInput);
    }

    return { fileInput, cameraInput };
}

/**
 * Shows the modal sheet for choosing photo source on mobile/tablet.
 */
function _showPhotoSourceModal(onCameraClick, onGalleryClick) {
    let modal = document.getElementById('modal-photo-source-choice');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'modal-photo-source-choice';
        modal.className = 'modal-backdrop';
        modal.style.zIndex = '99999';
        modal.innerHTML = `
            <div class="modal-card" style="max-width: 360px; text-align: center; padding: 20px;">
                <h3 style="font-size: 16px; font-weight: 600; margin-bottom: 8px; color: var(--text-primary); display: flex; align-items: center; justify-content: center; gap: 8px;">
                    <span class="material-symbols-rounded" style="color: var(--accent);">add_a_photo</span>
                    Carica Foto Articolo
                </h3>
                <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: 20px;">
                    Scegli come desideri acquisire la foto:
                </p>
                <div style="display: flex; flex-direction: column; gap: 10px;">
                    <button type="button" id="btn-choice-camera" class="btn-primary" style="padding: 12px; display: flex; align-items: center; justify-content: center; gap: 10px; font-size: 14px; font-weight: 600;">
                        <span class="material-symbols-rounded">photo_camera</span>
                        Scatta Foto
                    </button>
                    <button type="button" id="btn-choice-gallery" class="btn-secondary" style="padding: 12px; display: flex; align-items: center; justify-content: center; gap: 10px; font-size: 14px; font-weight: 600;">
                        <span class="material-symbols-rounded">image</span>
                        Scegli da File / Galleria
                    </button>
                    <button type="button" id="btn-choice-cancel" class="btn-secondary" style="padding: 10px; font-size: 13px; margin-top: 4px; border: none; background: transparent; color: var(--text-muted);">
                        Annulla
                    </button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    const btnCamera = modal.querySelector('#btn-choice-camera');
    const btnGallery = modal.querySelector('#btn-choice-gallery');
    const btnCancel = modal.querySelector('#btn-choice-cancel');

    const closeModal = () => {
        modal.classList.remove('active');
    };

    btnCamera.onclick = () => {
        closeModal();
        if (onCameraClick) onCameraClick();
    };

    btnGallery.onclick = () => {
        closeModal();
        if (onGalleryClick) onGalleryClick();
    };

    btnCancel.onclick = closeModal;
    modal.onclick = (e) => {
        if (e.target === modal) closeModal();
    };

    modal.classList.add('active');
}

/**
 * Prompts user for a photo based on viewport layout:
 * - Desktop (> 1024px): Directly triggers file input.
 * - Mobile & Tablet (<= 1024px): Shows choice modal (Camera vs File/Gallery).
 *
 * @param {Object} options
 * @param {Function} options.onSuccess - Callback receiving (compressedBlob, originalFile)
 * @param {Function} [options.onError] - Optional error callback
 */
export function requestPhotoUpload({ onSuccess, onError }) {
    const { fileInput, cameraInput } = _getOrCreateInputs();

    const handleFileChange = async (e) => {
        const files = e.target.files;
        if (!files || files.length === 0) return;
        const file = files[0];
        try {
            const compressed = await compressImage(file);
            if (onSuccess) onSuccess(compressed, file);
        } catch (err) {
            console.error('Photo compression error:', err);
            if (onError) onError(err);
        }
    };

    // Rebind change listeners
    fileInput.onchange = handleFileChange;
    cameraInput.onchange = handleFileChange;

    const isDesktop = window.innerWidth > 1024;

    if (isDesktop) {
        fileInput.value = '';
        fileInput.click();
    } else {
        _showPhotoSourceModal(
            () => {
                cameraInput.value = '';
                cameraInput.click();
            },
            () => {
                fileInput.value = '';
                fileInput.click();
            }
        );
    }
}
