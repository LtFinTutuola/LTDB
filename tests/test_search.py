"""
tests/test_search.py
--------------------
Comprehensive test suite for the hybrid warehouse search pipeline:
  - _cosine_similarity utility
  - _count_available_stock helper
  - Short-circuit exact code lookup (EAN, supplier_code, normalized_vendor_code)
  - Static SQL plan execution & macro-category hierarchy expansion
  - Hybrid search primary regime (high threshold 0.65 + semantic_query)
  - Zero-results relaxation fallback regime (low threshold 0.50 + raw_query)
  - Dedicated LLM color variant filtering (color_filter_node)
  - `in_stock_only` filtering
  - Standardized `build_embedding_text` and `build_blueprint_embedding_text`
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
    ColorFilterResponse,
    BlueprintColorSelection,
    WarehouseSearchGraphState,
)


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
        colors=colors,
        status=status,
    )
    db.add(art)
    db.commit()
    db.refresh(art)
    return art


# ---------------------------------------------------------------------------
# _cosine_similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    """Unit tests for the pure-Python cosine similarity function."""

    def test_identical_vectors(self):
        from src.services.search_service import _cosine_similarity
        v = [1.0, 2.0, 3.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-9

    def test_orthogonal_vectors(self):
        from src.services.search_service import _cosine_similarity
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(_cosine_similarity(a, b)) < 1e-9

    def test_opposite_vectors(self):
        from src.services.search_service import _cosine_similarity
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert abs(_cosine_similarity(a, b) + 1.0) < 1e-9

    def test_zero_vector_returns_zero(self):
        from src.services.search_service import _cosine_similarity
        a = [0.0, 0.0]
        b = [1.0, 2.0]
        assert _cosine_similarity(a, b) == 0.0

    def test_similarity_in_range(self):
        from src.services.search_service import _cosine_similarity
        import random
        random.seed(42)
        a = [random.random() for _ in range(128)]
        b = [random.random() for _ in range(128)]
        score = _cosine_similarity(a, b)
        assert -1.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# _count_available_stock
# ---------------------------------------------------------------------------

class TestCountAvailableStock:
    def test_counts_only_available(self, db_session):
        from src.services.search_service import _count_available_stock
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        batch = _make_batch(db_session)

        _make_article(db_session, bp.id, batch.id, status=ArticleStatus.AVAILABLE)
        _make_article(db_session, bp.id, batch.id, status=ArticleStatus.SOLD)
        _make_article(db_session, bp.id, batch.id, status=ArticleStatus.AVAILABLE)

        stock = _count_available_stock(db_session, bp.id)
        assert stock == 2

    def test_zero_stock_for_empty_blueprint(self, db_session):
        from src.services.search_service import _count_available_stock
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        assert _count_available_stock(db_session, bp.id) == 0


# ---------------------------------------------------------------------------
# execute_semantic_search — filter mode (static SQL plan)
# ---------------------------------------------------------------------------

class TestSemanticSearchFilterMode:
    @pytest.mark.asyncio
    async def test_filter_mode_returns_blueprints(self, db_session):
        brand = _make_brand(db_session, "FilterBrand")
        _make_blueprint(
            db_session,
            brand.id,
            article_name="Borsa Classica",
            tags=["pelle", "sera"],
        )

        mock_plan = SearchQueryPlan(
            brand_name="FilterBrand",
            category_name=None,
            requires_semantic_search=False,
            semantic_query=None,
        )

        with patch(
            "src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient"
        ) as MockPlanLLM:
            mock_client = MagicMock()
            mock_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = mock_client

            from src.services import search_service
            result = await search_service.execute_semantic_search(
                db_session, "articoli FilterBrand"
            )

        assert "results" in result
        assert "message" in result
        assert len(result["results"]) == 1
        assert result["results"][0]["brand_name"] == "FilterBrand"

    @pytest.mark.asyncio
    async def test_filter_mode_no_match_returns_empty(self, db_session):
        _make_brand(db_session, "EmptyBrand")

        mock_plan = SearchQueryPlan(
            brand_name="EmptyBrand",
            category_name=None,
            requires_semantic_search=False,
            semantic_query=None,
        )

        with patch(
            "src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient"
        ) as MockPlanLLM:
            mock_client = MagicMock()
            mock_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = mock_client

            from src.services import search_service
            result = await search_service.execute_semantic_search(
                db_session, "qualcosa EmptyBrand"
            )

        assert result["results"] == []
        assert "Nessun" in result["message"]


# ---------------------------------------------------------------------------
# execute_semantic_search — similarity mode
# ---------------------------------------------------------------------------

class TestSemanticSearchSimilarityMode:
    @pytest.mark.asyncio
    async def test_similarity_mode_no_embeddings_returns_empty(self, db_session):
        brand = _make_brand(db_session, "SimilarityBrand")
        _make_blueprint(db_session, brand.id, embedding=None)

        mock_plan = SearchQueryPlan(
            brand_name=None,
            category_name=None,
            requires_semantic_search=True,
            semantic_query="elegante sera",
        )

        with patch(
            "src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient"
        ) as MockPlanLLM, patch(
            "src.agents.warehouse_search_agent.nodes.semantic_rank_node.LLMClient"
        ) as MockEmbedLLM:
            plan_client = MagicMock()
            plan_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = plan_client

            embed_client = MagicMock()
            embed_client.generate_embedding = AsyncMock(return_value=[1.0, 0.0, 0.0])
            MockEmbedLLM.return_value = embed_client

            from src.services import search_service
            result = await search_service.execute_semantic_search(
                db_session, "qualcosa di elegante"
            )

        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_similarity_mode_with_embeddings_returns_results(self, db_session):
        brand = _make_brand(db_session, "EmbedBrand")
        query_vec = [1.0, 0.0, 0.0]
        similar_vec = [0.9, 0.1, 0.0]
        different_vec = [0.0, 0.0, 1.0]

        _make_blueprint(db_session, brand.id, article_name="Similar", embedding=similar_vec)
        _make_blueprint(db_session, brand.id, article_name="Different", embedding=different_vec)

        mock_plan = SearchQueryPlan(
            brand_name=None,
            category_name=None,
            requires_semantic_search=True,
            semantic_query="test query",
        )

        with patch(
            "src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient"
        ) as MockPlanLLM, patch(
            "src.agents.warehouse_search_agent.nodes.semantic_rank_node.LLMClient"
        ) as MockEmbedLLM:
            plan_client = MagicMock()
            plan_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = plan_client

            embed_client = MagicMock()
            embed_client.generate_embedding = AsyncMock(return_value=query_vec)
            MockEmbedLLM.return_value = embed_client

            from src.services import search_service
            result = await search_service.execute_semantic_search(
                db_session, "test query"
            )

        results = result["results"]
        assert len(results) == 1
        assert results[0]["article_name"] == "Similar"


# ---------------------------------------------------------------------------
# Interpret query fallback (query_plan_node resilience)
# ---------------------------------------------------------------------------

class TestInterpretQueryFallback:
    @pytest.mark.asyncio
    async def test_llm_error_falls_back_to_similarity(self, db_session):
        """If LLMClient raises in query_plan_node, it should fall back to pure semantic search."""
        from src.agents.warehouse_search_agent.nodes.query_plan_node import query_plan_node

        state = WarehouseSearchGraphState(
            raw_query="qualcosa di elegante",
            in_stock_only=False,
            brands_catalog=["Guess"],
            categories_catalog={},
        )

        with patch(
            "src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient"
        ) as MockLLM:
            MockLLM.side_effect = Exception("LLM unavailable")
            output = await query_plan_node(state)

        plan = output["query_plan"]
        assert plan.requires_semantic_search is True
        assert plan.semantic_query == "qualcosa di elegante"
        assert plan.brand_name is None
        assert plan.category_name is None


# ---------------------------------------------------------------------------
# Step 1: Short-circuit by EAN / Supplier Code / Normalized Vendor Code
# ---------------------------------------------------------------------------

class TestExactCodeShortCircuit:
    @pytest.mark.asyncio
    async def test_short_circuit_by_ean_and_supplier_code(self, db_session):
        brand = _make_brand(db_session, "Guess")
        bp = _make_blueprint(
            db_session,
            brand.id,
            article_name="Guess Emelie",
            normalized_vendor_code="HWVG84#",
        )
        batch = _make_batch(db_session)

        _make_article(
            db_session,
            bp.id,
            batch.id,
            ean="8059347000111",
            supplier_code="HWVG84-BLK",
            colors=["black"],
            status=ArticleStatus.AVAILABLE,
        )
        _make_article(
            db_session,
            bp.id,
            batch.id,
            ean="8059347000222",
            supplier_code="HWVG84-RED",
            colors=["astral red"],
            status=ArticleStatus.AVAILABLE,
        )

        # Ensure WarehouseSearchAgent is NEVER invoked during short-circuit
        with patch("src.services.search_service.WarehouseSearchAgent") as MockAgent:
            from src.services import search_service

            # 1. Search by exact EAN (only the "black" variant should be returned)
            res_ean = await search_service.execute_semantic_search(db_session, "8059347000111")
            assert len(res_ean["results"]) == 1
            assert res_ean["results"][0]["colors"] == ["black"]
            MockAgent.assert_not_called()

            # 2. Search by exact supplier_code (case-insensitive)
            res_sup = await search_service.execute_semantic_search(db_session, "hwvg84-red")
            assert len(res_sup["results"]) == 1
            assert res_sup["results"][0]["colors"] == ["astral red"]
            MockAgent.assert_not_called()

            # 3. Search by normalized_vendor_code (returns both color variants of the blueprint)
            res_norm = await search_service.execute_semantic_search(db_session, "HWVG84#")
            assert len(res_norm["results"]) == 2
            MockAgent.assert_not_called()


# ---------------------------------------------------------------------------
# Step 2 & 3: Category hierarchy expansion in SQL filter
# ---------------------------------------------------------------------------

class TestCategoryHierarchyAndStaticSqlPlan:
    @pytest.mark.asyncio
    async def test_static_sql_plan_with_category_hierarchy_expansion(self, db_session):
        brand_guess = _make_brand(db_session, "Guess")
        brand_other = _make_brand(db_session, "Samsonite")

        macro_borse = _make_category(db_session, name="Borse", description="Borse donna")
        sub_bauletto = _make_category(
            db_session, name="Bauletto", description="Borsa bauletto", parent_id=macro_borse.id
        )
        sub_tote = _make_category(
            db_session, name="Tote / Shopper", description="Borsa shopping", parent_id=macro_borse.id
        )
        macro_valigie = _make_category(db_session, name="Valigie", description="Bagagli")

        bp1 = _make_blueprint(
            db_session, brand_guess.id, article_name="Guess Bauletto", category_id=sub_bauletto.id
        )
        bp2 = _make_blueprint(
            db_session, brand_guess.id, article_name="Guess Shopper", category_id=sub_tote.id
        )
        _make_blueprint(
            db_session, brand_other.id, article_name="Trolley Samsonite", category_id=macro_valigie.id
        )

        mock_plan = SearchQueryPlan(
            brand_name="Guess",
            category_name="Borse",
            requires_semantic_search=False,
            semantic_query=None,
        )

        with patch(
            "src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient"
        ) as MockPlanLLM:
            plan_client = MagicMock()
            plan_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = plan_client

            from src.services import search_service
            result = await search_service.execute_semantic_search(db_session, "borse di Guess")

        returned_ids = {r["blueprint_id"] for r in result["results"]}
        assert returned_ids == {str(bp1.id), str(bp2.id)}


# ---------------------------------------------------------------------------
# Step 4: Dual-regime semantic ranking & zero-results relaxation fallback
# ---------------------------------------------------------------------------

class TestHybridDualRegimeAndFallback:
    @pytest.mark.asyncio
    async def test_hybrid_search_primary_regime_high_threshold(self, db_session):
        """
        When static SQL filters yield candidates and requires_semantic_search=True,
        only candidates with cosine similarity >= 0.65 (HIGH_THRESHOLD) pass in primary regime,
        and `semantic_query` is passed to generate_embedding.
        """
        brand = _make_brand(db_session, "Guess")
        cat = _make_category(db_session, name="Borse")

        # Cosine similarity with [1.0, 0.0] is 1.0 (>= 0.65) and 0.6 (< 0.65)
        bp_high = _make_blueprint(
            db_session,
            brand.id,
            article_name="Guess Tracolla",
            category_id=cat.id,
            embedding=[1.0, 0.0],
        )
        _make_blueprint(
            db_session,
            brand.id,
            article_name="Guess Pochette",
            category_id=cat.id,
            embedding=[0.6, 0.8],  # dot=0.6, norm=1.0 -> cosine=0.60 < 0.65
        )

        mock_plan = SearchQueryPlan(
            brand_name="Guess",
            category_name="Borse",
            requires_semantic_search=True,
            semantic_query="con la tracolla",
        )

        with patch(
            "src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient"
        ) as MockPlanLLM, patch(
            "src.agents.warehouse_search_agent.nodes.semantic_rank_node.LLMClient"
        ) as MockEmbedLLM:
            plan_client = MagicMock()
            plan_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = plan_client

            embed_client = MagicMock()
            embed_client.generate_embedding = AsyncMock(return_value=[1.0, 0.0])
            MockEmbedLLM.return_value = embed_client

            from src.services import search_service
            result = await search_service.execute_semantic_search(
                db_session, "borsa di Guess con la tracolla"
            )

            embed_client.generate_embedding.assert_called_once_with("con la tracolla")

        assert len(result["results"]) == 1
        assert result["results"][0]["blueprint_id"] == str(bp_high.id)

    @pytest.mark.asyncio
    async def test_zero_results_fallback_relaxes_filters_and_uses_low_threshold(self, db_session):
        """
        When SQL filters return 0 candidates (e.g., brand has no items in that category),
        fallback relaxes SQL filters, embeds the full `raw_query`, and filters with LOW_THRESHOLD (0.50).
        """
        brand_guess = _make_brand(db_session, "Guess")
        brand_bridge = _make_brand(db_session, "The Bridge")
        cat_borse = _make_category(db_session, name="Borse")
        cat_zaini = _make_category(db_session, name="Zaini")

        # The Bridge has a bag with cosine similarity 0.55 (>= 0.50 low threshold, < 0.65 high threshold)
        bp_fallback = _make_blueprint(
            db_session,
            brand_bridge.id,
            article_name="The Bridge Tracolla",
            category_id=cat_borse.id,
            embedding=[0.55, 0.83516],
        )

        # Plan asks for Guess + Zaini (0 SQL candidates in DB)
        mock_plan = SearchQueryPlan(
            brand_name="Guess",
            category_name="Zaini",
            requires_semantic_search=True,
            semantic_query="con la tracolla",
        )

        raw_query = "zaino di Guess con la tracolla"

        with patch(
            "src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient"
        ) as MockPlanLLM, patch(
            "src.agents.warehouse_search_agent.nodes.semantic_rank_node.LLMClient"
        ) as MockEmbedLLM:
            plan_client = MagicMock()
            plan_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = plan_client

            embed_client = MagicMock()
            embed_client.generate_embedding = AsyncMock(return_value=[1.0, 0.0])
            MockEmbedLLM.return_value = embed_client

            from src.services import search_service
            result = await search_service.execute_semantic_search(db_session, raw_query)

            # Must have embedded the full raw_query during fallback
            embed_client.generate_embedding.assert_called_once_with(raw_query)

        assert len(result["results"]) == 1
        assert result["results"][0]["blueprint_id"] == str(bp_fallback.id)


# ---------------------------------------------------------------------------
# Step 5: Dedicated LLM Color Filtering (color_filter_node)
# ---------------------------------------------------------------------------

class TestColorFilterNode:
    @pytest.mark.asyncio
    async def test_color_filter_node_filters_variants(self, db_session):
        brand = _make_brand(db_session, "Guess")
        cat = _make_category(db_session, name="Borse")
        bp = _make_blueprint(
            db_session,
            brand.id,
            article_name="Guess Emelie",
            category_id=cat.id,
            embedding=[1.0, 0.0],
        )
        batch = _make_batch(db_session)
        _make_article(db_session, bp.id, batch.id, colors=["black"], status=ArticleStatus.AVAILABLE)
        _make_article(db_session, bp.id, batch.id, colors=["astral red"], status=ArticleStatus.AVAILABLE)
        _make_article(db_session, bp.id, batch.id, colors=["bone town"], status=ArticleStatus.AVAILABLE)

        mock_plan = SearchQueryPlan(
            brand_name="Guess",
            category_name="Borse",
            requires_semantic_search=False,
            semantic_query="rossa",
        )
        mock_color_res = ColorFilterResponse(
            has_color_constraint=True,
            selections=[
                BlueprintColorSelection(
                    blueprint_id=str(bp.id),
                    selected_colors=["astral red"],
                )
            ],
        )

        with patch(
            "src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient"
        ) as MockPlanLLM, patch(
            "src.agents.warehouse_search_agent.nodes.color_filter_node.LLMClient"
        ) as MockColorLLM:
            plan_client = MagicMock()
            plan_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = plan_client

            color_client = MagicMock()
            color_client.call = AsyncMock(return_value=mock_color_res.model_dump_json())
            MockColorLLM.return_value = color_client

            from src.services import search_service
            result = await search_service.execute_semantic_search(
                db_session, "borsa di Guess rossa"
            )

        assert len(result["results"]) == 1
        assert result["results"][0]["colors"] == ["astral red"]
        assert "Astral red" in result["results"][0]["article_name"]


# ---------------------------------------------------------------------------
# in_stock_only flag
# ---------------------------------------------------------------------------

class TestInStockOnlyFlag:
    @pytest.mark.asyncio
    async def test_in_stock_only_excludes_sold_variants(self, db_session):
        brand = _make_brand(db_session, "The Bridge")
        bp = _make_blueprint(db_session, brand.id, article_name="Story Bauletto")
        batch = _make_batch(db_session)

        _make_article(db_session, bp.id, batch.id, colors=["black"], status=ArticleStatus.AVAILABLE)
        _make_article(db_session, bp.id, batch.id, colors=["brown"], status=ArticleStatus.SOLD)

        mock_plan = SearchQueryPlan(
            brand_name="The Bridge",
            category_name=None,
            requires_semantic_search=False,
            semantic_query=None,
        )

        with patch(
            "src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient"
        ) as MockPlanLLM:
            plan_client = MagicMock()
            plan_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = plan_client

            from src.services import search_service

            # in_stock_only = False -> returns both variants (black with stock=1, brown with stock=0)
            res_all = await search_service.execute_semantic_search(
                db_session, "The Bridge", in_stock_only=False
            )
            assert len(res_all["results"]) == 2

            # in_stock_only = True -> returns only the available "black" variant
            res_stock = await search_service.execute_semantic_search(
                db_session, "The Bridge", in_stock_only=True
            )
            assert len(res_stock["results"]) == 1
            assert res_stock["results"][0]["colors"] == ["black"]
            assert res_stock["results"][0]["stock"] == 1


# ---------------------------------------------------------------------------
# Embedding text standardization
# ---------------------------------------------------------------------------

class TestBuildEmbeddingTextStandardization:
    def test_build_embedding_text_standardization(self, db_session):
        from src.services.pim_service import build_blueprint_embedding_text

        brand = _make_brand(db_session, "Guess")
        macro_cat = _make_category(db_session, name="Borse")
        sub_cat = _make_category(db_session, name="Bauletto", parent_id=macro_cat.id)
        bp = _make_blueprint(
            db_session,
            brand.id,
            category_id=sub_cat.id,
            article_name="Guess Emelie Handbag",
            description="Borsa a mano logata",
            extended_description="Borsa elegante con tracolla rimovibile e zip.",
            tags=["bauletto", "borsa con tracolla"],
            materials=["similpelle", "poliuretano"],
        )
        batch = _make_batch(db_session)
        _make_article(db_session, bp.id, batch.id, colors=["astral red"])

        text = build_blueprint_embedding_text(db_session, bp)
        assert "Brand: Guess" in text
        assert "Categoria: Borse" in text
        assert "Sottocategoria: Bauletto" in text
        assert "Articolo: Guess Emelie Handbag" in text
        assert "Descrizione: Borsa a mano logata" in text
        assert "Dettagli: Borsa elegante con tracolla rimovibile e zip." in text
        assert "bauletto, borsa con tracolla" in text
        assert "similpelle, poliuretano" in text
        # Colors must NOT be in the blueprint embedding text
        assert "astral red" not in text
