"""
src/utils/color_matcher.py
--------------------------
Fast, deterministic color intent detection and variant filtering.
Maps multilingual (Italian/English) and commercial fashion color names
into canonical color families without requiring LLM calls.
"""
from __future__ import annotations
import re
from typing import Dict, List, Set, Tuple, Optional

COLOR_FAMILIES: Dict[str, List[str]] = {
    "nero": [
        "nero", "nera", "neri", "nere", "black", "noir", "total black", "dark black",
        "ebony", "onice", "liquorice"
    ],
    "bianco": [
        "bianco", "bianca", "bianchi", "bianche", "white", "panna", "ivory", "avorio",
        "saline clay", "off white", "off-white", "latte", "snow", "gesso"
    ],
    "blu": [
        "blu", "blue", "dark blue", "navy", "dark navy", "harbor blue", "azzurro",
        "azzurra", "azzurri", "azzurre", "ocean", "cobalto", "avio", "celeste",
        "turchese", "denim", "indigo", "notte", "midnight", "cyan", "royal blue"
    ],
    "rosso": [
        "rosso", "rossa", "rossi", "rosse", "red", "astral red", "barolo", "bordeaux",
        "burgundy", "rubino", "corallo", "cherry", "wine", "ciliegia", "scarlatto", "fire"
    ],
    "marrone": [
        "marrone", "marroni", "brown", "cuoio", "cognac", "tan", "tabacco", "cioccolato",
        "dark brown", "morotesta", "testa di moro", "castagno", "caramello", "espresso", "coffee"
    ],
    "beige": [
        "beige", "sabbia", "bone", "bone town", "vanilla", "camel", "cammello", "natural",
        "tortora", "ecru", "taupe", "fango", "nude", "crema", "cream", "visone"
    ],
    "verde": [
        "verde", "verdi", "green", "salvia", "olive", "oliva", "military", "militare",
        "forest", "smeraldo", "kaki", "khaki", "muschio", "moss", "menta", "mint", "sage"
    ],
    "grigio": [
        "grigio", "grigia", "grigi", "grigie", "grey", "gray", "silver", "argento",
        "antracite", "canna di fucile", "gunmetal", "titanio", "fumè", "fumo", "charcoal", "piombo"
    ],
    "giallo": [
        "giallo", "gialla", "gialli", "gialle", "yellow", "senape", "mustard", "ochre",
        "ocra", "limone", "sole", "curcuma", "amber", "ambra"
    ],
    "arancione": [
        "arancione", "arancioni", "orange", "zucca", "arancio", "tangerine", "pesca", "peach",
        "terracotta", "apricot", "ruggine", "rust"
    ],
    "rosa": [
        "rosa", "pink", "cipria", "fuxia", "fucsia", "magenta", "antico", "blush",
        "rose", "confetto", "cipria", "flamingo", "malva"
    ],
    "viola": [
        "viola", "purple", "violet", "lilla", "malva", "prugna", "plum", "lavanda", "ametista"
    ],
    "oro": [
        "oro", "gold", "dorato", "dorata", "dorati", "dorate", "golden", "bronzo", "bronze",
        "champagne", "platino", "platinum", "metallic", "metal"
    ],
}

# Inverted index: individual color keyword -> set of canonical family names
_KEYWORD_TO_FAMILIES: Dict[str, Set[str]] = {}
for family, terms in COLOR_FAMILIES.items():
    for term in terms:
        cleaned = term.strip().lower()
        if cleaned not in _KEYWORD_TO_FAMILIES:
            _KEYWORD_TO_FAMILIES[cleaned] = set()
        _KEYWORD_TO_FAMILIES[cleaned].add(family)


def tokenize_text(text: str) -> List[str]:
    """Extract lowercased alphanumeric tokens from text."""
    if not text:
        return []
    return [t.lower() for t in re.findall(r"[a-zA-Z0-9àèéìòùÀÈÉÌÒÙ#*]+", text)]


def detect_color_intent(text: str) -> Tuple[bool, Set[str], List[str]]:
    """
    Detect whether the input text expresses a color constraint.

    Returns:
        tuple (has_color_constraint, matching_families, matched_raw_tokens)
    """
    if not text:
        return False, set(), []

    text_lower = text.lower()
    matched_families: Set[str] = set()
    matched_tokens: List[str] = []

    # Check multi-word color expressions first (e.g. "saline clay", "off white", "dark blue")
    for keyword, families in _KEYWORD_TO_FAMILIES.items():
        if " " in keyword or "-" in keyword:
            pattern = r"\b" + re.escape(keyword) + r"\b"
            if re.search(pattern, text_lower):
                matched_families.update(families)
                matched_tokens.append(keyword)

    # Check single-word tokens
    tokens = tokenize_text(text_lower)
    for tok in tokens:
        if tok in _KEYWORD_TO_FAMILIES:
            matched_families.update(_KEYWORD_TO_FAMILIES[tok])
            if tok not in matched_tokens:
                matched_tokens.append(tok)

    has_constraint = len(matched_families) > 0
    return has_constraint, matched_families, matched_tokens


def filter_colors_by_text(
    query_text: str,
    available_colors: List[str],
) -> Tuple[bool, List[str]]:
    """
    Fast deterministic variant color selection for a blueprint.

    Args:
        query_text: User search text (or filtered semantic_query).
        available_colors: Colors currently stored on articles for this blueprint.

    Returns:
        tuple (has_color_constraint, matching_colors)
    """
    if not available_colors:
        return False, []

    has_constraint, families, raw_tokens = detect_color_intent(query_text)
    if not has_constraint:
        return False, list(available_colors)

    matched: List[str] = []
    for cand in available_colors:
        cand_clean = cand.strip().lower()
        cand_tokens = tokenize_text(cand_clean)

        # 1. Direct substring / raw token match (e.g. "barolo", "cuoio", "astral red")
        direct_match = any(
            tok in cand_clean or cand_clean in tok or any(ct == tok for ct in cand_tokens)
            for tok in raw_tokens
        )
        if direct_match:
            matched.append(cand)
            continue

        # 2. Family match: check if candidate belongs to one of the detected families
        cand_families: Set[str] = set()
        for ctok in cand_tokens:
            if ctok in _KEYWORD_TO_FAMILIES:
                cand_families.update(_KEYWORD_TO_FAMILIES[ctok])
        if cand_clean in _KEYWORD_TO_FAMILIES:
            cand_families.update(_KEYWORD_TO_FAMILIES[cand_clean])

        if any(f in families for f in cand_families):
            matched.append(cand)

    return True, matched
