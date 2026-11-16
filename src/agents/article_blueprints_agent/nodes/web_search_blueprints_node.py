import asyncio
import re

from src.agents.llm_client import LLMClient
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.core.logger import get_logger
from src.utils.llm_models import BLUEPRINTS_WEB_SEARCH_MODEL as _MODEL
from src.utils.prompts import BLUEPRINTS_WEB_SEARCH_SYSTEM_PROMPT

logger = get_logger()


def _is_found(raw_text: str) -> bool:
    found_match = re.search(r"<FOUND>(.*?)</FOUND>", raw_text, re.DOTALL | re.IGNORECASE)
    if found_match and found_match.group(1).strip().lower() == "false":
        return False
    return True


def _parse_name_and_desc(raw_text: str, fallback_name: str, fallback_desc: str) -> tuple[str, str]:
    if not _is_found(raw_text):
        return fallback_name, fallback_desc

    name_match = re.search(r"<NAME>(.*?)</NAME>", raw_text, re.DOTALL | re.IGNORECASE)
    desc_match = re.search(r"<DESCRIPTION>(.*?)</DESCRIPTION>", raw_text, re.DOTALL | re.IGNORECASE)

    name = name_match.group(1).strip() if name_match else fallback_name
    desc = desc_match.group(1).strip() if desc_match else fallback_desc
    return name, desc


_RETRY_TEMPERATURES = (0.0, 0.3, 0.7)


def _has_grounding_evidence(client: LLMClient, extracted_urls: list[str]) -> bool:
    if extracted_urls:
        return True
    get_logs = getattr(client, "get_logs", None)
    logs = get_logs() if callable(get_logs) else None
    if isinstance(logs, list) and logs:
        last = logs[-1]
        if isinstance(last, dict):
            q_usage = last.get("query_usage")
            if isinstance(q_usage, int) and q_usage > 0:
                return True
            if last.get("grounding_detected") is True:
                return True
    return False


def _build_search_prompt(
    brand: str,
    code_label: str,
    code_value: str,
    description: str,
    is_barcode: bool = False,
) -> str:
    brand_first_token = brand.split()[0] if brand and brand.split() else brand
    if is_barcode:
        if description:
            search_strategy = (
                f"1. Primo tentativo: {brand} {code_value}\n"
                f"2. Secondo tentativo (se il primo non dà risultati pertinenti): {code_value} {brand_first_token}\n"
                f"3. Ultimo tentativo: {code_value}"
            )
        else:
            search_strategy = (
                f"1. Primo tentativo: {brand} {code_value}\n"
                f"2. Secondo tentativo (se il primo non dà risultati pertinenti): {code_value}"
            )
    elif description:
        search_strategy = (
            f"1. Primo tentativo: {brand} {description} {code_value}\n"
            f"2. Secondo tentativo (se il primo non dà risultati pertinenti): {brand} {code_value}\n"
            f"3. Ultimo tentativo: {code_value} {brand_first_token}"
        )
    else:
        search_strategy = (
            f"1. Primo tentativo: {brand} {code_value}\n"
            f"2. Secondo tentativo (se il primo non dà risultati pertinenti): {code_value} {brand_first_token}"
        )

    return (
        f"Trova il nome ufficiale e la descrizione del prodotto nel catalogo o e-commerce del brand.\n\n"
        f"--- DATI DI PARTENZA ---\n"
        f"Brand: {brand}\n"
        f"{code_label}: {code_value}\n"
        f"Nome/Descrizione Input: {description}\n\n"
        f"--- STRATEGIA DI RICERCA ---\n"
        f"{search_strategy}\n\n"
        f"Analizza i risultati web e restituisci <FOUND>true|false</FOUND>, <NAME>...</NAME> e <DESCRIPTION>...</DESCRIPTION>."
    )


