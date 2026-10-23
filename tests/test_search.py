"""
tests/test_search.py
--------------------
Comprehensive test suite for the redesigned warehouse search pipeline:
  - Vector cosine similarity utility
  - Count available stock helper
  - Tier 1 exact code short-circuit (EAN, supplier_code, prefix, wildcard #)
  - Relational filtering & macro-category hierarchy expansion
  - Deterministic keyword AND matching (whole-word boundary, singular/plural, diacritics folding)
  - Strict zero-results policy without fallback relaxation
  - Two-phase color variant filtering (memo cache + dictionary + index-based LLM)
  - Multi-criteria sorting (date, category, brand, relevance)
  - Server-side pagination at 50 items/page and 200-result capping
  - Snapshot caching with transparent re-sorting
"""
import datetime
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from src.models.pim import Brand, Category, ArticleBlueprint
from src.models.wms import Article, ArticleStatus, Batch, Supplier
from src.schemas.pim import BrandCreate
from src.repositories.base import BaseRepository
from src.agents.warehouse_search_agent.state import (
    SearchQueryPlan,
    ColorIndexResponse,
    WarehouseSearchGraphState,
)
from src.utils.vector_math import cosine_similarity
from src.services.search_service import _count_available_stock, _cosine_similarity
from src.core.cache import search_snapshot_cache, query_plan_cache, color_decision_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_brand(db, name="SearchBrand"):
    return BaseRepository(Brand).create(db, obj_in=BrandCreate(name=name))


def _make_category(db, name="Borse", description="Macro categoria borse", parent_id=None):
    cat = Category(name=name, description=description, parent_id=parent_id)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


def _make_blueprint(
    db,
    brand_id,
    article_name="Test Article",
    tags=None,
    materials=None,
    embedding=None,
    description="Desc",
    extended_description="Extended desc",
    category_id=None,
    normalized_vendor_code=None,
):
    bp = ArticleBlueprint(
        brand_id=brand_id,
        category_id=category_id,
        normalized_vendor_code=normalized_vendor_code,
        article_name=article_name,
        description=description,
        extended_description=extended_description,
        tags=tags or ["test"],
        materials=materials or ["cotton"],
        embedding=embedding,
    )
    db.add(bp)
    db.commit()
    db.refresh(bp)
    return bp


