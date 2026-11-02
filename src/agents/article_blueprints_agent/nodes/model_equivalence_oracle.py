"""
Model Equivalence Oracle v3.0
-----------------------------
Architettura Neuro-Simbolica a Micro-Giudici Paralleli e Scoring a 3 Soglie.
Decomposizione in 5 micro-giudici LLM (TYPOLOGY, LINE, SIZE, MATERIAL, CONSTRUCTION)
combinati con morfologia del codice e relazione colore deterministica in Python.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from pathlib import Path
from typing import Any, Literal, Optional
import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.core.logger import get_logger
from src.utils.vendor_code_normalizer import clean_code, family_key

logger = get_logger()

# Costanti di verdetto di primo livello
SAME_MODEL = "SAME_MODEL"
DIFFERENT_MODEL = "DIFFERENT_MODEL"
AMBIGUOUS = "AMBIGUOUS"
INSUFFICIENT_DATA = "AMBIGUOUS"  # backward compatibility alias

# Costanti di esito morfologico del codice
CODE_IDENTICAL = "IDENTICAL"
CODE_VERY_HIGH = "VERY_HIGH"
CODE_HIGH = "HIGH"
CODE_NEUTRAL = "NEUTRAL"
CODE_CONFLICT = "CONFLICT"

# Costanti per candidati
COSINE_THRESHOLD = 0.80

_STOPWORDS = {
    "di", "da", "il", "la", "lo", "le", "un", "una", "con", "per", "in", "e", "the", "a", "of", "and"
}

_GEMINI_YAML_PATH = Path(__file__).resolve().parent.parent.parent / "gemini.yaml"

DEFAULT_ORACLE_CONFIG: dict[str, Any] = {
    "model_name": "gemini-3.1-flash-lite",
    "temperature": 0.0,
    "max_concurrency": 5,
    "base_score": 50,
    "high_threshold": 75,
    "low_threshold": 35,
    "negative_constraint_threshold": 20,
    "dimensions": {
        "TYPOLOGY": {"MATCH": 15, "UNKNOWN": 0, "CONFLICT": -45},
        "LINE": {"MATCH": 20, "UNKNOWN": 0, "CONFLICT": -40},
        "SIZE": {"MATCH": 10, "UNKNOWN": 0, "CONFLICT": -50},
        "MATERIAL": {"MATCH": 3, "UNKNOWN": 0, "CONFLICT": -30},
        "CONSTRUCTION": {"MATCH": 10, "UNKNOWN": 0, "CONFLICT": -35},
    },
    "code_weights": {
        "IDENTICAL": 6,
        "VERY_HIGH": 6,
        "HIGH": 4,
        "NEUTRAL": 0,
        "CONFLICT": -10,
    },
    "morphology": {
        "max_variable_residue_very_high": 4,
        "max_variable_residue_high": 6,
        "min_prefix_ratio_high": 0.60,
        "min_prefix_ratio_very_high": 0.72,
        "min_prefix_len": 4,
    },
}


# ---------------------------------------------------------------------------
# Configurazione e Validazione (H6 & Invarianti R4)
# ---------------------------------------------------------------------------

def load_oracle_config() -> dict[str, Any]:
    """
    Carica e valida la configurazione dell'oracolo da gemini.yaml.
    Viene riletta a ogni invocazione (H6) con fallback su DEFAULT_ORACLE_CONFIG.
    """
    cfg = json.loads(json.dumps(DEFAULT_ORACLE_CONFIG))
    if _GEMINI_YAML_PATH.exists():
        try:
            with open(_GEMINI_YAML_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if "oracle_equivalence" in data and isinstance(data["oracle_equivalence"], dict):
                    user_cfg = data["oracle_equivalence"]
                    for k, v in user_cfg.items():
                        if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
                            cfg[k].update(v)
                        else:
                            cfg[k] = v
        except Exception as exc:
            logger.log_agent("model_equivalence_oracle", "config_load_error", "warn", exc=str(exc))

    validate_oracle_config(cfg)
    return cfg


def validate_oracle_config(cfg: dict[str, Any]) -> None:
    """
    Valida la coerenza dei parametri e la tenuta degli invarianti strutturali:
      - negative_constraint_threshold < low_threshold < high_threshold
      - Tutti i pesi CONFLICT <= 0 e UNKNOWN == 0
      - Invariante R4: le sole dimensioni deboli (TYPOLOGY MATCH + MATERIAL MATCH + CODE IDENTICAL/VERY_HIGH)
        sommate al base_score non possono raggiungere la soglia alta (evita auto-merge da soli tipologia e codice).
    """
    neg_thresh = cfg.get("negative_constraint_threshold", 20)
    low_thresh = cfg.get("low_threshold", 35)
    high_thresh = cfg.get("high_threshold", 75)
    base_score = cfg.get("base_score", 50)

    if not (neg_thresh < low_thresh < high_thresh):
        raise ValueError(
            f"Configurazione oracolo invalida: atteso negative_constraint_threshold ({neg_thresh}) "
            f"< low_threshold ({low_thresh}) < high_threshold ({high_thresh})"
        )

    dims = cfg.get("dimensions", {})
    for dim_name, weights in dims.items():
        if weights.get("UNKNOWN", 0) != 0:
            raise ValueError(f"Dimensione {dim_name}: il peso UNKNOWN deve essere 0")
        if weights.get("CONFLICT", 0) > 0:
            raise ValueError(f"Dimensione {dim_name}: il peso CONFLICT deve essere <= 0")

    # Invariante R4: deboli = TYPOLOGY + MATERIAL + max(CODE)
    typo_match = dims.get("TYPOLOGY", {}).get("MATCH", 15)
    mat_match = dims.get("MATERIAL", {}).get("MATCH", 3)
    code_max = max(cfg.get("code_weights", {}).values()) if cfg.get("code_weights") else 6
    weak_sum = base_score + typo_match + mat_match + code_max
    if weak_sum >= high_thresh:
        raise ValueError(
            f"Violazione Invariante R4: base_score ({base_score}) + TYPOLOGY ({typo_match}) + "
            f"MATERIAL ({mat_match}) + max_code ({code_max}) = {weak_sum} >= high_threshold ({high_thresh}). "
            f"I pesi permetterebbero un auto-merge (SAME_MODEL) basato solo su tipologia e codice senza "
            f"prove positive su LINE, SIZE o CONSTRUCTION!"
        )


# ---------------------------------------------------------------------------
# Schemi Pydantic per Output Strutturato
# ---------------------------------------------------------------------------

class DimensionEvaluationResult(BaseModel):
    dimension: Literal["TYPOLOGY", "LINE", "SIZE", "MATERIAL", "CONSTRUCTION"]
    extracted_value_x: str = Field(
        default="NON_SPECIFICATO",
        description="Valore estratto dal testo dell'Articolo X per questa dimensione."
    )
    extracted_value_y: str = Field(
        default="NON_SPECIFICATO",
        description="Valore estratto dal testo dell'Articolo Y per questa dimensione."
    )
    verdict: Literal["MATCH", "CONFLICT", "UNKNOWN"] = Field(
        description="MATCH se compatibili/identici; CONFLICT solo per contraddizione esplicita; UNKNOWN se assente."
    )
    reasoning: str = Field(default="", description="Spiegazione sintetica.")


class OracleEvaluationResult(BaseModel):
    """Risultato completo della comparazione di una coppia da parte dell'Oracolo v3.0."""
    model_config = ConfigDict(extra="allow")

    verdict: Literal["SAME_MODEL", "AMBIGUOUS", "DIFFERENT_MODEL"]
    raw_score: float
    final_score: float
    write_negative_constraint: bool = False
    ambiguity_reason: Optional[str] = None
    code_verdict: str = CODE_NEUTRAL
    color_relation: Literal["DIFFERENT", "SAME", "UNKNOWN"] = "UNKNOWN"
    conflict_count: int = 0
    dimensions: dict[str, DimensionEvaluationResult] = Field(default_factory=dict)
    reasoning: str = ""

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)


