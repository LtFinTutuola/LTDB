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
    overfitting_errors = _check_anti_overfitting(candidate, state.must_split_groups)
    if overfitting_errors:
        contrastive_errors.extend(overfitting_errors)

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


def _longest_common_prefix(strings: List[str]) -> str:
    if not strings:
        return ""
    s1 = min(strings)
    s2 = max(strings)
    for i, ch in enumerate(s1):
        if ch != s2[i]:
            return s1[:i]
    return s1


def _longest_common_suffix(strings: List[str], prefix_len: int = 0) -> str:
    if not strings:
        return ""
    rev_strings = [s[prefix_len:][::-1] for s in strings]
    rev_lcp = _longest_common_prefix(rev_strings)
    return rev_lcp[::-1]


def build_contrastive_structural_hints(
    must_split_groups: List[List[str]],
    must_merge_groups: List[List[str]],
) -> str:
    """
    Deterministically computes exact string prefix/suffix boundaries from Must-Merge
    and Must-Split groups so the LLM does not have to guess character counts.
    """
    hints: List[str] = []
    inferred_root_len: int | None = None
    inferred_color_len: int | None = None

    # 1. Exact divergence analysis within Must-Merge groups
    for idx, grp in enumerate(must_merge_groups or [], 1):
        clean_grp = [c.strip() for c in grp if c and c.strip()]
        if len(clean_grp) >= 2:
            lcp = _longest_common_prefix(clean_grp)
            lcs = _longest_common_suffix(clean_grp, prefix_len=len(lcp))
            diff_segments = [c[len(lcp): len(c) - len(lcs)] for c in clean_grp]
            if len(lcs) == 0 and len(lcp) > 0:
                suffix_lens = sorted({len(c) - len(lcp) for c in clean_grp})
                max_k = max(suffix_lens)
                inferred_root_len = len(lcp)
                if len(suffix_lens) == 1:
                    inferred_color_len = max_k
                    hints.append(
                        f"  * [ANALISI MATEMATICA MUST-MERGE Gruppo {idx} {clean_grp}]: "
                        f"Il prefisso comune identico è '{lcp}' (primi {len(lcp)} caratteri), "
                        f"mentre i segmenti che variano per il colore sono {diff_segments} "
                        f"(esattamente gli ULTIMI {max_k} caratteri). "
                        f"Pertanto, il gruppo '(?P<color_code>...)' DEVE catturare almeno gli ultimi {max_k} caratteri "
                        f"(lasciando al massimo i primi {len(lcp)} caratteri come radice modello)!"
                    )
                else:
                    hints.append(
                        f"  * [ANALISI MATEMATICA MUST-MERGE Gruppo {idx} {clean_grp}]: "
                        f"Il prefisso comune identico è '{lcp}' (primi {len(lcp)} caratteri), "
                        f"mentre i suffissi colore variabili sono {diff_segments} (lunghezza da {min(suffix_lens)} a {max_k} caratteri). "
                        f"Pertanto, '(?P<color_code>...)' DEVE catturare l'intero suffisso dopo i primi {len(lcp)} caratteri!"
                    )
            elif len(lcp) > 0 or len(lcs) > 0:
                hints.append(
                    f"  * [ANALISI MATEMATICA MUST-MERGE Gruppo {idx} {clean_grp}]: "
                    f"Prefisso comune = '{lcp}' ({len(lcp)} caratteri), suffisso comune = '{lcs}' ({len(lcs)} caratteri), "
                    f"segmento variabile colore = {diff_segments}. Il gruppo '(?P<color_code>...)' DEVE coprire l'intero segmento {diff_segments}!"
                )

    # 2. Exact divergence analysis across Must-Split groups
    valid_split_groups = [[c.strip() for c in g if c and c.strip()] for g in (must_split_groups or [])]
    valid_split_groups = [g for g in valid_split_groups if g]
    if len(valid_split_groups) >= 2:
        group_reps = [_longest_common_prefix(g) if len(g) >= 2 else g[0] for g in valid_split_groups]
        for i in range(len(group_reps)):
            for j in range(i + 1, len(group_reps)):
                r1, r2 = group_reps[i], group_reps[j]
                shared_p = _longest_common_prefix([r1, r2])
                cut_len = inferred_root_len if inferred_root_len and inferred_root_len > len(shared_p) else min(len(r1), len(r2))
                d1 = r1[len(shared_p):cut_len]
                d2 = r2[len(shared_p):cut_len]
                if d1 and d2 and d1 != d2:
                    hints.append(
                        f"  * [ANALISI MATEMATICA MUST-SPLIT Gruppo {i + 1} vs Gruppo {j + 1}]: "
                        f"Condividono i primi {len(shared_p)} caratteri ('{shared_p}') e si distinguono nei caratteri "
                        f"dal {len(shared_p) + 1}° al {cut_len}° ('{d1}' vs '{d2}'). "
                        f"Quindi i primi {cut_len} caratteri identificano il modello e NON devono MAI essere catturati in '(?P<color_code>...)'!"
                    )

        if inferred_root_len is not None and inferred_color_len is not None:
            all_codes = [c for g in valid_split_groups for c in g]
            if all(len(c) == inferred_root_len + inferred_color_len for c in all_codes):
                distinct_roots = [g[0][:inferred_root_len] for g in valid_split_groups]
                if len(set(distinct_roots)) == len(valid_split_groups):
                    hints.append(
                        f"  * [CONCLUSIONE MATEMATICA ESATTA]: Tutti i codici analizzati ({len(all_codes[0])} caratteri) "
                        f"si dividono esattamente in: primi {inferred_root_len} caratteri = codice base modello "
                        f"(es. {distinct_roots}), ULTIMI {inferred_color_len} caratteri = variante colore 'color_code'."
                    )

    if not hints:
        return ""
    return (
        "**VINCOLI MATEMATICI DETERMINISTICI SULLA LUNGHEZZA DEI SEGMENTI (CALCOLATI AUTOMATICAMENTE):**\n"
        + "\n".join(hints) + "\n"
    )


