"""
validation_node.py
------------------
Validates the candidate regex against every input vendor code.
If all codes match, populates the output fields and marks success.
If any codes fail, records them for retry context.
Also validates ReDoS safety via compile check and timed execution.
"""
import re
import time
from typing import List

from src.agents.codes_deduction_agent.state import CodesDeductionGraphState
from src.core.logger import get_logger

logger = get_logger()

# Maximum time allowed per regex match (in seconds)
_MATCH_TIMEOUT_SECONDS = 0.1


def _validate_regex_safety(pattern: str, sample_codes: List[str]) -> str:
    """
    Validate that a regex pattern is safe to use:
    1. Must compile without errors
    2. Must not exhibit catastrophic backtracking (tested via timed execution)

    Returns an error message string if unsafe, or empty string if safe.
    """
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return f"Regex compilation failed: {exc}"

    # Test execution time on sample codes
    for code in sample_codes[:20]:
        start = time.monotonic()
        try:
            compiled.match(code)
        except Exception:
            pass
        elapsed = time.monotonic() - start
        if elapsed > _MATCH_TIMEOUT_SECONDS:
            return (
                f"Regex execution exceeded timeout ({elapsed:.3f}s > {_MATCH_TIMEOUT_SECONDS}s) "
                f"on code '{code}'. Possible catastrophic backtracking (ReDoS)."
            )

    return ""


def validation_node(state: CodesDeductionGraphState) -> dict:
    """
    Validate the candidate regex against all vendor codes.
    Determines routing: success → END, failure → retry or error.
    """
    logger.log_agent(
        "validation_node", "node_entry", "ok",
        iteration=state.iteration,
        candidate_regex=state.candidate_regex,
    )
    print(f"[validation_node] Validating regex '{state.candidate_regex}' against {len(state.vendor_codes)} codes...")

    candidate = state.candidate_regex

    if not candidate:
        print("[validation_node] Empty regex candidate. Marking as failure.")
        return {
            "validation_failures": state.vendor_codes[:10],
            "success": False,
            "error_message": "Regex synthesis produced an empty pattern." if state.iteration >= state.max_iterations else "",
        }

    # Safety check
    safety_error = _validate_regex_safety(candidate, state.vendor_codes)
    if safety_error:
        print(f"[validation_node] Safety check failed: {safety_error}")
        return {
            "validation_failures": state.vendor_codes[:10],
            "success": False,
            "error_message": safety_error if state.iteration >= state.max_iterations else "",
        }

    # Validate against all codes
    try:
        compiled = re.compile(candidate)
    except re.error as exc:
        return {
            "validation_failures": state.vendor_codes[:10],
            "success": False,
            "error_message": f"Regex compilation failed: {exc}" if state.iteration >= state.max_iterations else "",
        }

    successes: list[dict] = []
    failures: list[str] = []

    for code in state.vendor_codes:
        match = compiled.match(code)
        if match and "model_code" in match.groupdict():
            model_code = match.group("model_code")
            successes.append({"raw": code, "normalized": model_code})
        else:
            failures.append(code)

    match_rate = len(successes) / len(state.vendor_codes) * 100 if state.vendor_codes else 0
    print(f"[validation_node] Match rate: {match_rate:.1f}% ({len(successes)}/{len(state.vendor_codes)})")

    if not failures:
        # All codes matched — success
        # Pick up to 5 diverse examples
        examples = successes[:5]
        print(f"[validation_node] Validation passed. Regex confirmed.")
        result = {
            "success": True,
            "output_regex": candidate,
            "output_examples": examples,
            "validation_failures": [],
            "error_message": "",
        }
        logger.log_agent("validation_node", "node_exit", "ok", output=result)
        return result
    else:
        # Some codes failed
        print(f"[validation_node] {len(failures)} codes failed validation.")
        error_msg = ""
        if state.iteration >= state.max_iterations:
            error_msg = (
                f"Regex validation failed after {state.max_iterations} attempts. "
                f"{len(failures)} codes could not be matched: {failures[:10]}"
            )
            print(f"[validation_node] Max retries exceeded. Reporting failure.")

        result = {
            "validation_failures": failures,
            "success": False,
            "error_message": error_msg,
        }
        logger.log_agent("validation_node", "node_exit", "ok", output=result)
        return result
