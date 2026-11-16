"""
regex_synthesis_node.py
-----------------------
Translates the natural language pattern analysis into a Python regex
with a named capture group (?P<model_code>...) for the normalized vendor code.
"""
import json
from pathlib import Path
import yaml

from src.agents.llm_client import LLMClient
from src.agents.codes_deduction_agent.state import CodesDeductionGraphState
from src.agents.codes_deduction_agent.nodes.validation_node import build_contrastive_structural_hints
from src.core.logger import get_logger

logger = get_logger()


def _get_model() -> str:
    """Read the configurable model from gemini.yaml."""
    gemini_yaml_path = Path("src/agents/gemini.yaml")
    if gemini_yaml_path.exists():
        with open(gemini_yaml_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("codes_deduction_agent_model", "gemini-3.1-flash-lite")
    return "gemini-3.1-flash-lite"


from src.utils.prompts import CODES_REGEX_SYNTHESIS_SYSTEM_PROMPT as _SYSTEM_PROMPT


async def regex_synthesis_node(state: CodesDeductionGraphState) -> dict:
    """
    Synthesize a Python regex from the pattern analysis result.
    """
    logger.log_agent(
        "regex_synthesis_node", "node_entry", "ok",
        iteration=state.iteration,
    )
    print(f"[regex_synthesis_node] Synthesizing regex from pattern analysis...")

    client = LLMClient()
    model = _get_model()

    # Provide a few sample codes for the LLM to test against mentally
    sample_codes = state.vendor_codes[:15]
    samples_text = "\n".join(f"- {code}" for code in sample_codes)

    extra_constraints = ""
    if state.must_split_groups and len(state.must_split_groups) >= 2:
        grp_lines = [f"  * Gruppo {i+1}: {', '.join(g)}" for i, g in enumerate(state.must_split_groups)]
        extra_constraints += (
            "\n**Vincolo Must-Split (NON devono avere lo stesso codice normalizzato dopo aver mascherato `color_code` con `#`):**\n"
            + "\n".join(grp_lines) + "\n"
        )
    if state.must_merge_groups:
        m_lines = [f"  * Stesso modello: {', '.join(g)}" for g in state.must_merge_groups if len(g) >= 2]
        if m_lines:
            extra_constraints += (
                "\n**Vincolo Must-Merge (DEVONO avere lo stesso codice normalizzato dopo aver mascherato `color_code` con `#`):**\n"
                + "\n".join(m_lines) + "\n"
            )
    structural_hints = build_contrastive_structural_hints(state.must_split_groups, state.must_merge_groups)
    if structural_hints:
        extra_constraints += "\n" + structural_hints
    if state.user_feedback:
        extra_constraints += f"\n**Indicazione esplicita dell'utente:** \"{state.user_feedback}\"\n"
    if state.contrastive_errors:
        extra_constraints += "\n**Errori da correggere rispetto al tentativo precedente:**\n" + "\n".join(f"- {e}" for e in state.contrastive_errors) + "\n"

    prompt = (
        f"**Analisi Strutturale:**\n{state.analysis_result}\n\n"
        f"**Codici Esempio per Verifica:**\n{samples_text}\n"
        f"{extra_constraints}\n"
        f"Genera il regex Python con il named capture group 'color_code' (se presente il segmento colore) e la spiegazione in linguaggio naturale semplice."
    )

    try:
        raw_res = await client.call(
            model_name=model,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Heuristic Deduction - Regex Synthesis",
            response_mime_type="application/json",
            temperature=0.0,
        )
        parsed = json.loads(raw_res)
        candidate_regex = parsed.get("regex", "")
        explanation = parsed.get("explanation", "")
    except Exception as exc:
        print(f"[regex_synthesis_node] Warning: Synthesis failed ({exc}).")
        candidate_regex = ""
        explanation = ""

    print(f"[regex_synthesis_node] Candidate regex: {candidate_regex}")
    result = {
        "candidate_regex": candidate_regex,
        "output_explanation": explanation,
    }
    logger.log_agent("regex_synthesis_node", "node_exit", "ok", output=result)
    return result
