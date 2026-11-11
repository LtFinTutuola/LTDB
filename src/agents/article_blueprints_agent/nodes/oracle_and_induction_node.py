"""
oracle_and_induction_node
-------------------------
Per ogni cluster di nuovi articoli:
  1. cerca nella coorte DB del brand i candidati "stesso modello" (morfologia/embedding),
  2. interroga l'Oracolo di Equivalenza v3.0 (architettura neuro-simbolica a 5 micro-giudici paralleli),
  3. ordina i candidati e risolve con priorità: miglior SAME_MODEL, altrimenti miglior AMBIGUOUS, altrimenti DIFFERENT_MODEL,
  4. produce record di riconciliazione (``heuristic_reconciliations``):
     - MASK_UPDATE: richiede conferma (HITL) SOLO se modifica retroattivamente blueprint esistenti in DB;
     - DISAMBIGUATION: richiede conferma (HITL) per sciogliere ambiguità o casi di variante cromatica non dimostrabile;
     - NEGATIVE_CONSTRAINT: registrato solo se l'Oracolo certifica DIFFERENT_MODEL sotto la soglia dedicata (<= 20).

Il nodo è attivo solo se ``state.brand_id`` è valorizzato.
"""
from __future__ import annotations

import copy
import hashlib
import uuid
from typing import Any, Optional

from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.agents.article_blueprints_agent.nodes.model_equivalence_oracle import (
    AMBIGUOUS, DIFFERENT_MODEL, SAME_MODEL,
    OracleEvaluationResult, embed_entity,
    evaluate_pair, select_candidates,
)
from src.agents.llm_client import LLMClient
from src.core.logger import get_logger
from src.utils import vendor_code_normalizer as vcn

logger = get_logger()

STATUS_PENDING = "PENDING_USER_CONFIRMATION"
STATUS_NOT_REQUIRED = "NOT_REQUIRED"
MAX_DB_CANDIDATES = 3


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _cluster_codes(cluster: dict) -> list[str]:
    seen: list[str] = []
    for it in cluster.get("cluster_items", []):
        c = vcn.sterilize_separators(it.get("vendor_code") or it.get("VendorCode"))
        if c and c not in seen:
            seen.append(c)
    return seen


def _cluster_colors(cluster: dict) -> set[str]:
    out = set()
    for it in cluster.get("cluster_items", []):
        cols = it.get("colors") or it.get("Color") or []
        if isinstance(cols, str):
            cols = [cols]
        out.update(str(c).strip().lower() for c in cols if c and str(c).strip())
    return out


def _existing_rule_for(code: str, rules: list[dict]) -> Optional[dict]:
    r = vcn.find_applicable_rule(code, rules)
    if r:
        return r
    sig, ln = vcn.compute_invariant_signature(code), len(code)
    for r in rules:
        if r.get("type") == "FIXED" and r.get("length") == ln and r.get("signature") == sig:
            return r
    return None


def _induce_mask_for_candidate(
    codes: list[str],
    cand_codes: set[str],
    existing_rule: Optional[dict],
    negatives: list[list[str]],
) -> dict:
    """
    Calcola la maschera posizionale tra i codici in ingresso e quelli del candidato DB.
    Se il candidato contiene molteplici varianti storiche (eventualmente frutto di una
    precedente maschera errata), privilegia la variante a minima divergenza qualora
    la maschera della coppia differisca dalla regola precedente o l'insieme completo fallisca.
    """
    all_codes = sorted(set(codes) | set(cand_codes))
    if len(cand_codes) > 1 and codes:
        ref = codes[0]

        def _cand_dist(cc: str) -> tuple[int, int, int, int]:
            has_mask = 1 if vcn.MASK_CHAR in cc else 0
            same_len_penalty = 0 if len(cc) == len(ref) else 1
            common_pref = 0
            for x, y in zip(cc, ref):
                if x == y:
                    common_pref += 1
                else:
                    break
            diff_chars = abs(len(cc) - len(ref)) + sum(1 for x, y in zip(cc, ref) if x != y)
            return (has_mask, same_len_penalty, -common_pref, diff_chars)

        best_cand = min(sorted(cand_codes), key=_cand_dist)
        pair_res = vcn.induce_positional_mask(sorted(set(codes) | {best_cand}), existing_rule, negatives)
        full_res = vcn.induce_positional_mask(all_codes, existing_rule, negatives)
        if full_res["status"] != vcn.OK and pair_res["status"] == vcn.OK:
            return pair_res
        if (
            pair_res["status"] == vcn.OK
            and existing_rule
            and existing_rule.get("masked_positions")
            and pair_res["rule"].get("masked_positions") != existing_rule.get("masked_positions")
        ):
            return pair_res
        return full_res
    return vcn.induce_positional_mask(all_codes, existing_rule, negatives)


