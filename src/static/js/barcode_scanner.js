/**
 * src/static/js/barcode_scanner.js
 * --------------------------------
 * Mobile-friendly Barcode Scanner designed to work over plain HTTP without SSL/HTTPS.
 * Uses <input type="file" accept="image/*" capture="environment"> to directly activate
 * the phone camera via the operating system, bypassing browser getUserMedia HTTPS restrictions.
 * 
 * Decoding pipeline:
 * 1. Native window.BarcodeDetector (fast hardware acceleration on Chromium/Android)
 * 2. Pure-JS Canvas Scanline EAN-13/EAN-8/UPC Decoder (instant client-side fallback)
 * 3. Server-side API fallback: POST /api/v1/catalog/scan-barcode (multi-pass analysis)
 */

// EAN-13 Tables
const L_MAP = {
    '0001101': '0', '0011001': '1', '0010011': '2', '0111101': '3', '0100011': '4',
    '0110001': '5', '0101111': '6', '0111011': '7', '0110111': '8', '0001011': '9'
};
const G_MAP = {
    '0100111': '0', '0110011': '1', '0011011': '2', '0100001': '3', '0011101': '4',
    '0111001': '5', '0000101': '6', '0010001': '7', '0001001': '8', '0010111': '9'
};
const R_MAP = {
    '1110010': '0', '1100110': '1', '1101100': '2', '1000010': '3', '1011100': '4',
    '1001110': '5', '1010000': '6', '1000100': '7', '1001000': '8', '1110100': '9'
};
const P_MAP = {
    'LLLLLL': 0, 'LLGLGG': 1, 'LLGGLG': 2, 'LLGGGL': 3, 'LGLLGG': 4,
    'LGGLLG': 5, 'LGGGLL': 6, 'LGLGLG': 7, 'LGLGGL': 8, 'LGGLGL': 9
};

function normalizeToNModules(widths, targetSum) {
    const total = widths.reduce((a, b) => a + b, 0);
    if (total <= 0) return null;
    const mods = widths.map(w => Math.max(1, Math.round((w * targetSum) / total)));
    const diff = targetSum - mods.reduce((a, b) => a + b, 0);
    if (diff !== 0) {
        const errors = widths.map((w, i) => Math.abs((w * targetSum) / total - mods[i]));
        const sortedIndices = [0, 1, 2, 3].sort((a, b) => errors[b] - errors[a]);
        for (const idx of sortedIndices) {
            if (mods[idx] + diff >= 1) {
                mods[idx] += diff;
                break;
            }
        }
        if (mods.reduce((a, b) => a + b, 0) !== targetSum) return null;
    }
    return mods;
}