# Backwards compatibility alias
class OracleVerdictOutput(BaseModel):
    verdict: Literal["SAME_MODEL", "DIFFERENT_MODEL", "AMBIGUOUS"]
    color_relation: Literal["DIFFERENT_COLOR", "SAME_COLOR", "DIFFERENT", "SAME", "UNKNOWN"] = "UNKNOWN"
    reasoning: str = ""
    final_score: float = 100.0
    raw_score: float = 100.0
    write_negative_constraint: bool = False
    code_verdict: str = CODE_NEUTRAL
    dimensions: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Calcolo Morfologico Deterministico del Codice (Dimensione 6)
# ---------------------------------------------------------------------------

def _common_prefix_len(a: str, b: str) -> int:
    i = 0
    min_len = min(len(a), len(b))
    while i < min_len and a[i] == b[i]:
        i += 1
    return i


def calc_code_morphology(code_x: str, code_y: str, config: Optional[dict[str, Any]] = None) -> str:
    """
    Valuta deterministamente l'affinità morfologica tra due vendor code (Dimensione 6).
    Restituisce: IDENTICAL, VERY_HIGH, HIGH, NEUTRAL, CONFLICT.
    """
    cfg = config or load_oracle_config()
    morph_cfg = cfg.get("morphology", {})
    max_res_vh = morph_cfg.get("max_variable_residue_very_high", 4)
    max_res_h = morph_cfg.get("max_variable_residue_high", 6)
    min_ratio_vh = morph_cfg.get("min_prefix_ratio_very_high", 0.72)
    min_ratio_h = morph_cfg.get("min_prefix_ratio_high", 0.60)
    min_p_len = morph_cfg.get("min_prefix_len", 4)

    cx = clean_code(code_x)
    cy = clean_code(code_y)

    if not cx or not cy:
        return CODE_CONFLICT

    if cx == cy:
        return CODE_IDENTICAL

    lx, ly = len(cx), len(cy)
    p = _common_prefix_len(cx, cy)

    if p < min_p_len:
        return CODE_CONFLICT

    if lx != ly:
        # Se lunghezze diverse e nessun separatore esplicito comune, penalizza come conflitto
        return CODE_CONFLICT

    # lx == ly e p >= min_p_len
    r = lx - p  # lunghezza residua variabile
    ratio = p / lx

    if 1 <= r <= max_res_vh and (ratio >= min_ratio_vh or family_key(cx) == family_key(cy)):
        return CODE_VERY_HIGH

    if 1 <= r <= max_res_h and ratio >= min_ratio_h:
        return CODE_HIGH

    return CODE_NEUTRAL