async def _enrich_blueprint_cluster(bp: dict, brand: str, client: LLMClient) -> tuple[dict, list[str]]:
    items = bp.get("cluster_items", [])
    if not items:
        return bp, []

    # Use the first item to perform the search
    base_item = items[0]
    vendor_code = base_item.get("vendor_code") or base_item.get("VendorCode") or ""
    barcode = str(base_item.get("barcode") or base_item.get("Barcode") or base_item.get("ean") or "").strip()
    description = base_item.get("article_name") or base_item.get("description") or base_item.get("Description") or ""

    vendor_prompt = _build_search_prompt(
        brand=brand,
        code_label="Codice / Modello (VendorCode)",
        code_value=vendor_code,
        description=description,
        is_barcode=False,
    )
    has_distinct_barcode = bool(barcode and barcode != vendor_code)
    barcode_prompt = (
        _build_search_prompt(
            brand=brand,
            code_label="Codice a barre (EAN / Barcode)",
            code_value=barcode,
            description=description,
            is_barcode=True,
        )
        if has_distinct_barcode
        else None
    )

    fallback_name = description if description else f"{brand} {vendor_code}".strip()
    fallback_desc = description

    first_attempt_result: tuple[str, str] | None = None
    web_confirmed = False
    warning_list: list[str] = []
    name = fallback_name
    desc = fallback_desc

    try:
        from src.agents.base import AgentException

        for attempt_idx, temp in enumerate(_RETRY_TEMPERATURES, start=1):
            use_barcode = attempt_idx == 2 and barcode_prompt is not None
            current_prompt = barcode_prompt if use_barcode else vendor_prompt
            try:
                raw_text, extracted_urls = await client.call_with_grounding(
                    model_name=_MODEL,
                    system_prompt=BLUEPRINTS_WEB_SEARCH_SYSTEM_PROMPT,
                    prompt=current_prompt,
                    pipeline_stage="Stage 3 - Web Search Extraction",
                    max_output_tokens=4096,
                    temperature=temp,
                    thinking_level="LOW",
                )
            except Exception as attempt_exc:
                if "AgentException" in str(type(attempt_exc)) or first_attempt_result is None:
                    raise
                logger.log_agent(
                    "web_search_blueprints_node",
                    "retry_attempt_failed",
                    "warning",
                    vendor_code=vendor_code,
                    attempt=attempt_idx,
                    temperature=temp,
                    error=str(attempt_exc),
                )
                break

            found = _is_found(raw_text)
            cand_name, cand_desc = _parse_name_and_desc(raw_text, fallback_name, fallback_desc)
            if attempt_idx == 1:
                first_attempt_result = (cand_name, cand_desc)

            has_grounding = _has_grounding_evidence(client, extracted_urls)
            if has_grounding and found:
                name, desc = cand_name, cand_desc
                web_confirmed = True
                break

            if has_grounding and not found:
                if attempt_idx == 1 and barcode_prompt is not None:
                    logger.log_agent(
                        "web_search_blueprints_node",
                        "not_found_retry_with_barcode",
                        "warning",
                        vendor_code=vendor_code,
                        barcode=barcode,
                        attempt=attempt_idx,
                        temperature=temp,
                    )
                    continue
                break

            logger.log_agent(
                "web_search_blueprints_node",
                "grounding_missing_retry",
                "warning",
                vendor_code=vendor_code,
                attempt=attempt_idx,
                temperature=temp,
            )

        if not web_confirmed:
            if first_attempt_result is not None:
                name, desc = first_attempt_result
            unverified_warning = (
                f"Attenzione: i dati anagrafici per l'articolo '{name}' (cod. '{vendor_code}') "
                f"non sono stati confermati da una ricerca sul web."
            )
            warning_list.append(unverified_warning)
    except Exception as exc:
        warning = f"[web_search_blueprints_node] Cluster for '{vendor_code}': web search failed ({exc}). Using fallback."
        print(warning)
        name, desc = fallback_name, fallback_desc
        warning_list = [warning]
        # Propagate AgentException to halt ingestion as per requirements
        if "AgentException" in str(type(exc)):
            raise exc

    bp_copy = dict(bp)
    if not web_confirmed and warning_list:
        bp_copy["web_search_unverified"] = True
        bp_copy["warning_message"] = warning_list[0]
    # Apply to all items in the cluster
    for item in bp_copy.get("cluster_items", []):
        item["article_name"] = name
        item["article_description"] = desc
        item["description"] = desc
        if not web_confirmed and warning_list:
            item["web_search_unverified"] = True
            item["warning_message"] = warning_list[0]

    return bp_copy, warning_list


async def web_search_blueprints_node(state: BlueprintsGraphState) -> dict:
    """
    Perform concurrent web searches (temperature=0.0) for new blueprint clusters to attach canonical article_name and article_description.
    """
    logger.log_agent("web_search_blueprints_node", "node_entry", "ok", 
                     new_blueprints_count=len(state.new_blueprints))
    print(f"[web_search_blueprints_node] Running web search (temp=0.0) for {len(state.new_blueprints)} cluster(s)...")
    
    if not state.new_blueprints:
        return {"new_blueprints": [], "warnings": []}
        
    client = LLMClient()
    tasks = [
        _enrich_blueprint_cluster(bp, state.brand_name, client)
        for bp in state.new_blueprints
    ]
    results = await asyncio.gather(*tasks)

    enriched_blueprints = [res[0] for res in results]
    warnings = []
    for res in results:
        if res[1]:
            warnings.extend(res[1])

    print("[web_search_blueprints_node] Web search complete.")
    result = {"new_blueprints": enriched_blueprints, "warnings": warnings}
    logger.log_agent("web_search_blueprints_node", "node_exit", "ok", output={"warnings_count": len(warnings)})
    return result
