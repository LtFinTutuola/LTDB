"""
Normalizzatore deterministico dei vendor code (nessuna dipendenza da LLM).

Implementa:
- Step 0: Sterilizzazione dei separatori contigui in '@' e calcolo dello scheletro alfanumerico puro
- Slicing Posizionale e Segment-Aware a 3 rami (DELIMITED, COMPACT, AGNOSTIC)
- Profilazione morfologica (CodeEmbedding)
- Motore di similarità bimodale (S_morph + S_trigram Sørensen-Dice + Damerau-Levenshtein)
- Guardrail Anti-Typo interattivo (Tipi A, B, C)
"""
from __future__ import annotations

import re
from itertools import combinations
from typing import Any, Optional

MASK_CHAR = "#"
MIN_PROTECTED_CHARS = 2
_MASKED_MODES = ("COLOR_MASKED", "COLOR_MASKED_COMPACT", "COLOR_MASKED_DELIMITED", "DELIMITED")

# Status di induzione
OK = "OK"
NO_VARIANCE = "NO_VARIANCE"
DISCONTINUOUS_ANOMALY = "DISCONTINUOUS_ANOMALY"
INSUFFICIENT_ANCHOR = "INSUFFICIENT_ANCHOR"
SIGNATURE_MISMATCH = "SIGNATURE_MISMATCH"
NEGATIVE_CONSTRAINT_VIOLATION = "NEGATIVE_CONSTRAINT_VIOLATION"


# ---------------------------------------------------------------------------
# Step 0: Pulizia, Sterilizzazione Separatori e Scheletro Alfanumerico
# ---------------------------------------------------------------------------

def clean_code(raw: Optional[str]) -> str:
    """Pulizia di base: strip e maiuscolo."""
    return (raw or "").strip().upper()


def sterilize_separators(raw: Optional[str]) -> str:
    """
    Step 0: Converte qualsiasi sequenza contigua di separatori
    (-, --, spazio, /, //, \\, \\\\, |, ., _, __, *) in un singolo '@' canonico.
    Rimuove eventuali '@' iniziali e finali.
    """
    if not raw:
        return ""
    code = raw.strip().upper()
    code = re.sub(r"[\s\-/\\|._*]+", "@", code)
    return code.strip("@")


def compute_skeleton_code(code: Optional[str]) -> str:
    """
    Ritorna lo scheletro alfanumerico puro del codice, depurato da '@', '#'
    e da qualsiasi separatore residuo.
    """
    if not code:
        return ""
    return re.sub(r"[@#\s\-/\\|._*]+", "", code.strip().upper())


# ---------------------------------------------------------------------------
# Profilazione Morfologica (CodeEmbedding)
# ---------------------------------------------------------------------------

def compute_root_blocks_pattern(root_str: str) -> str:
    """
    Converte una stringa radice in pattern di blocchi preservando '@'.
    Es. '140879@1IDU' -> '[NUM:6]@[NUM:1][ALPHA:3]'
        'P10FZP61' -> '[ALPHA:1][NUM:2][ALPHA:4][NUM:1]'
        '04704@081' -> '[NUM:5]@[NUM:3]'
    """
    if not root_str:
        return ""
    tokens = []
    pattern = re.compile(r"([A-Z]+|[0-9]+|@|[^A-Z0-9@]+)")
    for match in pattern.finditer(root_str.upper()):
        part = match.group(0)
        if part == "@":
            tokens.append("@")
        elif part.isalpha():
            tokens.append(f"[ALPHA:{len(part)}]")
        elif part.isdigit():
            tokens.append(f"[NUM:{len(part)}]")
        else:
            tokens.append(part)
    return "".join(tokens)


def compute_root_prefix_anchor(root_str: str) -> str:
    """
    Estrae l'ancora iniziale del codice/radice (fino a max 4 caratteri).
    Es. 'P10FZP61' -> 'P10', 'AW5646WOL01' -> 'AW', '1408791IDU' -> '1408'.
    """
    if not root_str:
        return ""
    clean = root_str.replace("@", "")
    if len(clean) <= 4:
        return clean
    m = re.match(r"^([A-Z]+[0-9]*|[0-9]+[A-Z]*)", clean)
    if m and 2 <= len(m.group(1)) <= 4:
        return m.group(1)
    return clean[:4]