def _make_batch(db, company_name="TestSupplier", delivery_note="DN001"):
    supplier = Supplier(company_name=company_name)
    db.add(supplier)
    db.flush()
    batch = Batch(
        supplier_id=supplier.id,
        delivery_note_number=delivery_note,
        document_date=datetime.date.today(),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def _make_article(
    db,
    blueprint_id,
    batch_id,
    ean=None,
    supplier_code=None,
    colors=None,
    status=ArticleStatus.AVAILABLE,
):
    art = Article(
        article_blueprint_id=blueprint_id,
        batch_id=batch_id,
        ean=ean,
        supplier_code=supplier_code,
        colors=colors or ["black"],
        status=status,
    )
    db.add(art)
    db.commit()
    db.refresh(art)
    return art


# ---------------------------------------------------------------------------
# Cosine Similarity utility
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-6
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        v1 = [1.0, 0.0]
        v2 = [0.0, 1.0]
        assert abs(cosine_similarity(v1, v2)) < 1e-6

    def test_opposite_vectors(self):
        v1 = [1.0, 0.0]
        v2 = [-1.0, 0.0]
        assert abs(cosine_similarity(v1, v2) - (-1.0)) < 1e-6

    def test_zero_vector_returns_zero(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
        assert cosine_similarity([], [1.0]) == 0.0
        assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0

    def test_similarity_in_range(self):
        sim = cosine_similarity([0.5, 0.5], [0.8, 0.2])
        assert -1.0 <= sim <= 1.0


# ---------------------------------------------------------------------------
# Count available stock
# ---------------------------------------------------------------------------

class TestCountAvailableStock:
    def test_counts_only_available(self, db_session):
        brand = _make_brand(db_session, "StockBrand")
        bp = _make_blueprint(db_session, brand.id)
        batch = _make_batch(db_session)
        _make_article(db_session, bp.id, batch.id, status=ArticleStatus.AVAILABLE)
        _make_article(db_session, bp.id, batch.id, status=ArticleStatus.AVAILABLE)
        _make_article(db_session, bp.id, batch.id, status=ArticleStatus.SOLD)

        stock = _count_available_stock(db_session, bp.id)
        assert stock == 2

    def test_zero_stock_for_empty_blueprint(self, db_session):
        brand = _make_brand(db_session, "EmptyBrand")
        bp = _make_blueprint(db_session, brand.id)
        assert _count_available_stock(db_session, bp.id) == 0


# ---------------------------------------------------------------------------
# Tier 1: Short-circuit by EAN / Supplier Code / Normalized Vendor Code
# ---------------------------------------------------------------------------

class TestExactCodeShortCircuit:
    @pytest.mark.asyncio
    async def test_short_circuit_by_ean_and_supplier_code(self, db_session):
        brand = _make_brand(db_session, "CodeBrand")
        bp = _make_blueprint(db_session, brand.id, article_name="Code Article", normalized_vendor_code="NV123")
        batch = _make_batch(db_session)
        _make_article(db_session, bp.id, batch.id, ean="8001234567890", supplier_code="SUP-999", colors=["black"])

        from src.services import search_service

        # By EAN
        res_ean = await search_service.execute_search(db_session, "8001234567890")
        assert len(res_ean["results"]) == 1
        assert res_ean["results"][0]["blueprint_id"] == str(bp.id)
        assert res_ean["results"][0]["barcode"] == "8001234567890"

        # By Supplier Code
        res_sup = await search_service.execute_search(db_session, "SUP-999")
        assert len(res_sup["results"]) == 1
        assert res_sup["results"][0]["blueprint_id"] == str(bp.id)

        # By Normalized Vendor Code
        res_nvc = await search_service.execute_search(db_session, "NV123")
        assert len(res_nvc["results"]) == 1
        assert res_nvc["results"][0]["blueprint_id"] == str(bp.id)

    @pytest.mark.asyncio
    async def test_wildcard_hash_code_matching(self, db_session):
        brand = _make_brand(db_session, "WildcardBrand")
        bp = _make_blueprint(
            db_session,
            brand.id,
            article_name="Wildcard Model",
            normalized_vendor_code="048519#",
        )
        batch = _make_batch(db_session)
        _make_article(db_session, bp.id, batch.id, supplier_code="04851901", colors=["cuoio"])

        from src.services import search_service
        res = await search_service.execute_search(db_session, "048519")
        assert len(res["results"]) == 1
        assert res["results"][0]["blueprint_id"] == str(bp.id)

    @pytest.mark.asyncio
    async def test_supplier_code_prefix_matching(self, db_session):
        brand = _make_brand(db_session, "PrefixBrand")
        bp = _make_blueprint(db_session, brand.id, article_name="Prefix Model")
        batch = _make_batch(db_session)
        _make_article(db_session, bp.id, batch.id, supplier_code="V90234X")

        from src.services import search_service
        res = await search_service.execute_search(db_session, "V902")
        assert len(res["results"]) == 1
        assert res["results"][0]["blueprint_id"] == str(bp.id)


# ---------------------------------------------------------------------------
# Category Hierarchy & Relational Filters
# ---------------------------------------------------------------------------

class TestCategoryHierarchyAndStaticSqlPlan:
    @pytest.mark.asyncio
    async def test_static_sql_plan_with_category_hierarchy_expansion(self, db_session):
        brand = _make_brand(db_session, "HierarchyBrand")
        cat_macro = _make_category(db_session, name="Borse", parent_id=None)
        cat_sub = _make_category(db_session, name="Borse a tracolla", parent_id=cat_macro.id)
        cat_other = _make_category(db_session, name="Zaini", parent_id=None)

        bp_macro = _make_blueprint(db_session, brand.id, article_name="Macro Bag", category_id=cat_macro.id)
        bp_sub = _make_blueprint(db_session, brand.id, article_name="Sub Bag", category_id=cat_sub.id)
        bp_other = _make_blueprint(db_session, brand.id, article_name="Backpack", category_id=cat_other.id)

        mock_plan = SearchQueryPlan(
            brand=brand.name,
            category="Borse",
        )

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM:
            client = MagicMock()
            client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = client

            from src.services import search_service
            result = await search_service.execute_search(db_session, "borse HierarchyBrand")

        returned_ids = {r["blueprint_id"] for r in result["results"]}
        assert str(bp_macro.id) in returned_ids
        assert str(bp_sub.id) in returned_ids
        assert str(bp_other.id) not in returned_ids


# ---------------------------------------------------------------------------
# Strict Zero-Results Policy (NO FALLBACK)
# ---------------------------------------------------------------------------

class TestStrictNoFallback:
    @pytest.mark.asyncio
    async def test_zero_matching_items_returns_empty_with_interpreted_plan(self, db_session):
        """
        When brand and category have 0 matching items (e.g. searching 'borsa the bridge'
        when The Bridge only has wallets in DB), system must return 0 results and explain
        the interpreted plan. It must NEVER fallback to cross-category items!
        """
        brand_bridge = _make_brand(db_session, "The Bridge")
        cat_borse = _make_category(db_session, name="Borse")
        cat_portafogli = _make_category(db_session, name="Portafogli")

        # The Bridge only has wallets
        _make_blueprint(db_session, brand_bridge.id, article_name="The Bridge Portafoglio", category_id=cat_portafogli.id)

        mock_plan = SearchQueryPlan(
            brand="The Bridge",
            category="Borse",
        )

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM:
            client = MagicMock()
            client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = client

            from src.services import search_service
            result = await search_service.execute_search(db_session, "borsa the bridge")

        assert len(result["results"]) == 0
        assert result["total_items"] == 0
        assert result["interpreted_plan"] is not None
        assert result["interpreted_plan"]["brand"] == "The Bridge"
        assert result["interpreted_plan"]["category"] == "Borse"
        assert "Nessun articolo trovato" in result["message"]
        assert "The Bridge" in result["message"]
        assert "Borse" in result["message"]


# ---------------------------------------------------------------------------
# Keyword AND Matching & Relevance
# ---------------------------------------------------------------------------

class TestKeywordLexicalSearchMode:
    @pytest.mark.asyncio
    async def test_keyword_and_matching_and_whole_word(self, db_session):
        brand = _make_brand(db_session, "Guess")
        cat = _make_category(db_session, name="Borse")

        # Blueprint with 'borsa' and 'tracolla'
        bp_match = _make_blueprint(
            db_session, brand.id,
            article_name="Borsa Guess con tracolla",
            category_id=cat.id,
        )
        # Blueprint with 'borsone' (must NOT match whole-word 'borsa')
        _make_blueprint(
            db_session, brand.id,
            article_name="Borsone Guess da viaggio",
            category_id=cat.id,
        )

        mock_plan = SearchQueryPlan(
            brand="Guess",
            category="Borse",
            keywords=["tracolla"],
        )

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM:
            client = MagicMock()
            client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = client

            from src.services import search_service
            result = await search_service.execute_search(db_session, "borsa Guess tracolla")

        assert len(result["results"]) == 1
        assert result["results"][0]["blueprint_id"] == str(bp_match.id)

    @pytest.mark.asyncio
    async def test_singular_plural_inflection_matching(self, db_session):
        brand = _make_brand(db_session, "Piquadro")
        cat = _make_category(db_session, name="Zaini")
        bp = _make_blueprint(db_session, brand.id, article_name="Zaino Porta Computer", category_id=cat.id)

        mock_plan = SearchQueryPlan(
            brand="Piquadro",
            category="Zaini",
            keywords=["computer"],
        )

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM:
            client = MagicMock()
            client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = client

            from src.services import search_service
            # Query in plural 'zaini'
            result = await search_service.execute_search(db_session, "zaini Piquadro computer")

        assert len(result["results"]) == 1
        assert result["results"][0]["blueprint_id"] == str(bp.id)


# ---------------------------------------------------------------------------
# Two-Phase Color Filtering
# ---------------------------------------------------------------------------

class TestTwoPhaseColorFilter:
    @pytest.mark.asyncio
    async def test_dictionary_inclusive_color_match(self, db_session):
        brand = _make_brand(db_session, "The Bridge")
        cat = _make_category(db_session, name="Cartelle")
        bp = _make_blueprint(db_session, brand.id, article_name="Cartella Cuoio", category_id=cat.id)
        batch = _make_batch(db_session)
        _make_article(db_session, bp.id, batch.id, colors=["cuoio"])
        _make_article(db_session, bp.id, batch.id, colors=["nero"])

        mock_plan = SearchQueryPlan(
            brand="The Bridge",
            category="Cartelle",
            color="marrone",
        )

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM:
            client = MagicMock()
            client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = client

            from src.services import search_service
            result = await search_service.execute_search(db_session, "cartella the bridge marrone")
        assert len(result["results"]) == 1
        assert result["results"][0]["colors"] == ["cuoio"]

    @pytest.mark.asyncio
    async def test_index_based_llm_color_filter_with_memo_cache(self, db_session):
        brand = _make_brand(db_session, "Guess")
        cat = _make_category(db_session, name="Borse")
        bp = _make_blueprint(db_session, brand.id, article_name="Guess Bag", category_id=cat.id)
        batch = _make_batch(db_session)
        _make_article(db_session, bp.id, batch.id, colors=["electric sky"])
        _make_article(db_session, bp.id, batch.id, colors=["ruby wine"])

        mock_plan = SearchQueryPlan(
            brand="Guess",
            category="Borse",
            color="blu",
        )
        # Commercial color 'electric sky' -> LLM matches index 0
        mock_color_res = ColorIndexResponse(indices=[0])

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM, \
             patch("src.agents.warehouse_search_agent.nodes.color_filter_node.LLMClient") as MockColorLLM:
            p_client = MagicMock()
            p_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = p_client

            c_client = MagicMock()
            c_client.call = AsyncMock(return_value=mock_color_res.model_dump_json())
            MockColorLLM.return_value = c_client

            from src.services import search_service
            result1 = await search_service.execute_search(db_session, "borsa Guess blu")
            assert len(result1["results"]) == 1
            assert result1["results"][0]["colors"] == ["electric sky"]

            # Second call on same color decision should HIT color_decision_cache
            # Clear snapshot cache to re-evaluate color filter node
            search_snapshot_cache.clear()
            c_client.call.reset_mock()

            result2 = await search_service.execute_search(db_session, "borsa Guess blu")
            assert len(result2["results"]) == 1
            # LLMClient was NOT called a second time due to pair memo
            c_client.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_color_llm_failure_returns_zero_and_degraded_notice(self, db_session):
        brand = _make_brand(db_session, "Guess")
        cat = _make_category(db_session, name="Borse")
        bp = _make_blueprint(db_session, brand.id, article_name="Guess Bag", category_id=cat.id)
        batch = _make_batch(db_session)
        _make_article(db_session, bp.id, batch.id, colors=["obscure colorway xyz"])

        mock_plan = SearchQueryPlan(
            brand="Guess",
            category="Borse",
            color="rosso",
        )

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM, \
             patch("src.agents.warehouse_search_agent.nodes.color_filter_node.LLMClient") as MockColorLLM:
            p_client = MagicMock()
            p_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = p_client

            # LLM raises
            MockColorLLM.side_effect = Exception("LLM timeout")

            from src.services import search_service
            result = await search_service.execute_search(db_session, "borsa Guess rossa")

        assert len(result["results"]) == 0
        assert "filtro colore non ha potuto verificare" in result["message"]


# ---------------------------------------------------------------------------
# Multi-Criteria Sorting & Server-Side Pagination
# ---------------------------------------------------------------------------

class TestSortingAndPagination:
    @pytest.mark.asyncio
    async def test_pagination_and_sorting_by_date_and_brand(self, db_session):
        brand_a = _make_brand(db_session, "AlphaBrand")
        brand_z = _make_brand(db_session, "ZetaBrand")
        cat = _make_category(db_session, name="Borse")
        batch = _make_batch(db_session)

        bp_a = _make_blueprint(db_session, brand_a.id, article_name="Bag Alpha", category_id=cat.id)
        bp_z = _make_blueprint(db_session, brand_z.id, article_name="Bag Zeta", category_id=cat.id)

        _make_article(db_session, bp_a.id, batch.id, colors=["black"])
        _make_article(db_session, bp_z.id, batch.id, colors=["red"])

        mock_plan = SearchQueryPlan(category="Borse")

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM:
            client = MagicMock()
            client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = client

            from src.services import search_service

            # Sort Brand ASC
            res_brand_asc = await search_service.execute_search(
                db_session, "borse", sort_by="brand", sort_dir="asc", page=1, page_size=50
            )
            assert res_brand_asc["results"][0]["brand_name"] == "AlphaBrand"
            assert res_brand_asc["results"][1]["brand_name"] == "ZetaBrand"

            # Sort Brand DESC
            res_brand_desc = await search_service.execute_search(
                db_session, "borse", sort_by="brand", sort_dir="desc", page=1, page_size=50
            )
            assert res_brand_desc["results"][0]["brand_name"] == "ZetaBrand"
            assert res_brand_desc["results"][1]["brand_name"] == "AlphaBrand"

            # Check pagination structure
            assert res_brand_asc["page"] == 1
            assert res_brand_asc["limit"] == 50
            assert res_brand_asc["total_items"] == 2
            assert res_brand_asc["total_pages"] == 1


# ---------------------------------------------------------------------------
# Snapshot Caching
# ---------------------------------------------------------------------------

class TestSearchSnapshotCache:
    @pytest.mark.asyncio
    async def test_snapshot_cached_and_reordered_without_agent(self, db_session):
        brand = _make_brand(db_session, "Guess")
        cat = _make_category(db_session, name="Borse")
        batch = _make_batch(db_session)
        bp1 = _make_blueprint(db_session, brand.id, article_name="Bag 1", category_id=cat.id)
        bp2 = _make_blueprint(db_session, brand.id, article_name="Bag 2", category_id=cat.id)
        _make_article(db_session, bp1.id, batch.id, colors=["nero"])
        _make_article(db_session, bp2.id, batch.id, colors=["bianco"])

        mock_plan = SearchQueryPlan(brand="Guess", category="Borse")

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM:
            client = MagicMock()
            client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = client

            from src.services import search_service

            # First search runs the agent
            res1 = await search_service.execute_search(db_session, "borse guess", sort_by="date", sort_dir="desc")
            assert len(res1["results"]) == 2
            assert client.call.call_count == 1

            # Second search with sort change: must HIT snapshot cache and NOT call agent
            client.call.reset_mock()
            res2 = await search_service.execute_search(db_session, "borse guess", sort_by="date", sort_dir="asc")
            assert len(res2["results"]) == 2
            client.call.assert_not_called()


# ---------------------------------------------------------------------------
# In Stock Only Filter
# ---------------------------------------------------------------------------

class TestInStockOnlyFlag:
    @pytest.mark.asyncio
    async def test_in_stock_only_excludes_sold_variants(self, db_session):
        brand = _make_brand(db_session, "StockTestBrand")
        bp = _make_blueprint(db_session, brand.id, article_name="Stock Article")
        batch = _make_batch(db_session)
        _make_article(db_session, bp.id, batch.id, colors=["black"], status=ArticleStatus.AVAILABLE)
        _make_article(db_session, bp.id, batch.id, colors=["red"], status=ArticleStatus.SOLD)

        mock_plan = SearchQueryPlan(brand=brand.name)

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM:
            client = MagicMock()
            client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = client

            from src.services import search_service

            res_all = await search_service.execute_search(db_session, brand.name, in_stock_only=False)
            assert len(res_all["results"]) == 2

            res_stock = await search_service.execute_search(db_session, brand.name, in_stock_only=True)
            assert len(res_stock["results"]) == 1
            assert res_stock["results"][0]["colors"] == ["black"]


# ---------------------------------------------------------------------------
# Max 200 Results Cap & Truncation
# ---------------------------------------------------------------------------

class TestMaxResultsCap:
    @pytest.mark.asyncio
    async def test_results_capped_at_200_with_truncated_flag(self, db_session):
        brand = _make_brand(db_session, "CapBrand")
        batch = _make_batch(db_session)

        # Create 250 blueprints with 1 article each
        for i in range(250):
            bp = _make_blueprint(db_session, brand.id, article_name=f"Cap Item {i}")
            _make_article(db_session, bp.id, batch.id, colors=["nero"])

        mock_plan = SearchQueryPlan(brand="CapBrand")

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM:
            client = MagicMock()
            client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = client

            from src.services import search_service
            res = await search_service.execute_search(db_session, "CapBrand", page=1, page_size=50)

        assert res["total_items"] == 200
        assert res["truncated"] is True
        assert len(res["results"]) == 50
        assert res["total_pages"] == 4


# ---------------------------------------------------------------------------
# Relevance Scoring
# ---------------------------------------------------------------------------

class TestRelevanceScoring:
    @pytest.mark.asyncio
    async def test_relevance_ranks_name_match_above_description_match(self, db_session):
        brand = _make_brand(db_session, "RelBrand")
        batch = _make_batch(db_session)

        # bp1: keyword in description only
        bp_desc = _make_blueprint(
            db_session, brand.id,
            article_name="Simple Bag",
            description="Bellissima borsa con tracolla regolabile",
        )
        _make_article(db_session, bp_desc.id, batch.id, colors=["nero"])

        # bp2: keyword in article name (higher weight 10 vs 3)
        bp_name = _make_blueprint(
            db_session, brand.id,
            article_name="Borsa Tracolla Deluxe",
            description="Articolo elegante",
        )
        _make_article(db_session, bp_name.id, batch.id, colors=["nero"])

        mock_plan = SearchQueryPlan(
            brand="RelBrand",
            keywords=["tracolla"],
        )

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM:
            client = MagicMock()
            client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = client

            from src.services import search_service
            res = await search_service.execute_search(db_session, "tracolla RelBrand", sort_by="relevance", sort_dir="desc")

        assert len(res["results"]) == 2
        # Name match should be ranked 1st
        assert res["results"][0]["blueprint_id"] == str(bp_name.id)
        assert res["results"][1]["blueprint_id"] == str(bp_desc.id)


# ---------------------------------------------------------------------------
# Text Normalization & Variance Utils
# ---------------------------------------------------------------------------

class TestTextNormalizeUtils:
    def test_fold_text_accents_and_ordinals(self):
        from src.utils.text_normalize import fold_text
        assert fold_text("Alviero Martini 1ª Classe") == "alviero martini 1a classe"
        assert fold_text("Borsà Tràcolla") == "borsa tracolla"
        assert fold_text("Élégant") == "elegant"

    def test_contains_whole_word_prevents_substring_false_positives(self):
        from src.utils.text_normalize import contains_whole_word
        # "borsa" must NOT match "borsone"
        assert contains_whole_word("borsa", "Borsone da viaggio") is False
        assert contains_whole_word("borsa", "Borsa a mano") is True
        # plural/singular variance
        assert contains_whole_word("borse", "Borsa a tracolla") is True
        assert contains_whole_word("borsa", "Due borse eleganti") is True


# ---------------------------------------------------------------------------
# TTL Cache Unit Tests
# ---------------------------------------------------------------------------

class TestTTLCacheUtils:
    def test_ttl_cache_set_get_and_expiration(self):
        from src.core.cache import TTLCache
        import time

        cache = TTLCache(maxsize=10, default_ttl_seconds=0.1)
        cache.set("key1", "val1")
        assert cache.get("key1") == "val1"

        time.sleep(0.15)
        # Expired
        assert cache.get("key1") is None

    def test_ttl_cache_lru_eviction(self):
        from src.core.cache import TTLCache

        cache = TTLCache(maxsize=2, default_ttl_seconds=60.0)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)  # evicts 'a'

        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.get("c") == 3

