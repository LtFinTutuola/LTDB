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

from src.utils.llm_models import SALES_CHAT_QUERY_PLAN_MODEL as _MODEL

_DOW_NAMES_IT = [
    "Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"
]

from src.utils.prompts import SALES_CHAT_QUERY_PLAN_SYSTEM_PROMPT as _SYSTEM_PROMPT


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