def _affected_db_blueprints(cohort: list[dict], rules_after: list[dict]) -> list[dict]:
    """Blueprint DB il cui codice normalizzato cambierebbe applicando ``rules_after``."""
    affected = []
    for bp in cohort:
        codes = [vcn.sterilize_separators(v.get("supplier_code")) for v in bp.get("variants", []) if v.get("supplier_code")]
        new_codes = {vcn.apply_rules(c, rules_after) for c in codes}
        new_codes.discard(None)
        current = bp.get("normalized_vendor_code")
        if new_codes and any(nc != current for nc in new_codes):
            affected.append({**bp, "_new_normalized": sorted(new_codes)[0]})
    return affected


def _db_bp_view(bp: dict) -> dict:
    raw_code = None
    for v in bp.get("variants", []):
        if v.get("supplier_code"):
            raw_code = v["supplier_code"]
            break
    if not raw_code:
        raw_code = bp.get("raw_vendor_code") or bp.get("normalized_vendor_code")

    return {
        "id": bp["id"],
        "article_name": bp.get("article_name"),
        "normalized_vendor_code": bp.get("normalized_vendor_code"),
        "raw_vendor_code": raw_code,
        "new_normalized_vendor_code": bp.get("_new_normalized"),
        "variants": [
            {"supplier_code": v.get("supplier_code"), "colors": list(v.get("colors") or [])}
            for v in bp.get("variants", [])
        ],
    }


def _incoming_view(cluster: dict) -> list[dict]:
    return [
        {
            "item_id": it.get("item_id"),
            "vendor_code": vcn.clean_code(it.get("vendor_code") or it.get("VendorCode")),
            "colors": list(it.get("colors") or []),
            "article_name": it.get("article_name") or cluster.get("article_name"),
        }
        for it in cluster.get("cluster_items", [])
    ]


def _set_item_norm(cluster: dict, code: str) -> None:
    for it in cluster.get("cluster_items", []):
        it["normalized_vendor_code"] = code


def _record(**kw: Any) -> dict:
    base = {
        "reconciliation_id": str(uuid.uuid4()),
        "status": STATUS_NOT_REQUIRED,
        "requires_confirmation": False,
        "kind": "MASK_UPDATE",
        "staging_blueprint_ids": [],
        "fallback_normalized_code": None,
        "incoming_items": [],
        "matched_db_blueprints": [],
        "proposed_heuristic_update": None,
        "rule_update": None,
        "mode_update": None,
        "negative_pairs": [],
        "merge_target_blueprint_id": None,
        "oracle": None,
    }
    base.update(kw)
    return base


def _proposal(rules_before: list[dict], rule: dict, sample: str, explanation: str) -> dict:
    return {
        "old_mask_preview": vcn.apply_rules(sample, rules_before),
        "new_mask_preview": vcn.apply_rule(sample, rule),
        "explanation": explanation,
    }


