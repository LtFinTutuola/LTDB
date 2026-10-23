"""
src/agents/warehouse_search_agent/nodes/query_plan_node.py
----------------------------------------------------------
Node 1: Translates a user natural language query into a structured SearchQueryPlan JSON
using database schema context, live Brand/Category taxonomy, deterministic safety nets,
and a 24-hour query plan cache.
"""
from __future__ import annotations
import hashlib
import json
import re
from typing import List, Dict, Any, Optional

from src.agents.llm_client import LLMClient
from src.agents.warehouse_search_agent.state import WarehouseSearchGraphState, SearchQueryPlan
from src.core.cache import query_plan_cache
from src.core.logger import get_logger
from src.utils.text_normalize import fold_text, tokenize_words, word_variants
from src.utils.color_matcher import detect_color_intent

logger = get_logger()

_MODEL = "gemini-3.1-flash-lite"

_SYSTEM_PROMPT = """Sei un query planner AI per il catalogo magazzino di un negozio di pelletteria, valigeria e accessori moda (LTDB).
Il tuo compito è convertire la query naturale dell'utente in un piano di ricerca JSON strutturato (`SearchQueryPlan`):
- `brand`: se l'utente cita esplicitamente un brand presente nella lista dei Brand a catalogo, riporta il nome ESATTO del Brand a catalogo; altrimenti null.
- `category`: se l'utente cita una categoria o sottocategoria presente nella tassonomia fornita (es. "borsa" -> "Borse", "bauletto" -> "Bauletto", "tracolla" -> se presente come sottocategoria o "Borse a tracolla"), riporta il nome ESATTO a catalogo; altrimenti null.
- `color`: se la query esprime una preferenza di colore, estrai UN SOLO colore in forma base maschile singolare (es. "marrone", "nero", "blu", "rosso", "beige", "verde", "bianco", "cuoio", "cognac"); altrimenti null.
- `keywords`: lista di parole residue rilevanti (nomi modello, linea, codici, caratteristiche specifiche) normalizzate al singolare, in minuscolo, senza accenti, escludendo parole vuote (stopword) e i termini già estratti per brand, categoria e colore. Se non ci sono parole residue, lista vuota [].
"""

_STOPWORDS = {
    "di", "a", "da", "in", "con", "su", "per", "tra", "fra",
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una",
    "del", "dello", "della", "dei", "degli", "delle",
    "al", "allo", "alla", "ai", "agli", "alle",
    "dal", "dallo", "dalla", "dai", "dagli", "dalle",
    "nel", "nello", "nella", "nei", "negli", "nelle",
    "sul", "sullo", "sulla", "sui", "sugli", "sulle",
    "e", "ed", "o", "od", "che", "cui", "quale", "quali",
    "tipo", "modello", "colore", "articoli", "articolo",
}


