"""
nodes/web_search_node.py
-------------------------
Stage 3a: Web-search enrichment for a single product item.

Uses Google Search grounding to find the official product page and
extract colours, materials, dimensions, and description, wrapped in
XML-like tags that downstream nodes can parse.

Failure is NON-FATAL: a warning is recorded and the pipeline continues
with empty web data so the remaining enrichment nodes can still run.
"""
import re

from src.agents.llm_client import LLMClient
from src.agents.data_ingestion_agent.state import ItemState

_SYSTEM_PROMPT = (
    "You are an AI Agent specialized in Product Data Enrichment for a retail ERP system. "
    "Your goal is to browse the web, find the official technical sheet or e-commerce page "
    "of the requested product and return a comprehensive description of the product, "
    "including all its details, such as product colors, materials, dimensions and a detailed description.\n"
    "CRITICAL RULES:\n"
    "1. Use the Google search tool to find real specifications.\n"
    "2. MUST wrap specific information in exact XML-like tags:\n"
    "   <COLORS>...</COLORS>\n"
    "   <MATERIALS>...</MATERIALS>\n"
    "   <DIMENSIONS>...</DIMENSIONS>\n"
    "   <DESCRIPTION>...</DESCRIPTION>\n"
    "3. If a data point is not available online, write 'Dato non disponibile' inside its tag.\n"
    "4. IMPORTANT: Write the output in Italian. DO NOT cite textual sources or generate a <SOURCES> tag."
)

_MODEL = "gemini-3.1-flash-lite"


def _parse_web_search_output(raw_text: str) -> str:
    """Extract and reassemble the XML-tagged sections from the web search response."""
    colors = re.search(r"<COLORS>(.*?)</COLORS>", raw_text, re.DOTALL)
    materials = re.search(r"<MATERIALS>(.*?)</MATERIALS>", raw_text, re.DOTALL)
    description = re.search(r"<DESCRIPTION>(.*?)</DESCRIPTION>", raw_text, re.DOTALL)
    dimensions = re.search(r"<DIMENSIONS>(.*?)</DIMENSIONS>", raw_text, re.DOTALL)

    parts: list[str] = []
    if description:
        parts.append(f"DESCRIPTION:\n{description.group(1).strip()}")
    if dimensions:
        parts.append(f"DIMENSIONS:\n{dimensions.group(1).strip()}")
    if colors:
        parts.append(f"COLORS:\n{colors.group(1).strip()}")
    if materials:
        parts.append(f"MATERIALS:\n{materials.group(1).strip()}")

    return "\n\n".join(parts)


async def web_search_node(state: ItemState) -> dict:
    """
    Perform a web search to enrich a single product item.

    Returns:
        Partial ItemState update with web_search_raw, web_search_parsed,
        and web_search_urls. On failure, returns empty strings/lists and
        appends a warning.
    """
    vendor_code = state.item.get("VendorCode", "")
    description = state.item.get("Description", "")
    color = state.item.get("Color", "")
    brand = state.brand

    print(f"[web_search_node] Stage 3a for SKU: {vendor_code}")

    prompt = (
        f"Find all technical and commercial specifications for this product.\n\n"
        f"--- STARTING DATA ---\n"
        f"Brand: {brand}\n"
        f"SKU / Model: {vendor_code}\n"
        f"Original Description: {description}\n"
        f"Color Code: {color}\n\n"
        f"--- SUGGESTED TARGET SEARCH QUERY ---\n"
        f'"{brand} {vendor_code}" OR "{brand} {description}"\n\n'
        f"Analyze the web results and return the detailed description with the required XML tags."
    )

    client = LLMClient()

    try:
        raw_text, urls = await client.call_with_grounding(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 3a - Web Search",
            max_output_tokens=1024,
        )
        parsed_text = _parse_web_search_output(raw_text)
        return {
            "web_search_raw": raw_text,
            "web_search_parsed": parsed_text,
            "web_search_urls": urls,
        }
    except Exception as exc:
        warning = f"[web_search_node] SKU '{vendor_code}': web search failed ({exc})."
        print(warning)
        return {
            "web_search_raw": "",
            "web_search_parsed": "",
            "web_search_urls": [],
            "warnings": [warning],
        }
