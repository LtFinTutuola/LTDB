"""
tests/test_data_ingestion_agent.py
------------------------------------
Unit tests for all DataIngestionAgent components.

All tests use mocking — no real LLM calls and no real PDF files are needed.
DB-dependent tests use the db_session fixture from conftest.py (in-memory SQLite).
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic import ValidationError

from src.agents.base import AgentException
from src.agents.data_ingestion_agent.state import GraphState, ItemState, CategoryNode


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_categories() -> dict:
    """A minimal two-macro brand hierarchy dict (raw Python, pre-Pydantic)."""
    return {
        "Borse": {
            "description": "Borse da donna",
            "sub_categories": {
                "Tote / Shopper": "Borsa capiente",
                "Tracolla / Crossbody": "Borsa a tracolla",
            },
        },
        "Portafogli": {
            "description": "Portafogli",
            "sub_categories": {
                "Continental / Zip-Around": "Portafoglio lungo",
            },
        },
    }


@pytest.fixture
def single_macro_categories() -> dict:
    """A brand hierarchy with only one macro-category (triggers short-circuit)."""
    return {
        "Borse": {
            "description": "Borse da donna",
            "sub_categories": {
                "Tote / Shopper": "Borsa capiente",
            },
        },
    }


@pytest.fixture
def base_graph_state(sample_categories) -> GraphState:
    return GraphState(
        file_path="/tmp/test.pdf",
        brand="Samsonite",
        categories=sample_categories,
        allowed_sex=["Uomo", "Donna", "Unisex"],
        raw_text="Raw text content",
        cleaned_text="Cleaned text content",
    )


@pytest.fixture
def base_item_state(sample_categories) -> ItemState:
    return ItemState(
        item={"VendorCode": "V001", "Barcode": None, "Description": "Test Bag", "Color": "BLK", "Quantity": 10},
        brand="Samsonite",
        categories=sample_categories,
        allowed_sex=["Uomo", "Donna", "Unisex"],
        web_search_raw="<COLORS>Nero</COLORS><MATERIALS>Pelle</MATERIALS><DESCRIPTION>Borsa elegante</DESCRIPTION><DIMENSIONS>30x20</DIMENSIONS>",
        web_search_parsed="DESCRIPTION:\nBorsa elegante\n\nDIMENSIONS:\n30x20\n\nCOLORS:\nNero\n\nMATERIALS:\nPelle",
    )


# ---------------------------------------------------------------------------
# State Validation Tests
# ---------------------------------------------------------------------------

class TestGraphStateValidation:
    def test_valid_state_construction(self, sample_categories):
        state = GraphState(
            file_path="/tmp/test.pdf",
            brand="Samsonite",
            categories=sample_categories,
            allowed_sex=["Uomo", "Donna"],
        )
        assert state.file_path == "/tmp/test.pdf"
        assert state.brand == "Samsonite"
        assert isinstance(state.categories["Borse"], CategoryNode)
        assert state.enriched_items == []
        assert state.warnings == []

    def test_missing_required_field_raises_validation_error(self, sample_categories):
        with pytest.raises(ValidationError):
            GraphState(
                # file_path is missing
                brand="Samsonite",
                categories=sample_categories,
                allowed_sex=["Uomo"],
            )

    def test_missing_brand_raises_validation_error(self, sample_categories):
        with pytest.raises(ValidationError):
            GraphState(
                file_path="/tmp/test.pdf",
                categories=sample_categories,
                allowed_sex=["Uomo"],
            )


# ---------------------------------------------------------------------------
# ingestion_node Tests
# ---------------------------------------------------------------------------

class TestIngestionNode:
    @pytest.mark.asyncio
    async def test_success(self, base_graph_state):
        from src.agents.data_ingestion_agent.nodes.ingestion_node import ingestion_node

        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Page 1 content"
        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]

        with patch("pdfplumber.open", return_value=mock_pdf):
            result = await ingestion_node(base_graph_state)

        assert "raw_text" in result
        assert "Page 1 content" in result["raw_text"]

    @pytest.mark.asyncio
    async def test_empty_pdf_raises_agent_exception(self, base_graph_state):
        from src.agents.data_ingestion_agent.nodes.ingestion_node import ingestion_node

        mock_page = MagicMock()
        mock_page.extract_text.return_value = None  # No extractable text
        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]

        with patch("pdfplumber.open", return_value=mock_pdf):
            with pytest.raises(AgentException, match="no extractable text"):
                await ingestion_node(base_graph_state)

    @pytest.mark.asyncio
    async def test_pdfplumber_exception_raises_agent_exception(self, base_graph_state):
        from src.agents.data_ingestion_agent.nodes.ingestion_node import ingestion_node

        with patch("pdfplumber.open", side_effect=Exception("File corrupted")):
            with pytest.raises(AgentException, match="Failed to read PDF"):
                await ingestion_node(base_graph_state)


# ---------------------------------------------------------------------------
# cleanup_node Tests
# ---------------------------------------------------------------------------

class TestCleanupNode:
    @pytest.mark.asyncio
    async def test_success(self, base_graph_state):
        from src.agents.data_ingestion_agent.nodes.cleanup_node import cleanup_node

        with patch("src.agents.data_ingestion_agent.nodes.cleanup_node.LLMClient") as MockLLMClient:
            mock_instance = MockLLMClient.return_value
            mock_instance.call = AsyncMock(return_value="Cleaned text output")

            result = await cleanup_node(base_graph_state)

        assert result["cleaned_text"] == "Cleaned text output"

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_raw_text(self, base_graph_state):
        from src.agents.data_ingestion_agent.nodes.cleanup_node import cleanup_node

        with patch("src.agents.data_ingestion_agent.nodes.cleanup_node.LLMClient") as MockLLMClient:
            mock_instance = MockLLMClient.return_value
            mock_instance.call = AsyncMock(side_effect=Exception("LLM unavailable"))

            result = await cleanup_node(base_graph_state)

        # Falls back to raw text
        assert result["cleaned_text"] == base_graph_state.raw_text
        assert len(result["warnings"]) == 1
        assert "LLM cleanup failed" in result["warnings"][0]


# ---------------------------------------------------------------------------
# extraction_node Tests
# ---------------------------------------------------------------------------

class TestExtractionNode:
    @pytest.mark.asyncio
    async def test_success(self, base_graph_state):
        from src.agents.data_ingestion_agent.nodes.extraction_node import extraction_node

        mock_json = json.dumps([{"VendorCode": "V001", "Description": "Bag", "Quantity": 5}])

        with patch("src.agents.data_ingestion_agent.nodes.extraction_node.LLMClient") as MockLLMClient:
            mock_instance = MockLLMClient.return_value
            mock_instance.call = AsyncMock(return_value=mock_json)

            result = await extraction_node(base_graph_state)

        assert "base_items" in result
        assert len(result["base_items"]) == 1
        assert result["base_items"][0]["VendorCode"] == "V001"

    @pytest.mark.asyncio
    async def test_invalid_json_raises_agent_exception(self, base_graph_state):
        from src.agents.data_ingestion_agent.nodes.extraction_node import extraction_node

        with patch("src.agents.data_ingestion_agent.nodes.extraction_node.LLMClient") as MockLLMClient:
            mock_instance = MockLLMClient.return_value
            mock_instance.call = AsyncMock(return_value="THIS IS NOT JSON {{{")

            with pytest.raises(AgentException, match="Failed to parse"):
                await extraction_node(base_graph_state)

    @pytest.mark.asyncio
    async def test_empty_list_raises_agent_exception(self, base_graph_state):
        from src.agents.data_ingestion_agent.nodes.extraction_node import extraction_node

        with patch("src.agents.data_ingestion_agent.nodes.extraction_node.LLMClient") as MockLLMClient:
            mock_instance = MockLLMClient.return_value
            mock_instance.call = AsyncMock(return_value="[]")

            with pytest.raises(AgentException, match="empty"):
                await extraction_node(base_graph_state)

    @pytest.mark.asyncio
    async def test_llm_failure_raises_agent_exception(self, base_graph_state):
        from src.agents.data_ingestion_agent.nodes.extraction_node import extraction_node

        with patch("src.agents.data_ingestion_agent.nodes.extraction_node.LLMClient") as MockLLMClient:
            mock_instance = MockLLMClient.return_value
            mock_instance.call = AsyncMock(side_effect=Exception("API error"))

            with pytest.raises(AgentException, match="LLM call failed"):
                await extraction_node(base_graph_state)


# ---------------------------------------------------------------------------
# macro_category_node Tests
# ---------------------------------------------------------------------------

class TestMacroCategoryNode:
    @pytest.mark.asyncio
    async def test_short_circuit_single_macro(self, base_item_state, single_macro_categories):
        from src.agents.data_ingestion_agent.nodes.macro_category_node import macro_category_node

        state = base_item_state.model_copy(update={"categories": single_macro_categories})

        with patch("src.agents.data_ingestion_agent.nodes.macro_category_node.LLMClient") as MockLLMClient:
            result = await macro_category_node(state)
            # LLM should NOT be called
            MockLLMClient.assert_not_called()

        assert result["macro_category"] == "Borse"

    @pytest.mark.asyncio
    async def test_llm_path_multiple_macros(self, base_item_state):
        from src.agents.data_ingestion_agent.nodes.macro_category_node import macro_category_node

        mock_response = json.dumps({"macro_category": "Portafogli"})

        with patch("src.agents.data_ingestion_agent.nodes.macro_category_node.LLMClient") as MockLLMClient:
            mock_instance = MockLLMClient.return_value
            mock_instance.call = AsyncMock(return_value=mock_response)

            result = await macro_category_node(base_item_state)

        assert result["macro_category"] == "Portafogli"

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none_with_warning(self, base_item_state):
        from src.agents.data_ingestion_agent.nodes.macro_category_node import macro_category_node

        with patch("src.agents.data_ingestion_agent.nodes.macro_category_node.LLMClient") as MockLLMClient:
            mock_instance = MockLLMClient.return_value
            mock_instance.call = AsyncMock(side_effect=Exception("API error"))

            result = await macro_category_node(base_item_state)

        assert result["macro_category"] is None
        assert len(result["warnings"]) == 1


# ---------------------------------------------------------------------------
# sub_category_node Tests
# ---------------------------------------------------------------------------

class TestSubCategoryNode:
    @pytest.mark.asyncio
    async def test_short_circuit_single_sub(self, base_item_state, sample_categories):
        from src.agents.data_ingestion_agent.nodes.sub_category_node import sub_category_node

        # Portafogli has only one sub-category
        state = base_item_state.model_copy(update={"macro_category": "Portafogli"})

        with patch("src.agents.data_ingestion_agent.nodes.sub_category_node.LLMClient") as MockLLMClient:
            result = await sub_category_node(state)
            MockLLMClient.assert_not_called()

        assert result["sub_category"] == "Continental / Zip-Around"

    @pytest.mark.asyncio
    async def test_short_circuit_invalid_macro(self, base_item_state):
        from src.agents.data_ingestion_agent.nodes.sub_category_node import sub_category_node

        state = base_item_state.model_copy(update={"macro_category": "NONEXISTENT"})

        with patch("src.agents.data_ingestion_agent.nodes.sub_category_node.LLMClient") as MockLLMClient:
            result = await sub_category_node(state)
            MockLLMClient.assert_not_called()

        assert result["sub_category"] is None

    @pytest.mark.asyncio
    async def test_llm_path_multiple_subs(self, base_item_state):
        from src.agents.data_ingestion_agent.nodes.sub_category_node import sub_category_node

        state = base_item_state.model_copy(update={"macro_category": "Borse"})
        mock_response = json.dumps({"sub_category": "Tracolla / Crossbody"})

        with patch("src.agents.data_ingestion_agent.nodes.sub_category_node.LLMClient") as MockLLMClient:
            mock_instance = MockLLMClient.return_value
            mock_instance.call = AsyncMock(return_value=mock_response)

            result = await sub_category_node(state)

        assert result["sub_category"] == "Tracolla / Crossbody"


# ---------------------------------------------------------------------------
# web_search_node Tests
# ---------------------------------------------------------------------------

class TestWebSearchNode:
    @pytest.mark.asyncio
    async def test_failure_is_non_fatal(self, base_item_state):
        from src.agents.data_ingestion_agent.nodes.web_search_node import web_search_node

        with patch("src.agents.data_ingestion_agent.nodes.web_search_node.LLMClient") as MockLLMClient:
            mock_instance = MockLLMClient.return_value
            mock_instance.call_with_grounding = AsyncMock(side_effect=Exception("Search API down"))

            result = await web_search_node(base_item_state)

        assert result["web_search_raw"] == ""
        assert result["web_search_urls"] == []
        assert len(result["warnings"]) == 1
        assert "web search failed" in result["warnings"][0]

    @pytest.mark.asyncio
    async def test_success_returns_parsed_text(self, base_item_state):
        from src.agents.data_ingestion_agent.nodes.web_search_node import web_search_node

        raw = "<COLORS>Nero</COLORS><MATERIALS>Pelle</MATERIALS><DESCRIPTION>Elegante</DESCRIPTION><DIMENSIONS>30x20</DIMENSIONS>"

        with patch("src.agents.data_ingestion_agent.nodes.web_search_node.LLMClient") as MockLLMClient:
            mock_instance = MockLLMClient.return_value
            mock_instance.call_with_grounding = AsyncMock(return_value=(raw, ["https://example.com"]))

            result = await web_search_node(base_item_state)

        assert result["web_search_raw"] == raw
        assert "Elegante" in result["web_search_parsed"]
        assert result["web_search_urls"] == ["https://example.com"]


# ---------------------------------------------------------------------------
# item_merge_node Tests
# ---------------------------------------------------------------------------

class TestItemMergeNode:
    @pytest.mark.asyncio
    async def test_merge_produces_single_element_list(self, base_item_state):
        from src.agents.data_ingestion_agent.nodes.item_merge_node import item_merge_node

        state = base_item_state.model_copy(update={
            "macro_category": "Borse",
            "sub_category": "Tote / Shopper",
            "sex": "Donna",
            "materials": ["Pelle"],
            "colors": ["Nero"],
            "article_name": "Borsa Tote",
            "product_short_description": "Borsa capiente",
            "product_extended_description": "Borsa molto capiente in pelle",
            "tags": ["borsa", "pelle"],
        })

        result = await item_merge_node(state)

        assert "enriched_items" in result
        assert len(result["enriched_items"]) == 1
        merged = result["enriched_items"][0]
        assert merged["VendorCode"] == "V001"
        assert merged["category"] == "Borse"
        assert merged["sub_category"] == "Tote / Shopper"
        assert merged["sex"] == "Donna"
        assert merged["article_name"] == "Borsa Tote"


# ---------------------------------------------------------------------------
# CategoryRepository Tests
# ---------------------------------------------------------------------------

class TestCategoryRepository:
    def test_get_brand_hierarchy_success(self, db_session):
        from src.repositories.pim_repo import CategoryRepository
        from src.models.pim import Brand, Category, BrandCategory
        from fastapi import HTTPException

        # Seed a brand with a macro and sub category
        brand = Brand(name="TestBrand")
        db_session.add(brand)
        db_session.flush()

        macro = Category(name="Borse", description="Borse da donna", parent_id=None)
        db_session.add(macro)
        db_session.flush()

        sub = Category(name="Tote / Shopper", description="Borsa capiente", parent_id=macro.id)
        db_session.add(sub)
        db_session.flush()

        link = BrandCategory(brand_id=brand.id, category_id=macro.id)
        db_session.add(link)
        db_session.commit()

        repo = CategoryRepository()
        hierarchy = repo.get_brand_hierarchy(db_session, str(brand.id))

        assert "Borse" in hierarchy
        assert hierarchy["Borse"]["description"] == "Borse da donna"
        assert "Tote / Shopper" in hierarchy["Borse"]["sub_categories"]

    def test_get_brand_hierarchy_brand_not_found(self, db_session):
        from src.repositories.pim_repo import CategoryRepository
        from fastapi import HTTPException

        repo = CategoryRepository()
        with pytest.raises(HTTPException) as exc_info:
            repo.get_brand_hierarchy(db_session, "00000000-0000-0000-0000-000000000000")

        assert exc_info.value.status_code == 404
        assert "00000000-0000-0000-0000-000000000000" in exc_info.value.detail


# ---------------------------------------------------------------------------
# AgentException Propagation Test
# ---------------------------------------------------------------------------

class TestAgentExceptionPropagation:
    @pytest.mark.asyncio
    async def test_agent_exception_propagates_from_aexecute(self, sample_categories):
        from src.agents.data_ingestion_agent.agent import DataIngestionAgent

        agent = DataIngestionAgent()

        # Patch ingestion_node to immediately raise AgentException
        async def mock_failing_node(state):
            raise AgentException("PDF unreadable")

        with patch(
            "src.agents.data_ingestion_agent.agent.ingestion_node",
            new=mock_failing_node,
        ):
            with pytest.raises(AgentException, match="PDF unreadable"):
                await agent.aexecute({
                    "file_path": "/tmp/bad.pdf",
                    "brand": "Samsonite",
                    "categories": sample_categories,
                    "allowed_sex": ["Uomo", "Donna", "Unisex"],
                })

    @pytest.mark.asyncio
    async def test_invalid_input_data_raises_agent_exception(self, sample_categories):
        from src.agents.data_ingestion_agent.agent import DataIngestionAgent

        agent = DataIngestionAgent()

        with pytest.raises(AgentException, match="Invalid input_data"):
            await agent.aexecute({
                # Missing file_path and brand
                "categories": sample_categories,
            })