# ---------------------------------------------------------------------------
# Risoluzione Deterministica del Colore
# ---------------------------------------------------------------------------

def calc_color_relation(colors_x: Any, colors_y: Any) -> str:
    """
    Confronto deterministico degli array di colore:
      - 'DIFFERENT' se entrambi hanno colori dichiarati e non hanno intersezione (es. {'taupe'} != {'sin'})
      - 'SAME' se hanno intersezione comune
      - 'UNKNOWN' se uno o entrambi non hanno colori specificati
    """
    def _to_set(c: Any) -> set[str]:
        if not c:
            return set()
        if isinstance(c, (list, tuple, set)):
            return {str(x).strip().lower() for x in c if x and str(x).strip()}
        return {str(c).strip().lower()}

    sx = _to_set(colors_x)
    sy = _to_set(colors_y)

    if not sx or not sy:
        return "UNKNOWN"

    return "SAME" if bool(sx & sy) else "DIFFERENT"


# ---------------------------------------------------------------------------
# Costruzione Contesto e Anti-Genericità
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


def build_article_comparison_context(item: dict) -> str:
    """
    Costruisce il contesto testuale standardizzato per i micro-giudici LLM.
    Include solo: article_name, description, extended_description, dimensions, materials, tags.
    Esclude tassativamente: vendor_code, supplier_code, category, colors, raw_description.
    """
    name = item.get("article_name") or item.get("name") or "NON_SPECIFICATO"
    desc = item.get("description") or "NON_SPECIFICATO"
    ext_desc = (
        item.get("extended_description")
        or item.get("product_extended_description")
        or "NON_SPECIFICATO"
    )

    w, h, d = _parse_dimensions(item.get("dimensions"))
    dim_parts = [f"{lbl}={v}cm" for lbl, v in (("larghezza", w), ("altezza", h), ("profondità", d)) if v is not None]
    dims_str = ", ".join(dim_parts) if dim_parts else "NON_SPECIFICATO"

    mats = item.get("materials") or []
    materials_str = ", ".join(str(m) for m in mats) if mats else "NON_SPECIFICATO"

    tags = item.get("tags") or []
    tags_str = ", ".join(str(t) for t in tags) if tags else "NON_SPECIFICATO"

    return (
        f"- Nome/Titolo: {name}\n"
        f"- Descrizione Breve: {desc}\n"
        f"- Descrizione Estesa: {ext_desc}\n"
        f"- Dimensioni Misure: {dims_str}\n"
        f"- Materiali Dichiarati: {materials_str}\n"
        f"- Tag Descrittivi: {tags_str}"
    )


