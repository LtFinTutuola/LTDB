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
        # Empty regex means no color masking (each vendor code is its own model)
        print("[validation_node] Empty regex candidate. No masking needed.")
        norm_map = {c: c for c in state.vendor_codes}
        contrastive_errors = _check_contrastive_constraints(norm_map, state.must_split_groups, state.must_merge_groups)
        if contrastive_errors:
            error_msg = "; ".join(contrastive_errors) if state.iteration >= state.max_iterations else ""
            return {
                "validation_failures": [],
                "contrastive_errors": contrastive_errors,
                "success": False,
                "error_message": error_msg,
            }
        examples = [{"raw": c, "normalized": c} for c in state.vendor_codes[:5]]
        result = {
            "success": True,
            "output_regex": "",
            "output_examples": examples,
            "validation_failures": [],
            "contrastive_errors": [],
            "error_message": "",
        }
        logger.log_agent("validation_node", "node_exit", "ok", output=result)
        return result

    # Safety check
    safety_error = _validate_regex_safety(candidate, state.vendor_codes)
    if safety_error:
        print(f"[validation_node] Safety check failed: {safety_error}")
        return {
            "validation_failures": state.vendor_codes[:10],
            "contrastive_errors": [],
            "success": False,
            "error_message": safety_error if state.iteration >= state.max_iterations else "",
        }

    # Validate against all codes
    try:
        compiled = re.compile(candidate)
    except re.error as exc:
        return {
            "validation_failures": state.vendor_codes[:10],
            "contrastive_errors": [],
            "success": False,
            "error_message": f"Regex compilation failed: {exc}" if state.iteration >= state.max_iterations else "",
        }

    successes: list[dict] = []
    failures: list[str] = []
    norm_map: dict[str, str] = {}

    for code in state.vendor_codes:
        match = compiled.search(code)
        if match:
            if "color_code" in match.groupdict() and match.group("color_code") is not None:
                normalized_code = code[:match.start("color_code")] + "#" + code[match.end("color_code"):]
            else:
                normalized_code = code
            successes.append({"raw": code, "normalized": normalized_code})
            norm_map[code] = normalized_code
        else:
            failures.append(code)

    # Also compute normalized codes for any codes in must_split_groups / must_merge_groups
    for grp in (state.must_split_groups or []) + (state.must_merge_groups or []):
        for c in grp:
            if c not in norm_map:
                m = compiled.search(c)
                if m:
                    if "color_code" in m.groupdict() and m.group("color_code") is not None:
                        norm_map[c] = c[:m.start("color_code")] + "#" + c[m.end("color_code"):]
                    else:
                        norm_map[c] = c
                else:
                    if c not in failures:
                        failures.append(c)

    contrastive_errors = _check_contrastive_constraints(
        norm_map, state.must_split_groups, state.must_merge_groups
    )

    match_rate = len(successes) / len(state.vendor_codes) * 100 if state.vendor_codes else 0
    print(f"[validation_node] Match rate: {match_rate:.1f}% ({len(successes)}/{len(state.vendor_codes)}), contrastive_errors={len(contrastive_errors)}")

    if not failures and not contrastive_errors:
        # All codes matched and contrastive constraints passed — success
        examples = successes[:5]
        print(f"[validation_node] Validation passed. Regex confirmed.")
        result = {
            "success": True,
            "output_regex": candidate,
            "output_examples": examples,
            "validation_failures": [],
            "contrastive_errors": [],
            "error_message": "",
        }
        logger.log_agent("validation_node", "node_exit", "ok", output=result)
        return result
    else:
        print(f"[validation_node] {len(failures)} codes failed validation, {len(contrastive_errors)} contrastive errors.")
        error_msg = ""
        if state.iteration >= state.max_iterations:
            parts = []
            if failures:
                parts.append(f"{len(failures)} codes could not be matched: {failures[:10]}")
            if contrastive_errors:
                parts.append(f"Contrastive constraints violated: {'; '.join(contrastive_errors)}")
            error_msg = f"Regex validation failed after {state.max_iterations} attempts. " + " | ".join(parts)
            print(f"[validation_node] Max retries exceeded. Reporting failure.")

        result = {
            "validation_failures": failures,
            "contrastive_errors": contrastive_errors,
            "success": False,
            "error_message": error_msg,
        }
        logger.log_agent("validation_node", "node_exit", "ok", output=result)
        return result


def _check_contrastive_constraints(
    norm_map: dict[str, str],
    must_split_groups: List[List[str]],
    must_merge_groups: List[List[str]],
) -> List[str]:
    """
    Deterministic Contrastive Sandbox validator:
    1. Must-Merge: all codes inside the same group in must_merge_groups must map to the identical normalized code.
    2. Must-Split: codes in different groups of must_split_groups must map to strictly disjoint normalized codes.
    """
    errors: List[str] = []

    # 1. Check Must-Merge
    for idx, grp in enumerate(must_merge_groups or []):
        clean_grp = [c for c in grp if c in norm_map]
        if len(clean_grp) >= 2:
            norms = {norm_map[c] for c in clean_grp}
            if len(norms) > 1:
                mapping_str = ", ".join(f"{c} -> {norm_map[c]}" for c in clean_grp)
                errors.append(
                    f"Vincolo Must-Merge violato per il gruppo {idx + 1}: i codici {clean_grp} devono generare "
                    f"lo stesso codice normalizzato, ma hanno prodotto: {mapping_str}"
                )

    # 2. Check Must-Split across distinct groups
    if must_split_groups and len(must_split_groups) >= 2:
        group_norms: List[tuple[int, dict[str, List[str]]]] = []
        for idx, grp in enumerate(must_split_groups):
            n_to_raw: dict[str, List[str]] = {}
            for c in grp:
                if c in norm_map:
                    n_to_raw.setdefault(norm_map[c], []).append(c)
            group_norms.append((idx + 1, n_to_raw))

        for i in range(len(group_norms)):
            for j in range(i + 1, len(group_norms)):
                g1_idx, g1_map = group_norms[i]
                g2_idx, g2_map = group_norms[j]
                shared_norms = set(g1_map.keys()) & set(g2_map.keys())
                for sn in shared_norms:
                    errors.append(
                        f"Vincolo Must-Split violato tra Gruppo {g1_idx} ({g1_map[sn]}) e Gruppo {g2_idx} ({g2_map[sn]}): "
                        f"entrambi generano lo stesso codice normalizzato '{sn}', ma sono stati separati dall'utente in modelli distinti!"
                    )

    return errors