function decodeEAN13Runs(runs) {
    const N = runs.length;
    if (N < 59) return null;

    for (let s = 0; s <= N - 59; s++) {
        // Start guard: 101 (Black, White, Black)
        if (!runs[s][0] || runs[s + 1][0] || !runs[s + 2][0]) continue;
        const g1 = runs[s][1], g2 = runs[s + 1][1], g3 = runs[s + 2][1];
        const mEst = (g1 + g2 + g3) / 3.0;
        if (Math.max(g1, g2, g3) > mEst * 2.8 || Math.min(g1, g2, g3) < mEst * 0.35) continue;

        // Center guard at s + 27: 01010 (White, Black, White, Black, White)
        const c = s + 27;
        if (runs[c][0] || !runs[c + 1][0] || runs[c + 2][0] || !runs[c + 3][0] || runs[c + 4][0]) continue;

        // End guard at s + 56: 101 (Black, White, Black)
        const e = s + 56;
        if (!runs[e][0] || runs[e + 1][0] || !runs[e + 2][0]) continue;

        const leftDigits = [];
        const parities = [];
        let ok = true;
        for (let d = 0; d < 6; d++) {
            const w = [runs[s + 3 + d * 4][1], runs[s + 3 + d * 4 + 1][1], runs[s + 3 + d * 4 + 2][1], runs[s + 3 + d * 4 + 3][1]];
            const mods = normalizeToNModules(w, 7);
            if (!mods) { ok = false; break; }
            const bStr = '0'.repeat(mods[0]) + '1'.repeat(mods[1]) + '0'.repeat(mods[2]) + '1'.repeat(mods[3]);
            if (L_MAP[bStr]) {
                leftDigits.push(L_MAP[bStr]);
                parities.push('L');
            } else if (G_MAP[bStr]) {
                leftDigits.push(G_MAP[bStr]);
                parities.push('G');
            } else {
                ok = false;
                break;
            }
        }
        if (!ok) continue;

        const pStr = parities.join('');
        if (P_MAP[pStr] === undefined) continue;
        const firstDigit = String(P_MAP[pStr]);

        const rightDigits = [];
        for (let d = 0; d < 6; d++) {
            const w = [runs[c + 5 + d * 4][1], runs[c + 5 + d * 4 + 1][1], runs[c + 5 + d * 4 + 2][1], runs[c + 5 + d * 4 + 3][1]];
            const mods = normalizeToNModules(w, 7);
            if (!mods) { ok = false; break; }
            const bStr = '1'.repeat(mods[0]) + '0'.repeat(mods[1]) + '1'.repeat(mods[2]) + '0'.repeat(mods[3]);
            if (R_MAP[bStr]) {
                rightDigits.push(R_MAP[bStr]);
            } else {
                ok = false;
                break;
            }
        }
        if (!ok) continue;

        const candidate = firstDigit + leftDigits.join('') + rightDigits.join('');
        const dg = candidate.split('').map(Number);
        let sChk = 0;
        for (let i = 0; i < 12; i++) {
            sChk += dg[i] * (i % 2 === 0 ? 1 : 3);
        }
        const expected = (10 - (sChk % 10)) % 10;
        if (dg[12] === expected) {
            return candidate;
        }
    }
    return null;
}

function decodeEAN8Runs(runs) {
    const N = runs.length;
    if (N < 43) return null;

    for (let s = 0; s <= N - 43; s++) {
        if (!runs[s][0] || runs[s + 1][0] || !runs[s + 2][0]) continue;
        const c = s + 19;
        if (runs[c][0] || !runs[c + 1][0] || runs[c + 2][0] || !runs[c + 3][0] || runs[c + 4][0]) continue;
        const e = s + 40;
        if (!runs[e][0] || runs[e + 1][0] || !runs[e + 2][0]) continue;

        const leftDigits = [];
        let ok = true;
        for (let d = 0; d < 4; d++) {
            const w = [runs[s + 3 + d * 4][1], runs[s + 3 + d * 4 + 1][1], runs[s + 3 + d * 4 + 2][1], runs[s + 3 + d * 4 + 3][1]];
            const mods = normalizeToNModules(w, 7);
            if (!mods) { ok = false; break; }
            const bStr = '0'.repeat(mods[0]) + '1'.repeat(mods[1]) + '0'.repeat(mods[2]) + '1'.repeat(mods[3]);
            if (L_MAP[bStr]) {
                leftDigits.push(L_MAP[bStr]);
            } else {
                ok = false;
                break;
            }
        }
        if (!ok) continue;

        const rightDigits = [];
        for (let d = 0; d < 4; d++) {
            const w = [runs[c + 5 + d * 4][1], runs[c + 5 + d * 4 + 1][1], runs[c + 5 + d * 4 + 2][1], runs[c + 5 + d * 4 + 3][1]];
            const mods = normalizeToNModules(w, 7);
            if (!mods) { ok = false; break; }
            const bStr = '1'.repeat(mods[0]) + '0'.repeat(mods[1]) + '1'.repeat(mods[2]) + '0'.repeat(mods[3]);
            if (R_MAP[bStr]) {
                rightDigits.push(R_MAP[bStr]);
            } else {
                ok = false;
                break;
            }
        }
        if (!ok) continue;

        const candidate = leftDigits.join('') + rightDigits.join('');
        const dg = candidate.split('').map(Number);
        let sChk = 0;
        for (let i = 0; i < 7; i++) {
            sChk += dg[i] * (i % 2 === 0 ? 3 : 1);
        }
        const expected = (10 - (sChk % 10)) % 10;
        if (dg[7] === expected) {
            return candidate;
        }
    }
    return null;
}

