import pytest
from pydantic import ValidationError
from unittest.mock import patch, AsyncMock

from src.agents.single_item_extraction_agent.state import SingleItemExtractionState
from src.agents.single_item_extraction_agent.nodes.normalize_node import normalize_node
from src.agents.single_item_extraction_agent.nodes.web_search_node import web_search_node
from src.agents.single_item_extraction_agent import SingleItemExtractionAgent

@pytest.fixture
def base_state():
    return SingleItemExtractionState(
        brand="Gucci",
        vendor_code="G001",
        colors=["Nero"]
    )

class TestSingleItemExtractionState:
    def test_valid_state(self):
        state = SingleItemExtractionState(brand="Gucci", vendor_code="G001", colors=["Nero"])
        assert state.brand == "Gucci"
        assert state.vendor_code == "G001"
        assert state.colors == ["Nero"]
        assert state.quantity == 1
        assert state.extracted_item is None

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            SingleItemExtractionState(brand="Gucci", vendor_code="G001") # Missing desc and colors

class TestNormalizeNode:
    @pytest.mark.asyncio
    async def test_normalize_node(self, base_state):
        res = await normalize_node(base_state)
        item = res["extracted_item"]
        assert "item_id" in item
        assert item["vendor_code"] == "G001"
        assert item["quantity"] == 1
        assert item["barcode"] == ""
        # Should not inject hints
        assert "colors" not in item

class TestSingleItemWebSearchNode:
    @pytest.mark.asyncio
    async def test_web_search_success(self, base_state):
        base_state.extracted_item = {
            "item_id": "uuid-1", "vendor_code": "G001", "quantity": 1, "barcode": ""
        }

        mock_raw = "<NAME>Gucci Jackie</NAME><DESCRIPTION>Borsa grande</DESCRIPTION><OFFICIAL_SHORT_DESC>Jackie 1961</OFFICIAL_SHORT_DESC><OFFICIAL_COLORS>Nero Opaco</OFFICIAL_COLORS>"

        with patch("src.agents.single_item_extraction_agent.nodes.web_search_node.LLMClient") as MockClient:
            mock_inst = MockClient.return_value
            mock_inst.call_with_grounding = AsyncMock(return_value=(mock_raw, ["http://example.com"]))

            res = await web_search_node(base_state)
            
            item = res["extracted_item"]
            assert item["article_name"] == "Gucci Jackie"
            assert item["article_description"] == "Borsa grande"
            assert item["description"] == "Jackie 1961"
            assert item["colors"] == ["Nero Opaco"]

    @pytest.mark.asyncio
    async def test_web_search_fallback(self, base_state):
        base_state.extracted_item = {
            "item_id": "uuid-1", "vendor_code": "G001", "quantity": 1, "barcode": ""
        }

        with patch("src.agents.single_item_extraction_agent.nodes.web_search_node.LLMClient") as MockClient:
            mock_inst = MockClient.return_value
            mock_inst.call_with_grounding = AsyncMock(return_value=("", []))

            from src.agents.base import AgentException
            with pytest.raises(AgentException, match="non trovato online"):
                await web_search_node(base_state)

class TestSingleItemExtractionAgent:
    @pytest.mark.asyncio
    async def test_agent_aexecute_end_to_end(self):
        agent = SingleItemExtractionAgent()
        
        mock_raw = "<NAME>Gucci Jackie</NAME><DESCRIPTION>Borsa grande</DESCRIPTION><OFFICIAL_SHORT_DESC>Jackie 1961</OFFICIAL_SHORT_DESC><OFFICIAL_COLORS>Nero Opaco</OFFICIAL_COLORS>"
        
        with patch("src.agents.single_item_extraction_agent.nodes.web_search_node.LLMClient") as MockClient:
            mock_inst = MockClient.return_value
            mock_inst.call_with_grounding = AsyncMock(return_value=(mock_raw, ["http://example.com"]))
            
            res = await agent.aexecute({
                "brand": "Gucci",
                "vendor_code": "G001",
                "colors": ["Nero"]
            })
            
            assert "items" in res
            assert len(res["items"]) == 1
            item = res["items"][0]
            assert item["vendor_code"] == "G001"
            assert item["article_name"] == "Gucci Jackie"
            assert item["colors"] == ["Nero Opaco"]
