"""
src/agents/llm_client.py
------------------------
Shared, agent-agnostic wrapper around the Google GenAI SDK.

Reads credentials from src/gemini.yaml at instantiation so that no
API keys are ever hardcoded or read from environment variables directly.
All nodes in any agent that need LLM access must use this class.
"""
import os
import uuid
from pathlib import Path
from typing import Optional, Any

import yaml
from google import genai
from google.genai import types
from src.core.logger import get_logger

logger = get_logger()


# Path to credentials — in the same directory as this file
_GEMINI_YAML_PATH = Path(__file__).resolve().parent / "gemini.yaml"


def _load_gemini_config() -> dict:
    """Load api_key and endpoint_url from src/agents/gemini.yaml."""
    if not _GEMINI_YAML_PATH.exists():
        raise FileNotFoundError(
            f"Gemini config file not found at '{_GEMINI_YAML_PATH}'. "
            "Please create src/agents/gemini.yaml with 'api_key' and 'endpoint_url' fields."
        )
    with open(_GEMINI_YAML_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _safe_dump(obj: Any) -> Any:
    """Safely convert a Pydantic/SDK model to JSON-serializable primitives."""
    if obj is None:
        return None
    if isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json", exclude_none=True)
            if isinstance(dumped, (dict, list, str, int, float, bool)):
                return dumped
        except Exception:
            pass
    return None


def _extract_response_telemetry(response: Any) -> dict[str, Any]:
    """Extract token usage, grounding metadata, and tool/part details from a GenAI response."""
    meta = getattr(response, "usage_metadata", None)
    input_tokens = getattr(meta, "prompt_token_count", None) if meta else None
    output_tokens = getattr(meta, "candidates_token_count", None) if meta else None
    tool_use_prompt_tokens = getattr(meta, "tool_use_prompt_token_count", None) if meta else None
    thoughts_tokens = getattr(meta, "thoughts_token_count", None) if meta else None
    total_tokens = getattr(meta, "total_token_count", None) if meta else None

    candidates = getattr(response, "candidates", None) or []
    candidate = candidates[0] if candidates else None

    finish_reason = None
    finish_message = None
    gm = None
    url_context_metadata = None
    citation_metadata = None
    tool_parts: list[dict[str, Any]] = []

    if candidate is not None:
        raw_finish = getattr(candidate, "finish_reason", None)
        finish_reason = str(raw_finish) if raw_finish is not None else None
        finish_message = getattr(candidate, "finish_message", None)
        if not isinstance(finish_message, (str, type(None))):
            finish_message = str(finish_message)
        gm = getattr(candidate, "grounding_metadata", None)
        url_context_metadata = _safe_dump(getattr(candidate, "url_context_metadata", None))
        citation_metadata = _safe_dump(getattr(candidate, "citation_metadata", None))

        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            part_info: dict[str, Any] = {}
            for field in ("tool_call", "tool_response", "function_call", "function_response"):
                val = _safe_dump(getattr(part, field, None))
                if val is not None:
                    part_info[field] = val
            if part_info:
                tool_parts.append(part_info)

    extracted_urls: list[str] = []
    web_queries: list[str] = []
    image_queries: list[str] = []
    retrieval_queries: list[str] = []
    grounding_metadata = _safe_dump(gm)

    if gm is not None:
        for chunk in getattr(gm, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            if web is not None:
                uri = getattr(web, "uri", None) or getattr(web, "title", None)
                if uri:
                    extracted_urls.append(uri)

        web_queries = list(getattr(gm, "web_search_queries", None) or [])
        image_queries = list(getattr(gm, "image_search_queries", None) or [])
        retrieval_queries = list(getattr(gm, "retrieval_queries", None) or [])

    query_usage = len(web_queries) + len(image_queries) + len(retrieval_queries)
    has_grounding_evidence = bool(
        query_usage > 0
        or extracted_urls
        or (
            isinstance(grounding_metadata, dict)
            and (
                grounding_metadata.get("grounding_chunks")
                or grounding_metadata.get("grounding_supports")
                or grounding_metadata.get("retrieval_metadata")
            )
        )
        or (isinstance(tool_use_prompt_tokens, int) and tool_use_prompt_tokens > 0)
        or tool_parts
    )

    return {
        "input_tokens": input_tokens if isinstance(input_tokens, int) else 0,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else 0,
        "tool_use_prompt_tokens": tool_use_prompt_tokens if isinstance(tool_use_prompt_tokens, int) else 0,
        "thoughts_tokens": thoughts_tokens if isinstance(thoughts_tokens, int) else 0,
        "total_tokens": total_tokens if isinstance(total_tokens, int) else 0,
        "query_usage": query_usage,
        "grounding_detected": has_grounding_evidence,
        "extracted_urls": extracted_urls,
        "grounding_metadata": grounding_metadata,
        "url_context_metadata": url_context_metadata,
        "citation_metadata": citation_metadata,
        "tool_parts": tool_parts,
        "finish_reason": finish_reason,
        "finish_message": finish_message,
    }


class LLMCallRecord:
    """Lightweight container for a single LLM call's telemetry."""

    __slots__ = ("call_id", "pipeline_stage", "model_name",
                 "input_tokens", "output_tokens", "query_usage",
                 "grounding_detected", "prompt", "output")

    def __init__(
        self,
        call_id: str,
        pipeline_stage: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        query_usage: int,
        prompt: str,
        output: str,
        grounding_detected: bool = False,
    ) -> None:
        self.call_id = call_id
        self.pipeline_stage = pipeline_stage
        self.model_name = model_name
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.query_usage = query_usage
        self.grounding_detected = grounding_detected
        self.prompt = prompt
        self.output = output

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


class LLMClient:
    """
    Async wrapper around the Google GenAI `generate_content` API.

    Usage (inside any async node):
        client = LLMClient()
        text = await client.call(
            model_name="gemini-3.1-flash-lite",
            system_prompt="You are ...",
            prompt="...",
        )

    All calls are logged internally; retrieve them via `get_logs()`.
    """

    def __init__(self) -> None:
        config = _load_gemini_config()
        api_key: str = config.get("api_key", "")
        if not api_key or api_key == "YOUR_GEMINI_API_KEY":
            raise ValueError(
                "Gemini API key is not set. Please update 'api_key' in src/gemini.yaml."
            )
        self._client = genai.Client(api_key=api_key)
        self._logs: list[LLMCallRecord] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def call(
        self,
        model_name: str,
        system_prompt: str,
        prompt: str,
        pipeline_stage: str = "unknown",
        tools: Optional[list] = None,
        response_schema: Optional[Any] = None,
        response_mime_type: str = "text/plain",
        max_output_tokens: int = 8192,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Execute an async LLM call and return the raw text response.

        Args:
            model_name:         Gemini model identifier (e.g. 'gemini-3.1-flash-lite').
            system_prompt:      System instruction for the model.
            prompt:             User-facing prompt string.
            pipeline_stage:     Human-readable label for telemetry logs.
            tools:              Optional list of tool configs (e.g. google_search).
            response_schema:    Optional Pydantic model for structured JSON output.
            response_mime_type: MIME type for the response ('application/json' or 'text/plain').
            max_output_tokens:  Token cap for the response.
            temperature:        Optional temperature setting for the model.

        Returns:
            The raw text of the model's response.

        Raises:
            Exception: Any underlying SDK exception is allowed to propagate so
                       callers (nodes) can decide whether the error is fatal.
        """
        config_kwargs: dict[str, Any] = {
            "system_instruction": system_prompt,
            "response_mime_type": response_mime_type,
            "max_output_tokens": max_output_tokens,
        }
        if temperature is not None:
            config_kwargs["temperature"] = temperature
        if tools:
            config_kwargs["tools"] = tools
        if response_schema is not None:
            config_kwargs["response_schema"] = response_schema
            config_kwargs["response_mime_type"] = "application/json"

        gen_config = types.GenerateContentConfig(**config_kwargs)

        response = await self._client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
            config=gen_config,
        )

        # --- Telemetry ---
        call_id = str(uuid.uuid4())
        telemetry = _extract_response_telemetry(response)
        output_text = response.text or ""

        record = LLMCallRecord(
            call_id=call_id,
            pipeline_stage=pipeline_stage,
            model_name=model_name,
            input_tokens=telemetry["input_tokens"],
            output_tokens=telemetry["output_tokens"],
            query_usage=telemetry["query_usage"],
            prompt=prompt,
            output=output_text,
            grounding_detected=telemetry["grounding_detected"],
        )
        self._logs.append(record)

        log_kwargs: dict[str, Any] = {
            "call_id": call_id,
            "pipeline_stage": pipeline_stage,
            "model_name": model_name,
            "tokens_in": telemetry["input_tokens"],
            "tokens_out": telemetry["output_tokens"],
            "tool_use_prompt_tokens": telemetry["tool_use_prompt_tokens"],
            "thoughts_tokens": telemetry["thoughts_tokens"],
            "query_usage": telemetry["query_usage"],
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "system_prompt": system_prompt,
            "prompt": prompt,
            "output": output_text,
            "finish_reason": telemetry["finish_reason"],
        }
        if tools or telemetry["grounding_detected"]:
            log_kwargs["tools"] = [_safe_dump(t) if _safe_dump(t) is not None else t for t in (tools or [])]
            log_kwargs["grounding_detected"] = telemetry["grounding_detected"]
            log_kwargs["grounding_metadata"] = telemetry["grounding_metadata"]
            log_kwargs["extracted_urls"] = telemetry["extracted_urls"]
            if telemetry["tool_parts"]:
                log_kwargs["tool_parts"] = telemetry["tool_parts"]

        logger.log_llm("LLMClient", "generate_content", "ok", **log_kwargs)

        return output_text

    async def call_with_grounding(
        self,
        model_name: str,
        system_prompt: str,
        prompt: str,
        pipeline_stage: str = "unknown",
        max_output_tokens: int = 1024,
        temperature: Optional[float] = None,
        thinking_level: Optional[types.ThinkingLevel | str] = None,
    ) -> tuple[str, list[str]]:
        """
        Variant of `call` that also returns the grounding source URLs.
        Used exclusively by web_search_node.

        Returns:
            Tuple of (response_text, list_of_urls).
        """
        config_kwargs = {
            "tools": [{"google_search": {}}],
            "response_mime_type": "text/plain",
            "max_output_tokens": max_output_tokens,
            "system_instruction": system_prompt,
        }
        if temperature is not None:
            config_kwargs["temperature"] = temperature
        resolved_thinking_level: Optional[str] = None
        if thinking_level is not None:
            level_enum = (
                types.ThinkingLevel(thinking_level.upper())
                if isinstance(thinking_level, str)
                else thinking_level
            )
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level_enum)
            resolved_thinking_level = level_enum.value

        gen_config = types.GenerateContentConfig(**config_kwargs)

        response = await self._client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
            config=gen_config,
        )

        # --- Telemetry ---
        call_id = str(uuid.uuid4())
        telemetry = _extract_response_telemetry(response)
        extracted_urls = telemetry["extracted_urls"]
        output_text = response.text or ""

        record = LLMCallRecord(
            call_id=call_id,
            pipeline_stage=pipeline_stage,
            model_name=model_name,
            input_tokens=telemetry["input_tokens"],
            output_tokens=telemetry["output_tokens"],
            query_usage=telemetry["query_usage"],
            prompt=prompt,
            output=output_text,
            grounding_detected=telemetry["grounding_detected"],
        )
        self._logs.append(record)

        logger.log_llm(
            "LLMClient",
            "generate_content_grounded",
            "ok",
            call_id=call_id,
            pipeline_stage=pipeline_stage,
            model_name=model_name,
            tokens_in=telemetry["input_tokens"],
            tokens_out=telemetry["output_tokens"],
            tool_use_prompt_tokens=telemetry["tool_use_prompt_tokens"],
            thoughts_tokens=telemetry["thoughts_tokens"],
            total_tokens=telemetry["total_tokens"],
            query_usage=telemetry["query_usage"],
            grounding_detected=telemetry["grounding_detected"],
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            thinking_level=resolved_thinking_level,
            tools=config_kwargs["tools"],
            system_prompt=system_prompt,
            prompt=prompt,
            output=output_text,
            extracted_urls=extracted_urls,
            grounding_metadata=telemetry["grounding_metadata"],
            url_context_metadata=telemetry["url_context_metadata"],
            citation_metadata=telemetry["citation_metadata"],
            tool_parts=telemetry["tool_parts"],
            finish_reason=telemetry["finish_reason"],
            finish_message=telemetry["finish_message"],
        )

        return output_text, extracted_urls

    def get_logs(self) -> list[dict]:
        """Return all recorded LLM call telemetry as a list of dicts."""
        return [r.to_dict() for r in self._logs]

    async def generate_embedding(self, text: str, model_name: str = "gemini-embedding-001") -> list[float]:
        """
        Generate an embedding vector for a given text.
        
        Args:
            text: The text to embed.
            model_name: The embedding model to use.
            
        Returns:
            A list of floats representing the embedding vector.
        """
        response = await self._client.aio.models.embed_content(
            model=model_name,
            contents=text,
        )
        call_id = str(uuid.uuid4())
        logger.log_llm("LLMClient", "generate_embedding", "ok", 
                       call_id=call_id, pipeline_stage="embedding", model_name=model_name,
                       tokens_in=None, tokens_out=None, query_usage=0)
        return response.embeddings[0].values

    def generate_embedding_sync(self, text: str, model_name: str = "gemini-embedding-001") -> list[float]:
        """
        Synchronously generate an embedding vector for a given text.
        
        Args:
            text: The text to embed.
            model_name: The embedding model to use.
            
        Returns:
            A list of floats representing the embedding vector.
        """
        response = self._client.models.embed_content(
            model=model_name,
            contents=text,
        )
        call_id = str(uuid.uuid4())
        logger.log_llm("LLMClient", "generate_embedding_sync", "ok", 
                       call_id=call_id, pipeline_stage="embedding", model_name=model_name,
                       tokens_in=None, tokens_out=None, query_usage=0)
        return response.embeddings[0].values

