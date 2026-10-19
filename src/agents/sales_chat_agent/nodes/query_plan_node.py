"""
src/agents/sales_chat_agent/nodes/query_plan_node.py
----------------------------------------------------
Node 1: Translates the user message + conversational history + context_date + columns.yaml
+ PIM taxonomy into a structured SalesQueryPlan JSON.
"""
from datetime import datetime
from typing import List, Dict, Any

from src.agents.llm_client import LLMClient
from src.agents.sales_chat_agent.state import SalesChatGraphState, SalesQueryPlan
from src.core.logger import get_logger

logger = get_logger()

_MODEL = "gemini-3.1-flash-lite"

_DOW_NAMES_IT = [
    "Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"
]

_SYSTEM_PROMPT = """Sei il Query Planner AI dell'agente conversazionale per le vendite di "La Triestina" (LTDB), negozio di pelletteria, valigeria e accessori moda.
Il database delle vendite (`excel_sales`) registra ogni vendita giornaliera associandola a:
1. Una `date` (YYYY-MM-DD), un prezzo di listino (`starting_price`), un prezzo di vendita effettivo (`selling_price`), un flag cambio (`is_exchange`) e uno stato (`ORPHAN` se non ancora riconciliata con il magazzino, `RECONCILED` se già collegata a un articolo fisico a catalogo).
2. Una colonna merceologica Excel (`excel_file_column`), definita dallo schema `columns.yaml` fornito nel prompt (es. `B` = borse, `Sam/AT` = samsonite / american tourister, `V` = valigie / accessori da viaggio / beautycase, `PP` = piccola pelletteria / portafogli / portachiavi, `C` = cinture, `CAR` = cartelle / cartelle da ufficio / zaini da ufficio, `BORSELLI` = borselli / borselli da uomo, `O+A` = ombrelli / occhiali / altro).
3. Per le sole vendite `RECONCILED`, il collegamento al catalogo PIM/WMS (`brands`, `categories`, `article_blueprints` con `embedding` semantico e `articles` con `colors`).

Il tuo compito è analizzare l'ultimo messaggio dell'utente (tenendo conto della `Cronologia Conversazione` e della `Data di Riferimento (Oggi)`) e produrre un JSON conforme a `SalesQueryPlan`:
- `standalone_query`: riformula la domanda dell'utente in modo autonomo e completo, risolvendo eventuali pronomi o riferimenti ai turni precedenti della chat (es. se prima aveva chiesto delle borse e ora chiede "E ieri?", diventa "Quante borse abbiamo venduto ieri?").
- `is_pure_chitchat`: `true` SOLO se il messaggio è un saluto ("ciao", "buongiorno") o ringraziamento generico che non chiede alcun dato; altrimenti `false`.
- `start_date` e `end_date`: date in formato `YYYY-MM-DD` calcolate rispetto alla `Data di Riferimento (Oggi = context_date)`. Se l'utente dice "oggi" o non specifica date (né nella domanda né nel contesto precedente), usa `context_date` per entrambi.
- `include_stats_comparison`: `true` se l'utente chiede quantità vendute, incassi, statistiche, andamenti o confronti ("rispetto al solito", "rispetto alla media", "rispetto a ieri/settimana scorsa").
- `baseline_start_date` e `baseline_end_date`: valorizzali (`YYYY-MM-DD`) SOLO se l'utente chiede un confronto con uno specifico periodo X esplicito (es. "rispetto a ieri", "rispetto agli ultimi 7 giorni"). Se invece chiede "rispetto al solito" o "rispetto alla media", lasciali `null` (il tool calcola già in automatico media storica generale e media del giorno della settimana).
- `excel_columns`:
  - Se l'utente menziona una tipologia merceologica coperta dalle colonne di `columns.yaml` (es. "borse" -> `["B"]`, "cartelle" o "zaini da ufficio" -> `["CAR"]`, "valigie" -> `["V"]`, "portafogli" -> `["PP"]`, "cinture" -> `["C"]`, "borselli" -> `["BORSELLI"]`, "Samsonite" o "American Tourister" -> `["Sam/AT"]`, "ombrelli" o "occhiali" -> `["O+A"]`), inserisci i relativi header esatti in `excel_columns`.
  - Se la domanda riguarda tutte le vendite in generale (es. "quanto abbiamo venduto oggi rispetto al solito?"), restituisci `[]`.
- `brand_name`: se l'utente cita esplicitamente un brand presente nella lista `Brands a catalogo`, riporta il nome esatto; altrimenti `null`.
- `category_name`:
  - Se l'utente cita una categoria o sottocategoria presente nella `Tassonomia Categorie a DB`, riporta il nome esatto; altrimenti `null`.
  - Se in una domanda di follow-up l'utente chiede di elencare tutte le vendite riconciliate registrate nella colonna discussa al turno precedente (es. "puoi elencarmi le 4 vendite associate ad un articolo?"), mantieni `excel_columns` del turno precedente; puoi mantenere `category_name` oppure impostarlo a `null` se l'utente vuole vedere tutti gli articoli riconciliati di quella colonna indipendentemente dalla categoria.
- `requires_semantic_search`:
  - Imposta `false` se la domanda riguarda solo conteggi/statistiche per colonna merceologica, brand, categoria, elenchi/date di vendite e/o colore semplice (es. "quante borse abbiamo venduto oggi?", "quante cartelle abbiamo venduto oggi rispetto al solito?", "quanto abbiamo venduto oggi?", "elencami le date di queste vendite").
  - Imposta `true` quando la domanda richiede attributi qualitativi, stilistici, funzionali o di materiale sugli articoli venduti (es. "abbiamo venduto borse eleganti oggi?", "zaini impermeabili", "borse in pelle con tracolla").
- `semantic_query`:
  - Estrai la porzione descrittiva/qualitativa e/o di colore della richiesta (es. da "abbiamo venduto borse eleganti oggi?" -> `"eleganti"`; da "borse di Guess nere" -> `"nere"`). Imposta `null` se non ci sono attributi descrittivi né di colore.
"""