def extract_root_and_color(
    raw_code: str,
    heuristic_mode: str = "COLOR_MASKED_COMPACT",
    color_segment_index: Optional[int] = None,
    root_length: Optional[int] = None,
    color_length: Optional[int] = None,
    rules: Optional[list[dict[str, Any]]] = None,
) -> tuple[str, str, str]:
    """
    Estrae (sterilized_code, root_str, color_segment) secondo 3 rami:
    - COLOR_AGNOSTIC: root_str = sterilized_code, color = ""
    - COLOR_MASKED_COMPACT: ancoraggio per root_length (o color_length)
      con fallback Separator-Agnostic se c'è un '@' al confine
    - COLOR_MASKED_DELIMITED: split su '@', guardia len(segments) >= 2,
      estrazione di segments[color_segment_index] e ricomposizione di root_str con '@'
    """
    sterilized = sterilize_separators(raw_code)
    if not sterilized:
        return ("", "", "")

    # Deduzione automatica da rules se fornito
    if rules:
        for r in rules:
            if r.get("type") == "DELIMITED":
                heuristic_mode = "COLOR_MASKED_DELIMITED"
                if color_segment_index is None:
                    color_segment_index = r.get("color_segment_index")
                break
            elif r.get("type") == "FIXED" and root_length is None:
                m_pos = r.get("masked_positions", [])
                if m_pos:
                    root_length = m_pos[0]
            elif r.get("type") == "ELASTIC" and root_length is None:
                pfx = r.get("prefix", "")
                root_length = len(pfx.replace("@", ""))

    mode = (heuristic_mode or "").upper()
    if mode == "COLOR_AGNOSTIC":
        return (sterilized, sterilized, "")

    if mode in ("COLOR_MASKED_DELIMITED", "DELIMITED") or ("@" in sterilized and mode == "COLOR_MASKED" and color_segment_index is not None):
        segments = sterilized.split("@")
        c_idx = -1 if color_segment_index is None else color_segment_index
        # Rettifica 2.A: guardia contro IndexError e segmenti insufficienti
        if len(segments) < 2:
            return (sterilized, sterilized, "")
        if c_idx >= len(segments) or c_idx < -len(segments):
            return (sterilized, sterilized, "")
        norm_idx = c_idx if c_idx >= 0 else len(segments) + c_idx
        color_segment = segments[norm_idx]
        root_segments = [s for i, s in enumerate(segments) if i != norm_idx]
        root_str = "@".join(root_segments)
        return (sterilized, root_str, color_segment)

    # COLOR_MASKED_COMPACT o fallback
    clean = compute_skeleton_code(sterilized)
    if root_length is not None and root_length > 0:
        # Fallback Separator-Agnostic: es. 'P10FZP61@B01' con root_length=8
        if len(sterilized) > root_length and sterilized[root_length] == "@":
            root_str = sterilized[:root_length]
            color_segment = sterilized[root_length + 1:]
            return (sterilized, root_str, color_segment)
        if len(clean) >= root_length:
            root_str = clean[:root_length]
            color_segment = clean[root_length:]
            return (sterilized, root_str, color_segment)
    elif color_length is not None and color_length > 0:
        if len(clean) > color_length:
            root_str = clean[:-color_length]
            color_segment = clean[-color_length:]
            return (sterilized, root_str, color_segment)

    return (sterilized, clean, "")


# ---------------------------------------------------------------------------
# Metriche di Distanza e Similarità Bimodale
# ---------------------------------------------------------------------------

def damerau_levenshtein_distance(s1: str, s2: str) -> int:
    """Distanza di Damerau-Levenshtein (supporta trasposizione adiacente)."""
    d: dict[tuple[int, int], int] = {}
    len1, len2 = len(s1), len(s2)
    for i in range(-1, len1 + 1):
        d[(i, -1)] = i + 1
    for j in range(-1, len2 + 1):
        d[(-1, j)] = j + 1

    for i in range(len1):
        for j in range(len2):
            cost = 0 if s1[i] == s2[j] else 1
            d[(i, j)] = min(
                d[(i - 1, j)] + 1,        # deletion
                d[(i, j - 1)] + 1,        # insertion
                d[(i - 1, j - 1)] + cost  # substitution
            )
            if i > 0 and j > 0 and s1[i] == s2[j - 1] and s1[i - 1] == s2[j]:
                d[(i, j)] = min(d[(i, j)], d[(i - 2, j - 2)] + 1)  # transposition

    return d[(len1 - 1, len2 - 1)]


