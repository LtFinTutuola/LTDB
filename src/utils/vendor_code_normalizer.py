"""
Normalizzatore deterministico dei vendor code (nessuna dipendenza da LLM).

Implementa lo Slicing Posizionale carattere-per-carattere con Strict Minimal Masking:
un carattere diventa ``#`` solo se osservato variare tra due articoli certificati come
STESSO MODELLO con colori diversi.

Regole supportate (salvate in ``BrandHeuristic.heuristic_metadata["rules"]``):
  - FIXED   : codici di pari lunghezza e pari firma separatori -> posizioni mascherate
              (``#`` ripetuto per ogni posizione mascherata, es. ``ABCDEF12###``).
  - ELASTIC : codici di lunghezza diversa -> ancore prefisso/suffisso, span variabile
              sostituito da un singolo ``#`` (es. ``BAG100-#``).
"""
from __future__ import annotations

import re
from itertools import combinations
from typing import Any, Optional

MASK_CHAR = "#"
MIN_PROTECTED_CHARS = 2

# Status di induzione
OK = "OK"
NO_VARIANCE = "NO_VARIANCE"
DISCONTINUOUS_ANOMALY = "DISCONTINUOUS_ANOMALY"
INSUFFICIENT_ANCHOR = "INSUFFICIENT_ANCHOR"
SIGNATURE_MISMATCH = "SIGNATURE_MISMATCH"
NEGATIVE_CONSTRAINT_VIOLATION = "NEGATIVE_CONSTRAINT_VIOLATION"


def clean_code(raw: Optional[str]) -> str:
    return (raw or "").strip().upper()


def compute_invariant_signature(code: str) -> str:
    """Firma separatori: ogni carattere alfanumerico diventa 'x', i separatori restano."""
    return "".join("x" if ch.isalnum() else ch for ch in clean_code(code))


# ---------------------------------------------------------------------------
# Applicazione di una regola
# ---------------------------------------------------------------------------

def apply_rule(raw: str, rule: dict[str, Any]) -> Optional[str]:
    """Applica una singola regola al codice. Ritorna None se la regola non è applicabile."""
    code = clean_code(raw)
    if not code or not rule:
        return None
    rtype = rule.get("type")
    if rtype == "FIXED":
        if len(code) != rule.get("length") or compute_invariant_signature(code) != rule.get("signature"):
            return None
        masked = set(rule.get("masked_positions", []))
        return "".join(MASK_CHAR if i in masked else ch for i, ch in enumerate(code))
    if rtype == "ELASTIC":
        prefix, suffix = rule.get("prefix", ""), rule.get("suffix", "")
        if len(code) < len(prefix) + len(suffix) + 1:
            return None
        if not code.startswith(prefix) or (suffix and not code.endswith(suffix)):
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
    """Percorso regex (pattern con gruppo ``color_code``): ``#`` ripetuto per la lunghezza del gruppo."""
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
    Priorità: regole in heuristic_metadata -> regex con color_code -> passthrough.
    """
    code = clean_code(raw)
    if not code or heuristic is None:
        return code
    metadata = getattr(heuristic, "heuristic_metadata", None) or {}
    out = apply_rules(code, metadata.get("rules", []))
    if out is not None:
        return out
    out = normalize_with_regex(code, getattr(heuristic, "pattern", "") or "")
    return out if out is not None else code


# ---------------------------------------------------------------------------
# Induzione
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
    Ritorna ``{"status", "rule", "detail"}``.
    """
    distinct = sorted({clean_code(c) for c in codes if clean_code(c)})
    if len(distinct) < 2:
        return _fail(NO_VARIANCE, "servono almeno due codici distinti")

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
        if existing_rule and existing_rule.get("type") == "FIXED" \
                and existing_rule.get("length") == length and existing_rule.get("signature") == signature:
            masked |= set(existing_rule.get("masked_positions", []))
        if any(not distinct[0][i].isalnum() for i in masked):
            return _fail(SIGNATURE_MISMATCH, "separatore nello span variabile")
        ordered = sorted(masked)
        if ordered[-1] - ordered[0] + 1 != len(ordered):
            return _fail(DISCONTINUOUS_ANOMALY, "posizioni variabili non contigue")
        if length - len(ordered) < MIN_PROTECTED_CHARS:
            return _fail(INSUFFICIENT_ANCHOR)
        rule = {"type": "FIXED", "signature": signature, "length": length, "masked_positions": ordered}
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
        rule = {"type": "ELASTIC", "prefix": prefix, "suffix": suffix}

    if _violates_negative(rule, negative_pairs or []):
        return _fail(NEGATIVE_CONSTRAINT_VIOLATION, "la regola collasserebbe un'esclusione nota")
    return {"status": OK, "rule": rule, "detail": ""}


def rule_to_regex(rule: dict[str, Any]) -> str:
    """Pattern regex (con gruppo ``color_code``) equivalente alla regola, per visualizzazione/compatibilità API."""
    if not rule:
        return ""
    if rule.get("type") == "FIXED":
        pos = rule["masked_positions"]
        start, n, length = pos[0], len(pos), rule["length"]
        return f"^(?=.{{{length}}}$).{{{start}}}(?P<color_code>.{{{n}}}).*$"
    if rule.get("type") == "ELASTIC":
        return f"^{re.escape(rule['prefix'])}(?P<color_code>.+?){re.escape(rule['suffix'])}$"
    return ""


def merge_rule(rules: list[dict[str, Any]], new_rule: dict[str, Any]) -> list[dict[str, Any]]:
    """Inserisce/sostituisce ``new_rule`` nella lista, rimpiazzando una regola della stessa famiglia."""
    out = []
    replaced = False
    for r in rules or []:
        if r.get("type") == new_rule.get("type") == "FIXED" \
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


# ---------------------------------------------------------------------------
# Famiglie DDT / pivot
# ---------------------------------------------------------------------------

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
    """Coppie di codici (entro la stessa famiglia) a massima divergenza: i migliori candidati per l'oracolo."""
    pairs: list[tuple[str, str]] = []
    for members in partition_ddt_cohort_families(codes).values():
        if len(members) < 2:
            continue
        ranked = sorted(combinations(members, 2), key=lambda p: -_divergence(*p))
        pairs.extend(ranked[:max_pairs])
    return pairs[: max_pairs * 3]
