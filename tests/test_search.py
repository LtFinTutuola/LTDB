"""
tests/test_search.py
--------------------
Tests for the search_service module:
  - _cosine_similarity utility
  - _build_result_item helper
  - _filter_search with mocked DB data
  - execute_semantic_search with mocked LLMClient (filter and similarity modes)
"""
import pytest
import math
from unittest.mock import patch, AsyncMock, MagicMock

from src.models.pim import Brand, Category, BrandCategory, ArticleBlueprint
from src.models.wms import Article, ArticleStatus
from src.schemas.pim import BrandCreate
from src.schemas.catalog import SearchInterpretation
from src.repositories.base import BaseRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_brand(db, name="SearchBrand"):
    return BaseRepository(Brand).create(db, obj_in=BrandCreate(name=name))


def _make_blueprint(db, brand_id, article_name="Test Article", tags=None,
                    materials=None, embedding=None, description="Desc"):
    bp = ArticleBlueprint(
        brand_id=brand_id,
        article_name=article_name,
        description=description,
        extended_description="Extended desc",
        tags=tags or ["test"],
        materials=materials or ["cotton"],
        embedding=embedding,
    )
    db.add(bp)
    db.commit()
    db.refresh(bp)
    return bp


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

        # Create a minimal batch/supplier chain is complex; we add Articles directly
        # using raw ORM (bypassing full batch creation)
        from src.models.wms import Batch, Supplier
        import datetime
        supplier = Supplier(company_name="S")
        db_session.add(supplier)
        db_session.flush()
        batch = Batch(supplier_id=supplier.id,
                      delivery_note_number="DN001",
                      document_date=datetime.date.today())
        db_session.add(batch)
        db_session.flush()

        a1 = Article(article_blueprint_id=bp.id, batch_id=batch.id,
                     status=ArticleStatus.AVAILABLE)
        a2 = Article(article_blueprint_id=bp.id, batch_id=batch.id,
                     status=ArticleStatus.SOLD)
        a3 = Article(article_blueprint_id=bp.id, batch_id=batch.id,
                     status=ArticleStatus.AVAILABLE)
        db_session.add_all([a1, a2, a3])
        db_session.commit()

        stock = _count_available_stock(db_session, bp.id)
        assert stock == 2

    def test_zero_stock_for_empty_blueprint(self, db_session):
        from src.services.search_service import _count_available_stock
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        assert _count_available_stock(db_session, bp.id) == 0


# ---------------------------------------------------------------------------
# execute_semantic_search — filter mode
# ---------------------------------------------------------------------------

class TestSemanticSearchFilterMode:
    @pytest.mark.asyncio
    async def test_filter_mode_returns_blueprints(self, db_session):
        brand = _make_brand(db_session, "FilterBrand")
        _make_blueprint(db_session, brand.id, article_name="Borsa Rossa",
                        tags=["pelle", "sera"])

        mock_interp = SearchInterpretation(
            mode="filter",
            brand_name="FilterBrand",
            free_text="borsa pelle",
        )

        with patch("src.services.search_service._interpret_query",
                   new_callable=AsyncMock, return_value=mock_interp):
            from src.services import search_service
            result = await search_service.execute_semantic_search(
                db_session, "borsa in pelle FilterBrand"
            )

        assert "results" in result
        assert "message" in result
        assert isinstance(result["results"], list)

    @pytest.mark.asyncio
    async def test_filter_mode_no_match_returns_empty(self, db_session):
        brand = _make_brand(db_session, "EmptyBrand")

        mock_interp = SearchInterpretation(
            mode="filter",
            brand_name="EmptyBrand",
            free_text="qualcosa",
        )

        with patch("src.services.search_service._interpret_query",
                   new_callable=AsyncMock, return_value=mock_interp):
            from src.services import search_service
            result = await search_service.execute_semantic_search(
                db_session, "qualcosa"
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
        _make_blueprint(db_session, brand.id, embedding=None)  # no embedding

        mock_interp = SearchInterpretation(mode="similarity", free_text="elegante sera")

        with patch("src.services.search_service._interpret_query",
                   new_callable=AsyncMock, return_value=mock_interp):
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

        _make_blueprint(db_session, brand.id, article_name="Similar",
                        embedding=similar_vec)
        _make_blueprint(db_session, brand.id, article_name="Different",
                        embedding=different_vec)

        mock_interp = SearchInterpretation(mode="similarity", free_text="test query")

        with patch("src.services.search_service._interpret_query",
                   new_callable=AsyncMock, return_value=mock_interp), \
             patch("src.services.search_service.LLMClient") as MockLLM:
            mock_client = MagicMock()
            mock_client.generate_embedding = AsyncMock(return_value=query_vec)
            MockLLM.return_value = mock_client

            from src.services import search_service
            result = await search_service.execute_semantic_search(
                db_session, "test query"
            )

        # "Similar" should come first (higher cosine similarity to query_vec)
        results = result["results"]
        assert len(results) == 2
        assert results[0]["article_name"] == "Similar"


# ---------------------------------------------------------------------------
# Interpret query fallback
# ---------------------------------------------------------------------------

class TestInterpretQueryFallback:
    @pytest.mark.asyncio
    async def test_llm_error_falls_back_to_similarity(self, db_session):
        """If LLMClient raises, _interpret_query should return similarity mode."""
        with patch("src.services.search_service.LLMClient") as MockLLM:
            MockLLM.side_effect = Exception("LLM unavailable")
            from src.services.search_service import _interpret_query
            result = await _interpret_query("qualcosa di elegante")

        assert result.mode == "similarity"
        assert result.free_text == "qualcosa di elegante"