def _serialize_dimensions(dims: Any) -> dict:
    """Garantisce che tutte le valutazioni dimensionali siano serializzabili in JSON."""
    if not isinstance(dims, dict):
        return {}
    res = {}
    for k, v in dims.items():
        if hasattr(v, "model_dump"):
            res[k] = v.model_dump()
        elif hasattr(v, "to_dict"):
            res[k] = v.to_dict()
        elif isinstance(v, dict):
            res[k] = v
        else:
            res[k] = str(v)
    return res


# ---------------------------------------------------------------------------
# Nodo
# ---------------------------------------------------------------------------

async def oracle_and_induction_node(state: BlueprintsGraphState) -> dict:
    if not state.brand_id or not state.new_blueprints:
        return {}

    logger.log_agent("oracle_and_induction_node", "node_entry", "ok", clusters=len(state.new_blueprints))
    ctx = state.brand_heuristic or {}
    rules: list[dict] = copy.deepcopy(ctx.get("rules") or [])
    negatives: list[list[str]] = copy.deepcopy(ctx.get("negative_constraints") or [])
    mode: str = ctx.get("mode") or "PENDING_COHORT"
    cohort = state.brand_cohort or []

    client = LLMClient()
    clusters = [dict(bp) for bp in state.new_blueprints]
    records: list[dict] = []
    warnings: list[str] = []
    attached_bps: list[dict] = []
    attached_items: list[dict] = []
    resolved_update: dict = {}

    def _attach(cluster: dict, cand: dict, norm_code: Optional[str]) -> None:
        for it in cluster.get("cluster_items", []):
            it["article_blueprint_id"] = cand["id"]
            if norm_code:
                it["normalized_vendor_code"] = norm_code
            it["needs_photo_only"] = True
            attached_items.append(it)
        cat_info = cand.get("category")
        sub_cat_info = cand.get("sub_category")
        if not cat_info and not sub_cat_info and (cand.get("category_id") or cand.get("category_name")):
            cat_info = {"id": str(cand.get("category_id")), "description": cand.get("category_name")} if cand.get("category_id") else cand.get("category_name")
        attached_bp_data = {
            "id": cand["id"],
            "is_new": False,
            "article_name": cand.get("article_name"),
            "description": cand.get("description"),
            "extended_description": cand.get("extended_description"),
            "tags": list(cand.get("tags") or []),
            "materials": list(cand.get("materials") or []),
            "dimensions": cand.get("dimensions"),
            "category": cat_info,
            "sub_category": sub_cat_info,
            "cluster_items": cluster.get("cluster_items", []),
        }
        attached_bps.append(attached_bp_data)
        resolved_data = {
            "id": cand["id"],
            "is_new": False,
            "article_name": cand.get("article_name"),
            "description": cand.get("description"),
            "extended_description": cand.get("extended_description"),
            "tags": list(cand.get("tags") or []),
            "materials": list(cand.get("materials") or []),
            "dimensions": cand.get("dimensions"),
            "category": cat_info,
            "sub_category": sub_cat_info,
        }
        resolved_update[cand["id"]] = resolved_data

    # ---------------- Passata A: cluster vs coorte DB ----------------
    remaining: list[dict] = []
    for cluster in clusters:
        codes = _cluster_codes(cluster)
        if not codes or not cohort:
            remaining.append(cluster)
            continue

        embedding = await embed_entity(cluster, client)
        cands = select_candidates(codes, embedding, cohort, top_k=MAX_DB_CANDIDATES)
        if not cands:
            remaining.append(cluster)
            continue

        # Valuta tutti i candidati selezionati
        evaluated_cands: list[tuple[Any, dict]] = []
        for cand in cands:
            eval_res = await evaluate_pair(client, cluster, cand)
            evaluated_cands.append((eval_res, cand))

        def _get_score(r: Any) -> float:
            if hasattr(r, "final_score"):
                return float(getattr(r, "final_score"))
            if isinstance(r, dict):
                return float(r.get("final_score", 100.0))
            return 100.0

        def _get_code_rank(r: Any) -> int:
            cv = getattr(r, "code_verdict", "NEUTRAL") if hasattr(r, "code_verdict") else (
                r.get("code_verdict", "NEUTRAL") if isinstance(r, dict) else "NEUTRAL"
            )
            return {"IDENTICAL": 4, "VERY_HIGH": 3, "HIGH": 2, "NEUTRAL": 1, "CONFLICT": 0}.get(str(cv), 1)

        def _cand_sort_key(t: tuple[Any, dict]) -> tuple[int, float, int]:
            r = t[0]
            cr = _get_code_rank(r)
            tier = 1 if cr >= 2 else (0 if cr == 1 else -1)
            return (tier, _get_score(r), cr)

        def _get_relation(r: Any) -> str:
            rel = getattr(r, "color_relation", "UNKNOWN") if hasattr(r, "color_relation") else (
                r.get("color_relation", "UNKNOWN") if isinstance(r, dict) else "UNKNOWN"
            )
            if rel in ("DIFFERENT_COLOR", "DIFFERENT"):
                return "DIFFERENT"
            if rel in ("SAME_COLOR", "SAME"):
                return "SAME"
            return "UNKNOWN"

        same_cands = [(r, c) for r, c in evaluated_cands if getattr(r, "verdict", None) == SAME_MODEL or (isinstance(r, dict) and r.get("verdict") == SAME_MODEL)]
        ambig_cands = [(r, c) for r, c in evaluated_cands if getattr(r, "verdict", None) == AMBIGUOUS or (isinstance(r, dict) and r.get("verdict") == AMBIGUOUS)]
        diff_cands = [(r, c) for r, c in evaluated_cands if getattr(r, "verdict", None) == DIFFERENT_MODEL or (isinstance(r, dict) and r.get("verdict") == DIFFERENT_MODEL)]

        handled = False

        # Caso 1: Almeno un candidato ha verdetto SAME_MODEL (H4: vince compatibilità codice + punteggio più alto)
        if same_cands:
            same_cands.sort(key=_cand_sort_key, reverse=True)
            if len(same_cands) > 1:
                warnings.append(
                    f"oracle: molteplici candidati DB con verdetto SAME_MODEL per '{codes[0]}'. "
                    f"Selezionato '{same_cands[0][1].get('article_name')}' (score {_get_score(same_cands[0][0]):.1f})."
                )
            best_res, cand = same_cands[0]
            cand_codes = {vcn.sterilize_separators(v.get("supplier_code")) for v in cand.get("variants", []) if v.get("supplier_code")}
            if cand.get("normalized_vendor_code"):
                cand_codes.add(vcn.sterilize_separators(cand.get("normalized_vendor_code")))
            same_code = set(codes) & cand_codes
            relation = _get_relation(best_res)

            if same_code:
                if relation == "DIFFERENT":
                    # Q2: stesso codice, colori diversi -> COLOR_AGNOSTIC
                    if mode in ("PENDING_COHORT", "COLOR_AGNOSTIC"):
                        records.append(_record(
                            kind="MODE_UPDATE", mode_update="COLOR_AGNOSTIC",
                            incoming_items=_incoming_view(cluster)
                        ))
                        mode = "COLOR_AGNOSTIC"
                    _attach(cluster, cand, cand.get("normalized_vendor_code"))
                    handled = True
                else:
                    # Stesso codice ma colore non dimostrabilmente diverso (A3 / H3)
                    rec = _record(
                        kind="DISAMBIGUATION",
                        status=STATUS_PENDING,
                        requires_confirmation=True,
                        staging_blueprint_ids=[cluster["id"]],
                        incoming_items=_incoming_view(cluster),
                        matched_db_blueprints=[_db_bp_view(cand)],
                        merge_target_blueprint_id=cand["id"],
                        oracle={
                            "final_score": best_res.final_score,
                            "raw_score": best_res.raw_score,
                            "ambiguity_reason": "COLOR_NOT_DEMONSTRABLE",
                            "code_verdict": best_res.code_verdict,
                            "color_relation": best_res.color_relation,
                            "dimensions": _serialize_dimensions(getattr(best_res, "dimensions", {})),
                            "reasoning": "Codici identici ma variante cromatica non dimostrabile.",
                        },
                    )
                    cluster["_heuristic_reconciliation_id"] = rec["reconciliation_id"]
                    records.append(rec)
                    remaining.append(cluster)
                    handled = True
            else:
                # Codici diversi
                if relation == "DIFFERENT":
                    # Q1: stesso modello, colori diversi, codici diversi -> induzione posizionale
                    all_codes = sorted(set(codes) | cand_codes)
                    res = _induce_mask_for_candidate(codes, cand_codes, _existing_rule_for(all_codes[0], rules), negatives)
                    if res["status"] == vcn.OK:
                        new_rule = res["rule"]
                        new_rules = vcn.merge_rule(rules, new_rule)
                        affected = _affected_db_blueprints(cohort, new_rules)
                        explanation = (
                            "Le posizioni variabili del codice identificano il colore; "
                            "il resto del codice identifica il modello."
                        )
                        if affected:
                            # Assicura che il candidato bersaglio del confronto (cand) sia sempre al primo posto (indice 0)
                            cand_in_affected = any(a.get("id") == cand.get("id") for a in affected)
                            ordered_affected = [cand] + [a for a in affected if a.get("id") != cand.get("id")] if not cand_in_affected else sorted(affected, key=lambda a: 0 if a.get("id") == cand.get("id") else 1)
                            rec = _record(
                                kind="MASK_UPDATE", status=STATUS_PENDING, requires_confirmation=True,
                                staging_blueprint_ids=[cluster["id"]], incoming_items=_incoming_view(cluster),
                                matched_db_blueprints=[_db_bp_view(a) for a in ordered_affected],
                                proposed_heuristic_update=_proposal(rules, new_rule, codes[0], explanation),
                                rule_update=new_rule, mode_update=new_rule.get("heuristic_mode", "COLOR_MASKED"),
                                color_segment_index=new_rule.get("color_segment_index"),
                                merge_target_blueprint_id=cand["id"],
                                oracle={
                                    "final_score": _get_score(best_res),
                                    "raw_score": getattr(best_res, "raw_score", _get_score(best_res)),
                                    "ambiguity_reason": getattr(best_res, "ambiguity_reason", None),
                                    "code_verdict": getattr(best_res, "code_verdict", "NEUTRAL"),
                                    "color_relation": relation,
                                    "dimensions": _serialize_dimensions(getattr(best_res, "dimensions", {})),
                                    "reasoning": getattr(best_res, "reasoning", ""),
                                },
                            )
                            cluster["_heuristic_reconciliation_id"] = rec["reconciliation_id"]
                            records.append(rec)
                            remaining.append(cluster)  # resta blueprint separato finché l'utente non conferma
                        else:
                            records.append(_record(
                                kind="MASK_UPDATE", rule_update=new_rule,
                                mode_update=new_rule.get("heuristic_mode", "COLOR_MASKED"),
                                color_segment_index=new_rule.get("color_segment_index"),
                                incoming_items=_incoming_view(cluster)
                            ))
                            rules = new_rules
                            mode = "COLOR_MASKED"
                            _attach(cluster, cand, vcn.apply_rules(codes[0], rules) or cand.get("normalized_vendor_code"))
                        handled = True
                    else:
                        # Induzione fallita (H3): devia a DISAMBIGUATION
                        warnings.append(
                            f"induction: '{codes[0]}' vs '{cand.get('article_name')}' -> {res['status']}: "
                            f"induzione automatica fallita, richiesta disambiguazione manuale."
                        )
                        rec = _record(
                            kind="DISAMBIGUATION",
                            status=STATUS_PENDING,
                            requires_confirmation=True,
                            staging_blueprint_ids=[cluster["id"]],
                            incoming_items=_incoming_view(cluster),
                            matched_db_blueprints=[_db_bp_view(cand)],
                            merge_target_blueprint_id=cand["id"],
                            oracle={
                                "final_score": _get_score(best_res),
                                "raw_score": getattr(best_res, "raw_score", _get_score(best_res)),
                                "ambiguity_reason": "MASK_INDUCTION_FAILED",
                                "code_verdict": getattr(best_res, "code_verdict", "NEUTRAL"),
                                "color_relation": relation,
                                "dimensions": _serialize_dimensions(getattr(best_res, "dimensions", {})),
                                "reasoning": f"Stesso modello secondo l'Oracolo ma induzione maschera fallita ({res['status']}).",
                            },
                        )
                        cluster["_heuristic_reconciliation_id"] = rec["reconciliation_id"]
                        records.append(rec)
                        remaining.append(cluster)
                        handled = True
                else:
                    # Stesso modello ma colore non dimostrabilmente diverso (A3 / H3)
                    all_codes = sorted(set(codes) | cand_codes)
                    ind_res = _induce_mask_for_candidate(codes, cand_codes, _existing_rule_for(all_codes[0], rules), negatives)
                    proposed_rule = ind_res["rule"] if ind_res["status"] == vcn.OK else None
                    explanation = "Le posizioni variabili del codice identificano il colore; il resto identifica il modello."
                    rec = _record(
                        kind="DISAMBIGUATION",
                        status=STATUS_PENDING,
                        requires_confirmation=True,
                        staging_blueprint_ids=[cluster["id"]],
                        incoming_items=_incoming_view(cluster),
                        matched_db_blueprints=[_db_bp_view(cand)],
                        merge_target_blueprint_id=cand["id"],
                        oracle={
                            "final_score": _get_score(best_res),
                            "raw_score": getattr(best_res, "raw_score", _get_score(best_res)),
                            "ambiguity_reason": "COLOR_NOT_DEMONSTRABLE",
                            "code_verdict": getattr(best_res, "code_verdict", "NEUTRAL"),
                            "color_relation": relation,
                            "dimensions": _serialize_dimensions(getattr(best_res, "dimensions", {})),
                            "reasoning": f"Modello equivalente ma variante cromatica non dimostrabile ({relation}).",
                        },
                        proposed_heuristic_update=_proposal(rules, proposed_rule, codes[0], explanation) if proposed_rule else None,
                        rule_update=proposed_rule,
                        mode_update=proposed_rule.get("heuristic_mode", "COLOR_MASKED") if proposed_rule else None,
                        color_segment_index=proposed_rule.get("color_segment_index") if proposed_rule else None,
                    )
                    cluster["_heuristic_reconciliation_id"] = rec["reconciliation_id"]
                    records.append(rec)
                    remaining.append(cluster)
                    handled = True

        # Caso 2: Nessun SAME_MODEL, ma presente almeno un AMBIGUOUS
        elif ambig_cands:
            ambig_cands.sort(key=_cand_sort_key, reverse=True)
            best_res, cand = ambig_cands[0]
            cand_codes = {vcn.sterilize_separators(v.get("supplier_code")) for v in cand.get("variants", []) if v.get("supplier_code")}
            if cand.get("normalized_vendor_code"):
                cand_codes.add(vcn.sterilize_separators(cand.get("normalized_vendor_code")))

            # Calcolo di prova della maschera posizionale se l'utente dovesse confermare l'unione
            all_codes = sorted(set(codes) | cand_codes)
            ind_res = _induce_mask_for_candidate(codes, cand_codes, _existing_rule_for(all_codes[0], rules), negatives)
            proposed_rule = ind_res["rule"] if ind_res["status"] == vcn.OK else None
            affected = _affected_db_blueprints(cohort, vcn.merge_rule(rules, proposed_rule)) if proposed_rule else []

            explanation = "Le posizioni variabili del codice identificano il colore; il resto identifica il modello."
            rec = _record(
                kind="DISAMBIGUATION",
                status=STATUS_PENDING,
                requires_confirmation=True,
                staging_blueprint_ids=[cluster["id"]],
                incoming_items=_incoming_view(cluster),
                matched_db_blueprints=[_db_bp_view(cand)],
                merge_target_blueprint_id=cand["id"],
                oracle={
                    "final_score": _get_score(best_res),
                    "raw_score": getattr(best_res, "raw_score", _get_score(best_res)),
                    "ambiguity_reason": getattr(best_res, "ambiguity_reason", None) or "SCORE_BAND",
                    "code_verdict": getattr(best_res, "code_verdict", "NEUTRAL"),
                    "color_relation": _get_relation(best_res),
                    "dimensions": _serialize_dimensions(getattr(best_res, "dimensions", {})),
                    "reasoning": getattr(best_res, "reasoning", ""),
                },
                proposed_heuristic_update=_proposal(rules, proposed_rule, codes[0], explanation) if proposed_rule else None,
                rule_update=proposed_rule,
                mode_update=proposed_rule.get("heuristic_mode", "COLOR_MASKED") if proposed_rule else None,
                color_segment_index=proposed_rule.get("color_segment_index") if proposed_rule else None,
            )
            cluster["_heuristic_reconciliation_id"] = rec["reconciliation_id"]
            records.append(rec)
            remaining.append(cluster)
            handled = True

        # Caso 3: Tutti i candidati hanno verdetto DIFFERENT_MODEL
        else:
            for r, cand in diff_cands:
                cand_codes = {vcn.sterilize_separators(v.get("supplier_code")) for v in cand.get("variants", []) if v.get("supplier_code")}
                if cand.get("normalized_vendor_code"):
                    cand_codes.add(vcn.sterilize_separators(cand.get("normalized_vendor_code")))
                write_neg = getattr(r, "write_negative_constraint", False) if hasattr(r, "write_negative_constraint") else (r.get("write_negative_constraint", False) if isinstance(r, dict) else False)
                if write_neg:
                    # Registra vincolo negativo solo per codici della stessa famiglia morfologica
                    pairs = [[c, v] for c in codes for v in cand_codes if vcn.family_key(c) == vcn.family_key(v)]
                    if pairs:
                        records.append(_record(
                            kind="NEGATIVE_CONSTRAINT",
                            negative_pairs=pairs,
                            staging_blueprint_ids=[cluster["id"]],
                        ))
                        negatives.extend(pairs)
            remaining.append(cluster)
            handled = False

        if not handled and cluster not in remaining:
            remaining.append(cluster)

    # ---------------- Passata B: pivot in-batch (cluster vs cluster) ----------------
    alive = {c["id"]: c for c in remaining}
    reps = {}
    for c in remaining:
        codes = _cluster_codes(c)
        if codes and "_heuristic_reconciliation_id" not in c:
            reps.setdefault(codes[0], c["id"])
    pairs = vcn.select_pivot_pairs(list(reps.keys())) if len(reps) >= 2 else []
    merged_ids: set[str] = set()

    for c1, c2 in pairs:
        id1, id2 = reps.get(c1), reps.get(c2)
        if not id1 or not id2 or id1 == id2 or id1 in merged_ids or id2 in merged_ids:
            continue
        cl1, cl2 = alive.get(id1), alive.get(id2)
        if not cl1 or not cl2:
            continue

        verdict_res = await evaluate_pair(client, cl1, cl2)

        if verdict_res.verdict == DIFFERENT_MODEL:
            if verdict_res.write_negative_constraint:
                pr = [[c1, c2]]
                records.append(_record(
                    kind="NEGATIVE_CONSTRAINT", negative_pairs=pr,
                    staging_blueprint_ids=[id1, id2]
                ))
                negatives.extend(pr)
            continue

        if verdict_res.verdict == AMBIGUOUS:
            warnings.append(
                f"in-batch: '{c1}' vs '{c2}' ambiguo ({verdict_res.ambiguity_reason}): i cluster restano separati."
            )
            continue

        # verdict_res.verdict == SAME_MODEL
        col1, col2 = _cluster_colors(cl1), _cluster_colors(cl2)
        if col1 and col2:
            different = not (col1 & col2)
        else:
            different = verdict_res.color_relation == "DIFFERENT"

        if not different:
            continue

        res = vcn.induce_positional_mask([c1, c2], _existing_rule_for(c1, rules), negatives)
        if res["status"] != vcn.OK:
            warnings.append(f"induction in-batch: '{c1}' vs '{c2}' -> {res['status']}: nessuna maschera applicata.")
            continue

        new_rule = res["rule"]
        new_rules = vcn.merge_rule(rules, new_rule)

        # Applica la regola ai cluster vivi e fonde quelli che collassano sullo stesso codice normalizzato
        groups: dict[str, list[dict]] = {}
        for cl in list(alive.values()):
            code = _cluster_codes(cl)[0] if _cluster_codes(cl) else ""
            norm = vcn.apply_rules(code, new_rules) or code
            groups.setdefault(norm, []).append(cl)

        survivor_for_pair: Optional[dict] = None
        for norm, members in groups.items():
            if len(members) < 2:
                continue
            survivor = members[0]
            fallback = survivor["cluster_items"][0].get("normalized_vendor_code") or _cluster_codes(survivor)[0]
            for other in members[1:]:
                survivor["cluster_items"].extend(other.get("cluster_items", []))
                alive.pop(other["id"], None)
                merged_ids.add(other["id"])
            _set_item_norm(survivor, norm)
            if id1 in (survivor["id"],) or c1 in _cluster_codes(survivor):
                survivor_for_pair = survivor
            survivor["_fallback_norm"] = fallback

        affected = _affected_db_blueprints(cohort, new_rules)
        explanation = "Le posizioni variabili del codice identificano il colore; il resto identifica il modello."
        sv = survivor_for_pair or cl1

        if affected:
            rec = _record(
                kind="MASK_UPDATE", status=STATUS_PENDING, requires_confirmation=True,
                staging_blueprint_ids=[sv["id"]], fallback_normalized_code=sv.get("_fallback_norm"),
                incoming_items=_incoming_view(sv),
                matched_db_blueprints=[_db_bp_view(a) for a in affected],
                proposed_heuristic_update=_proposal(rules, new_rule, c1, explanation),
                rule_update=new_rule,
                mode_update=new_rule.get("heuristic_mode", "COLOR_MASKED"),
                color_segment_index=new_rule.get("color_segment_index"),
            )
            records.append(rec)
        else:
            records.append(_record(
                kind="MASK_UPDATE", rule_update=new_rule,
                mode_update=new_rule.get("heuristic_mode", "COLOR_MASKED"),
                color_segment_index=new_rule.get("color_segment_index"),
                staging_blueprint_ids=[sv["id"]], incoming_items=_incoming_view(sv)
            ))
            rules = new_rules
            mode = new_rule.get("heuristic_mode", "COLOR_MASKED")

    new_blueprints = []
    for c in remaining:
        if c["id"] in alive:
            c.pop("_fallback_norm", None)
            new_blueprints.append(c)
    for b in new_blueprints:
        b.pop("_heuristic_reconciliation_id", None)

    result: dict[str, Any] = {
        "new_blueprints": new_blueprints,
        "output_blueprints": list(state.output_blueprints) + attached_bps,
        "photo_only_items": list(state.photo_only_items) + attached_items,
        "resolved_blueprints": {**state.resolved_blueprints, **resolved_update},
        "heuristic_reconciliations": records,
        "warnings": list(state.warnings) + warnings,
    }
    logger.log_agent(
        "oracle_and_induction_node", "node_exit", "ok",
        records=len(records), attached=len(attached_bps), remaining=len(new_blueprints)
    )
    return result