function extractRunsFromRow(row, threshold) {
    const runs = [];
    let curr = row[0] < threshold;
    let cnt = 0;
    for (let i = 0; i < row.length; i++) {
        const isBlack = row[i] < threshold;
        if (isBlack === curr) {
            cnt++;
        } else {
            runs.push([curr, cnt]);
            curr = isBlack;
            cnt = 1;
        }
    }
    runs.push([curr, cnt]);
    return runs;
}

/**
 * Pure JavaScript Canvas barcode decoder
 */
export async function decodeBarcodeFromCanvas(canvas) {
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) return null;

    const w = canvas.width;
    const h = canvas.height;
    const imgData = ctx.getImageData(0, 0, w, h);
    const data = imgData.data;

    // Convert to grayscale grid or sample rows
    const getLuma = (x, y) => {
        const idx = (y * w + x) * 4;
        return (data[idx] * 299 + data[idx + 1] * 587 + data[idx + 2] * 114) / 1000;
    };

    const scanlines = [];

    // 1. Horizontal scanlines
    for (const yPct of [0.20, 0.28, 0.35, 0.42, 0.50, 0.58, 0.65, 0.72, 0.80]) {
        const y = Math.floor(h * yPct);
        const row = new Uint8Array(w);
        for (let x = 0; x < w; x++) {
            row[x] = getLuma(x, y);
        }
        scanlines.push(row);
        // Reversed
        const rev = new Uint8Array(w);
        for (let x = 0; x < w; x++) rev[x] = row[w - 1 - x];
        scanlines.push(rev);
    }

    // 2. Vertical scanlines (in case mobile was turned)
    for (const xPct of [0.25, 0.35, 0.50, 0.65, 0.75]) {
        const x = Math.floor(w * xPct);
        const col = new Uint8Array(h);
        for (let y = 0; y < h; y++) {
            col[y] = getLuma(x, y);
        }
        scanlines.push(col);
        const rev = new Uint8Array(h);
        for (let y = 0; y < h; y++) rev[y] = col[h - 1 - y];
        scanlines.push(rev);
    }

    for (const row of scanlines) {
        let minV = 255, maxV = 0;
        for (let i = 0; i < row.length; i++) {
            if (row[i] < minV) minV = row[i];
            if (row[i] > maxV) maxV = row[i];
        }
        if (maxV - minV < 30) continue;

        const thresholds = [
            Math.floor((minV + maxV) / 2),
            Math.floor(minV + (maxV - minV) * 0.4),
            Math.floor(minV + (maxV - minV) * 0.6),
        ];

        for (const th of thresholds) {
            const runs = extractRunsFromRow(row, th);

            const ean13 = decodeEAN13Runs(runs);
            if (ean13) return { barcode: ean13, format: 'EAN-13' };

            const ean8 = decodeEAN8Runs(runs);
            if (ean8) return { barcode: ean8, format: 'EAN-8' };
        }
    }

    return null;
}

/**
 * Decode a Barcode from an uploaded File or Blob
 */