def is_generic_article(item: dict) -> bool:
    """Verifica se la scheda è priva di contenuto informativo sufficiente (anti-genericità)."""
    name = str(item.get("article_name") or item.get("name") or "")
    desc = str(item.get("description") or "")
    ext_desc = str(item.get("extended_description") or item.get("product_extended_description") or "")
    w, h, d = _parse_dimensions(item.get("dimensions"))
    has_dims = any(v is not None for v in (w, h, d))

    def _toks(text: str) -> list[str]:
        return [t for t in re.findall(r"[a-zA-Z0-9]+", text.lower()) if t not in _STOPWORDS]

    tokens_name = len(_toks(name))
    tokens_desc = len(_toks(desc)) + len(_toks(ext_desc))
    return tokens_name < 2 and tokens_desc < 3 and not has_dims


# ---------------------------------------------------------------------------
# Prompt Ufficiali dei 5 Micro-Giudici LLM
# ---------------------------------------------------------------------------

_PROMPTS: dict[str, str] = {
    "TYPOLOGY": (
        "Sei un giudice tecnico incaricato di valutare ESCLUSIVAMENTE la TIPOLOGIA MERCEOLOGICA FISICA "
        "di due articoli (Articolo X e Articolo Y) nel settore moda, pelletteria e accessori.\n"
        "Leggi l'intera descrizione fornita per capire che cosa sono fisicamente i due oggetti "
        "(es. Zaino, Sacca/Shopping Bag, Borsa a tracolla, Portafoglio, Trolley/Valigia, Beauty Case, Cintura, Portachiavi).\n"
        "REGOLE TASSATIVE:\n"
        "1. CECITÀ SELETTIVA: Ignora completamente nome linea, dimensioni, colori, materiali, tasche secondarie o stile promozionale.\n"
        "2. Rispondi MATCH se entrambi i testi descrivono lo stesso tipo di oggetto fisico (es. entrambi Zaini).\n"
        "3. Rispondi CONFLICT se descrivono due oggetti di natura fisica diversa (es. Sacca vs Zaino; Borsa vs Portafoglio).\n"
        "4. Rispondi UNKNOWN solo se il testo di uno o entrambi gli articoli non permette di determinare la tipologia."
    ),
    "LINE": (
        "Sei un giudice tecnico incaricato di valutare ESCLUSIVAMENTE il NOME DELLA LINEA COMMERCIALE "
        "(o Collezione / Nome Modello) di due articoli dello stesso brand (Articolo X e Articolo Y).\n"
        "Cerca nei titoli e nelle descrizioni il nome proprio della linea o collezione (es. 'MD20', 'Soundbox', 'Urban Groove').\n"
        "REGOLE TASSATIVE:\n"
        "1. CECITÀ SELETTIVA: Ignora se il nome del brand compare in un titolo ed è omesso nell'altro. Ignora tipologia, misure, materiali e aggettivi commerciali.\n"
        "2. Rispondi MATCH se entrambi gli articoli appartengono alla medesima linea/collezione nominale.\n"
        "3. Rispondi CONFLICT SOLO se entrambi i testi indicano esplicitamente un nome di linea/collezione e questi sono diversi tra loro (es. 'MD20' vs 'Hunter').\n"
        "4. Rispondi UNKNOWN se in uno o entrambi gli articoli il nome della linea commerciale non è esplicitato. L'omissione del nome linea in uno dei due testi NON è mai CONFLICT, ma sempre UNKNOWN."
    ),
    "SIZE": (
        "Sei un giudice tecnico incaricato di valutare ESCLUSIVAMENTE le SPECIFICHE DIMENSIONALI, TAGLIA E CAPACITÀ "
        "di due articoli (Articolo X e Articolo Y).\n"
        "Analizza i testi cercando: dimensioni metriche (es. '67 cm', '77 cm', '20x30x10 cm'), misure in pollici, litri o taglie commerciali ('Cabin Size', 'Bagaglio a mano' vs 'Stiva', 'Small' vs 'Large').\n"
        "REGOLE TASSATIVE:\n"
        "1. CECITÀ SELETTIVA: Non confondere aggettivi generici su dettagli costruttivi (es. 'maxi tasca frontale', 'design compatto') con la taglia o volume del modello.\n"
        "2. Rispondi UNKNOWN se in uno o entrambi gli articoli non sono dichiarate misure numeriche o taglie specifiche, o se le rappresentazioni non sono comparabili. L'assenza di dati dimensionali è SEMPRE UNKNOWN e MAI CONFLICT.\n"
        "3. Rispondi MATCH SOLO in caso di equivalenza esplicita dichiarata da entrambi (stesse misure o stessa taglia).\n"
        "4. Rispondi CONFLICT se entrambi gli articoli dichiarano esplicitamente una taglia, dimensione o capacità e queste sono incompatibili (es. '67 cm' vs '77 cm', 'Small' vs 'Medium')."
    ),
    "MATERIAL": (
        "Sei un giudice tecnico esperto in materiali tessili e pelletteria incaricato di valutare ESCLUSIVAMENTE "
        "la COMPATIBILITÀ DEL MATERIALE PRIMARIO tra due articoli (Articolo X e Articolo Y).\n"
        "Poiché i testi provengono da fonti web diverse, una scheda può citare il nome specifico/brevettato della materia prima (es. 'Mitrix', 'Cordura', 'Pelle Saffiano', 'Jacquard') mentre l'altra usa un termine generico ('Tessuto', 'Sintetico', 'Poliestere', 'Pelle').\n"
        "REGOLE TASSATIVE:\n"
        "1. CECITÀ SELETTIVA: Ignora forma, dimensioni, colori e finiture secondarie.\n"
        "2. Rispondi MATCH se i materiali coincidono OPPURE appartengono alla stessa famiglia merceologica espressa con diverso grado di specificità (es. ['tessuto tecnico', 'mitrix', 'jacquard'] e ['tessuto'] sono MATCH).\n"
        "3. Rispondi CONFLICT SOLO se i materiali primari appartengono a nature merceologiche fisicamente incompatibili (es. 'Vera Pelle/Cuoio' vs 'Tessuto/Nylon/Poliestere'; 'Pelle naturale' vs 'Ecopelle/PU'; 'Guscio rigido in Policarbonato' vs 'Tessuto morbido').\n"
        "4. Rispondi UNKNOWN se in uno o entrambi gli articoli il materiale non è indicato."
    ),
    "CONSTRUCTION": (
        "Sei un giudice tecnico incaricato di valutare ESCLUSIVAMENTE la COMPATIBILITÀ DELL'ARCHITETTURA COSTRUTTIVA "
        "e dei DETTAGLI DI COSTRUZIONE tra due articoli (Articolo X e Articolo Y).\n"
        "REGOLE TASSATIVE:\n"
        "1. IMMUNITÀ ALL'OMISSIONE E PARAFRASI: Siti web diversi enfatizzano dettagli accessori diversi o usano parafrasi (es. 'tasche di servizio interne' citate solo da uno; 'tasca frontale con zip' vs 'maxi tasca frontale'). L'omissione di un dettaglio secondario o una diversa formulazione commerciale NON È MAI CONFLICT!\n"
        "2. Rispondi CONFLICT SOLO se rilevi caratteristiche costruttive primarie mutuamente esclusive nello stesso aspetto (es. 'Zaino Monospalla' vs 'Zaino a 2 spallacci'; 'Trolley 2 ruote' vs 'Trolley 4 ruote Spinner'; 'Modello Espandibile (EXP)' vs 'Non espandibile'; 'Chiusura con patta' vs 'Chiusura zip-around perimetrale'). Dettagli compatibili non mutualmente esclusivi (es. 'con portamonete' vs 'con ribaltina') non sono CONFLICT.\n"
        "3. Rispondi MATCH se la struttura costruttiva descritta è pienamente compatibile.\n"
        "4. Rispondi UNKNOWN se i testi non contengono dettagli costruttivi rilevanti o non comparabili."
    ),
}


