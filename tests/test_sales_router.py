"""
tests/test_sales_router.py
--------------------------
Comprehensive test suite for the conversational sales hybrid RAG system:
  - src/columns.yaml schema loading and integration with excel_synchronization_service
  - SQLite persistence of SalesChatSession and SalesChatMessage
  - Listing past chats (GET /api/v1/sales/chats) and reloading a chat (GET /api/v1/sales/chats/{id})
  - Continuing an existing chat (reloading prior messages into agent chat_history) vs starting a new chat
  - ORPHAN sales counting and per-column statistical comparisons (general_avg, dow_avg, custom period X)
  - Hybrid RAG on RECONCILED sales + ORPHAN transparency, strict semantic ranking (no catalog fallback),
    and sold-article color granularity
"""
from datetime import date, timedelta
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from src.main import app
from src.models.pim import Brand, Category, ArticleBlueprint
from src.models.wms import Article, ArticleStatus, Batch, Supplier
from src.models.excel_sales import ExcelSale
from src.schemas.excel_sales import ExcelSaleStatus
from src.agents.sales_chat_agent.state import (
    SalesQueryPlan,
    SalesColorFilterResponse,
    SaleColorSelection,
)
from src.services.excel_synchronization_service import load_columns_schema, get_category_headers
from src.repositories.excel_synchronization_repo import excel_synchronization_repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_catalog_item(
    db,
    brand_name="Guess",
    macro_cat_name="Borse",
    sub_cat_name="Borse a spalla",
    article_name="Borsa Elegante Sera",
    embedding=None,
    colors=None,
    status=ArticleStatus.SOLD,
):
    brand = db.query(Brand).filter(Brand.name == brand_name).first()
    if not brand:
        brand = Brand(name=brand_name)
        db.add(brand)
        db.flush()

    macro = db.query(Category).filter(Category.name == macro_cat_name, Category.parent_id.is_(None)).first()
    if not macro:
        macro = Category(name=macro_cat_name, description="Macro borse", parent_id=None)
        db.add(macro)
        db.flush()

    sub = db.query(Category).filter(Category.name == sub_cat_name, Category.parent_id == macro.id).first()
    if not sub:
        sub = Category(name=sub_cat_name, description="Sottocategoria borse a spalla", parent_id=macro.id)
        db.add(sub)
        db.flush()

    bp = ArticleBlueprint(
        brand_id=brand.id,
        category_id=sub.id,
        article_name=article_name,
        description="Borsa elegante da cerimonia",
        extended_description="Borsa elegante con finiture dorate",
        tags=["elegante", "sera"],
        materials=["pelle"],
        embedding=embedding or [1.0, 0.0, 0.0],
    )
    db.add(bp)
    db.flush()

    supplier = Supplier(company_name="Fornitore Test")
    db.add(supplier)
    db.flush()

    batch = Batch(
        supplier_id=supplier.id,
        delivery_note_number="DDT-100",
        document_date=date(2026, 10, 1),
    )
    db.add(batch)
    db.flush()

    art = Article(
        article_blueprint_id=bp.id,
        batch_id=batch.id,
        colors=colors or ["black"],
        status=status,
    )
    db.add(art)
    db.commit()
    db.refresh(art)
    return brand, macro, sub, bp, art


def _add_excel_sale(
    db,
    sale_date: date,
    row_idx: int,
    col_idx: int,
    selling_price: float,
    starting_price: float = 240.0,
    status: ExcelSaleStatus = ExcelSaleStatus.ORPHAN,
    article_id: str = None,
    is_exchange: bool = False,
    raw_code: str = None,
) -> ExcelSale:
    sale = ExcelSale(
        date=sale_date,
        excel_row_index=row_idx,
        excel_file_column=col_idx,
        status=status,
        starting_price=starting_price,
        selling_price=selling_price,
        is_exchange=is_exchange,
        raw_article_code=raw_code,
        article_id=article_id,
    )
    db.add(sale)
    db.commit()
    db.refresh(sale)
    return sale


# ---------------------------------------------------------------------------
# Backward-compatible endpoint smoke tests
# ---------------------------------------------------------------------------

