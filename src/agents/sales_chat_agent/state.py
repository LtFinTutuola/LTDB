"""
src/agents/sales_chat_agent/state.py
------------------------------------
Shared state and structured LLM output schemas for SalesConversationalAgent.
"""
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


class SalesQueryPlan(BaseModel):
    """
    Structured output schema produced by query_plan_node for the conversational sales agent.
    Maps user query + chat history + columns.yaml + PIM taxonomy into a concrete execution plan.
    """
    standalone_query: str = Field(
        ...,
        description="Riformulazione autonoma della domanda dell'utente che risolve eventuali riferimenti ai messaggi precedenti della conversazione.",
    )
    is_pure_chitchat: bool = Field(
        default=False,
        description="True SOLO se il messaggio dell'utente è un saluto, ringraziamento o chiacchiera generica che non richiede alcuna interrogazione dei dati di vendita.",
    )
    start_date: str = Field(
        ...,
        description="Data di inizio del periodo target in formato YYYY-MM-DD (calcolata rispetto a context_date, es. 'oggi' -> context_date).",
    )
    end_date: str = Field(
        ...,
        description="Data di fine del periodo target in formato YYYY-MM-DD (uguale a start_date se la domanda riguarda un singolo giorno).",
    )
    include_stats_comparison: bool = Field(
        default=True,
        description="True se la domanda richiede conteggi, incassi, medie, P&L o confronti ('rispetto al solito', 'rispetto alla media', 'rispetto a periodo X').",
    )
    baseline_start_date: Optional[str] = Field(
        default=None,
        description="Data inizio (YYYY-MM-DD) di uno specifico periodo X di confronto se esplicitamente richiesto dall'utente (es. 'rispetto alla settimana scorsa'), altrimenti null.",
    )
    baseline_end_date: Optional[str] = Field(
        default=None,
        description="Data fine (YYYY-MM-DD) dello specifico periodo X di confronto se richiesto dall'utente, altrimenti null.",
    )
    excel_columns: List[str] = Field(
        default_factory=list,
        description=(
            "Lista degli header delle colonne Excel pertinenti dallo schema columns.yaml "
            "(es. ['B'], ['CAR'], ['Sam/AT'], ['V'], ['PP'], ['C'], ['BORSELLI'], ['O+A']). "
            "Lista vuota [] se la domanda riguarda tutte le vendite nel complesso."
        ),
    )
    brand_name: Optional[str] = Field(
        default=None,
        description="Nome esatto del Brand presente nella lista Brands a catalogo se citato esplicitamente, altrimenti null.",
    )
    category_name: Optional[str] = Field(
        default=None,
        description="Nome esatto della macro-categoria o sotto-categoria dalla tassonomia PIM se citata, altrimenti null.",
    )
    requires_semantic_search: bool = Field(
        default=False,
        description=(
            "True se la domanda richiede attributi descrittivi, qualitativi, funzionali, di stile o materiale "
            "(es. 'eleganti', 'sportive', 'con tracolla', 'in pelle') non deducibili dalla sola colonna Excel o categoria statica."
        ),
    )
    semantic_query: Optional[str] = Field(
        default=None,
        description=(
            "Porzione residua della ricerca contenente i concetti descrittivi/qualitativi e/o di colore "
            "per l'embedding e il filtro colore (es. 'eleganti', 'nere con tracolla'). Null se non ci sono concetti residui."
        ),
    )


class SaleColorSelection(BaseModel):
    """Color selection for a single reconciled sale candidate."""
    sale_id: str = Field(..., description="The ID of the candidate ExcelSale row.")
    selected_colors: List[str] = Field(
        default_factory=list,
        description="List of matching color strings from the sold article's available_colors.",
    )


class SalesColorFilterResponse(BaseModel):
    """
    Structured output schema produced by color_filter_node in SalesConversationalAgent.
    """
    has_color_constraint: bool = Field(
        default=False,
        description="True if the query text expresses a color or shade preference, False otherwise.",
    )
    selections: List[SaleColorSelection] = Field(
        default_factory=list,
        description="Selected colors per sale_id when has_color_constraint is True.",
    )


class SalesChatGraphState(BaseModel):
    """Shared state for the SalesConversationalAgent LangGraph pipeline."""

    # Input fields
    raw_query: str = Field(..., description="Original message sent by the user.")
    context_date: str = Field(..., description="Reference date (YYYY-MM-DD) active in the sales page UI.")
    chat_history: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Chronological list of previous turns [{'role': 'user'|'assistant', 'content': '...'}].",
    )
    columns_schema: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Excel sales column schema loaded from src/columns.yaml.",
    )
    brands_catalog: List[str] = Field(default_factory=list, description="Available Brand names in DB.")
    categories_catalog: Dict[str, Any] = Field(
        default_factory=dict,
        description="Macro-category and sub-category hierarchy in DB.",
    )

    # Internal pipeline state
    query_plan: Optional[SalesQueryPlan] = Field(
        default=None,
        description="Structured sales query plan generated by query_plan_node.",
    )
    stats_comparison: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Deterministic statistical and comparative aggregations from sql_and_stats_node.",
    )
    daily_pnl_summary: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Daily P&L summary when applicable.",
    )
    reconciled_candidates: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Reconciled sales rows matching the SQL filters.",
    )
    other_reconciled_in_columns: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Reconciled sales rows in the target Excel column(s) that were excluded because their "
            "linked PIM article belongs to a different category/brand or is missing from the catalog."
        ),
    )
    orphan_summary: Dict[str, Any] = Field(
        default_factory=dict,
        description="Summary of ORPHAN sales rows in the target period and columns.",
    )
    ranked_reconciled: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Reconciled sales after semantic similarity filtering.",
    )
    filtered_reconciled: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Reconciled sales after color variant verification.",
    )

    # Output fields
    reply: str = Field(
        default="",
        description="Final conversational answer in Italian generated by answer_synthesis_node.",
    )