export async function decodeBarcodeFromFile(file) {
    // 1. Try Native BarcodeDetector if available (hardware accelerated)
    if (typeof window !== 'undefined' && 'BarcodeDetector' in window) {
        try {
            const formats = await window.BarcodeDetector.getSupportedFormats();
            if (formats && formats.includes('ean_13')) {
                const detector = new window.BarcodeDetector({
                    formats: ['ean_13', 'ean_8', 'code_128', 'upc_a', 'upc_e', 'code_39']
                });
                const bitmap = await createImageBitmap(file);
                const results = await detector.detect(bitmap);
                if (results && results.length > 0 && results[0].rawValue) {
                    return { barcode: results[0].rawValue, format: results[0].format || 'EAN-13' };
                }
            }
        } catch (e) {
            console.warn('BarcodeDetector error, proceeding to canvas fallback:', e);
        }
    }

    // 2. Pure JavaScript Canvas Scanline Decoder
    try {
        const img = await new Promise((resolve, reject) => {
            const i = new Image();
            i.onload = () => resolve(i);
            i.onerror = reject;
            i.src = URL.createObjectURL(file);
        });

        const canvas = document.createElement('canvas');
        let w = img.naturalWidth || img.width;
        let h = img.naturalHeight || img.height;
        const maxDim = 1200;
        if (Math.max(w, h) > maxDim) {
            const scale = maxDim / Math.max(w, h);
            w = Math.floor(w * scale);
            h = Math.floor(h * scale);
        }
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0, w, h);
        URL.revokeObjectURL(img.src);

        const canvasResult = await decodeBarcodeFromCanvas(canvas);
        if (canvasResult) {
            return canvasResult;
        }
    } catch (e) {
        console.warn('Canvas barcode decoding error:', e);
    }

    // 3. Server-side API fallback
    try {
        const res = await fetch('/api/v1/catalog/scan-barcode', {
            method: 'POST',
            body: file,
            headers: {
                'Content-Type': file.type || 'application/octet-stream'
            }
        });
        if (res.ok) {
            const data = await res.json();
            if (data.status === 'success' && data.barcode) {
                return { barcode: data.barcode, format: data.format || 'EAN-13' };
            }
        }
    } catch (e) {
        console.warn('Server barcode scan error:', e);
    }

    return null;
}

/**
 * Initializes a camera barcode trigger button with an HTML file input using capture="environment".
 * This allows camera scanning over plain HTTP without needing an SSL certificate!
 */
export function setupBarcodeScanner({
    cameraInputId = 'warehouse-barcode-camera-input',
    triggerBtnIds = ['warehouse-barcode-scan-btn', 'warehouse-search-scan-btn'],
    onSuccess,
    onError,
    showToastFn
}) {
    let cameraInput = document.getElementById(cameraInputId);
    if (!cameraInput) {
        cameraInput = document.createElement('input');
        cameraInput.type = 'file';
        cameraInput.id = cameraInputId;
        cameraInput.accept = 'image/*';
        cameraInput.setAttribute('capture', 'environment');
        cameraInput.style.display = 'none';
        document.body.appendChild(cameraInput);
    }

    // Trigger button listeners
    triggerBtnIds.forEach(id => {
        const btn = document.getElementById(id);
        if (btn) {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                cameraInput.value = ''; // Reset input to trigger change even for duplicate photos
                cameraInput.click();
            });
        }
    });

    // Handle captured photo
    cameraInput.addEventListener('change', async (e) => {
        const files = e.target.files;
        if (!files || files.length === 0) return;

        const file = files[0];
        if (showToastFn) showToastFn('📷 Elaborazione scansione barcode...', 'info');

        try {
            const result = await decodeBarcodeFromFile(file);
            if (result && result.barcode) {
                if (showToastFn) showToastFn(`✅ Barcode rilevato: ${result.barcode}`, 'success');
                if (onSuccess) onSuccess(result.barcode, result.format);
            } else {
                const msg = 'Nessun codice a barre rilevato. Inquadra il barcode da vicino e con buona luce.';
                if (showToastFn) showToastFn(`⚠️ ${msg}`, 'error');
                if (onError) onError(msg);
            }
        } catch (err) {
            console.error('Scan failed:', err);
            const msg = 'Errore durante la scansione del barcode.';
            if (showToastFn) showToastFn(`❌ ${msg}`, 'error');
            if (onError) onError(msg);
        }
    });
}
