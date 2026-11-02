"""
Model Equivalence Oracle.

Stabilisce se due articoli (descritti solo dalla loro scheda prodotto, MAI dai vendor code)
sono lo STESSO MODELLO in colori diversi oppure MODELLI DIVERSI.

Pipeline a stadi:
  - Stage 2: veti deterministici (anti-genericità, tolleranza dimensionale, tassonomia/materiale macro-famiglia).
  - Stage 3: giudice LLM (temperature=0, response_schema Pydantic), invocato solo se nessun veto scatta.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from src.core.logger import get_logger
from src.utils.vendor_code_normalizer import clean_code, family_key

logger = get_logger()

_MODEL = "gemini-3.1-flash-lite"
COSINE_THRESHOLD = 0.80
DIM_ABS_TOLERANCE_CM = 3.0
DIM_REL_TOLERANCE = 0.08

SAME_MODEL = "SAME_MODEL"
DIFFERENT_MODEL = "DIFFERENT_MODEL"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

_MATERIAL_FAMILIES = {
    "leather": ("pelle", "leather", "cuoio", "nappa", "saffiano"),
    "textile": ("nylon", "tela", "canvas", "cotone", "cotton", "poliestere", "polyester", "tessuto", "fabric", "jacquard"),
    "metal": ("metallo", "metal", "acciaio", "steel", "ottone", "brass", "oro", "argento"),
    "synthetic": ("pvc", "pu", "similpelle", "eco", "plastica", "plastic", "resina", "abs", "policarbonato"),
}
_STOPWORDS = {"di", "da", "il", "la", "lo", "le", "un", "una", "con", "per", "in", "e", "the", "a", "of", "and"}


class CanonicalProductEntity(BaseModel):
    name: str = ""
    description: str = ""
    category: Optional[str] = None
    sub_category: Optional[str] = None
    width_cm: Optional[float] = None
    height_cm: Optional[float] = None
    depth_cm: Optional[float] = None
    materials: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)


class OracleVerdictOutput(BaseModel):
    verdict: Literal["SAME_MODEL", "DIFFERENT_MODEL", "INSUFFICIENT_DATA"]
    color_relation: Literal["DIFFERENT_COLOR", "SAME_COLOR", "UNKNOWN"] = "UNKNOWN"
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Costruzione entità
# ---------------------------------------------------------------------------

def _parse_dimensions(dims: Any) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if not dims:
        return None, None, None
    if isinstance(dims, str):
        try:
            dims = json.loads(dims)
        except (ValueError, TypeError):
            return None, None, None
    if not isinstance(dims, dict):
        return None, None, None

    def _f(v: Any) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return _f(dims.get("width_cm")), _f(dims.get("height_cm")), _f(dims.get("depth_cm"))


def _cat_name(cat: Any) -> Optional[str]:
    if isinstance(cat, dict):
        cat = cat.get("description") or cat.get("name")
    return str(cat).strip().lower() if cat else None


def entity_from_card(card: dict) -> CanonicalProductEntity:
    """Entità a partire da una scheda blueprint in staging (cluster sintetizzato/arricchito)."""
    w, h, d = _parse_dimensions(card.get("dimensions"))
    colors = []
    for it in card.get("cluster_items", []) or []:
        for c in it.get("colors") or []:
            colors.append(str(c).lower())
    return CanonicalProductEntity(
        name=card.get("article_name") or "",
        description=card.get("description") or "",
        category=_cat_name(card.get("category")),
        sub_category=_cat_name(card.get("sub_category")),
        width_cm=w, height_cm=h, depth_cm=d,
        materials=[str(m).lower() for m in (card.get("materials") or [])],
        tags=[str(t).lower() for t in (card.get("tags") or [])],
        colors=sorted(set(colors)),
    )


def entity_from_blueprint(bp: dict) -> CanonicalProductEntity:
    """Entità a partire da un record di coorte DB (vedi ``pim_repo.get_brand_cohort``)."""
    w, h, d = _parse_dimensions(bp.get("dimensions"))
    colors = sorted({str(c).lower() for v in bp.get("variants", []) for c in (v.get("colors") or [])})
    return CanonicalProductEntity(
        name=bp.get("article_name") or "",
        description=bp.get("description") or "",
        category=_cat_name(bp.get("category_name")),
        width_cm=w, height_cm=h, depth_cm=d,
        materials=[str(m).lower() for m in (bp.get("materials") or [])],
        tags=[str(t).lower() for t in (bp.get("tags") or [])],
        colors=colors,
    )


# ---------------------------------------------------------------------------
# Stage 2: veti deterministici
# ---------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z0-9]+", (text or "").lower()) if t not in _STOPWORDS]


def _is_generic(e: CanonicalProductEntity) -> bool:
    has_dims = any(v is not None for v in (e.width_cm, e.height_cm, e.depth_cm))
    return len(_tokens(e.name)) < 2 and len(_tokens(e.description)) < 3 and not has_dims


def _material_families(e: CanonicalProductEntity) -> set[str]:
    fams = set()
    blob = " ".join(e.materials + _tokens(e.description))
    for fam, kws in _MATERIAL_FAMILIES.items():
        if any(re.search(rf"\b{re.escape(k)}\b", blob) for k in kws):
            fams.add(fam)
    return fams


def deterministic_veto(a: CanonicalProductEntity, b: CanonicalProductEntity) -> Optional[str]:
    """Ritorna INSUFFICIENT_DATA / DIFFERENT_MODEL se uno stadio deterministico decide, altrimenti None."""
    # 2.1 anti-genericità
    if _is_generic(a) or _is_generic(b):
        return INSUFFICIENT_DATA
    # 2.2 tolleranza dimensionale (null-permissiva)
    for x, y in ((a.width_cm, b.width_cm), (a.height_cm, b.height_cm), (a.depth_cm, b.depth_cm)):
        if x is not None and y is not None and abs(x - y) > max(DIM_ABS_TOLERANCE_CM, DIM_REL_TOLERANCE * max(x, y)):
            return DIFFERENT_MODEL
    # 2.3 tassonomia e macro-famiglia materiale (solo conflitti inter-famiglia)
    if a.category and b.category and a.category != b.category:
        return DIFFERENT_MODEL
    if a.sub_category and b.sub_category and a.sub_category != b.sub_category:
        return DIFFERENT_MODEL
    fa, fb = _material_families(a), _material_families(b)
    if fa and fb and fa.isdisjoint(fb):
        return DIFFERENT_MODEL
    return None


# ---------------------------------------------------------------------------
# Selezione candidati
# ---------------------------------------------------------------------------

def cosine_similarity(a: Optional[list[float]], b: Optional[list[float]]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _common_prefix_len(a: str, b: str) -> int:
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return i


def select_candidates(
    cluster_codes: list[str],
    cluster_embedding: Optional[list[float]],
    cohort: list[dict],
    top_k: int = 3,
) -> list[dict]:
    """Candidati dalla coorte del brand: morfologia del codice (famiglia/prefisso) OR coseno >= soglia."""
    scored: list[tuple[int, float, dict]] = []
    codes = [clean_code(c) for c in cluster_codes if clean_code(c)]
    for bp in cohort:
        variant_codes = [clean_code(v.get("supplier_code")) for v in bp.get("variants", []) if v.get("supplier_code")]
        morph = any(
            family_key(c) == family_key(v) or _common_prefix_len(c, v) >= 4
            for c in codes for v in variant_codes
        )
        cos = cosine_similarity(cluster_embedding, bp.get("embedding"))
        if morph or cos >= COSINE_THRESHOLD:
            scored.append((0 if morph else 1, -cos, bp))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [bp for _, _, bp in scored[:top_k]]


# ---------------------------------------------------------------------------
# Stage 3: giudice LLM
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "Sei un esperto di cataloghi prodotto moda e lusso. Ti vengono date le schede di due articoli dello stesso brand. "
    "Devi stabilire se descrivono lo STESSO MODELLO (anche in colori diversi) oppure MODELLI DIVERSI "
    "(linee, forme, taglie o varianti strutturali diverse).\n"
    "REGOLE:\n"
    "1. Ignora completamente il colore nel giudicare se il modello è lo stesso.\n"
    "2. Taglie/dimensioni sostanzialmente diverse indicano modelli diversi.\n"
    "3. Se le informazioni non bastano per decidere, rispondi INSUFFICIENT_DATA: NON indovinare.\n"
    "4. In color_relation indica DIFFERENT_COLOR solo se i due articoli sono chiaramente di colore diverso, "
    "SAME_COLOR se lo stesso colore, UNKNOWN se non determinabile.\n"
    "Restituisci ESCLUSIVAMENTE un JSON conforme allo schema."
)


def _render_entity(label: str, e: CanonicalProductEntity) -> str:
    dims = [f"{n}={v}cm" for n, v in (("larghezza", e.width_cm), ("altezza", e.height_cm), ("profondità", e.depth_cm)) if v is not None]
    return (
        f"{label}:\n- Nome: {e.name}\n- Descrizione: {e.description}\n- Categoria: {e.category or 'n/d'}"
        f" / {e.sub_category or 'n/d'}\n- Dimensioni: {', '.join(dims) or 'n/d'}\n"
        f"- Materiali: {', '.join(e.materials) or 'n/d'}\n- Colori noti: {', '.join(e.colors) or 'n/d'}"
    )


async def judge_pair(client: Any, a: CanonicalProductEntity, b: CanonicalProductEntity) -> OracleVerdictOutput:
    prompt = f"{_render_entity('ARTICOLO A', a)}\n\n{_render_entity('ARTICOLO B', b)}\n\nSono lo stesso modello?"
    try:
        raw = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Model Equivalence Oracle",
            response_schema=OracleVerdictOutput,
            temperature=0.0,
        )
        return OracleVerdictOutput.model_validate_json(raw)
    except Exception as exc:  # fail-safe: nessuna decisione = nessuna azione distruttiva
        logger.log_agent("model_equivalence_oracle", "judge_failed", "warn", exc=str(exc))
        return OracleVerdictOutput(verdict=INSUFFICIENT_DATA, reasoning=f"judge_failed: {exc}")


async def evaluate_pair(client: Any, a: CanonicalProductEntity, b: CanonicalProductEntity) -> OracleVerdictOutput:
    """Veti deterministici -> altrimenti giudice LLM."""
    veto = deterministic_veto(a, b)
    if veto is not None:
        return OracleVerdictOutput(verdict=veto, reasoning="deterministic_veto")  # type: ignore[arg-type]
    return await judge_pair(client, a, b)


async def embed_entity(entity: CanonicalProductEntity, client: Any = None) -> Optional[list[float]]:
    """Embedding best-effort (None se non disponibile)."""
    text = " ".join(filter(None, [entity.name, entity.description, entity.category or ""]))
    if not text.strip():
        return None
    try:
        from src.agents.llm_client import LLMClient
        c = client or LLMClient()
        return await asyncio.to_thread(c.generate_embedding_sync, text)
    except Exception:
        return None
