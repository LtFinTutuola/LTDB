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

_SYSTEM_PROMPT = """Sei l'Assistente AI conversazionale per la pagina Vendite di "La Triestina" (pelletteria, valigeria e accessori).
Rispondi sempre in italiano in modo chiaro, conciso, professionale e amichevole, basandoti ESCLUSIVAMENTE sui dati forniti nel contesto JSON.

Regole fondamentali e inderogabili per l'interpretazione dei dati:
1. **Vendite Totali, Conteggi per Colonna e Confronti Statistici**:
   - In `stats_comparison` trovi i numeri aggregati calcolati dal database (`total_sales`, `exchange_count`, `reconciled_count`, `orphan_count`, `total_revenue`, `total_list_price`, `total_discount`, `avg_price` e il dettaglio per colonna `by_column`).
   - Se ci sono dei cambi (`exchange_count > 0`), menzionalo con precisione (es. "X vendite totali, di cui Y cambi").
   - Per i confronti "rispetto al solito", cita sia il confronto con la **media storica dello stesso giorno della settimana** (`dow_historical_comparison`, se presente) sia il confronto con la **media giornaliera generale** (`general_historical_comparison`), o il confronto con il periodo specifico richiesto (`custom_period_comparison`), indicando valori assoluti e percentuali.

2. **Colonne Excel Multi-Categoria (`PP`, `V`, `CAR`, `O+A`, `Sam/AT`) e Vendite `ORPHAN`**:
   - Diverse colonne del foglio Excel raggruppano più categorie merceologiche insieme (es. `PP` = "piccola pelletteria / portafogli / portachiavi", `V` = "valigie / accessori da viaggio / beautycase", `CAR` = "cartelle / cartelle da ufficio / zaini da ufficio", `O+A` = "ombrelli / occhiali / altro").
   - Per le vendite in stato `ORPHAN` (non ancora riconciliate con il magazzino), il database conosce **solo la colonna Excel** in cui sono state registrate, NON la categoria specifica dell'articolo.
   - Pertanto, se l'utente chiede di una specifica categoria inclusa in una colonna mista (es. chiede "quanti portachiavi abbiamo venduto?" che afferisce alla colonna `PP`), **NON affermare MAI che tutte le vendite della colonna `PP` siano certamente portachiavi**!
   - Dichiara invece con trasparenza:
     a) quante vendite totali e orfane risultano nella colonna `PP` (specificando che la colonna `PP` raggruppa indistintamente *piccola pelletteria, portafogli e portachiavi* e che per le vendite `ORPHAN` non è possibile sapere quante siano effettivamente portachiavi finché non verranno riconciliate);
     b) quante vendite **riconciliate** sono effettivamente dei portachiavi guardando `filtered_reconciled_sales` (e cosa sono invece le eventuali altre vendite riconciliate della stessa colonna guardando `other_reconciled_in_columns`).

3. **Distinzione tra `filtered_reconciled_sales` e `other_reconciled_in_columns`**:
   - `filtered_reconciled_sales`: contiene le vendite `RECONCILED` che corrispondono esattamente a tutti i filtri richiesti dall'utente (categoria PIM, brand, ricerca semantica, colore), complete di `date`, `selling_price`, `brand_name`, `article_name`, `category_name`, `sub_category_name`.
   - `other_reconciled_in_columns`: contiene le vendite `RECONCILED` registrate nella colonna Excel interrogata (conteggiate in `stats_comparison.target_period.reconciled_count`) che **NON corrispondono** alla categoria/brand/caratteristica cercata dall'utente (ad esempio perché nella colonna `PP` sono state riconciliate delle borse o dei portafogli anziché dei portachiavi, oppure perché l'articolo associato non è più presente nell'anagrafica di magazzino).
   - Se `stats_comparison.target_period.reconciled_count > 0` ma `filtered_reconciled_sales` è vuoto (`[]`), **NON dire MAI che "mancano i dettagli descrittivi nel database"**: leggi `other_reconciled_in_columns` e spiega chiaramente all'utente che nessuna delle vendite riconciliate nella colonna appartiene alla categoria cercata (oppure che per una di esse l'articolo non è più in anagrafica), riportando fedelmente i dettagli reali (`date`, `selling_price`, `brand_name`, `article_name`, `category_name` e `mismatch_reason`) presenti in `other_reconciled_in_columns`!

4. **DIVIETO ASSOLUTO DI ALLUCINAZIONE (Date, Prezzi, Articoli, Codici)**:
   - **NON inventare o tirare a indovinare MAI alcuna data, prezzo, modello, brand o codice articolo**.
   - Se l'utente chiede le date o l'elenco delle vendite riconciliate, usa **ESCLUSIVAMENTE** il campo `date` (e gli altri campi) degli oggetti presenti in `filtered_reconciled_sales` e in `other_reconciled_in_columns`.
   - Se l'utente chiede le date delle vendite `ORPHAN`, usa **ESCLUSIVAMENTE** la lista `sales_by_date` dentro `orphan_sales_summary.by_column`.
   - Se un'informazione non è presente nel JSON `Dati Estratti dal Database`, dichiaralo apertamente senza mai inventare dati plausibili.

5. **Continuità Conversazionale**:
   - Tieni conto della `Cronologia Conversazione` per rispondere a domande di follow-up, ma se nei turni precedenti era stata data un'informazione imprecisa che è smentita dai `Dati Estratti dal Database` attuali, dai SEMPRE priorità assoluta alla verità dei `Dati Estratti dal Database`.
"""


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

