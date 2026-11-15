"""
tests/test_audit_logging.py
---------------------------
Verifies that AppLogger automatically enriches records with current user context
and that AccessLogMiddleware logs HTTP requests and security events correctly.
"""
import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from src.main import app
from src.core.logger import get_logger
from src.core.request_context import AuthUser, current_user_var
from src.core.config import settings

logger = get_logger()


def test_logger_enrichment_with_user_context():
    """Verifies that AppLogger automatically includes user and role when ContextVar is set."""
    user = AuthUser(id="123", username="test_operator", role="base")
    token = current_user_var.set(user)

    try:
        logger.log_execution("test_component", "test_event", "ok", custom_attr="val123")
    finally:
        current_user_var.reset(token)

    # Read the latest record in the log file
    with open(logger.file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    last_record = json.loads(lines[-1])
    assert last_record["cat"] == "EXEC"
    assert last_record["component"] == "test_component"
    assert last_record["event"] == "test_event"
    assert last_record["user"] == "test_operator"
    assert last_record["role"] == "base"
    assert last_record["custom_attr"] == "val123"


def test_log_security_category():
    """Verifies that log_security records under SEC category."""
    logger.log_security("sec_component", "sec_event", "warning", alert_reason="test")

    with open(logger.file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    last_record = json.loads(lines[-1])
    assert last_record["cat"] == "SEC"
    assert last_record["component"] == "sec_component"
    assert last_record["event"] == "sec_event"
    assert last_record["status"] == "warning"
    assert last_record["alert_reason"] == "test"


def test_access_log_middleware_captures_client_ip():
    """Verifies that AccessLogMiddleware logs requests with X-Forwarded-For IP address."""
    client = TestClient(app)
    # Perform a request with X-Forwarded-For header
    client.get(
        "/api/v1/excel-synchronization/agent-config",
        headers={"X-Forwarded-For": "203.0.113.195, 10.0.0.1"},
    )

    with open(logger.file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Find the http_access record
    matching = [
        json.loads(line) for line in lines
        if json.loads(line).get("component") == "http_access"
        and json.loads(line).get("path") == "/api/v1/excel-synchronization/agent-config"
    ]
    assert len(matching) > 0
    record = matching[-1]
    assert record["ip"] == "203.0.113.195"
    assert record["user"] == "edge-agent"
    assert record["status_code"] == 200


@pytest.mark.asyncio
async def test_llm_client_call_with_grounding_logs_full_metadata():
    """Verifies that call_with_grounding logs full grounding_metadata, tool tokens, and system_prompt."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch
    from google.genai import types
    import src.agents.llm_client as llm_mod

    gm = types.GroundingMetadata(
        web_search_queries=["Ynot? STE008F7"],
        image_search_queries=["Ynot? STE008F7 bag"],
        grounding_chunks=[
            types.GroundingChunk(
                web=types.GroundingChunkWeb(uri="https://www.ynot.it/ste008f7", title="Ynot? Street")
            )
        ],
    )
    usage = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=480,
        candidates_token_count=85,
        tool_use_prompt_token_count=120,
        thoughts_token_count=10,
        total_token_count=695,
    )
    candidate = types.Candidate(
        grounding_metadata=gm,
        finish_reason=types.FinishReason.STOP,
        content=types.Content(role="model", parts=[types.Part(text="<NAME>Ynot? Street</NAME>")]),
    )
    fake_response = SimpleNamespace(
        text="<NAME>Ynot? Street</NAME>",
        usage_metadata=usage,
        candidates=[candidate],
    )

    with patch.object(llm_mod, "_load_gemini_config", return_value={"api_key": "test-key"}), \
         patch.object(llm_mod.genai, "Client") as mock_genai_client, \
         patch.object(llm_mod.logger, "log_llm") as mock_log_llm:
        mock_genai_client.return_value.aio.models.generate_content = AsyncMock(return_value=fake_response)

        client = llm_mod.LLMClient()
        text, urls = await client.call_with_grounding(
            model_name="gemini-3.1-flash-lite",
            system_prompt="SYS_PROMPT_TEST",
            prompt="USER_PROMPT_TEST",
            pipeline_stage="Stage 3 - Web Search Extraction",
            temperature=0.0,
        )

    assert text == "<NAME>Ynot? Street</NAME>"
    assert urls == ["https://www.ynot.it/ste008f7"]

    mock_log_llm.assert_called_once()
    kwargs = mock_log_llm.call_args.kwargs
    assert kwargs["system_prompt"] == "SYS_PROMPT_TEST"
    assert kwargs["temperature"] == 0.0
    assert kwargs["tools"] == [{"google_search": {}}]
    assert kwargs["query_usage"] == 2
    assert kwargs["grounding_detected"] is True
    assert kwargs["tool_use_prompt_tokens"] == 120
    assert kwargs["thoughts_tokens"] == 10
    assert kwargs["grounding_metadata"]["image_search_queries"] == ["Ynot? STE008F7 bag"]
    assert kwargs["grounding_metadata"]["web_search_queries"] == ["Ynot? STE008F7"]
    assert kwargs["extracted_urls"] == ["https://www.ynot.it/ste008f7"]


@pytest.mark.asyncio
async def test_llm_client_grounding_detected_via_tool_tokens_without_queries():
    """Verifies grounding_detected is True when tool_use_prompt_token_count > 0 even if web_search_queries is empty."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from google.genai import types
    import src.agents.llm_client as llm_mod

    usage = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=480,
        candidates_token_count=40,
        tool_use_prompt_token_count=95,
        total_token_count=615,
    )
    candidate = types.Candidate(
        grounding_metadata=None,
        finish_reason=types.FinishReason.STOP,
        content=types.Content(role="model", parts=[types.Part(text="https://example.com/img.jpg")]),
    )
    fake_response = SimpleNamespace(
        text="https://example.com/img.jpg",
        usage_metadata=usage,
        candidates=[candidate],
    )

    with patch.object(llm_mod, "_load_gemini_config", return_value={"api_key": "test-key"}), \
         patch.object(llm_mod.genai, "Client") as mock_genai_client, \
         patch.object(llm_mod.logger, "log_llm") as mock_log_llm:
        mock_genai_client.return_value.aio.models.generate_content = AsyncMock(return_value=fake_response)

        client = llm_mod.LLMClient()
        await client.call_with_grounding(
            model_name="gemini-3.1-flash-lite",
            system_prompt="SYS",
            prompt="USER",
            pipeline_stage="Stage 5 - Photo Search Grounded",
        )

    kwargs = mock_log_llm.call_args.kwargs
    assert kwargs["query_usage"] == 0
    assert kwargs["grounding_metadata"] is None
    assert kwargs["tool_use_prompt_tokens"] == 95
    assert kwargs["grounding_detected"] is True


@pytest.mark.asyncio
async def test_llm_client_grounding_not_detected_when_all_metadata_absent():
    """Verifies grounding_detected is False when no grounding metadata, tool parts, or tool tokens exist."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from google.genai import types
    import src.agents.llm_client as llm_mod

    usage = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=480,
        candidates_token_count=50,
        total_token_count=530,
    )
    candidate = types.Candidate(
        grounding_metadata=None,
        finish_reason=types.FinishReason.STOP,
        content=types.Content(role="model", parts=[types.Part(text="fallback")]),
    )
    fake_response = SimpleNamespace(
        text="fallback",
        usage_metadata=usage,
        candidates=[candidate],
    )

    with patch.object(llm_mod, "_load_gemini_config", return_value={"api_key": "test-key"}), \
         patch.object(llm_mod.genai, "Client") as mock_genai_client, \
         patch.object(llm_mod.logger, "log_llm") as mock_log_llm:
        mock_genai_client.return_value.aio.models.generate_content = AsyncMock(return_value=fake_response)

        client = llm_mod.LLMClient()
        await client.call_with_grounding(
            model_name="gemini-3.1-flash-lite",
            system_prompt="SYS",
            prompt="USER",
            pipeline_stage="Stage 3 - Web Search Extraction",
        )

    kwargs = mock_log_llm.call_args.kwargs
    assert kwargs["query_usage"] == 0
    assert kwargs["grounding_metadata"] is None
    assert kwargs["tool_use_prompt_tokens"] == 0
    assert kwargs["grounding_detected"] is False
