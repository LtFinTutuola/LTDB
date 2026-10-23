"""
src/utils/text_normalize.py
---------------------------
Text normalization utilities: diacritics folding, casing neutralization,
tokenization and Italian singular/plural word variance matching.
"""
from __future__ import annotations
import re
import unicodedata
from typing import List, Set, Optional


def fold_text(text: Optional[str]) -> str:
    """
    Normalize text by removing diacritical marks (accents), converting special
    ordinal markers (1ª, 1ᴬ -> 1a) and converting to lower casefold.
    """
    if not text:
        return ""
    # Replace ordinal indicators common in fashion brand names
    text = text.replace("1ª", "1a").replace("1ᴬ", "1a").replace("1°", "1o")
    # Decompose unicode to separate base characters and combining accents
    nfkd = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return stripped.casefold().strip()


def tokenize_words(text: Optional[str], min_length: int = 2) -> List[str]:
    """
    Extract normalized alphanumeric word tokens from text.
    """
    if not text:
        return []
    folded = fold_text(text)
    tokens = re.findall(r"[a-z0-9#*]+", folded)
    return [t for t in tokens if len(t) >= min_length]


def word_variants(word: str) -> Set[str]:
    """
    Generate basic Italian singular/plural inflectional variants for a word.
    Examples:
      - 'borsa' -> {'borsa', 'borse'}
      - 'zaino' -> {'zaino', 'zaini'}
      - 'borsello' -> {'borsello', 'borselli'}
      - 'cartella' -> {'cartella', 'cartelle'}
    """
    w = fold_text(word)
    if not w or len(w) < 3:
        return {w} if w else set()

    variants = {w}
    if w.endswith("a"):
        variants.add(w[:-1] + "e")
    elif w.endswith("e"):
        variants.add(w[:-1] + "a")
        variants.add(w[:-1] + "i")
    elif w.endswith("o"):
        variants.add(w[:-1] + "i")
    elif w.endswith("i"):
        variants.add(w[:-1] + "o")
        variants.add(w[:-1] + "e")

    return variants


def contains_whole_word(keyword: str, text: str) -> bool:
    """
    Check if keyword (or any of its singular/plural variants) occurs as a whole word
    in text, using folded matching.
    Prevents false positive substring matches (e.g. 'borsa' matching 'borsone').
    """
    if not keyword or not text:
        return False

    text_folded = fold_text(text)
    variants = word_variants(keyword)

    # Tokenize the target text into words for exact token set lookup
    text_tokens = set(re.findall(r"[a-z0-9#*]+", text_folded))
    return any(v in text_tokens for v in variants)