# ---------------------------------------------------------------------------
# Motore Deterministico di Scoring a 3 Soglie con Regole R1-R7
# ---------------------------------------------------------------------------

def evaluate_equivalence_matrix(
    dimensions: dict[str, DimensionEvaluationResult],
    code_verdict: str,
    config: Optional[dict[str, Any]] = None,
    is_generic: bool = False,
    has_error: bool = False,
    error_reason: str = "",
) -> dict[str, Any]:
    """
    Applica il calcolo del punteggio e le regole strutturali R1-R7:
      - R1: Nessun conflitto può produrre SAME_MODEL (cap a high_threshold - 1).
      - R2: Esattamente 1 conflitto forza AMBIGUOUS (floor low_threshold + 1, cap high_threshold - 1).
      - R3: DIFFERENT_MODEL automatico richiede almeno 2 conflitti (altrimenti floor low_threshold + 1).
      - R4: Tipologia e codice da soli non possono portare a SAME_MODEL (garantito dai pesi validati).
      - R5: Vincolo negativo scritto solo per DIFFERENT_MODEL con final_score <= negative_constraint_threshold.
      - R6: Se codice IDENTICAL, la fascia DIFFERENT_MODEL è esclusa (floor low_threshold + 1).
      - R7: Errore micro-giudice o input generico sospendono il giudizio -> AMBIGUOUS, nessun vincolo.
    """
    cfg = config or load_oracle_config()
    base_score = cfg.get("base_score", 50)
    high_threshold = cfg.get("high_threshold", 75)
    low_threshold = cfg.get("low_threshold", 35)
    neg_threshold = cfg.get("negative_constraint_threshold", 20)

    dim_weights = cfg.get("dimensions", {})
    code_weights = cfg.get("code_weights", {})

    raw_score = float(base_score)
    for dim_name, res in dimensions.items():
        w_dict = dim_weights.get(dim_name, {})
        raw_score += w_dict.get(res.verdict, 0)

    raw_score += code_weights.get(code_verdict, 0)
    final_score = raw_score

    conflict_count = sum(1 for res in dimensions.values() if res.verdict == "CONFLICT")
    ambiguity_reason: Optional[str] = None

    # R7: Errore o input generico
    if has_error:
        verdict = AMBIGUOUS
        write_negative_constraint = False
        ambiguity_reason = "JUDGE_ERROR"
        return {
            "verdict": verdict,
            "raw_score": raw_score,
            "final_score": final_score,
            "write_negative_constraint": write_negative_constraint,
            "ambiguity_reason": ambiguity_reason,
            "code_verdict": code_verdict,
            "conflict_count": conflict_count,
            "dimensions": {k: v.model_dump() for k, v in dimensions.items()},
            "reasoning": f"Giudizio sospeso per errore durante l'inferenza: {error_reason}",
        }

    if is_generic:
        verdict = AMBIGUOUS
        write_negative_constraint = False
        ambiguity_reason = "GENERIC_INPUT"
        return {
            "verdict": verdict,
            "raw_score": raw_score,
            "final_score": final_score,
            "write_negative_constraint": write_negative_constraint,
            "ambiguity_reason": ambiguity_reason,
            "code_verdict": code_verdict,
            "conflict_count": conflict_count,
            "dimensions": {k: v.model_dump() for k, v in dimensions.items()},
            "reasoning": "Giudizio sospeso: schede informative con dati insufficienti o troppo generici.",
        }

    # R1: Nessun conflitto può produrre SAME_MODEL
    if conflict_count > 0:
        final_score = min(final_score, float(high_threshold - 1))

    # R2: Conflitto singolo forza AMBIGUOUS
    if conflict_count == 1:
        final_score = max(float(low_threshold + 1), min(final_score, float(high_threshold - 1)))
        ambiguity_reason = "SINGLE_CONFLICT"

    # R3: DIFFERENT_MODEL automatico richiede almeno 2 conflitti
    if conflict_count < 2 and final_score <= low_threshold:
        final_score = float(low_threshold + 1)
        ambiguity_reason = "WEAK_CONFLICT_AMBIGUOUS"

    # R6: Codice identico non può finire in DIFFERENT_MODEL automatico
    if code_verdict == CODE_IDENTICAL and final_score <= low_threshold:
        final_score = float(low_threshold + 1)
        ambiguity_reason = "IDENTICAL_CODE_NOT_SAME"

    # Assegnazione Verdetto da Fasce
    if final_score >= high_threshold:
        verdict = SAME_MODEL
        write_negative_constraint = False
    elif final_score <= low_threshold:
        verdict = DIFFERENT_MODEL
        # R5: Vincolo negativo scritto solo sotto soglia dedicata
        write_negative_constraint = (final_score <= neg_threshold)
    else:
        verdict = AMBIGUOUS
        write_negative_constraint = False
        if not ambiguity_reason:
            ambiguity_reason = "SCORE_BAND"

    reasoning_parts = [
        f"Score: {final_score:.1f} (raw: {raw_score:.1f})",
        f"Conflitti: {conflict_count}",
        f"Codice: {code_verdict}",
    ]
    if ambiguity_reason:
        reasoning_parts.append(f"Motivo ambiguità: {ambiguity_reason}")

    return {
        "verdict": verdict,
        "raw_score": raw_score,
        "final_score": final_score,
        "write_negative_constraint": write_negative_constraint,
        "ambiguity_reason": ambiguity_reason,
        "code_verdict": code_verdict,
        "conflict_count": conflict_count,
        "dimensions": {k: v.model_dump() for k, v in dimensions.items()},
        "reasoning": " | ".join(reasoning_parts),
    }