def test_chat_vendite(db_session):
    client = TestClient(app)
    payload = {
        "message": "Come vanno le vendite oggi?",
        "context_date": "2023-11-04",
    }
    response = client.post("/api/v1/sales/chat", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "vendite" in data["reply"].lower()
    assert "session_id" in data and data["session_id"]


def test_chat_problemi(db_session):
    client = TestClient(app)
    payload = {
        "message": "Ci sono problemi?",
        "context_date": "2023-11-04",
    }
    response = client.post("/api/v1/sales/chat", json=payload)
    assert response.status_code == 200
    assert "problem" in response.json()["reply"].lower()


def test_chat_fallback(db_session):
    client = TestClient(app)
    payload = {
        "message": "Ciao",
        "context_date": "2023-11-04",
    }
    response = client.post("/api/v1/sales/chat", json=payload)
    assert response.status_code == 200
    assert "ciao" in response.json()["reply"].lower()


# ---------------------------------------------------------------------------
# 1. src/columns.yaml & excel_synchronization_service integration
# ---------------------------------------------------------------------------

def test_columns_yaml_schema_and_headers():
    schema = load_columns_schema()
    assert len(schema) == 8
    headers = get_category_headers()
    assert headers == ["B", "Sam/AT", "V", "PP", "C", "CAR", "BORSELLI", "O+A"]

    by_header = {col["header"]: col["description"] for col in schema}
    assert by_header["B"] == "borse"
    assert by_header["C"] == "cinture"
    assert "cartelle" in by_header["CAR"]
    assert "samsonite" in by_header["Sam/AT"]


# ---------------------------------------------------------------------------
# 2. SQLite Chat Session Persistence, Reloading & Continuation
# ---------------------------------------------------------------------------

def test_chat_session_persistence_reload_and_continuation(db_session):
    client = TestClient(app)
    target_date = "2026-10-19"

    # First message without session_id -> creates new session
    res1 = client.post(
        "/api/v1/sales/chat",
        json={"message": "Quante borse abbiamo venduto oggi?", "context_date": target_date},
    )
    assert res1.status_code == 200
    body1 = res1.json()
    session_id_1 = body1["session_id"]
    assert session_id_1
    assert "Quante borse" in body1["session_title"]

    # Second message WITH session_id -> continues the same session and passes prior messages in chat_history
    captured_inputs = []
    original_aexecute = None
    from src.agents.sales_chat_agent import SalesConversationalAgent
    original_aexecute = SalesConversationalAgent.aexecute

    async def spy_aexecute(self, input_data: dict):
        captured_inputs.append(input_data)
        return await original_aexecute(self, input_data)

    with patch.object(SalesConversationalAgent, "aexecute", new=spy_aexecute):
        res2 = client.post(
            "/api/v1/sales/chat",
            json={
                "message": "E ieri invece?",
                "context_date": target_date,
                "session_id": session_id_1,
            },
        )
    assert res2.status_code == 200
    assert res2.json()["session_id"] == session_id_1
    assert len(captured_inputs) == 1
    # Verify prior 2 messages (user + assistant from turn 1) were loaded from SQLite into chat_history
    history_passed = captured_inputs[0]["chat_history"]
    assert len(history_passed) == 2
    assert history_passed[0]["role"] == "user"
    assert history_passed[0]["content"] == "Quante borse abbiamo venduto oggi?"
    assert history_passed[1]["role"] == "assistant"

    # Start a brand new chat ("Nuova chat", session_id=None)
    res3 = client.post(
        "/api/v1/sales/chat",
        json={"message": "Quanto abbiamo incassato oggi?", "context_date": target_date, "session_id": None},
    )
    assert res3.status_code == 200
    session_id_2 = res3.json()["session_id"]
    assert session_id_2 != session_id_1

    # List all chat sessions -> should return both sessions ordered by updated_at DESC
    list_res = client.get("/api/v1/sales/chats")
    assert list_res.status_code == 200
    sessions_list = list_res.json()
    assert len(sessions_list) == 2
    assert sessions_list[0]["id"] == session_id_2
    assert sessions_list[1]["id"] == session_id_1

    # Reload first chat session -> should return all 4 chronological messages (2 turns)
    detail_res = client.get(f"/api/v1/sales/chats/{session_id_1}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["id"] == session_id_1
    assert len(detail["messages"]) == 4
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
    assert detail["messages"][0]["content"] == "Quante borse abbiamo venduto oggi?"
    assert detail["messages"][2]["content"] == "E ieri invece?"


# ---------------------------------------------------------------------------
# 3. ORPHAN Sales & Per-Column Statistical Comparison Tool
# ---------------------------------------------------------------------------

def test_compute_sales_comparison_stats_orphan_and_column_filter(db_session):
    today = date(2026, 10, 19)  # Monday
    prev_monday = today - timedelta(days=7)  # 2026-10-12 (also Monday)
    tuesday = today - timedelta(days=6)      # 2026-10-13 (Tuesday)

    # Seed historical sales:
    # On prev_monday: 1 CAR (col 5) at 150€, 2 B (col 0) at 100€ each
    _add_excel_sale(db_session, prev_monday, 1, col_idx=5, selling_price=150.0)
    _add_excel_sale(db_session, prev_monday, 2, col_idx=0, selling_price=100.0)
    _add_excel_sale(db_session, prev_monday, 3, col_idx=0, selling_price=100.0)

    # On tuesday: 0 CAR, 1 B (col 0) at 120€
    _add_excel_sale(db_session, tuesday, 1, col_idx=0, selling_price=120.0)

    # On today (Monday): 3 CAR (col 5) all ORPHAN (one of which is an exchange), total 450€
    _add_excel_sale(db_session, today, 1, col_idx=5, selling_price=150.0)
    _add_excel_sale(db_session, today, 2, col_idx=5, selling_price=150.0)
    _add_excel_sale(db_session, today, 3, col_idx=5, selling_price=150.0, is_exchange=True)

    # Query stats specifically for column CAR (index 5) on `today`, with custom baseline = prev_monday..tuesday
    stats = excel_synchronization_repo.compute_sales_comparison_stats(
        db=db_session,
        start_date=today,
        end_date=today,
        excel_column_indices=[5],
        baseline_start_date=prev_monday,
        baseline_end_date=tuesday,
    )

    tp = stats["target_period"]
    assert tp["filtered_columns"] == ["CAR"]
    assert tp["total_sales"] == 3
    assert tp["exchange_count"] == 1
    assert tp["orphan_count"] == 3
    assert tp["reconciled_count"] == 0
    assert tp["total_revenue"] == 450.0

    # General historical average across the 3 active store days (prev_monday, tuesday, today):
    # Total CAR sold across all 3 days = 1 + 0 + 3 = 4 -> 4 / 3 = 1.33 per day
    gen = stats["general_historical_comparison"]
    assert gen["historical_active_days"] == 3
    assert gen["daily_avg_sales"] == 1.33
    assert gen["sales_comparison"]["diff"] == round(3.0 - 1.33, 2)

    # Day-of-Week average across the 2 Mondays (prev_monday, today):
    # Total CAR sold on Mondays = 1 + 3 = 4 -> 4 / 2 = 2.0 per Monday
    dow = stats["dow_historical_comparison"]
    assert dow["historical_dow_days"] == 2
    assert dow["daily_avg_sales"] == 2.0
    assert dow["sales_comparison"]["diff"] == 1.0
    assert dow["sales_comparison"]["diff_pct"] == 50.0

    # Custom baseline [prev_monday..tuesday]: 1 CAR sold across 1 active CAR day
    custom = stats["custom_period_comparison"]
    assert custom is not None
    assert custom["total_sales"] == 1
    assert custom["total_revenue"] == 150.0


# ---------------------------------------------------------------------------
# 4. Hybrid RAG on RECONCILED + ORPHAN Transparency + Color Granularity
# ---------------------------------------------------------------------------

def test_hybrid_sales_agent_reconciled_semantic_and_orphan_transparency(db_session):
    client = TestClient(app)
    today = date(2026, 10, 19)

    # Seed 2 RECONCILED bags sold today:
    # 1) Elegant black bag (embedding [1.0, 0.0, 0.0], color ["black"])
    _, _, _, bp_elegant, art_elegant_black = _seed_catalog_item(
        db_session,
        brand_name="Guess",
        article_name="Guess Luxe Sera",
        embedding=[1.0, 0.0, 0.0],
        colors=["black"],
    )
    # Also create a RED variant of the same blueprint that is still AVAILABLE in warehouse (NOT sold today!)
    art_unsold_red = Article(
        article_blueprint_id=bp_elegant.id,
        batch_id=art_elegant_black.batch_id,
        colors=["red"],
        status=ArticleStatus.AVAILABLE,
    )
    db_session.add(art_unsold_red)
    db_session.commit()

    # 2) Sporty backpack (orthogonal embedding [0.0, 1.0, 0.0], color ["blue"])
    _, _, _, _, art_sporty = _seed_catalog_item(
        db_session,
        brand_name="Guess",
        article_name="Guess Sport Gym",
        embedding=[0.0, 1.0, 0.0],
        colors=["blue"],
    )

    # Record today's ExcelSales in column B (index 0):
    # - 1 reconciled sale of art_elegant_black
    # - 1 reconciled sale of art_sporty
    # - 2 ORPHAN sales in column B not yet reconciled
    sale_elegant = _add_excel_sale(
        db_session, today, 1, col_idx=0, selling_price=190.0,
        status=ExcelSaleStatus.RECONCILED, article_id=art_elegant_black.id,
    )
    _add_excel_sale(
        db_session, today, 2, col_idx=0, selling_price=110.0,
        status=ExcelSaleStatus.RECONCILED, article_id=art_sporty.id,
    )
    _add_excel_sale(db_session, today, 3, col_idx=0, selling_price=95.0, status=ExcelSaleStatus.ORPHAN)
    _add_excel_sale(db_session, today, 4, col_idx=0, selling_price=105.0, status=ExcelSaleStatus.ORPHAN)

    mock_plan = SalesQueryPlan(
        standalone_query="Abbiamo venduto borse eleganti nere oggi?",
        is_pure_chitchat=False,
        start_date="2026-10-19",
        end_date="2026-10-19",
        include_stats_comparison=True,
        excel_columns=["B"],
        brand_name=None,
        category_name="Borse",
        requires_semantic_search=True,
        semantic_query="eleganti nere",
    )

    mock_color_res = SalesColorFilterResponse(
        has_color_constraint=True,
        selections=[
            SaleColorSelection(sale_id=str(sale_elegant.id), selected_colors=["black"]),
        ],
    )

    async def fake_llm_call(self, model_name, system_prompt, prompt, pipeline_stage="unknown", **kwargs):
        if pipeline_stage == "sales_chat_query_plan":
            return mock_plan.model_dump_json()
        if pipeline_stage == "sales_color_filter":
            return mock_color_res.model_dump_json()
        if pipeline_stage == "sales_chat_answer_synthesis":
            # Verify that the prompt passed to answer_synthesis_node contains:
            # - ONLY 1 filtered_reconciled sale (Guess Luxe Sera)
            # - 2 orphan sales in orphan_sales_summary
            assert "Guess Luxe Sera" in prompt
            assert "Guess Sport Gym" not in prompt
            assert '"total_orphan_sales": 2' in prompt
            return (
                "Sì, oggi abbiamo venduto 1 borsa elegante nera riconciliata (Guess Luxe Sera a € 190.00). "
                "Nota che ci sono anche 2 vendite nella colonna Borse (B) non ancora riconciliate."
            )
        return ""

    with patch("src.agents.llm_client.LLMClient.__init__", return_value=None), \
         patch("src.agents.llm_client.LLMClient.call", new=fake_llm_call), \
         patch("src.agents.llm_client.LLMClient.generate_embedding", new=AsyncMock(return_value=[1.0, 0.0, 0.0])):
        res = client.post(
            "/api/v1/sales/chat",
            json={"message": "Abbiamo venduto borse eleganti nere oggi?", "context_date": "2026-10-19"},
        )

    assert res.status_code == 200
    reply = res.json()["reply"]
    assert "Guess Luxe Sera" in reply
    assert "2 vendite nella colonna Borse" in reply


def test_color_granularity_excludes_unsold_variant_color(db_session):
    """
    If a blueprint has both 'black' (SOLD today) and 'red' (AVAILABLE in warehouse, NOT sold today),
    searching for 'rosse' on today's sales must only see ['black'] on the sold article and return 0 matches.
    """
    today = date(2026, 10, 19)
    _, _, _, bp, art_sold_black = _seed_catalog_item(
        db_session,
        brand_name="Furla",
        article_name="Furla Metropolis",
        embedding=[1.0, 0.0, 0.0],
        colors=["black"],
    )
    # Add unsold red variant of the same blueprint
    art_unsold_red = Article(
        article_blueprint_id=bp.id,
        batch_id=art_sold_black.batch_id,
        colors=["red"],
        status=ArticleStatus.AVAILABLE,
    )
    db_session.add(art_unsold_red)
    db_session.commit()

    sale_black = _add_excel_sale(
        db_session, today, 1, col_idx=0, selling_price=220.0,
        status=ExcelSaleStatus.RECONCILED, article_id=art_sold_black.id,
    )

    candidates_data = excel_synchronization_repo.get_sales_candidates_by_plan(
        db=db_session,
        start_date=today,
        end_date=today,
        excel_column_indices=[0],
    )
    rec = candidates_data["reconciled_candidates"]
    assert len(rec) == 1
    assert rec[0]["sale_id"] == str(sale_black.id)
    # Crucial check: available_colors only has ['black'] (the sold article's color), NOT ['black', 'red']
    assert rec[0]["available_colors"] == ["black"]


def test_vendite_page_chat_resizer_and_scrollbar_ui(db_session):
    """Verify that /vendite renders the panel resizer handle, chat toolbar, rounded input box, and markdown/scroll helpers."""
    client = TestClient(app)
    page_res = client.get("/vendite")
    assert page_res.status_code == 200
    assert 'id="work-panel-resizer"' in page_res.text
    assert 'id="chat-session-select"' in page_res.text
    assert 'id="btn-new-chat"' in page_res.text
    assert 'class="chat-input-box"' in page_res.text
    assert 'class="chat-send-btn"' in page_res.text
    assert "<svg" in page_res.text

    css_res = client.get("/static/css/sales_table.css")
    assert css_res.status_code == 200
    assert "--work-panel-width" in css_res.text
    assert ".work-panel__resizer" in css_res.text
    assert ".chat-messages::-webkit-scrollbar" in css_res.text
    assert "scrollbar-width: thin" in css_res.text
    assert ".chat-input-box" in css_res.text
    assert ".chat-send-btn" in css_res.text
    assert "max-height: 200px" in css_res.text

    js_res = client.get("/static/js/sales_page.js")
    assert js_res.status_code == 200
    assert "isExternalInputFocused" in js_res.text
    assert "shouldFocus = false" in js_res.text
    assert "activeEl.setSelectionRange" in js_res.text
    assert "document.activeElement.tagName === 'TEXTAREA'" in js_res.text

    chat_js_res = client.get("/static/js/sales_chat.js")
    assert chat_js_res.status_code == 200
    assert "MAX_TEXTAREA_LINES = 10" in chat_js_res.text
    assert "autoResizeChatInput" in chat_js_res.text
    assert "scrollChatToBottom" in chat_js_res.text
    assert "export function renderMarkdown" in chat_js_res.text


def test_multi_category_column_reconciled_mismatch_and_broken_article_tracking(db_session):
    """
    Reproduces the 'portachiavi' in column PP (col_idx=3) edge case:
    - Reconciled sales in column PP that actually belong to 'Borse' or 'Portafogli' (or have a deleted Article FK)
      must NOT silently disappear or be miscounted as ORPHAN.
    - They must appear in `other_reconciled_in_columns` with their exact dates, prices, and mismatch reasons.
    - `total_reconciled_in_scope` and `total_orphan_sales` must match `compute_sales_comparison_stats` 100%.
    - `orphan_summary` must include `sales_by_date` with exact dates.
    """
    import uuid as _uuid

    d_sep30 = date(2026, 9, 30)
    d_oct01 = date(2026, 10, 1)
    d_oct05 = date(2026, 10, 5)
    d_oct12 = date(2026, 10, 12)

    # Create 'Portachiavi' category in taxonomy (with 0 sold items)
    cat_keychain = Category(name="Portachiavi", description="Portachiavi")
    db_session.add(cat_keychain)
    db_session.commit()

    # Seed 1 Handbag (Borse -> Hobo) and 1 Wallet (Portafogli -> Continental)
    _, _, _, _, art_bag = _seed_catalog_item(
        db_session,
        brand_name="Guess",
        macro_cat_name="Borse",
        sub_cat_name="Hobo",
        article_name="Guess EMELIE LOGO TOP ZIP SHLDR BAG",
        embedding=[1.0, 0.0, 0.0],
        colors=["brown"],
    )
    _, _, _, _, art_wallet = _seed_catalog_item(
        db_session,
        brand_name="Guess",
        macro_cat_name="Portafogli",
        sub_cat_name="Continental / Zip-Around",
        article_name="LAUREL II SLG ZIP ARND CRD",
        embedding=[0.0, 1.0, 0.0],
        colors=["black"],
    )

    # Record 3 RECONCILED sales in column PP (col_idx=3):
    # 1) Bag on 2026-09-30
    _add_excel_sale(
        db_session, d_sep30, 1, col_idx=3, selling_price=40.0, starting_price=40.0,
        status=ExcelSaleStatus.RECONCILED, article_id=art_bag.id, raw_code="SYNTH-3-BAG",
    )
    # 2) Wallet on 2026-09-30
    _add_excel_sale(
        db_session, d_sep30, 2, col_idx=3, selling_price=30.0, starting_price=30.0,
        status=ExcelSaleStatus.RECONCILED, article_id=art_wallet.id, raw_code="SYNTH-3-WAL",
    )
    # 3) Reconciled sale with missing article on 2026-10-01
    _add_excel_sale(
        db_session, d_oct01, 1, col_idx=3, selling_price=30.0, starting_price=30.0,
        status=ExcelSaleStatus.RECONCILED, article_id=None, raw_code="SYNTH-3-MISSING",
    )

    # Record 2 ORPHAN sales in column PP (col_idx=3)
    _add_excel_sale(
        db_session, d_oct05, 1, col_idx=3, selling_price=50.0, starting_price=60.0,
        status=ExcelSaleStatus.ORPHAN, raw_code="SYNTH-3-ORP1",
    )
    _add_excel_sale(
        db_session, d_oct12, 1, col_idx=3, selling_price=70.0, starting_price=70.0,
        status=ExcelSaleStatus.ORPHAN, is_exchange=True, raw_code="SYNTH-3-ORP2",
    )

    candidates_data = excel_synchronization_repo.get_sales_candidates_by_plan(
        db=db_session,
        start_date=date(2026, 9, 19),
        end_date=date(2026, 10, 18),
        excel_column_indices=[3],
        category_name="Portachiavi",
    )
    stats_data = excel_synchronization_repo.compute_sales_comparison_stats(
        db=db_session,
        start_date=date(2026, 9, 19),
        end_date=date(2026, 10, 18),
        excel_column_indices=[3],
    )

    # 0 items match Portachiavi, but all 3 reconciled sales in PP are tracked in other_reconciled_in_columns
    assert candidates_data["reconciled_candidates"] == []
    other_rec = candidates_data["other_reconciled_in_columns"]
    assert len(other_rec) == 3

    # Exact alignment between get_sales_candidates_by_plan and compute_sales_comparison_stats
    assert candidates_data["total_reconciled_in_scope"] == stats_data["target_period"]["reconciled_count"] == 3
    assert candidates_data["orphan_summary"]["total_orphan_sales"] == stats_data["target_period"]["orphan_count"] == 2
    assert candidates_data["total_sales_in_scope"] == stats_data["target_period"]["total_sales"] == 5

    # Verify exact dates and details in other_reconciled_in_columns
    other_dates = [r["date"] for r in other_rec]
    assert other_dates == ["2026-09-30", "2026-09-30", "2026-10-01"]
    assert other_rec[0]["article_name"] == "Guess EMELIE LOGO TOP ZIP SHLDR BAG"
    assert "Borse (Hobo)" in other_rec[0]["mismatch_reason"]
    assert other_rec[1]["article_name"] == "LAUREL II SLG ZIP ARND CRD"
    assert "Portafogli (Continental / Zip-Around)" in other_rec[1]["mismatch_reason"]
    assert other_rec[2]["article_name"] is None
    assert "non è più presente nell'anagrafica" in other_rec[2]["mismatch_reason"]

    # Verify per-date breakdown on orphan_summary
    pp_bucket = candidates_data["orphan_summary"]["by_column"][0]
    assert pp_bucket["header"] == "PP"
    assert pp_bucket["sales_by_date"] == [
        {"date": "2026-10-05", "count": 1, "exchange_count": 0, "total_revenue": 50.0},
        {"date": "2026-10-12", "count": 1, "exchange_count": 1, "total_revenue": 70.0},
    ]


def test_chat_endpoint_passes_other_reconciled_in_columns_to_synthesis_node(db_session):
    """
    End-to-end verification that when the user asks about 'portachiavi' (or asks for the dates/details
    of reconciled sales in column PP), `answer_synthesis_node` receives `other_reconciled_in_columns`
    with the real dates and article names so it never hallucinates missing details or fake dates.
    """
    client = TestClient(app)
    d_sep30 = date(2026, 9, 30)
    d_oct01 = date(2026, 10, 1)

    cat_keychain = Category(name="Portachiavi", description="Portachiavi")
    db_session.add(cat_keychain)
    db_session.commit()

    _, _, _, _, art_bag = _seed_catalog_item(
        db_session,
        brand_name="Guess",
        macro_cat_name="Borse",
        sub_cat_name="Hobo",
        article_name="Guess EMELIE LOGO TOP ZIP SHLDR BAG",
        embedding=[1.0, 0.0, 0.0],
        colors=["brown"],
    )
    _add_excel_sale(
        db_session, d_sep30, 1, col_idx=3, selling_price=40.0,
        status=ExcelSaleStatus.RECONCILED, article_id=art_bag.id,
    )
    _add_excel_sale(
        db_session, d_oct01, 1, col_idx=3, selling_price=55.0,
        status=ExcelSaleStatus.ORPHAN,
    )

    mock_plan = SalesQueryPlan(
        standalone_query="Elenca le vendite riconciliate nella colonna PP nell'ultimo mese con dettaglio e data.",
        is_pure_chitchat=False,
        start_date="2026-09-19",
        end_date="2026-10-18",
        include_stats_comparison=False,
        excel_columns=["PP"],
        brand_name=None,
        category_name="Portachiavi",
        requires_semantic_search=False,
        semantic_query=None,
    )

    async def fake_llm_call(self, model_name, system_prompt, prompt, pipeline_stage="unknown", **kwargs):
        if pipeline_stage == "sales_chat_query_plan":
            return mock_plan.model_dump_json()
        if pipeline_stage == "sales_chat_answer_synthesis":
            assert '"other_reconciled_in_columns"' in prompt
            assert "2026-09-30" in prompt
            assert "Guess EMELIE LOGO TOP ZIP SHLDR BAG" in prompt
            assert "2026-10-01" in prompt
            return (
                "Nella colonna PP non risultano portachiavi riconciliati; l'unica vendita riconciliata "
                "del periodo è del 2026-09-30 e riguarda la borsa Guess EMELIE LOGO TOP ZIP SHLDR BAG (€ 40,00)."
            )
        return ""

    with patch("src.agents.llm_client.LLMClient.__init__", return_value=None), \
         patch("src.agents.llm_client.LLMClient.call", new=fake_llm_call):
        res = client.post(
            "/api/v1/sales/chat",
            json={
                "message": "Puoi elencarmi le vendite associate ad un articolo con data e dettaglio?",
                "context_date": "2026-10-19",
            },
        )

    assert res.status_code == 200
    body = res.json()
    assert "2026-09-30" in body["reply"]
    assert "Guess EMELIE LOGO TOP ZIP SHLDR BAG" in body["reply"]


def test_report_sales_chat_message_success_and_validation(db_session):
    """
    Verify POST /api/v1/sales/chat/report:
      - Valid message_id + non-empty reason flags the message as reported.
      - Database stores is_reported=True and report_reason=<reason>.
      - Empty reason returns 400 Bad Request.
      - Missing or empty message_id returns 400 Bad Request.
      - Unknown message_id returns 404 Not Found.
    """
    from src.models.sales import SalesChatSession, SalesChatMessage
    client = TestClient(app)

    session_obj = SalesChatSession(title="Chat di prova")
    db_session.add(session_obj)
    db_session.commit()

    msg = SalesChatMessage(
        session_id=str(session_obj.id),
        role="assistant",
        content="Abbiamo venduto 10 borse.",
    )
    db_session.add(msg)
    db_session.commit()

    assert msg.is_reported is False
    assert msg.report_reason is None

    # 1. Success reporting
    res = client.post(
        "/api/v1/sales/chat/report",
        json={
            "message_id": str(msg.id),
            "reason": "Il conteggio riporta 10 ma in realtà erano 7.",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["message_id"] == str(msg.id)
    assert data["is_reported"] is True
    assert data["report_reason"] == "Il conteggio riporta 10 ma in realtà erano 7."

    db_session.refresh(msg)
    assert msg.is_reported is True
    assert msg.report_reason == "Il conteggio riporta 10 ma in realtà erano 7."

    # 2. Validation: empty reason
    res_empty_reason = client.post(
        "/api/v1/sales/chat/report",
        json={"message_id": str(msg.id), "reason": "   "},
    )
    assert res_empty_reason.status_code == 400
    assert "motivo della segnalazione non può essere vuoto" in res_empty_reason.json()["detail"]

    # 3. Validation: empty message_id
    res_empty_id = client.post(
        "/api/v1/sales/chat/report",
        json={"message_id": "  ", "reason": "Qualche motivo"},
    )
    assert res_empty_id.status_code == 400
    assert "ID del messaggio non può essere vuoto" in res_empty_id.json()["detail"]

    # 4. Validation: unknown message_id
    res_404 = client.post(
        "/api/v1/sales/chat/report",
        json={"message_id": "00000000-0000-0000-0000-000000000000", "reason": "Messaggio inesistente"},
    )
    assert res_404.status_code == 404
    assert "non trovato" in res_404.json()["detail"]


def test_sales_chat_returns_message_id_and_chat_detail_exposes_reporting_fields(db_session):
    """
    Verify that POST /api/v1/sales/chat returns message_id and GET /api/v1/sales/chats/{id}
    exposes is_reported and report_reason on each message.
    """
    from src.models.sales import SalesChatSession, SalesChatMessage
    client = TestClient(app)

    mock_plan = SalesQueryPlan(
        standalone_query="Ciao",
        is_pure_chitchat=True,
        start_date="2026-10-19",
        end_date="2026-10-19",
    )

    async def fake_llm(self, model_name, system_prompt, prompt, pipeline_stage="unknown", **kwargs):
        if pipeline_stage == "sales_chat_query_plan":
            return mock_plan.model_dump_json()
        if pipeline_stage == "sales_chat_answer_synthesis":
            return "Ciao! Come posso aiutarti oggi?"
        return ""

    with patch("src.agents.llm_client.LLMClient.__init__", return_value=None), \
         patch("src.agents.llm_client.LLMClient.call", new=fake_llm):
        res = client.post(
            "/api/v1/sales/chat",
            json={"message": "Ciao Gemini!", "context_date": "2026-10-19"},
        )

    assert res.status_code == 200
    data = res.json()
    assert "message_id" in data
    assert data["message_id"] is not None
    msg_id = data["message_id"]
    sess_id = data["session_id"]

    # Verify session detail exposes the reporting fields
    detail_res = client.get(f"/api/v1/sales/chats/{sess_id}")
    assert detail_res.status_code == 200
    detail_data = detail_res.json()
    assistant_msgs = [m for m in detail_data["messages"] if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["id"] == msg_id
    assert assistant_msgs[0]["is_reported"] is False
    assert assistant_msgs[0]["report_reason"] is None


def test_sales_chat_ui_copy_and_report_elements(db_session):
    """
    Verify that /vendite HTML, CSS, and JS include the copy and report buttons,
    discreet grey styling, and frontend action handlers.
    """
    client = TestClient(app)

    # 1. HTML assertions
    page_res = client.get("/vendite")
    assert page_res.status_code == 200
    assert 'class="chat-action-btn btn-chat-copy"' in page_res.text
    assert 'class="chat-action-btn btn-chat-report"' in page_res.text
    assert 'title="Copia negli appunti"' in page_res.text
    assert 'title="Segnala messaggio"' in page_res.text
    assert '/static/assets/google-gemini-icon.webp' in page_res.text
    assert 'id="btn-chat-history"' in page_res.text
    assert 'id="chat-history-popup"' in page_res.text
    assert 'id="chat-history-list"' in page_res.text

    # 2. CSS assertions
    css_res = client.get("/static/css/sales_table.css")
    assert css_res.status_code == 200
    assert ".chat-msg-group" in css_res.text
    assert ".chat-bubble-actions" in css_res.text
    assert ".chat-action-btn" in css_res.text
    assert ".chat-action-btn.is-copied" in css_res.text
    assert ".chat-action-btn.is-reported" in css_res.text
    assert ".chat-toolbar__brand" in css_res.text
    assert ".chat-brand-logo" in css_res.text
    assert "width: 33px;" in css_res.text
    assert "height: 33px;" in css_res.text
    assert ".chat-header-btn" in css_res.text
    assert ".chat-history-popup" in css_res.text
    assert ".chat-history-item" in css_res.text

    # 3. JS assertions
    js_res = client.get("/static/js/sales_chat.js")
    assert js_res.status_code == 200
    assert "createChatActionButtons" in js_res.text
    assert "promptReportReason" in js_res.text
    assert "handleCopyMessage" in js_res.text
    assert "handleReportMessage" in js_res.text
    assert "/api/v1/sales/chat/report" in js_res.text
    assert "openChatHistoryPopup" in js_res.text
    assert "renderChatHistoryList" in js_res.text
    assert "btn-chat-history" in js_res.text
    assert "simulateStreamingText" in js_res.text


def test_sales_ui_and_sidebar_refinements(db_session):
    """
    Test the UI and prompt refinements:
    1. System prompt forbids repetitive greetings (Buongiorno, etc.)
    2. Datepicker calendar indicator is forced to white in CSS
    3. Excel sync indicator displays green dot by default and reveals sync text on hover
    4. Sidebar has admin chip and settings button inverted
    5. Sidebar hamburger toggle button is removed
    6. Chat toolbar header is aligned with top page elements
    """
    client = TestClient(app)

    # 1. System prompt check
    from src.agents.sales_chat_agent.nodes.answer_synthesis_node import _SYSTEM_PROMPT
    assert "NON iniziare MAI la risposta con formule di saluto" in _SYSTEM_PROMPT
    assert "Buongiorno" in _SYSTEM_PROMPT

    # 2. Vendite HTML page check
    res = client.get("/vendite")
    assert res.status_code == 200
    html = res.text

    # Excel sync indicator has green dot and sync-text (hidden by default via CSS, revealed on hover)
    assert 'id="syncIndicator"' in html
    assert 'class="sync-dot"' in html
    assert 'class="sync-text"' in html

    # Hamburger menu toggle button is removed
    assert 'id="btn-sidebar-toggle"' not in html

    # Sidebar bottom has settings before chip--admin (inverted)
    settings_idx = html.find('href="/settings"')
    admin_chip_idx = html.find('class="chip chip--admin"')
    assert settings_idx != -1 and admin_chip_idx != -1
    assert settings_idx < admin_chip_idx

    # 3. CSS check for datepicker white icon, sync hover reveal, and chat header alignment
    css_res = client.get("/static/css/sales_table.css")
    assert css_res.status_code == 200
    assert "#date-picker::-webkit-calendar-picker-indicator" in css_res.text
    assert "filter: brightness(0) invert(1);" in css_res.text
    assert ".sync-indicator .sync-text" in css_res.text
    assert ".sync-indicator:hover .sync-text" in css_res.text
    assert "padding: var(--space-md) var(--space-md) 0 var(--space-md);" in css_res.text
    assert "min-height: 48px;" in css_res.text

    # 4. JS check for sync tooltip title logic
    js_res = client.get("/static/js/sales_page.js")
    assert js_res.status_code == 200
    assert "container.setAttribute('title', titleText)" in js_res.text


def test_markdown_inline_code_placeholder_not_corrupted_by_italic(db_session):
    """
    Regression test for reported bug:
    Inline code spans (`ORPHAN`) must not use a placeholder token containing
    underscores (like @@INLINE_CODE_0@@) that gets mutated by the _italic_ regex
    into @@INLINE<em>CODE</em>0@@ before restoration.
    """
    client = TestClient(app)
    js_res = client.get("/static/js/sales_chat.js")
    assert js_res.status_code == 200
    js_text = js_res.text

    assert "@@INLINE_CODE_" not in js_text
    assert "@@CODE_BLOCK_" not in js_text
    assert "@@INLINECODE${codeSpans.length}@@" in js_text
    assert "@@INLINECODE${idx}@@" in js_text
    assert r"(^|[^\w_])_([^_]+)_(?![\w_])" in js_text
