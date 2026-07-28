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


class LLMCallRecord:
    """Lightweight container for a single LLM call's telemetry."""

    __slots__ = ("call_id", "pipeline_stage", "model_name",
                 "input_tokens", "output_tokens", "query_usage",
                 "prompt", "output")

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
    ) -> None:
        self.call_id = call_id
        self.pipeline_stage = pipeline_stage
        self.model_name = model_name
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.query_usage = query_usage
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
        meta = response.usage_metadata
        input_tokens = meta.prompt_token_count if meta else 0
        output_tokens = meta.candidates_token_count if meta else 0

        query_usage = 0
        if response.candidates and response.candidates[0].grounding_metadata:
            gm = response.candidates[0].grounding_metadata
            if hasattr(gm, "web_search_queries") and gm.web_search_queries:
                query_usage = len(gm.web_search_queries)

        self._logs.append(LLMCallRecord(
            call_id=call_id,
            pipeline_stage=pipeline_stage,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            query_usage=query_usage,
            prompt=prompt,
            output=response.text or "",
        ))

        return response.text or ""

    async def call_with_grounding(
        self,
        model_name: str,
        system_prompt: str,
        prompt: str,
        pipeline_stage: str = "unknown",
        max_output_tokens: int = 1024,
        temperature: Optional[float] = None,
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
            
        gen_config = types.GenerateContentConfig(**config_kwargs)

        response = await self._client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
            config=gen_config,
        )

        # Extract grounding URLs
        extracted_urls: list[str] = []
        if response.candidates and response.candidates[0].grounding_metadata:
            gm = response.candidates[0].grounding_metadata
            if hasattr(gm, "grounding_chunks"):
                for chunk in gm.grounding_chunks:
                    if hasattr(chunk, "web") and chunk.web:
                        uri = getattr(chunk.web, "uri", None) or getattr(chunk.web, "title", None)
                        if uri:
                            extracted_urls.append(uri)

        # Telemetry
        call_id = str(uuid.uuid4())
        meta = response.usage_metadata
        input_tokens = meta.prompt_token_count if meta else 0
        output_tokens = meta.candidates_token_count if meta else 0
        query_usage = 0
        if response.candidates and response.candidates[0].grounding_metadata:
            gm = response.candidates[0].grounding_metadata
            if hasattr(gm, "web_search_queries") and gm.web_search_queries:
                query_usage = len(gm.web_search_queries)

        self._logs.append(LLMCallRecord(
            call_id=call_id,
            pipeline_stage=pipeline_stage,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            query_usage=query_usage,
            prompt=prompt,
            output=response.text or "",
        ))

        return response.text or "", extracted_urls

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
        return response.embeddings[0].values