def levenshtein_distance(s1: str, s2: str) -> int:
    """Distanza di Levenshtein standard."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev[j + 1] + 1
            deletions = curr[j] + 1
            substitutions = prev[j] + (c1 != c2)
            curr.append(min(insertions, deletions, substitutions))
        prev = curr
    return prev[-1]


def trigram_dice_similarity(s1: str, s2: str) -> float:
    """Coefficiente di Sørensen-Dice su trigrammi con padding '$$...$$'."""
    if not s1 or not s2:
        return 0.0
    padded1 = f"$${s1.upper()}$$"
    padded2 = f"$${s2.upper()}$$"
    tri1 = [padded1[i:i+3] for i in range(len(padded1) - 2)]
    tri2 = [padded2[i:i+3] for i in range(len(padded2) - 2)]
    set1, set2 = set(tri1), set(tri2)
    intersection = len(set1 & set2)
    total = len(set1) + len(set2)
    if total == 0:
        return 1.0
    return (2.0 * intersection) / float(total)


def score_embedding_compatibility(
    sterilized_code: str,
    embedding: Any,
    heuristic: Any = None,
) -> tuple[float, Optional[str], Optional[str]]:
    """
    Calcola il punteggio di compatibilità morfologica S_morph in [0, 1].
    Ritorna (score, extracted_root, masked_code).
    """
    mode = getattr(heuristic, "heuristic_mode", "COLOR_MASKED_COMPACT") if heuristic else "COLOR_MASKED_COMPACT"
    c_idx = getattr(heuristic, "color_segment_index", None) if heuristic else None

    # Estrazione radice e colore secondo la configurazione dell'embedding
    _, root_str, color_segment = extract_root_and_color(
        sterilized_code,
        heuristic_mode=mode,
        color_segment_index=c_idx,
        root_length=getattr(embedding, "root_length", None),
    )

    clean_code_val = compute_skeleton_code(sterilized_code)
    clean_root = compute_skeleton_code(root_str)
    root_len = len(clean_root)
    color_len = len(compute_skeleton_code(color_segment))
    total_len = len(clean_code_val)

    score = 0.0

    # 1. Separator count check (con tolleranza separator-agnostic)
    code_segs = sterilized_code.split("@")
    code_sep_count = len(code_segs) - 1
    exp_sep_count = getattr(embedding, "separator_count", 0)

    if code_sep_count == exp_sep_count:
        score += 0.25
    elif mode == "COLOR_MASKED_COMPACT" and exp_sep_count == 0 and code_sep_count == 1:
        # Fallback Separator-Agnostic accettato
        score += 0.20

    # 2. Root length check
    exp_root_len = getattr(embedding, "root_length", 0)
    if exp_root_len and root_len == exp_root_len:
        score += 0.35
    elif exp_root_len and abs(root_len - exp_root_len) == 1:
        score += 0.15

    # 3. Root blocks pattern check
    curr_pattern = compute_root_blocks_pattern(root_str)
    exp_pattern = getattr(embedding, "root_blocks_pattern", "")
    if exp_pattern and curr_pattern == exp_pattern:
        score += 0.25
    elif exp_pattern and compute_root_prefix_anchor(root_str) == getattr(embedding, "root_prefix_anchor", ""):
        score += 0.15

    # 4. Color length range check
    c_min = getattr(embedding, "color_length_min", 1)
    c_max = getattr(embedding, "color_length_max", 5)
    if color_len > 0 and (c_min <= color_len <= c_max):
        score += 0.15
    elif color_len > 0 and (abs(color_len - c_min) <= 1 or abs(color_len - c_max) <= 1):
        score += 0.05

    score = min(1.0, score)

    # Costruzione codice mascherato
    masked_code = None
    if score >= 0.70 and color_len > 0:
        if mode in ("COLOR_MASKED_DELIMITED", "DELIMITED"):
            segs = sterilized_code.split("@")
            norm_idx = -1 if c_idx is None else (c_idx if c_idx >= 0 else len(segs) + c_idx)
            if 0 <= norm_idx < len(segs):
                segs[norm_idx] = MASK_CHAR
                masked_code = "@".join(segs)
        else:
            if "@" in sterilized_code and sterilized_code.startswith(root_str + "@"):
                masked_code = f"{root_str}@{MASK_CHAR * color_len}"
            else:
                masked_code = f"{root_str}{MASK_CHAR * color_len}"

    return (score, root_str, masked_code)


# ---------------------------------------------------------------------------
# Guardrail Anti-Typo Interattivo (Stadio 3)
# ---------------------------------------------------------------------------

def color_segment_length(sterilized: str, embedding: Any, heuristic: Any = None) -> int:
    """Lunghezza (alfanumerica) del segmento colore estratto secondo embedding/euristica."""
    mode = getattr(heuristic, "heuristic_mode", None) if heuristic else None
    mode = mode if mode in _MASKED_MODES else "COLOR_MASKED_COMPACT"
    c_idx = getattr(heuristic, "color_segment_index", None) if heuristic else None
    _, _, col = extract_root_and_color(
        sterilized,
        heuristic_mode=mode,
        color_segment_index=c_idx,
        root_length=getattr(embedding, "root_length", None),
    )
    return len(compute_skeleton_code(col))


def color_length_in_range(color_len: int, embeddings: list[Any]) -> bool:
    """True se la lunghezza colore rientra nel range di almeno un embedding (o se non c'è dato)."""
    if not embeddings or color_len <= 0:
        return True
    return any(e.color_length_min <= color_len <= e.color_length_max for e in embeddings)


def _typo_pair(candidate: str, ref: str) -> Optional[tuple[str, str]]:
    """Classifica la distanza tra due scheletri. Ritorna (anomaly_type, dettaglio) o None."""
    if not candidate or not ref or candidate == ref:
        return None
    len_diff = abs(len(candidate) - len(ref))
    if len_diff == 1:
        if levenshtein_distance(candidate, ref) == 1:
            return ("TYPE_A_LENGTH_VARIANCE", "variazione di lunghezza (+/- 1 carattere)")
        return None
    if len_diff == 0:
        d_dam = damerau_levenshtein_distance(candidate, ref)
        d_lev = levenshtein_distance(candidate, ref)
        if d_dam == 1 and d_lev == 2:
            return ("TYPE_C_TRANSPOSITION", "inversione di caratteri adiacenti")
        if d_lev == 1:
            idx = [i for i in range(len(candidate)) if candidate[i] != ref[i]]
            if len(idx) == 1:
                c1, c2 = candidate[idx[0]], ref[idx[0]]
                if (c1.isalpha() and c2.isdigit()) or (c1.isdigit() and c2.isalpha()):
                    return ("TYPE_B_PATTERN_VIOLATION", f"sostituzione carattere '{c1}' <-> '{c2}' (lettera/cifra)")
    return None


def detect_typo_anomaly(
    raw_code: str,
    embeddings: list[Any],
    blueprints: list[Any],
) -> Optional[dict[str, Any]]:
    """
    Verifica se un vendor code non matchato presenta una delle 3 anomalie typo:
    - Tipo A: DeltaLength = +/- 1 e Levenshtein = 1 (sul codice completo, sulla radice o sulla
      lunghezza del colore rispetto al range degli embedding)
    - Tipo B: DeltaLength = 0 e sostituzione lettera <-> cifra a edit distance 1
    - Tipo C: Trasposizione di caratteri adiacenti (Damerau = 1, Levenshtein = 2)

    I blueprint mascherati ('#') hanno uno scheletro che contiene SOLO la radice: il codice
    in ingresso viene quindi confrontato con la finestra di radice corrispondente, mentre i
    blueprint non mascherati e i supplier_code delle varianti vengono confrontati per intero.
    """
    clean = compute_skeleton_code(sterilize_separators(raw_code))
    if len(clean) < 3:
        return None
    embeddings = list(embeddings or [])

    # Riferimenti: (scheletro, "ROOT"|"FULL", blueprint)
    refs: list[tuple[str, str, Any]] = []
    for bp in blueprints or []:
        bp_norm = getattr(bp, "normalized_vendor_code", "") or ""
        sk = getattr(bp, "skeleton_vendor_code", None) or compute_skeleton_code(bp_norm)
        if sk:
            refs.append((sk, "ROOT" if MASK_CHAR in bp_norm else "FULL", bp))
        for art in (getattr(bp, "articles", None) or []):
            ask = compute_skeleton_code(getattr(art, "supplier_code", None))
            if ask:
                refs.append((ask, "FULL", bp))

    def _payload(bp: Any, sk: str, anomaly: str, detail: str) -> dict[str, Any]:
        name = getattr(bp, "article_name", "Unknown")
        return {
            "typo_warning": True,
            "suspect_code": raw_code,
            "suggested_match": (getattr(bp, "normalized_vendor_code", "") or sk),
            "blueprint_id": bp.id,
            "blueprint_name": name,
            "anomaly_type": anomaly,
            "reason": f"Possibile {detail} rispetto al modello '{name}' ({sk}).",
        }

    # 1. Codice già noto (nessun refuso)
    for sk, kind, _bp in refs:
        if kind == "FULL" and sk == clean:
            return None
        if kind == "ROOT" and len(clean) >= len(sk) and clean[:len(sk)] == sk \
                and color_length_in_range(len(clean) - len(sk), embeddings):
            return None

    # 2. Tipo A sul colore: radice nota ma lunghezza colore fuori range di esattamente 1
    if embeddings:
        for sk, kind, bp in refs:
            if kind == "ROOT" and len(clean) > len(sk) and clean[:len(sk)] == sk:
                col_len = len(clean) - len(sk)
                nearest = min(min(abs(col_len - e.color_length_min), abs(col_len - e.color_length_max)) for e in embeddings)
                if nearest == 1:
                    return _payload(bp, sk, "TYPE_A_LENGTH_VARIANCE", "variazione di lunghezza del colore (+/- 1 carattere)")

    # 3. Confronto con radici/codici completi noti
    for sk, kind, bp in refs:
        if kind == "FULL":
            candidates = [clean]
        else:
            candidates = []
            if len(clean) >= len(sk):
                candidates.append(clean[:len(sk)])
            for delta in (-1, 1):
                w = len(sk) + delta
                if w >= 3 and len(clean) > w:
                    candidates.append(clean[:w])
        for cand in candidates:
            hit = _typo_pair(cand, sk)
            if hit:
                return _payload(bp, sk, hit[0], hit[1])

    # 4. Ancore dei code_embeddings (prefisso serie), solo se non è l'ancora di un'altra serie nota
    anchors = {getattr(e, "root_prefix_anchor", "") for e in embeddings}
    for emb in embeddings:
        anchor = getattr(emb, "root_prefix_anchor", "")
        if not anchor or not getattr(emb, "root_length", 0) or len(clean) < len(anchor):
            continue
        clean_anchor = clean[:len(anchor)]
        if clean_anchor in anchors:
            continue
        hit = _typo_pair(clean_anchor, anchor)
        if hit and hit[0] in ("TYPE_B_PATTERN_VIOLATION", "TYPE_C_TRANSPOSITION"):
            return {
                "typo_warning": True,
                "suspect_code": raw_code,
                "suggested_match": anchor,
                "blueprint_id": None,
                "blueprint_name": None,
                "anomaly_type": hit[0],
                "reason": f"Possibile refuso nel prefisso serie '{clean_anchor}' (atteso '{anchor}').",
            }

    return None


# ---------------------------------------------------------------------------
# Regole Posizionali Legacy e Compatibilità
# ---------------------------------------------------------------------------

def compute_invariant_signature(code: str) -> str:
    """Firma separatori: ogni carattere alfanumerico diventa 'x', i separatori restano."""
    return "".join("x" if ch.isalnum() else ch for ch in clean_code(code))


def apply_rule(raw: str, rule: dict[str, Any]) -> Optional[str]:
    """Applica una singola regola al codice. Ritorna None se non è applicabile."""
    if not raw or not rule:
        return None
    rtype = rule.get("type")
    if rtype == "DELIMITED":
        sterilized = sterilize_separators(raw)
        segs = sterilized.split("@")
        num_segs = rule.get("num_segments")
        c_idx = rule.get("color_segment_index")
        if num_segs is not None and len(segs) != num_segs:
            return None
        if len(segs) < 2 or c_idx is None:
            return None
        if not (-len(segs) <= c_idx < len(segs)):
            return None
        norm_idx = c_idx if c_idx >= 0 else len(segs) + c_idx
        invariant_chars = sum(len(s) for i, s in enumerate(segs) if i != norm_idx)
        if invariant_chars < MIN_PROTECTED_CHARS:
            return None
        out_segs = list(segs)
        out_segs[norm_idx] = MASK_CHAR
        return "@".join(out_segs)

    code = clean_code(raw)
    if not code:
        return None
    if rtype == "FIXED":
        if len(code) != rule.get("length") or compute_invariant_signature(code) != rule.get("signature"):
            return None
        masked = set(rule.get("masked_positions", []))
        return "".join(MASK_CHAR if i in masked else ch for i, ch in enumerate(code))
    if rtype == "ELASTIC":
        prefix, suffix = rule.get("prefix", ""), rule.get("suffix", "")
        if len(code) < len(prefix) + len(suffix) + 1:
            return None
        # Supporta sia il separatore originale che '@'
        code_compat = code.replace("@", "-")
        matched = False
        if code.startswith(prefix) and (not suffix or code.endswith(suffix)):
            matched = True
        elif code_compat.startswith(prefix) and (not suffix or code_compat.endswith(suffix)):
            code = code_compat
            matched = True
        if not matched:
            return None
        return f"{prefix}{MASK_CHAR}{suffix}"
    return None


def apply_rules(raw: str, rules: list[dict[str, Any]]) -> Optional[str]:
    for rule in rules or []:
        out = apply_rule(raw, rule)
        if out is not None:
            return out
    return None


def normalize_with_regex(raw: str, pattern: str) -> Optional[str]:
    """Percorso regex (pattern con gruppo 'color_code')."""
    code = clean_code(raw)
    if not pattern or not code:
        return None
    try:
        match = re.search(pattern, code, re.IGNORECASE)
    except re.error:
        return None
    if not match or "color_code" not in (match.groupdict() or {}):
        return None
    start, end = match.span("color_code")
    if start < 0 or end <= start:
        return None
    return code[:start] + MASK_CHAR * (end - start) + code[end:]


def normalize_code(raw: str, heuristic: Any = None) -> str:
    """
    Normalizza un vendor code secondo l'euristica del brand.
    Priorità:
    1. Regole in heuristic_metadata (FIXED / ELASTIC)
    2. Nuove modalità COLOR_MASKED_DELIMITED / COLOR_MASKED_COMPACT
    3. Regex con color_code
    4. Passthrough con Step 0
    """
    if not raw:
        return ""
    sterilized = sterilize_separators(raw)
    if heuristic is None:
        return sterilized

    mode = getattr(heuristic, "heuristic_mode", None)
    if mode == "COLOR_AGNOSTIC":
        return sterilized

    # 1. Regole esplicite in metadata
    metadata = getattr(heuristic, "heuristic_metadata", None) or {}
    rules = metadata.get("rules", [])
    out = apply_rules(raw, rules)
    if out is not None:
        return out
    out = apply_rules(sterilized, rules)
    if out is not None:
        return out

    # 2. COLOR_MASKED_DELIMITED
    if mode in ("COLOR_MASKED_DELIMITED", "DELIMITED"):
        c_idx = getattr(heuristic, "color_segment_index", None)
        c_idx = -1 if c_idx is None else c_idx
        segments = sterilized.split("@")
        if len(segments) > 1 and (-len(segments) <= c_idx < len(segments)):
            norm_idx = c_idx if c_idx >= 0 else len(segments) + c_idx
            segs = list(segments)
            segs[norm_idx] = MASK_CHAR
            return "@".join(segs)

    # 3. COLOR_MASKED_COMPACT
    if mode in ("COLOR_MASKED_COMPACT", "COLOR_MASKED"):
        root_len = metadata.get("root_length")
        if root_len:
            _, root, col = extract_root_and_color(sterilized, heuristic_mode="COLOR_MASKED_COMPACT", root_length=root_len)
            if col:
                if "@" in sterilized and sterilized.startswith(root + "@"):
                    return f"{root}@{MASK_CHAR * len(col)}"
                return f"{root}{MASK_CHAR * len(col)}"

    # 4. Regex fallback: non per euristiche esplicitamente PENDING_COHORT, i cui pattern storici
    # possono essere greedy su codici compatti (es. P10QMTZ4551 -> P10QMTZ####).
    if mode != "PENDING_COHORT":
        pattern = getattr(heuristic, "pattern", "") or ""
        out = normalize_with_regex(raw, pattern)
        if out is not None:
            return out
        out = normalize_with_regex(sterilized, pattern)
        if out is not None:
            return out

    return sterilized


# ---------------------------------------------------------------------------
# Induzione Posizionale
# ---------------------------------------------------------------------------

def _fail(status: str, detail: str = "") -> dict[str, Any]:
    return {"status": status, "rule": None, "detail": detail}


def _violates_negative(rule: dict[str, Any], negative_pairs: list[list[str]]) -> bool:
    for pair in negative_pairs or []:
        if len(pair) != 2:
            continue
        a, b = apply_rule(pair[0], rule), apply_rule(pair[1], rule)
        if a is not None and a == b:
            return True
    return False


def induce_positional_mask(
    codes: list[str],
    existing_rule: Optional[dict[str, Any]] = None,
    negative_pairs: Optional[list[list[str]]] = None,
) -> dict[str, Any]:
    """
    Induce una regola a partire da codici dello STESSO MODELLO con colori diversi.
    Ritorna {"status", "rule", "detail"}.
    """
    sterilized_codes = [sterilize_separators(c) for c in codes if sterilize_separators(c)]
    distinct_sterilized = sorted(set(sterilized_codes))
    if len(distinct_sterilized) < 2:
        return _fail(NO_VARIANCE, "servono almeno due codici distinti")

    # Caso A: Codici Delimitati (tutti i codici presentano almeno un separatore '@')
    if all("@" in c for c in distinct_sterilized):
        seg_lists = [c.split("@") for c in distinct_sterilized]
        num_segs_set = {len(segs) for segs in seg_lists}
        if len(num_segs_set) != 1:
            return _fail(SIGNATURE_MISMATCH, "numero di segmenti diverso tra i codici")
        num_segs = num_segs_set.pop()
        if num_segs < 2:
            return _fail(SIGNATURE_MISMATCH, "segmenti insufficienti per induzione delimitata")

        var_seg_indices = [i for i in range(num_segs) if len({s[i] for s in seg_lists}) > 1]
        if not var_seg_indices:
            return _fail(NO_VARIANCE)
        if len(var_seg_indices) > 1:
            return _fail(DISCONTINUOUS_ANOMALY, "variano più segmenti contemporaneamente")

        color_seg_idx = var_seg_indices[0]
        first_segs = seg_lists[0]
        invariant_len = sum(len(first_segs[i]) for i in range(num_segs) if i != color_seg_idx)
        if invariant_len < MIN_PROTECTED_CHARS:
            return _fail(INSUFFICIENT_ANCHOR)

        rule = {
            "type": "DELIMITED",
            "num_segments": num_segs,
            "color_segment_index": color_seg_idx,
            "heuristic_mode": "COLOR_MASKED_DELIMITED",
        }
        if _violates_negative(rule, negative_pairs or []):
            return _fail(NEGATIVE_CONSTRAINT_VIOLATION, "la regola collasserebbe un'esclusione nota")
        return {"status": OK, "rule": rule, "detail": ""}

    # Caso B: Codici Compatti (o privi di '@' in almeno un codice)
    distinct = distinct_sterilized
    lengths = {len(c) for c in distinct}
    if len(lengths) == 1:
        length = lengths.pop()
        sigs = {compute_invariant_signature(c) for c in distinct}
        if len(sigs) != 1:
            return _fail(SIGNATURE_MISMATCH, "firma dei separatori diversa")
        signature = sigs.pop()
        masked = {i for i in range(length) if len({c[i] for c in distinct}) > 1}
        if not masked:
            return _fail(NO_VARIANCE)
        if any(not distinct[0][i].isalnum() for i in masked):
            return _fail(SIGNATURE_MISMATCH, "separatore nello span variabile")
        ordered = sorted(masked)
        if ordered[-1] - ordered[0] + 1 != len(ordered):
            return _fail(DISCONTINUOUS_ANOMALY, "posizioni variabili non contigue")
        if length - len(ordered) < MIN_PROTECTED_CHARS:
            return _fail(INSUFFICIENT_ANCHOR)
        rule = {
            "type": "FIXED",
            "signature": signature,
            "length": length,
            "masked_positions": ordered,
            "heuristic_mode": "COLOR_MASKED_COMPACT",
            "root_length": ordered[0] if ordered[0] > 0 else length - len(ordered),
        }
    else:
        prefix = distinct[0]
        for c in distinct[1:]:
            i = 0
            while i < min(len(prefix), len(c)) and prefix[i] == c[i]:
                i += 1
            prefix = prefix[:i]
        shortest = min(len(c) for c in distinct)
        max_suffix = shortest - len(prefix) - 1
        suffix = ""
        if max_suffix > 0:
            suffix = distinct[0][-max_suffix:]
            for c in distinct[1:]:
                i = 0
                while i < min(len(suffix), len(c)) and suffix[-1 - i] == c[-1 - i]:
                    i += 1
                suffix = suffix[len(suffix) - i:] if i else ""
        if len(prefix) + len(suffix) < MIN_PROTECTED_CHARS:
            return _fail(INSUFFICIENT_ANCHOR)
        # La porzione variabile (colore) di ogni codice deve essere non vuota e un unico token
        # alfanumerico (al più un '@' di confine iniziale): evita regole su codici troncati
        # o che attraversano più segmenti.
        for c in distinct:
            var = c[len(prefix): len(c) - len(suffix)] if suffix else c[len(prefix):]
            var = var[1:] if var.startswith("@") else var
            if not var:
                return _fail(NO_VARIANCE, "porzione variabile vuota (codice troncato)")
            if "@" in var:
                return _fail(SIGNATURE_MISMATCH, "la porzione variabile attraversa più segmenti")
        rule = {
            "type": "ELASTIC",
            "prefix": prefix,
            "suffix": suffix,
            "heuristic_mode": "COLOR_MASKED_COMPACT",
            "root_length": len(prefix.replace("@", "")),
        }

    if _violates_negative(rule, negative_pairs or []):
        return _fail(NEGATIVE_CONSTRAINT_VIOLATION, "la regola collasserebbe un'esclusione nota")
    return {"status": OK, "rule": rule, "detail": ""}


def rule_to_regex(rule: dict[str, Any]) -> str:
    """Pattern regex equivalente alla regola."""
    if not rule:
        return ""
    if rule.get("type") == "DELIMITED":
        num_segs = rule.get("num_segments", 2)
        c_idx = rule.get("color_segment_index", -1)
        norm_idx = c_idx if c_idx >= 0 else num_segs + c_idx
        parts = []
        for i in range(num_segs):
            if i == norm_idx:
                parts.append("(?P<color_code>[A-Za-z0-9]+)")
            else:
                parts.append("[A-Za-z0-9]+")
        return f"^{re.escape('@').join(parts)}$"
    if rule.get("type") == "FIXED":
        pos = rule["masked_positions"]
        start, n, length = pos[0], len(pos), rule["length"]
        return f"^(?=.{{{length}}}$).{{{start}}}(?P<color_code>.{{{n}}}).*$"
    if rule.get("type") == "ELASTIC":
        return f"^{re.escape(rule['prefix'])}(?P<color_code>.+?){re.escape(rule['suffix'])}$"
    return ""


def merge_rule(rules: list[dict[str, Any]], new_rule: dict[str, Any]) -> list[dict[str, Any]]:
    """Inserisce/sostituisce new_rule nella lista."""
    out = []
    replaced = False
    for r in rules or []:
        if r.get("type") == new_rule.get("type") == "DELIMITED" \
                and r.get("num_segments") == new_rule.get("num_segments") \
                and r.get("color_segment_index") == new_rule.get("color_segment_index"):
            out.append(new_rule)
            replaced = True
        elif r.get("type") == new_rule.get("type") == "FIXED" \
                and r.get("length") == new_rule.get("length") and r.get("signature") == new_rule.get("signature"):
            out.append(new_rule)
            replaced = True
        elif r.get("type") == new_rule.get("type") == "ELASTIC" \
                and (new_rule["prefix"].startswith(r["prefix"]) or r["prefix"].startswith(new_rule["prefix"])):
            out.append(new_rule)
            replaced = True
        else:
            out.append(r)
    if not replaced:
        out.append(new_rule)
    return out


def find_applicable_rule(raw: str, rules: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for r in rules or []:
        if apply_rule(raw, r) is not None:
            return r
    return None


def family_key(code: str) -> tuple[str, int, str]:
    c = clean_code(code)
    return (compute_invariant_signature(c), len(c), c[:4])


def partition_ddt_cohort_families(codes: list[str]) -> dict[tuple[str, int, str], list[str]]:
    families: dict[tuple[str, int, str], list[str]] = {}
    for c in sorted({clean_code(c) for c in codes if clean_code(c)}):
        families.setdefault(family_key(c), []).append(c)
    return families


def _divergence(a: str, b: str) -> int:
    if len(a) != len(b):
        return abs(len(a) - len(b)) + min(len(a), len(b))
    return sum(1 for x, y in zip(a, b) if x != y)


def select_pivot_pairs(codes: list[str], max_pairs: int = 2) -> list[tuple[str, str]]:
    """Coppie di codici a massima divergenza."""
    pairs: list[tuple[str, str]] = []
    for members in partition_ddt_cohort_families(codes).values():
        if len(members) < 2:
            continue
        ranked = sorted(combinations(members, 2), key=lambda p: -_divergence(*p))
        pairs.extend(ranked[:max_pairs])
    return pairs[: max_pairs * 3]
