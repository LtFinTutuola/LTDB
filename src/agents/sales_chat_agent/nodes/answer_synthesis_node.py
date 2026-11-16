"""
src/agents/sales_chat_agent/nodes/answer_synthesis_node.py
----------------------------------------------------------
Node 5: Synthesizes a natural, accurate conversational answer in Italian from:
  - User question & chat history
  - Deterministic statistical/comparative metrics (total, per-column, general_avg, dow_avg, custom baseline)
  - Filtered RECONCILED sales items
  - ORPHAN sales summary (ensuring full transparency when sales are not yet reconciled)
"""
import json
from src.agents.llm_client import LLMClient
from src.agents.sales_chat_agent.state import SalesChatGraphState
from src.core.logger import get_logger

logger = get_logger()

_MODEL = "gemini-3.1-flash-lite"

from src.utils.prompts import SALES_CHAT_ANSWER_SYNTHESIS_SYSTEM_PROMPT as _SYSTEM_PROMPT


async def answer_synthesis_node(state: SalesChatGraphState) -> dict:
    """
    Generate the final conversational response in Italian.
    """
    plan = state.query_plan
    other_reconciled = list(state.other_reconciled_in_columns or [])
    logger.log_agent(
        "answer_synthesis_node",
        "node_entry",
        "ok",
        raw_query=state.raw_query,
        is_pure_chitchat=plan.is_pure_chitchat if plan else False,
        filtered_reconciled_count=len(state.filtered_reconciled),
        other_reconciled_count=len(other_reconciled),
    )

    history_lines = []
    for msg in state.chat_history[-10:]:
        role_label = "Utente" if msg.get("role") == "user" else "Assistente"
        history_lines.append(f"{role_label}: {msg.get('content', '')}")
    history_str = "\n".join(history_lines) if history_lines else "Nessun messaggio precedente."

    if plan and plan.is_pure_chitchat:
        user_prompt = (
            f"Data di riferimento nella pagina: {state.context_date}\n"
            f"Cronologia Conversazione:\n{history_str}\n\n"
            f"Messaggio Utente: \"{state.raw_query}\"\n\n"
            f"Rispondi cordialmente ricordando brevemente che puoi aiutare con conteggi vendite, "
            f"ricerca articoli venduti e confronti statistici giornalieri."
        )
    else:
        context_payload = {
            "context_date": state.context_date,
            "query_plan": plan.model_dump() if plan else None,
            "stats_comparison": state.stats_comparison,
            "daily_pnl_summary": state.daily_pnl_summary,
            "filtered_reconciled_sales": state.filtered_reconciled,
            "other_reconciled_in_columns": other_reconciled,
            "orphan_sales_summary": state.orphan_summary,
        }
        user_prompt = (
            f"Cronologia Conversazione:\n{history_str}\n\n"
            f"Dati Estratti dal Database:\n"
            f"{json.dumps(context_payload, ensure_ascii=False, indent=2)}\n\n"
            f"Domanda Utente: \"{state.raw_query}\"\n\n"
            f"Formula una risposta precisa, chiara e conversazionale basata ESCLUSIVAMENTE sui dati estratti."
        )

    try:
        client = LLMClient()
        reply_text = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=user_prompt,
            pipeline_stage="sales_chat_answer_synthesis",
            response_mime_type="text/plain",
            temperature=0.2,
        )
        reply = (reply_text or "").strip()
    except Exception as exc:
        logger.log_agent("answer_synthesis_node", "synthesis_llm_error", "err", exc=exc)
        reply = _build_deterministic_fallback_reply(state)

    if not reply:
        reply = _build_deterministic_fallback_reply(state)

    logger.log_agent("answer_synthesis_node", "node_exit", "ok", reply_length=len(reply))
    return {"reply": reply}


def _build_deterministic_fallback_reply(state: SalesChatGraphState) -> str:
    """Fallback deterministic summary in case the LLM call fails."""
    q_lower = (state.raw_query or "").lower()
    stats = state.stats_comparison
    if not stats or "target_period" not in stats:
        return (
            f"Ricevuto il messaggio «{state.raw_query}» per la data {state.context_date}. "
            f"Al momento non ci sono dati di vendita disponibili."
        )
    tp = stats["target_period"]
    total_sales = tp.get("total_sales", 0)
    total_rev = tp.get("total_revenue", 0.0)
    orphan_cnt = tp.get("orphan_count", 0)
    rec_cnt = len(state.filtered_reconciled)
    extra_note = ""
    if "problem" in q_lower:
        extra_note = (
            f" Rilevate {orphan_cnt} vendite da riconciliare (nessun altro problema critico)."
            if orphan_cnt > 0
            else " Non è stato rilevato alcun problema nelle vendite."
        )
    return (
        f"Risposta per «{state.raw_query}» ({tp.get('start_date')} - {tp.get('end_date')}): "
        f"risultano {total_sales} vendite per un totale di € {total_rev:.2f} "
        f"({rec_cnt} articoli riconciliati compatibili con la ricerca, "
        f"{orphan_cnt} vendite ancora da riconciliare).{extra_note}"
    )

