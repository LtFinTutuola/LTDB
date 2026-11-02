"""
oracle_and_induction_node
-------------------------
Per ogni cluster di nuovi articoli:
  1. cerca nella coorte DB del brand i candidati "stesso modello" (morfologia/embedding),
  2. interroga l'Oracolo di Equivalenza (mai con i vendor code),
  3. classifica l'esito nella matrice Q1-Q4 e induce/estende la regola di mascheramento posizionale,
  4. produce record di riconciliazione (``heuristic_reconciliations``): richiedono conferma umana (HITL)
     SOLO se la nuova regola modifica retroattivamente blueprint già presenti a database.

Il nodo è attivo solo se ``state.brand_id`` è valorizzato.
"""
from __future__ import annotations

import copy
import hashlib
import uuid
from typing import Any, Optional

from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.agents.article_blueprints_agent.nodes.model_equivalence_oracle import (
    DIFFERENT_MODEL, INSUFFICIENT_DATA, SAME_MODEL,
    OracleVerdictOutput, embed_entity, entity_from_blueprint, entity_from_card,
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
        c = vcn.clean_code(it.get("vendor_code") or it.get("VendorCode"))
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


def _variant_colors(variants: list[dict]) -> set[str]:
    return {str(c).strip().lower() for v in variants for c in (v.get("colors") or []) if c}


def _colour_relation(cluster_colors: set[str], variants: list[dict], verdict: OracleVerdictOutput) -> str:
    """DIFFERENT / SAME / UNKNOWN: confronto deterministico se noto, altrimenti parere dell'oracolo."""
    known = _variant_colors(variants)
    if cluster_colors and known:
        return "SAME" if cluster_colors & known else "DIFFERENT"
    if verdict.color_relation == "DIFFERENT_COLOR":
        return "DIFFERENT"
    if verdict.color_relation == "SAME_COLOR":
        return "SAME"
    return "UNKNOWN"


def _existing_rule_for(code: str, rules: list[dict]) -> Optional[dict]:
    r = vcn.find_applicable_rule(code, rules)
    if r:
        return r
    sig, ln = vcn.compute_invariant_signature(code), len(code)
    for r in rules:
        if r.get("type") == "FIXED" and r.get("length") == ln and r.get("signature") == sig:
            return r
    return None


def _affected_db_blueprints(cohort: list[dict], rules_after: list[dict]) -> list[dict]:
    """Blueprint DB il cui codice normalizzato cambierebbe applicando ``rules_after``."""
    affected = []
    for bp in cohort:
        codes = [vcn.clean_code(v.get("supplier_code")) for v in bp.get("variants", []) if v.get("supplier_code")]
        new_codes = {vcn.apply_rules(c, rules_after) for c in codes}
        new_codes.discard(None)
        current = bp.get("normalized_vendor_code")
        if new_codes and any(nc != current for nc in new_codes):
            affected.append({**bp, "_new_normalized": sorted(new_codes)[0]})
    return affected


def _db_bp_view(bp: dict) -> dict:
    return {
        "id": bp["id"],
        "article_name": bp.get("article_name"),
        "normalized_vendor_code": bp.get("normalized_vendor_code"),
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


def _short_hash(text: str) -> str:
    return hashlib.sha1((text or "").lower().encode()).hexdigest()[:6].upper()


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
    }
    base.update(kw)
    return base


def _proposal(rules_before: list[dict], rule: dict, sample: str, explanation: str) -> dict:
    return {
        "old_mask_preview": vcn.apply_rules(sample, rules_before),
        "new_mask_preview": vcn.apply_rule(sample, rule),
        "explanation": explanation,
    }


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
        attached_bps.append({
            "id": cand["id"], "is_new": False,
            "article_name": cand.get("article_name"), "description": cand.get("description"),
            "cluster_items": cluster.get("cluster_items", []),
        })
        resolved_update[cand["id"]] = {
            "id": cand["id"], "is_new": False,
            "article_name": cand.get("article_name"), "description": cand.get("description"),
        }

    # ---------------- Passata A: cluster vs coorte DB ----------------
    remaining: list[dict] = []
    for cluster in clusters:
        codes = _cluster_codes(cluster)
        if not codes or not cohort:
            remaining.append(cluster)
            continue
        entity = entity_from_card(cluster)
        embedding = await embed_entity(entity, client)
        cands = select_candidates(codes, embedding, cohort, top_k=MAX_DB_CANDIDATES)
        handled = False
        ccolors = _cluster_colors(cluster)

        for cand in cands:
            verdict = await evaluate_pair(client, entity, entity_from_blueprint(cand))
            cand_codes = {vcn.clean_code(v.get("supplier_code")) for v in cand.get("variants", []) if v.get("supplier_code")}
            same_code = set(codes) & cand_codes

            if verdict.verdict == INSUFFICIENT_DATA:
                continue

            if verdict.verdict == DIFFERENT_MODEL:
                if same_code:  # Q4: stesso codice, modelli diversi -> SKU inaffidabile
                    unreliable_code = f"{codes[0]}~{_short_hash(cluster.get('article_name') or '')}"
                    _set_item_norm(cluster, unreliable_code)
                    records.append(_record(
                        kind="MODE_UPDATE", mode_update="UNRELIABLE_SKU",
                        staging_blueprint_ids=[cluster["id"]], incoming_items=_incoming_view(cluster),
                    ))
                    mode = "UNRELIABLE_SKU"
                    warnings.append(f"unreliable_sku: il codice '{codes[0]}' identifica modelli diversi")
                    handled = False
                    break
                pairs = [[c, v] for c in codes for v in cand_codes if vcn.family_key(c) == vcn.family_key(v)]
                if pairs:  # Q3: vincolo negativo (must-split)
                    records.append(_record(kind="NEGATIVE_CONSTRAINT", negative_pairs=pairs,
                                           staging_blueprint_ids=[cluster["id"]]))
                    negatives.extend(pairs)
                continue

            # SAME_MODEL
            variants = cand.get("variants", [])
            if same_code:
                relevant = [v for v in variants if vcn.clean_code(v.get("supplier_code")) in same_code]
                relation = _colour_relation(ccolors, relevant, verdict)
                if relation != "DIFFERENT":
                    continue
                # Q2: stesso codice, colori diversi -> codici agnostici rispetto al colore
                if mode in ("PENDING_COHORT", "COLOR_AGNOSTIC"):
                    records.append(_record(kind="MODE_UPDATE", mode_update="COLOR_AGNOSTIC",
                                           incoming_items=_incoming_view(cluster)))
                    mode = "COLOR_AGNOSTIC"
                _attach(cluster, cand, cand.get("normalized_vendor_code"))
                handled = True
                break

            relation = _colour_relation(ccolors, variants, verdict)
            if relation != "DIFFERENT":
                warnings.append(
                    f"oracle: '{codes[0]}' sembra lo stesso modello di '{cand.get('article_name')}' "
                    f"ma non è dimostrabile una variante cromatica: nessuna fusione."
                )
                continue

            # Q1: stesso modello, colore diverso, codici diversi -> induzione posizionale
            all_codes = sorted(set(codes) | cand_codes)
            res = vcn.induce_positional_mask(all_codes, _existing_rule_for(all_codes[0], rules), negatives)
            if res["status"] != vcn.OK:
                warnings.append(f"induction: '{codes[0]}' vs '{cand.get('article_name')}' -> {res['status']}: nessuna maschera applicata.")
                continue
            new_rule = res["rule"]
            new_rules = vcn.merge_rule(rules, new_rule)
            affected = _affected_db_blueprints(cohort, new_rules)
            explanation = (
                "Le posizioni variabili del codice identificano il colore; "
                "il resto del codice identifica il modello."
            )
            if affected:
                rec = _record(
                    kind="MASK_UPDATE", status=STATUS_PENDING, requires_confirmation=True,
                    staging_blueprint_ids=[cluster["id"]], incoming_items=_incoming_view(cluster),
                    matched_db_blueprints=[_db_bp_view(a) for a in affected],
                    proposed_heuristic_update=_proposal(rules, new_rule, codes[0], explanation),
                    rule_update=new_rule, merge_target_blueprint_id=cand["id"],
                )
                cluster["_heuristic_reconciliation_id"] = rec["reconciliation_id"]
                records.append(rec)
                remaining.append(cluster)  # fallback: resta blueprint separato finché l'utente non conferma
            else:
                records.append(_record(kind="MASK_UPDATE", rule_update=new_rule, mode_update="COLOR_MASKED",
                                       incoming_items=_incoming_view(cluster)))
                rules = new_rules
                mode = "COLOR_MASKED"
                _attach(cluster, cand, vcn.apply_rules(codes[0], rules) or cand.get("normalized_vendor_code"))
            handled = True
            break

        if not handled:
            remaining.append(cluster)
        elif cluster not in remaining and cluster.get("_heuristic_reconciliation_id") is None:
            pass  # attaccato automaticamente

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
        verdict = await evaluate_pair(client, entity_from_card(cl1), entity_from_card(cl2))
        if verdict.verdict == DIFFERENT_MODEL:
            pr = [[c1, c2]]
            records.append(_record(kind="NEGATIVE_CONSTRAINT", negative_pairs=pr,
                                   staging_blueprint_ids=[id1, id2]))
            negatives.extend(pr)
            continue
        if verdict.verdict != SAME_MODEL:
            continue
        col1, col2 = _cluster_colors(cl1), _cluster_colors(cl2)
        if col1 and col2:
            different = not (col1 & col2)
        else:
            different = verdict.color_relation == "DIFFERENT_COLOR"
        if not different:
            continue
        res = vcn.induce_positional_mask([c1, c2], _existing_rule_for(c1, rules), negatives)
        if res["status"] != vcn.OK:
            warnings.append(f"induction: '{c1}' vs '{c2}' -> {res['status']}: nessuna maschera applicata.")
            continue
        new_rule = res["rule"]
        new_rules = vcn.merge_rule(rules, new_rule)

        # Applica la regola ai cluster vivi e fonde quelli che collassano sullo stesso codice
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
            )
            records.append(rec)
        else:
            records.append(_record(kind="MASK_UPDATE", rule_update=new_rule, mode_update="COLOR_MASKED",
                                   staging_blueprint_ids=[sv["id"]], incoming_items=_incoming_view(sv)))
            rules = new_rules
            mode = "COLOR_MASKED"

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
    logger.log_agent("oracle_and_induction_node", "node_exit", "ok",
                     records=len(records), attached=len(attached_bps), remaining=len(new_blueprints))
    return result