def _hash_taxonomy(brands: List[str], categories: Dict[str, Any]) -> str:
    """Compute a short stable hash of catalog taxonomy for cache invalidation."""
    raw = json.dumps({"b": sorted(brands or []), "c": categories or {}}, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def _format_taxonomy_prompt(brands: List[str], categories: Dict[str, Any]) -> str:
    brands_str = ", ".join(brands) if brands else "Nessun brand registrato"
    cat_lines: List[str] = []
    for macro_name, macro_data in (categories or {}).items():
        desc = macro_data.get("description", "") if isinstance(macro_data, dict) else ""
        subs = macro_data.get("sub_categories", {}) if isinstance(macro_data, dict) else {}
        sub_names = ", ".join(subs.keys()) if subs else "Nessuna sottocategoria"
        cat_lines.append(f"- Macro-categoria: '{macro_name}' ({desc}) | Sottocategorie: [{sub_names}]")
    cats_str = "\n".join(cat_lines) if cat_lines else "Nessuna categoria registrata"
    return f"Brands disponibili a DB:\n[{brands_str}]\n\nTassonomia Categorie a DB:\n{cats_str}"


def _find_matching_brand(raw_token: str, brands_catalog: List[str]) -> Optional[str]:
    """Find exact or folded match for a brand token in catalog."""
    if not raw_token or not brands_catalog:
        return None
    folded_target = fold_text(raw_token)
    for b in brands_catalog:
        if fold_text(b) == folded_target:
            return b
    return None


def _find_matching_category(raw_token: str, categories_catalog: Dict[str, Any]) -> Optional[str]:
    """Find exact or folded match for a category name in taxonomy (macro or sub)."""
    if not raw_token or not categories_catalog:
        return None
    folded_target = fold_text(raw_token)
    target_variants = word_variants(folded_target)

    # Check macro categories first
    for macro_name in categories_catalog.keys():
        folded_macro = fold_text(macro_name)
        if folded_macro in target_variants:
            return macro_name

    # Check sub categories
    for macro_data in categories_catalog.values():
        if isinstance(macro_data, dict):
            subs = macro_data.get("sub_categories", {})
            for sub_name in subs.keys():
                if fold_text(sub_name) in target_variants:
                    return sub_name

    return None


async def query_plan_node(state: WarehouseSearchGraphState) -> dict:
    """
    Generate a structured SearchQueryPlan from the natural language query.
    Uses query plan cache, LLM extraction, taxonomy validation, and deterministic safety nets.
    """
    logger.log_agent(
        "query_plan_node",
        "node_entry",
        "ok",
        raw_query=state.raw_query,
        brands_count=len(state.brands_catalog),
        categories_count=len(state.categories_catalog),
    )

    query_folded = fold_text(state.raw_query)
    tax_hash = _hash_taxonomy(state.brands_catalog, state.categories_catalog)
    cache_key = f"{query_folded}::{tax_hash}"

    cached_plan: Optional[SearchQueryPlan] = query_plan_cache.get(cache_key)
    if cached_plan:
        logger.log_agent("query_plan_node", "cache_hit", "ok", query=state.raw_query)
        return {"query_plan": cached_plan}

    taxonomy_context = _format_taxonomy_prompt(state.brands_catalog, state.categories_catalog)
    user_prompt = (
        f"{taxonomy_context}\n\n"
        f"Query Utente: \"{state.raw_query}\"\n\n"
        f"Genera il piano di ricerca strutturato JSON."
    )

    plan: Optional[SearchQueryPlan] = None
    try:
        client = LLMClient()
        raw_json = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=user_prompt,
            pipeline_stage="search_query_plan",
            response_schema=SearchQueryPlan,
            response_mime_type="application/json",
            temperature=0.0,
        )
        plan = SearchQueryPlan.model_validate_json(raw_json)
        is_fallback = False
    except Exception as exc:
        logger.log_agent("query_plan_node", "llm_plan_error", "err", exc=exc, raw_query=state.raw_query)
        plan = SearchQueryPlan()
        is_fallback = True

    # ---------------------------------------------------------------------------
    # Taxonomy validation & fallback for LLM output
    # ---------------------------------------------------------------------------
    # 1. Validate Brand against catalog
    if plan.brand:
        exact_brand = _find_matching_brand(plan.brand, state.brands_catalog)
        if exact_brand:
            plan.brand = exact_brand
        else:
            # Not in catalog: demote to keyword so search term isn't lost
            plan.keywords.append(plan.brand)
            plan.brand = None

    # Deterministic Brand check if LLM missed it
    if not plan.brand and state.brands_catalog:
        raw_words = tokenize_words(state.raw_query)
        for b in sorted(state.brands_catalog, key=len, reverse=True):
            b_folded = fold_text(b)
            # Check if brand (which might be multi-word) is present in query
            if b_folded in query_folded:
                # Ensure word boundary match
                pattern = r"\b" + re.escape(b_folded) + r"\b"
                if re.search(pattern, query_folded):
                    plan.brand = b
                    break

    # 2. Validate Category against taxonomy
    if plan.category:
        exact_cat = _find_matching_category(plan.category, state.categories_catalog)
        if exact_cat:
            plan.category = exact_cat
        else:
            plan.keywords.append(plan.category)
            plan.category = None

    # Deterministic Category check if LLM missed it
    if not plan.category and state.categories_catalog:
        for macro_name, macro_data in state.categories_catalog.items():
            for variant in word_variants(macro_name):
                pattern = r"\b" + re.escape(variant) + r"\b"
                if re.search(pattern, query_folded):
                    plan.category = macro_name
                    break
            if plan.category:
                break
            if isinstance(macro_data, dict):
                subs = macro_data.get("sub_categories", {})
                for sub_name in subs.keys():
                    for variant in word_variants(sub_name):
                        pattern = r"\b" + re.escape(variant) + r"\b"
                        if re.search(pattern, query_folded):
                            plan.category = sub_name
                            break
                    if plan.category:
                        break
            if plan.category:
                break

    # 3. Deterministic Color check
    has_colors, _, color_tokens = detect_color_intent(state.raw_query)
    if not plan.color and has_colors and color_tokens:
        plan.color = fold_text(color_tokens[0])
    elif plan.color:
        plan.color = fold_text(plan.color)

    # 4. Clean and normalize keywords
    brand_tokens = {v for t in tokenize_words(plan.brand) for v in word_variants(t)} if plan.brand else set()
    cat_tokens = {v for t in tokenize_words(plan.category) for v in word_variants(t)} if plan.category else set()
    color_tokens_set = {v for t in tokenize_words(plan.color) for v in word_variants(t)} if plan.color else set()

    # Only fall back to raw query tokens if plan was generated via exception fallback
    if is_fallback:
        candidate_keywords = [
            t for t in tokenize_words(state.raw_query)
            if t not in _STOPWORDS
        ]
    else:
        candidate_keywords = plan.keywords or []

    cleaned_keywords: List[str] = []
    seen_kw = set()

    for kw in candidate_keywords:
        folded_kw = fold_text(kw)
        for token in tokenize_words(folded_kw):
            if token in _STOPWORDS:
                continue
            if token in brand_tokens or token in cat_tokens or token in color_tokens_set:
                continue
            if token not in seen_kw:
                seen_kw.add(token)
                cleaned_keywords.append(token)

    plan.keywords = cleaned_keywords

    # Store in 24h plan cache
    query_plan_cache.set(cache_key, plan)

    logger.log_agent("query_plan_node", "node_exit", "ok", output=plan.model_dump())
    return {"query_plan": plan}
