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


_SYSTEM_PROMPT = (
    "Riceverai un'analisi strutturale di codici prodotto e devi tradurla in un'espressione regolare Python.\n\n"
    "REGOLE OBBLIGATORIE:\n"
    "1. Il regex DEVE validare l'intera struttura morfologica del codice fornitore (è consigliato usare `^` e `$`).\n"
    "2. Se l'analisi indica la presenza di una variante colore, il regex DEVE contenere un named capture group chiamato 'color_code' che catturi ESATTAMENTE E SOLO il segmento colore.\n"
    "3. Se l'analisi indica che non c'è nessun codice colore, il regex DEVE validare la struttura del codice senza alcun gruppo 'color_code'. Non restituire mai una stringa vuota.\n"
    "4. DIVIETO DI OVERFITTING SUI MODELLI: Usa classi di caratteri generali (es. `[A-Z0-9]{8}` o `[A-Z]+[0-9]+`) per la struttura base. È SEVERAMENTE VIETATO inserire alternanze letterali con gli identificativi specifici dei modelli presenti negli esempi (es. VIETATO scrivere `(71|56)` o `(QMT71|QMT56)`): il regex deve matchare TUTTI i modelli presenti e futuri del brand!\n"
    "5. NON usare lookbehind/lookahead complessi che potrebbero causare backtracking catastrofico.\n"
    "6. Mantieni il regex il più semplice e leggibile possibile.\n\n"
    "Restituisci ESCLUSIVAMENTE un JSON con le seguenti chiavi:\n"
    "- 'regex': l'espressione regolare Python\n"
    "- 'explanation': spiegazione ASTRATTA e GENERICA della regola, valida per l'intero brand. "
    "È SEVERAMENTE VIETATO menzionare codici specifici o colori puntuali presenti negli esempi."
)


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
