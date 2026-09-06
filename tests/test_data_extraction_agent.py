import json
import pytest
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError

from src.agents.base import AgentException
from src.agents.data_extraction_agent.state import ExtractionGraphState
from src.agents.data_extraction_agent import DataExtractionAgent


@pytest.fixture
def base_state() -> ExtractionGraphState:
    return ExtractionGraphState(
        file_path="/tmp/test.pdf",
        brand="Gucci",
        raw_text="Raw text",
        cleaned_text="Cleaned text",
    )


class TestStateValidation:
    def test_valid_state(self):
        state = ExtractionGraphState(file_path="/tmp/test.pdf", brand="Gucci")
        assert state.file_path == "/tmp/test.pdf"
        assert state.brand == "Gucci"
        assert state.extracted_items == []

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            ExtractionGraphState(brand="Gucci")
        with pytest.raises(ValidationError):
            ExtractionGraphState(file_path="/tmp/test.pdf")

class TestExtractionNode:
    @pytest.mark.asyncio
    async def test_extraction_injects_uuid(self, base_state):
        from src.agents.data_extraction_agent.nodes.extraction_node import extraction_node

        mock_json = json.dumps([
            {"VendorCode": "G001", "Description": "Borsa", "Quantity": 2, "Color": "Nero"}
        ])

        with patch("src.agents.data_extraction_agent.nodes.extraction_node.LLMClient") as MockClient:
            mock_inst = MockClient.return_value
            mock_inst.call = AsyncMock(return_value=mock_json)

            res = await extraction_node(base_state)

        items = res["extracted_items"]
        assert len(items) == 1
        assert "item_id" in items[0]
        assert len(items[0]["item_id"]) == 36  # UUID string format
        assert items[0]["colors"] == ["Nero"]
        assert items[0]["vendor_code"] == "G001"
        assert items[0]["quantity"] == 2
        assert items[0]["_overwrite"] is True

    @pytest.mark.asyncio
    async def test_extraction_empty_raises(self, base_state):
        from src.agents.data_extraction_agent.nodes.extraction_node import extraction_node

        with patch("src.agents.data_extraction_agent.nodes.extraction_node.LLMClient") as MockClient:
            mock_inst = MockClient.return_value
            mock_inst.call = AsyncMock(return_value="[]")

            with pytest.raises(AgentException, match="empty"):
                await extraction_node(base_state)



class TestDataExtractionAgent:
    @pytest.mark.asyncio
    async def test_agent_aexecute_end_to_end(self):
        agent = DataExtractionAgent()

        mock_json = json.dumps([
            {"VendorCode": "G001", "Description": "Borsa", "Quantity": 2, "Color": "Nero"}
        ])
        mock_web = "<NAME>Gucci Jackie</NAME><DESCRIPTION>Borsa in pelle</DESCRIPTION>"

        with patch("src.agents.data_extraction_agent.nodes.ingestion_node.pdfplumber") as mock_pdf:
            mock_page = mock_pdf.open.return_value.__enter__.return_value.pages
            mock_page.__iter__.return_value = [AsyncMock(extract_text=lambda: "Row 1: G001 Borsa")]

            with patch("src.agents.data_extraction_agent.nodes.cleanup_node.LLMClient") as MockClean, \
                 patch("src.agents.data_extraction_agent.nodes.extraction_node.LLMClient") as MockExt:

                MockClean.return_value.call = AsyncMock(return_value="Cleaned text")
                MockExt.return_value.call = AsyncMock(return_value=mock_json)

                res = await agent.aexecute({"file_path": "/tmp/test.pdf", "brand": "Gucci"})

        assert "items" in res
        assert len(res["items"]) == 1
        assert res["items"][0]["vendor_code"] == "G001"
        assert res["items"][0]["description"] == "Borsa"