def _check_anti_overfitting(
    candidate_regex: str,
    must_split_groups: List[List[str]],
) -> List[str]:
    """
    Ensures the synthesized regex does not overfit to the specific model codes of the split groups
    by hardcoding literal alternations like `^P10QMT(71|56)(?P<color_code>...)` outside `color_code`.
    """
    if not candidate_regex or not must_split_groups or len(must_split_groups) < 2:
        return []

    # Remove the (?P<color_code>...) group content before checking for model alternations
    regex_without_color = re.sub(r"\(\?P<color_code>[^)]+\)", "", candidate_regex)

    all_split_codes = [c.strip().upper() for g in must_split_groups for c in g if c and c.strip()]
    if len(all_split_codes) < 2:
        return []

    errors: List[str] = []
    for m in re.finditer(r"\((?:\?:|\?P<[a-zA-Z0-9_]+>)?([A-Za-z0-9]+(?:\|[A-Za-z0-9]+)+)\)", regex_without_color):
        alt_expr = m.group(1)
        tokens = [t.strip().upper() for t in alt_expr.split("|") if t.strip()]
        if len(tokens) >= 2 and all(any(tok in code for code in all_split_codes) for tok in tokens):
            errors.append(
                f"Overfitting rilevato nel regex: il pattern contiene un'alternanza letterale specifica dei modelli "
                f"('({alt_expr})'). È VIETATO elencare i singoli codici modello con '|'; usa classi di caratteri generiche "
                f"(es. '[A-Z0-9]{{N}}' o '[0-9]{{N}}') valide per l'intero brand!"
            )
    return errors


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
                lcp = _longest_common_prefix(clean_grp)
                lcs = _longest_common_suffix(clean_grp, prefix_len=len(lcp))
                math_hint = ""
                if len(lcs) == 0 and len(lcp) > 0:
                    diff_parts = [c[len(lcp):] for c in clean_grp]
                    max_k = max(len(d) for d in diff_parts)
                    math_hint = (
                        f" [NOTA MATEMATICA: il prefisso comune è '{lcp}' (primi {len(lcp)} caratteri) e "
                        f"i suffissi colore divergenti sono {diff_parts} (ultimi {max_k} caratteri); "
                        f"'(?P<color_code>...)' DEVE catturare tutti gli ultimi {max_k} caratteri!]"
                    )
                errors.append(
                    f"Vincolo Must-Merge violato per il gruppo {idx + 1}: i codici {clean_grp} devono generare "
                    f"lo stesso codice normalizzato, ma hanno prodotto: {mapping_str}{math_hint}"
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


