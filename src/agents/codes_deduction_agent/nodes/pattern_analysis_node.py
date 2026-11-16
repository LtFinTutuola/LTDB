"""
pattern_analysis_node.py
------------------------
Analyzes a set of vendor codes to identify structural encoding patterns.
Instructs the LLM to describe the encoding scheme in natural language.
On retries, includes previously failed codes as additional context.
"""
import json
from pathlib import Path
import yaml

from src.agents.llm_client import LLMClient
from src.agents.codes_deduction_agent.state import CodesDeductionGraphState
from src.agents.codes_deduction_agent.nodes.validation_node import build_contrastive_structural_hints
from src.core.logger import get_logger

logger = get_logger()


from src.utils.llm_models import CODES_DEDUCTION_DEFAULT_MODEL


def _get_model() -> str:
    """Read the configurable model from gemini.yaml."""
    gemini_yaml_path = Path("src/agents/gemini.yaml")
    if gemini_yaml_path.exists():
        with open(gemini_yaml_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("codes_deduction_agent_model", CODES_DEDUCTION_DEFAULT_MODEL)
    return CODES_DEDUCTION_DEFAULT_MODEL


from src.utils.prompts import CODES_PATTERN_ANALYSIS_SYSTEM_PROMPT as _SYSTEM_PROMPT


async def pattern_analysis_node(state: CodesDeductionGraphState) -> dict:
    """
    Analyze vendor codes to identify encoding patterns.
    On retries, includes failure context from previous validation attempts.
    """
    iteration = state.iteration
    logger.log_agent(
        "pattern_analysis_node", "node_entry", "ok",
        iteration=iteration,
        vendor_codes_count=len(state.vendor_codes),
        has_failures=bool(state.validation_failures),
    )
    print(f"[pattern_analysis_node] Analyzing {len(state.vendor_codes)} vendor codes (iteration {iteration + 1})...")

    client = LLMClient()
    model = _get_model()

    # Build sample list (limit to 50 for prompt efficiency)
    sample_codes = state.vendor_codes[:50]
    codes_text = "\n".join(f"- {code}" for code in sample_codes)
    if len(state.vendor_codes) > 50:
        codes_text += f"\n... e altri {len(state.vendor_codes) - 50} codici"

    prompt = (
        f"**Brand:** {state.brand_name}\n\n"
        f"**Codici Fornitore da Analizzare:**\n{codes_text}\n\n"
    )

    # Add context from previous explanation if recalculating
    if state.previous_explanation and iteration == 0:
        prompt += (
            f"**NOTA:** Questo è un ricalcolo. La regola precedente era:\n"
            f"'{state.previous_explanation}'\n"
            f"La regola deve essere aggiornata per coprire anche i nuovi codici e i vincoli indicati dall'utente.\n\n"
        )

    # Add contrastive Must-Split constraints
    if state.must_split_groups and len(state.must_split_groups) >= 2:
        groups_desc = []
        for idx, grp in enumerate(state.must_split_groups, 1):
            groups_desc.append(f"  - Modello/Gruppo {idx}: {', '.join(grp)}")
        prompt += (
            "**VINCOLO TASSATIVO DI SEPARAZIONE (MUST-SPLIT):**\n"
            "L'operatore ha segnalato che i seguenti gruppi di codici appartengono a MODELLI FISICAMENTE DIVERSI "
            "(es. taglie, dimensioni o modelli differenti) e NON devono MAI generare lo stesso codice normalizzato:\n"
            + "\n".join(groups_desc) + "\n"
            "I caratteri che differenziano questi gruppi fanno parte del codice modello/taglia e NON devono essere inclusi nel codice colore!\n\n"
        )

    # Add contrastive Must-Merge constraints
    if state.must_merge_groups:
        merge_desc = []
        for idx, grp in enumerate(state.must_merge_groups, 1):
            if len(grp) >= 2:
                merge_desc.append(f"  - Varianti dello stesso modello {idx}: {', '.join(grp)}")
        if merge_desc:
            prompt += (
                "**VINCOLO TASSATIVO DI UNIONE (MUST-MERGE):**\n"
                "I seguenti codici sono varianti colore dello STESSO identico modello e DEVONO produrre lo stesso codice normalizzato:\n"
                + "\n".join(merge_desc) + "\n\n"
            )

    # Add deterministic mathematical breakdown of Must-Merge / Must-Split boundaries
    structural_hints = build_contrastive_structural_hints(state.must_split_groups, state.must_merge_groups)
    if structural_hints:
        prompt += structural_hints + "\n"

    # Add explicit user feedback / correction
    if state.user_feedback:
        prompt += (
            f"**CORREZIONE ESPLICita DELL'UTENTE (PRIORITÀ MASSIMA):**\n"
            f"L'utente ha corretto la deduzione precedente con questa indicazione:\n"
            f"\"{state.user_feedback}\"\n"
            f"Rispetta rigorosamente questa indicazione nella tua analisi strutturale.\n\n"
        )

    # Add contrastive errors from previous sandbox attempt
    if state.contrastive_errors:
        c_errors_text = "\n".join(f"- {err}" for err in state.contrastive_errors)
        prompt += (
            f"**ERRORE DI VALIDAZIONE CONTRASTIVA NEL TENTATIVO PRECEDENTE:**\n"
            f"{c_errors_text}\n"
            f"Correggi l'analisi affinché la parte catturata come colore non causi più questo errore.\n\n"
        )

    # Add failure context on retries
    if state.validation_failures:
        failures_text = "\n".join(f"- {code}" for code in state.validation_failures)
        prompt += (
            f"**ATTENZIONE:** Il tentativo precedente ha prodotto un regex che NON ha matchato "
            f"i seguenti codici:\n{failures_text}\n\n"
            f"Riesamina questi codici e aggiorna la tua analisi per includerli.\n"
        )

    prompt += "Analizza tutti i codici e identifica lo schema di codifica."

    try:
        raw_res = await client.call(
            model_name=model,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Heuristic Deduction - Pattern Analysis",
            response_mime_type="application/json",
            temperature=0.0,
        )
        parsed = json.loads(raw_res)
        analysis = json.dumps(parsed, ensure_ascii=False)
    except Exception as exc:
        print(f"[pattern_analysis_node] Warning: Analysis failed ({exc}). Using raw response.")
        analysis = str(raw_res) if 'raw_res' in dir() else f"Analysis failed: {exc}"

    print(f"[pattern_analysis_node] Analysis complete.")
    result = {
        "analysis_result": analysis,
        "iteration": iteration + 1,
    }
    logger.log_agent("pattern_analysis_node", "node_exit", "ok", output=result)
    return result