def _format_columns_prompt(columns_schema: List[Dict[str, Any]]) -> str:
    if not columns_schema:
        return "Nessuna colonna configurata."
    lines = [
        f"- Header: '{col['header']}' (indice {col['index']}) -> Descrizione: {col['description']}"
        for col in columns_schema
    ]
    return "Schema Colonne Vendite Excel (`columns.yaml`):\n" + "\n".join(lines)


def _format_taxonomy_prompt(brands: list[str], categories: dict) -> str:
    brands_str = ", ".join(brands) if brands else "Nessun brand registrato"
    cat_lines: list[str] = []
    for macro_name, macro_data in (categories or {}).items():
        desc = macro_data.get("description", "") if isinstance(macro_data, dict) else ""
        subs = macro_data.get("sub_categories", {}) if isinstance(macro_data, dict) else {}
        sub_names = ", ".join(subs.keys()) if subs else "Nessuna sottocategoria"
        cat_lines.append(f"- Macro-categoria: '{macro_name}' ({desc}) | Sottocategorie: [{sub_names}]")
    cats_str = "\n".join(cat_lines) if cat_lines else "Nessuna categoria registrata"
    return f"Brands disponibili a DB:\n[{brands_str}]\n\nTassonomia Categorie a DB:\n{cats_str}"


def _format_history_prompt(chat_history: List[Dict[str, str]]) -> str:
    if not chat_history:
        return "Nessun messaggio precedente (inizio conversazione)."
    recent = chat_history[-10:]
    lines = []
    for msg in recent:
        role_label = "Utente" if msg.get("role") == "user" else "Assistente"
        lines.append(f"{role_label}: {msg.get('content', '')}")
    return "\n".join(lines)


async def query_plan_node(state: SalesChatGraphState) -> dict:
    """
    Generate a structured SalesQueryPlan from the user message and conversation history.
    """
    logger.log_agent(
        "sales_query_plan_node",
        "node_entry",
        "ok",
        raw_query=state.raw_query,
        context_date=state.context_date,
        history_turns=len(state.chat_history),
    )

    try:
        dt = datetime.strptime(state.context_date, "%Y-%m-%d").date()
        dow_name = _DOW_NAMES_IT[dt.weekday()]
        date_context_str = f"{state.context_date} ({dow_name})"
    except Exception:
        date_context_str = state.context_date

    cols_context = _format_columns_prompt(state.columns_schema)
    taxonomy_context = _format_taxonomy_prompt(state.brands_catalog, state.categories_catalog)
    history_context = _format_history_prompt(state.chat_history)

    user_prompt = (
        f"Data di Riferimento (Oggi / context_date): {date_context_str}\n\n"
        f"{cols_context}\n\n"
        f"{taxonomy_context}\n\n"
        f"Cronologia Conversazione:\n{history_context}\n\n"
        f"Nuovo Messaggio Utente: \"{state.raw_query}\"\n\n"
        f"Genera il piano strutturato JSON `SalesQueryPlan`."
    )

    try:
        client = LLMClient()
        raw_json = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=user_prompt,
            pipeline_stage="sales_chat_query_plan",
            response_schema=SalesQueryPlan,
            response_mime_type="application/json",
            temperature=0.0,
        )
        plan = SalesQueryPlan.model_validate_json(raw_json)

        if not plan.start_date:
            plan.start_date = state.context_date
        if not plan.end_date:
            plan.end_date = plan.start_date
        if plan.requires_semantic_search and (not plan.semantic_query or not plan.semantic_query.strip()):
            plan.semantic_query = plan.standalone_query or state.raw_query

    except Exception as exc:
        logger.log_agent("sales_query_plan_node", "llm_plan_fallback", "err", exc=exc, raw_query=state.raw_query)
        plan = SalesQueryPlan(
            standalone_query=state.raw_query,
            is_pure_chitchat=False,
            start_date=state.context_date,
            end_date=state.context_date,
            include_stats_comparison=True,
            excel_columns=[],
            brand_name=None,
            category_name=None,
            requires_semantic_search=False,
            semantic_query=None,
        )

    result = {"query_plan": plan}
    logger.log_agent("sales_query_plan_node", "node_exit", "ok", output=plan.model_dump())
    return result