# ---------------------------------------------------------------------------
# Chiamata Asincrona ai Micro-Giudici
# ---------------------------------------------------------------------------

async def _judge_dimension(
    client: Any,
    dimension: Literal["TYPOLOGY", "LINE", "SIZE", "MATERIAL", "CONSTRUCTION"],
    context_x: str,
    context_y: str,
    config: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> DimensionEvaluationResult:
    """Esegue la singola micro-chiamata per una dimensione delimitata dal semaforo di concorrenza."""
    system_prompt = _PROMPTS[dimension]
    prompt = f"ARTICOLO X:\n{context_x}\n\nARTICOLO Y:\n{context_y}\n\nValuta la dimensione {dimension}."
    model_name = config.get("model_name", "gemini-3.1-flash-lite")
    temperature = config.get("temperature", 0.0)

    async with semaphore:
        try:
            raw = await client.call(
                model_name=model_name,
                system_prompt=system_prompt,
                prompt=prompt,
                pipeline_stage=f"Oracle:{dimension}",
                response_schema=DimensionEvaluationResult,
                temperature=temperature,
            )
            return DimensionEvaluationResult.model_validate_json(raw)
        except Exception as exc:
            logger.log_agent("model_equivalence_oracle", "micro_judge_failed", "warn", dim=dimension, exc=str(exc))
            return DimensionEvaluationResult(
                dimension=dimension,
                extracted_value_x="ERR",
                extracted_value_y="ERR",
                verdict="UNKNOWN",
                reasoning=f"Errore chiamata LLM: {exc}",
            )


# ---------------------------------------------------------------------------
# Funzione Principale di Valutazione Coppia: evaluate_pair
# ---------------------------------------------------------------------------

async def evaluate_pair(
    client: Any,
    a_item: dict,
    b_item: dict,
    config: Optional[dict[str, Any]] = None,
) -> OracleEvaluationResult:
    """
    Punto di ingresso principale dell'Oracolo v3.0 per confrontare due schede articolo:
      1. Carica la configurazione e valida anti-genericità.
      2. Estrae e valuta morfologia del codice e relazione colore deterministica.
      3. Esegue le 5 micro-chiamate in parallelo via asyncio.gather con gestione errori.
      4. Calcola lo scoring deterministico R1-R7 e logga i risultati.
    """
    cfg = config or load_oracle_config()
    max_conc = cfg.get("max_concurrency", 5)
    semaphore = asyncio.Semaphore(max_conc)

    # 1. Estrazione codici e valutazione morfologica
    def _extract_codes(it: dict) -> list[str]:
        codes = []
        c = it.get("vendor_code") or it.get("supplier_code")
        if c:
            codes.append(str(c))
        for v in it.get("variants", []) or []:
            vc = v.get("supplier_code") or v.get("vendor_code")
            if vc:
                codes.append(str(vc))
        for ci in it.get("cluster_items", []) or []:
            cic = ci.get("vendor_code") or ci.get("supplier_code")
            if cic:
                codes.append(str(cic))
        return [clean_code(x) for x in codes if clean_code(x)]

    codes_a = _extract_codes(a_item)
    codes_b = _extract_codes(b_item)

    best_code_verdict = CODE_NEUTRAL
    verdict_rank = {CODE_IDENTICAL: 4, CODE_VERY_HIGH: 3, CODE_HIGH: 2, CODE_NEUTRAL: 1, CODE_CONFLICT: 0}

    if codes_a and codes_b:
        for ca in codes_a:
            for cb in codes_b:
                cv = calc_code_morphology(ca, cb, cfg)
                if verdict_rank.get(cv, 0) > verdict_rank.get(best_code_verdict, 0):
                    best_code_verdict = cv
    elif not codes_a or not codes_b:
        best_code_verdict = CODE_NEUTRAL

    # 2. Estrazione colori e relazione deterministica
    def _extract_colors(it: dict) -> list[str]:
        colors = []
        if it.get("colors"):
            colors.extend(it["colors"] if isinstance(it["colors"], list) else [it["colors"]])
        for v in it.get("variants", []) or []:
            if v.get("colors"):
                colors.extend(v["colors"] if isinstance(v["colors"], list) else [v["colors"]])
        for ci in it.get("cluster_items", []) or []:
            if ci.get("colors"):
                colors.extend(ci["colors"] if isinstance(ci["colors"], list) else [ci["colors"]])
        return colors

    colors_a = _extract_colors(a_item)
    colors_b = _extract_colors(b_item)
    color_rel = calc_color_relation(colors_a, colors_b)

    # 3. Controllo anti-genericità
    generic_a = is_generic_article(a_item)
    generic_b = is_generic_article(b_item)
    if generic_a or generic_b:
        res_dict = evaluate_equivalence_matrix(
            dimensions={},
            code_verdict=best_code_verdict,
            config=cfg,
            is_generic=True,
        )
        res_dict["color_relation"] = color_rel
        return OracleEvaluationResult(**res_dict)

    # 4. Costruzione contesti senza vendor_code / categoria / colori / raw_description
    context_x = build_article_comparison_context(a_item)
    context_y = build_article_comparison_context(b_item)

    # 5. Esecuzione 5 micro-giudici paralleli
    dimensions_to_eval = ["TYPOLOGY", "LINE", "SIZE", "MATERIAL", "CONSTRUCTION"]
    tasks = [
        _judge_dimension(client, dim, context_x, context_y, cfg, semaphore)
        for dim in dimensions_to_eval
    ]

    results_list = await asyncio.gather(*tasks, return_exceptions=True)

    evaluated_dimensions: dict[str, DimensionEvaluationResult] = {}
    has_error = False
    error_msg = ""

    for dim, res in zip(dimensions_to_eval, results_list):
        if isinstance(res, Exception):
            has_error = True
            error_msg = str(res)
            evaluated_dimensions[dim] = DimensionEvaluationResult(
                dimension=dim,  # type: ignore[arg-type]
                extracted_value_x="ERR",
                extracted_value_y="ERR",
                verdict="UNKNOWN",
                reasoning=f"Eccezione: {res}",
            )
        elif isinstance(res, DimensionEvaluationResult):
            if res.extracted_value_x == "ERR" or "Errore chiamata LLM" in res.reasoning:
                has_error = True
                error_msg = res.reasoning
            evaluated_dimensions[dim] = res
        else:
            has_error = True
            error_msg = f"Risultato inatteso da {dim}: {res}"

    # 6. Scoring Matrix
    score_res = evaluate_equivalence_matrix(
        dimensions=evaluated_dimensions,
        code_verdict=best_code_verdict,
        config=cfg,
        has_error=has_error,
        error_reason=error_msg,
    )
    score_res["color_relation"] = color_rel

    logger.log_agent(
        "model_equivalence_oracle", "evaluated_pair", "ok",
        verdict=score_res["verdict"],
        final_score=score_res["final_score"],
        code_verdict=best_code_verdict,
        color_rel=color_rel,
        write_neg=score_res["write_negative_constraint"],
        ambiguity=score_res.get("ambiguity_reason"),
    )

    return OracleEvaluationResult(**score_res)


# ---------------------------------------------------------------------------
# Selezione Candidati e Embedding
# ---------------------------------------------------------------------------

def cosine_similarity(a: Optional[list[float]], b: Optional[list[float]]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def select_candidates(
    cluster_codes: list[str],
    cluster_embedding: Optional[list[float]],
    cohort: list[dict],
    top_k: int = 3,
) -> list[dict]:
    """
    Selezione candidati dalla coorte del brand:
      - affinità morfologica del codice (stessa famiglia o prefisso >= 4)
      - similarità semantica coseno >= soglia
    """
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


async def embed_entity(entity: Any, client: Any = None) -> Optional[list[float]]:
    """Embedding vettoriale best-effort (None se non disponibile)."""
    if isinstance(entity, dict):
        text = " ".join(filter(None, [entity.get("article_name"), entity.get("description"), entity.get("category_name")]))
    elif hasattr(entity, "name"):
        text = " ".join(filter(None, [getattr(entity, "name", ""), getattr(entity, "description", "")]))
    else:
        text = str(entity)

    if not text.strip():
        return None
    try:
        from src.agents.llm_client import LLMClient
        c = client or LLMClient()
        return await asyncio.to_thread(c.generate_embedding_sync, text)
    except Exception:
        return None


# Helper di costruzione entity per compatibilità
def entity_from_card(card: dict) -> dict:
    return card


def entity_from_blueprint(bp: dict) -> dict:
    return bp
