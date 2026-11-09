"""
staging_revision_service.py
----------------------------
Human-in-the-Loop staging revision operations engine.

Handles all 7 revision operation types that mutate the staging JSON
in-place before confirmation:
  - update_item, update_blueprint, reassign_item, create_blueprint,
    merge_blueprints, split_blueprint, delete_item

Operations within a single request are applied sequentially and
committed atomically: if any operation fails, none are persisted.
"""
import copy
import uuid
from typing import Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.pim import ArticleBlueprint, Category
from src.models.staging import JobStatus
from src.repositories import staging_repo
from src.schemas.data_ingestion import RevisionOperation, CreateBlueprintPayload
from src.core.logger import get_logger

logger = get_logger()

# Allowed mutable fields per operation type
_ITEM_EDITABLE_FIELDS = {"vendor_code", "barcode", "quantity", "colors"}
_ITEM_IMMUTABLE_FIELDS = {"item_id", "article_blueprint_id"}
_BLUEPRINT_IMMUTABLE_FIELDS = {"id", "is_new"}


_BLUEPRINT_EDITABLE_FIELDS = {"article_name", "description", "extended_description", "category", "sub_category", "tags", "materials", "dimensions"}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def revise_staging_data(
    db: Session,
    job_id: str,
    operations: List[RevisionOperation],
) -> dict:
    """
    Apply a list of revision operations to a completed staging job.

    Returns the updated staging data dict.
    Raises HTTPException on validation or processing errors.
    """
    logger.log_execution("staging_revision_service", "revise_start", "ok",
                         job_id=job_id, operation_count=len(operations))

    # 1. Load and validate job
    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Job not found")
    if job.status != JobStatus.COMPLETED.value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Job is not yet completed. Revisions can only be applied to completed jobs.")

    # 2. Deep-copy staging data as mutable working dict
    staging_data = copy.deepcopy(job.data or {})
    if "items" not in staging_data:
        staging_data["items"] = []
    if "blueprints" not in staging_data:
        staging_data["blueprints"] = []

    # 3. In-memory context for cross-operation references ($N)
    generated_ids: Dict[int, str] = {}

    # 4. Apply operations sequentially
    for idx, op in enumerate(operations):
        try:
            _dispatch_operation(db, staging_data, op, idx, generated_ids)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Operation {idx} ({op.op}) failed: {str(exc)}"
            )

    # 5. Persist the mutated data
    staging_repo.update_job_data(db, job_id=job_id, data=staging_data)

    logger.log_execution("staging_revision_service", "revise_success", "ok",
                         job_id=job_id, operations_applied=len(operations))
    return staging_data


# ---------------------------------------------------------------------------
# Operation dispatcher
# ---------------------------------------------------------------------------

def _dispatch_operation(
    db: Session,
    staging_data: dict,
    op: RevisionOperation,
    op_index: int,
    generated_ids: Dict[int, str],
) -> None:
    """Route an operation to its handler."""
    handlers = {
        "update_item": _apply_update_item,
        "update_blueprint": _apply_update_blueprint,
        "reassign_item": _apply_reassign_item,
        "create_blueprint": _apply_create_blueprint,
        "merge_blueprints": _apply_merge_blueprints,
        "split_blueprint": _apply_split_blueprint,
        "delete_item": _apply_delete_item,
        "confirm_heuristic_reconciliation": _apply_confirm_heuristic_reconciliation,
        "reject_heuristic_reconciliation": _apply_reject_heuristic_reconciliation,
    }
    handler = handlers.get(op.op)
    if not handler:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index}: unknown operation type '{op.op}'"
        )
    handler(db, staging_data, op, op_index, generated_ids)


# ---------------------------------------------------------------------------
# Operation handlers
# ---------------------------------------------------------------------------

def _apply_update_item(
    db: Session, staging_data: dict, op: RevisionOperation,
    op_index: int, generated_ids: Dict[int, str],
) -> None:
    """Update fields on a specific staged item."""
    if not op.item_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Operation {op_index} (update_item): 'item_id' is required")
    if not op.fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Operation {op_index} (update_item): 'fields' is required")

    # Validate no immutable fields
    for field in _ITEM_IMMUTABLE_FIELDS:
        if field in op.fields:
            if field == "article_blueprint_id":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Operation {op_index} (update_item): cannot change 'article_blueprint_id' "
                           f"via update_item. Use the 'reassign_item' operation instead."
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Operation {op_index} (update_item): field '{field}' is immutable"
            )

    # Validate only allowed fields
    invalid_fields = set(op.fields.keys()) - _ITEM_EDITABLE_FIELDS
    if invalid_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} (update_item): invalid fields {invalid_fields}. "
                   f"Allowed: {_ITEM_EDITABLE_FIELDS}"
        )

    item = _find_item(staging_data, op.item_id, op_index, "update_item")

    # Shallow merge
    for key, value in op.fields.items():
        item[key] = value


def _apply_update_blueprint(
    db: Session, staging_data: dict, op: RevisionOperation,
    op_index: int, generated_ids: Dict[int, str],
) -> None:
    """Update fields on a specific staged blueprint."""
    if not op.blueprint_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Operation {op_index} (update_blueprint): 'blueprint_id' is required")
    if not op.fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Operation {op_index} (update_blueprint): 'fields' is required")

    invalid_fields = set(op.fields.keys()) - _BLUEPRINT_EDITABLE_FIELDS
    if invalid_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} (update_blueprint): invalid fields {invalid_fields}. "
                   f"Allowed: {_BLUEPRINT_EDITABLE_FIELDS}"
        )
    
    # Validation for materials and tags
    if "tags" in op.fields and not op.fields["tags"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): 'tags' must be a non-empty list")
    if "materials" in op.fields and not op.fields["materials"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): 'materials' must be a non-empty list")

    bp = _find_blueprint(staging_data, op.blueprint_id, op_index, "update_blueprint")

    # Handle Category
    new_cat_id = None
    if "category" in op.fields:
        cat_ref = op.fields["category"]
        if not cat_ref or not isinstance(cat_ref, dict) or ("id" not in cat_ref and "description" not in cat_ref):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): 'category' must contain an 'id' or 'description'")
        
        if "id" in cat_ref:
            cat = db.query(Category).filter_by(id=cat_ref["id"]).first()
        else:
            cat = db.query(Category).filter_by(name=cat_ref["description"], parent_id=None).first()

        if not cat:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): category not found in database")
        
        new_cat_id = cat.id
        op.fields["category"] = {"id": str(cat.id), "description": cat.name}

    # Parent category for sub_category check
    parent_cat_id = new_cat_id or (bp.get("category") or {}).get("id")

    # Handle Sub Category
    if "sub_category" in op.fields:
        sub_ref = op.fields["sub_category"]
        if sub_ref is None:
            pass # allow clearing
        else:
            if not isinstance(sub_ref, dict) or ("id" not in sub_ref and "description" not in sub_ref):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): 'sub_category' must contain an 'id' or 'description' or be null")
            
            if "id" in sub_ref:
                sub_cat = db.query(Category).filter_by(id=sub_ref["id"]).first()
            else:
                query = db.query(Category).filter_by(name=sub_ref["description"])
                if parent_cat_id:
                    query = query.filter_by(parent_id=parent_cat_id)
                sub_cat = query.first()

            if not sub_cat:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): sub_category not found in database")
            
            if parent_cat_id and str(sub_cat.parent_id) != str(parent_cat_id):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): sub_category '{sub_cat.id}' does not belong to category '{parent_cat_id}'")
            
            op.fields["sub_category"] = {"id": str(sub_cat.id), "description": sub_cat.name}
    else:
        # If category changed and no new sub_category explicitly provided, validate existing
        if new_cat_id and bp.get("sub_category"):
            sub_cat = db.query(Category).filter_by(id=bp["sub_category"]["id"]).first()
            if not sub_cat or str(sub_cat.parent_id) != str(new_cat_id):
                op.fields["sub_category"] = None

    # Shallow merge
    for key, value in op.fields.items():
        bp[key] = value


def _invalidate_reconciliations_for_items_or_bps(staging_data: dict, affected_bp_ids: set[str], affected_item_ids: set[str]) -> None:
    """Rimuove dalle heuristic_reconciliations automatiche le voci che toccano item o blueprint modificati manualmente (Punto Operativo 3.3)."""
    if "heuristic_reconciliations" not in staging_data or not staging_data["heuristic_reconciliations"]:
        return
    recs = staging_data["heuristic_reconciliations"]
    filtered = []
    for r in recs:
        r_bp_ids = {bp.get("id") for bp in (r.get("matched_db_blueprints") or []) + (r.get("affected_db_blueprints") or []) if bp.get("id")}
        r_item_ids = {it.get("item_id") for it in (r.get("incoming_items") or []) if it.get("item_id")}
        if (r_bp_ids & affected_bp_ids) or (r_item_ids & affected_item_ids):
            continue
        filtered.append(r)
    staging_data["heuristic_reconciliations"] = filtered


def _apply_reassign_item(
    db: Session, staging_data: dict, op: RevisionOperation,
    op_index: int, generated_ids: Dict[int, str],
) -> None:
    """Reassign an item to a different blueprint."""
    if not op.item_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Operation {op_index} (reassign_item): 'item_id' is required")
    if not op.target_blueprint_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Operation {op_index} (reassign_item): 'target_blueprint_id' is required")

    item = _find_item(staging_data, op.item_id, op_index, "reassign_item")
    source_bp_id = item.get("article_blueprint_id")

    # Resolve target (may be a $N reference)
    target_id = _resolve_blueprint_ref(
        op.target_blueprint_id, generated_ids, staging_data, db, op_index, "reassign_item"
    )

    # Update reference
    item["article_blueprint_id"] = target_id

    # Invalidate automated reconciliations for affected elements
    _invalidate_reconciliations_for_items_or_bps(staging_data, {source_bp_id, target_id}, {op.item_id})

    # Orphan cleanup on source blueprint
    if source_bp_id and source_bp_id != target_id:
        _orphan_cleanup(staging_data, source_bp_id)


def _apply_create_blueprint(
    db: Session, staging_data: dict, op: RevisionOperation,
    op_index: int, generated_ids: Dict[int, str],
) -> None:
    """Create a new blueprint in the staging area."""
    if not op.blueprint:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Operation {op_index} (create_blueprint): 'blueprint' payload is required")

    # Validate category exists in DB
    _validate_category_in_db(db, op.blueprint.category.id, op_index, "create_blueprint")

    # Validate sub_category if provided
    if op.blueprint.sub_category:
        _validate_category_in_db(db, op.blueprint.sub_category.id, op_index, "create_blueprint")

    # Generate UUID and build blueprint dict
    new_id = str(uuid.uuid4())
    bp_dict = {
        "id": new_id,
        "is_new": True,
        "article_name": op.blueprint.article_name,
        "description": op.blueprint.description,
        "extended_description": op.blueprint.extended_description,
        "category": {"id": op.blueprint.category.id, "description": op.blueprint.category.description},
        "tags": op.blueprint.tags,
        "materials": op.blueprint.materials,
        "sub_category": (
            {"id": op.blueprint.sub_category.id, "description": op.blueprint.sub_category.description}
            if op.blueprint.sub_category else None
        ),
        "dimensions": op.blueprint.dimensions,
    }

    staging_data["blueprints"].append(bp_dict)

    # Store in generated_ids context for cross-operation references
    generated_ids[op_index] = new_id

    logger.log_execution("staging_revision_service", "blueprint_created", "ok",
                         op_index=op_index, blueprint_id=new_id)


def _apply_merge_blueprints(
    db: Session, staging_data: dict, op: RevisionOperation,
    op_index: int, generated_ids: Dict[int, str],
) -> None:
    """Merge multiple blueprints into one target."""
    if not op.source_blueprint_ids or len(op.source_blueprint_ids) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} (merge_blueprints): 'source_blueprint_ids' must contain at least 2 entries"
        )
    if not op.target_blueprint_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} (merge_blueprints): 'target_blueprint_id' is required"
        )
    if op.target_blueprint_id not in op.source_blueprint_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} (merge_blueprints): 'target_blueprint_id' must be one of 'source_blueprint_ids'"
        )

    # Validate all sources exist in staging
    for bp_id in op.source_blueprint_ids:
        _find_blueprint(staging_data, bp_id, op_index, "merge_blueprints")

    # Reassign items from absorbed blueprints to target
    absorbed_ids = [bp_id for bp_id in op.source_blueprint_ids if bp_id != op.target_blueprint_id]
    for item in staging_data["items"]:
        if item.get("article_blueprint_id") in absorbed_ids:
            item["article_blueprint_id"] = op.target_blueprint_id

    # Remove absorbed is_new blueprints from staging
    staging_data["blueprints"] = [
        bp for bp in staging_data["blueprints"]
        if bp.get("id") not in absorbed_ids or not bp.get("is_new", True)
    ]

    _invalidate_reconciliations_for_items_or_bps(staging_data, set(op.source_blueprint_ids), set())

    logger.log_execution("staging_revision_service", "blueprints_merged", "ok",
                         op_index=op_index, target=op.target_blueprint_id,
                         absorbed=absorbed_ids)


def _apply_split_blueprint(
    db: Session, staging_data: dict, op: RevisionOperation,
    op_index: int, generated_ids: Dict[int, str],
) -> None:
    """Split items from one blueprint into a new, separate blueprint."""
    if not op.source_blueprint_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} (split_blueprint): 'source_blueprint_id' is required"
        )
    if not op.item_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} (split_blueprint): 'item_ids' must be a non-empty list"
        )

    source_bp = _find_blueprint(staging_data, op.source_blueprint_id, op_index, "split_blueprint")

    # Validate that all item_ids currently reference the source blueprint
    source_items = {
        item.get("item_id") for item in staging_data["items"]
        if item.get("article_blueprint_id") == op.source_blueprint_id
    }
    
    provided_items = set(op.item_ids)
    if not provided_items.issubset(source_items):
        extra = provided_items - source_items
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} (split_blueprint): items {extra} do not belong to source blueprint '{op.source_blueprint_id}'"
        )
    
    if provided_items == source_items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} (split_blueprint): cannot move all items. Use update_blueprint instead."
        )

    # Generate new UUID for the split blueprint
    new_id = str(uuid.uuid4())

    # Clone source blueprint metadata and apply overrides
    new_bp = {k: v for k, v in source_bp.items() if k != "cluster_items"}
    new_bp["id"] = new_id
    new_bp["is_new"] = True
    
    if op.blueprint_overrides:
        invalid_fields = set(op.blueprint_overrides.keys()) - _BLUEPRINT_EDITABLE_FIELDS
        if invalid_fields:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Operation {op_index} (split_blueprint): invalid override fields {invalid_fields}. "
                       f"Allowed: {_BLUEPRINT_EDITABLE_FIELDS}"
            )
        
        for key, value in op.blueprint_overrides.items():
            new_bp[key] = value

    staging_data["blueprints"].append(new_bp)

    # Update item references for the items moved to the new blueprint
    for item in staging_data["items"]:
        if item.get("item_id") in provided_items:
            item["article_blueprint_id"] = new_id

    # Store in generated_ids context for cross-operation references
    generated_ids[op_index] = new_id

    _invalidate_reconciliations_for_items_or_bps(staging_data, {op.source_blueprint_id, new_id}, set(op.item_ids))

    logger.log_execution("staging_revision_service", "blueprint_split", "ok",
                         op_index=op_index, source=op.source_blueprint_id,
                         new_blueprint=new_id, items_moved=len(provided_items))


def _apply_delete_item(
    db: Session, staging_data: dict, op: RevisionOperation,
    op_index: int, generated_ids: Dict[int, str],
) -> None:
    """Remove an item from the staging data."""
    if not op.item_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Operation {op_index} (delete_item): 'item_id' is required")

    item = _find_item(staging_data, op.item_id, op_index, "delete_item")
    bp_id = item.get("article_blueprint_id")

    # Remove the item
    staging_data["items"] = [
        it for it in staging_data["items"] if it.get("item_id") != op.item_id
    ]

    # Orphan cleanup
    if bp_id:
        _orphan_cleanup(staging_data, bp_id)


def _find_reconciliation(staging_data: dict, rec_id: str, op_index: int, op_name: str) -> dict:
    for rec in staging_data.get("heuristic_reconciliations", []) or []:
        if rec.get("reconciliation_id") == rec_id:
            return rec
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Operation {op_index} ({op_name}): reconciliation '{rec_id}' not found in staging data"
    )


def _apply_confirm_heuristic_reconciliation(
    db: Session, staging_data: dict, op: RevisionOperation,
    op_index: int, generated_ids: Dict[int, str],
) -> None:
    """Confirm a pending heuristic reconciliation proposal (HITL approval)."""
    if not op.reconciliation_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} (confirm_heuristic_reconciliation): 'reconciliation_id' is required"
        )
    rec = _find_reconciliation(staging_data, op.reconciliation_id, op_index, "confirm_heuristic_reconciliation")
    rec["status"] = "ACCEPTED"

    from src.utils import vendor_code_normalizer as vcn
    import uuid

    kind = rec.get("kind")
    origin = rec.get("origin")
    rule = rec.get("rule_update")
    target_bp_id = rec.get("merge_target_blueprint_id")
    staging_bp_ids = set(rec.get("staging_blueprint_ids") or [])
    incoming_item_ids = {it.get("item_id") for it in (rec.get("incoming_items") or []) if it.get("item_id")}

    # Caso 0: Conferma di una card retroattiva su blueprint storici (RETROACTIVE_SPLIT / RETROACTIVE_MERGE)
    if kind in ("RETROACTIVE_SPLIT", "RETROACTIVE_MERGE"):
        return

    if target_bp_id:
        # Ensure target DB blueprint is present in staging_data["blueprints"] with is_new=False
        _resolve_blueprint_ref(target_bp_id, generated_ids, staging_data, db, op_index, "confirm_heuristic_reconciliation")

    # Se rule_update non era stato pre-calcolato (es. DISAMBIGUATION senza proposta) ma i codici differiscono, inducila ora
    if not rule and target_bp_id:
        inc_codes = [
            vcn.sterilize_separators(it.get("vendor_code"))
            for it in (rec.get("incoming_items") or [])
            if vcn.sterilize_separators(it.get("vendor_code"))
        ]
        target_bps = [
            bp for bp in (rec.get("matched_db_blueprints") or [])
            if str(bp.get("id")) == str(target_bp_id)
        ] or (rec.get("matched_db_blueprints") or [])
        cand_codes = {
            vcn.sterilize_separators(v.get("supplier_code"))
            for bp in target_bps
            for v in (bp.get("variants") or [])
            if vcn.sterilize_separators(v.get("supplier_code"))
        }
        if not cand_codes:
            for bp in target_bps:
                rc = vcn.sterilize_separators(bp.get("raw_vendor_code") or bp.get("normalized_vendor_code"))
                if rc and "#" not in rc:
                    cand_codes.add(rc)
        if inc_codes and cand_codes and (set(inc_codes) != cand_codes):
            ind_res = vcn.induce_positional_mask(sorted(set(inc_codes) | cand_codes))
            if ind_res["status"] == vcn.OK:
                rule = ind_res["rule"]
                rec["rule_update"] = rule

    # Caso 1: Conferma di DISAMBIGUATION
    if kind == "DISAMBIGUATION":
        # Trova il blueprint target a DB per recuperare il suo normalized_vendor_code attuale
        target_bp_norm = None
        for bp in staging_data.get("blueprints", []):
            if str(bp.get("id")) == str(target_bp_id):
                target_bp_norm = bp.get("normalized_vendor_code")
                break

        # Unisci gli articoli al blueprint target
        for item in staging_data.get("items", []):
            if item.get("article_blueprint_id") in staging_bp_ids or item.get("item_id") in incoming_item_ids:
                if target_bp_id:
                    item["article_blueprint_id"] = target_bp_id
                if target_bp_norm:
                    item["normalized_vendor_code"] = target_bp_norm

        # Pulizia blueprint rimasti orfani
        for old_bp_id in staging_bp_ids:
            if target_bp_id and old_bp_id != target_bp_id:
                _orphan_cleanup(staging_data, old_bp_id)

        # Se esiste una regola di mascheramento proposta
        if rule:
            affected_bps = rec.get("affected_db_blueprints") or []
            if affected_bps:
                # Modifica retroattiva su DB esistente (esplicita in affected_db_blueprints): catena la SECONDA richiesta esplicita (HITL)
                chained_mask_rec = {
                    "reconciliation_id": str(uuid.uuid4()),
                    "status": "PENDING_USER_CONFIRMATION",
                    "requires_confirmation": True,
                    "kind": "MASK_UPDATE",
                    "origin": "DISAMBIGUATION",
                    "staging_blueprint_ids": [target_bp_id] if target_bp_id else list(staging_bp_ids),
                    "incoming_items": rec.get("incoming_items", []),
                    "matched_db_blueprints": affected_bps,
                    "proposed_heuristic_update": rec.get("proposed_heuristic_update"),
                    "rule_update": rule,
                    "mode_update": rule.get("heuristic_mode", "COLOR_MASKED"),
                    "color_segment_index": rule.get("color_segment_index"),
                    "merge_target_blueprint_id": target_bp_id,
                }
                staging_data.setdefault("heuristic_reconciliations", []).append(chained_mask_rec)
            else:
                # Applica la maschera agli item correnti e al blueprint target in staging, e genera eventuali card HITL di split/merge retroattivo su DB
                target_masked_code = None
                for item in staging_data.get("items", []):
                    if item.get("article_blueprint_id") == target_bp_id or item.get("item_id") in incoming_item_ids:
                        raw_code = item.get("vendor_code") or ""
                        masked = vcn.apply_rule(raw_code, rule)
                        if masked:
                            item["normalized_vendor_code"] = masked
                            if not target_masked_code:
                                target_masked_code = masked
                for bp in staging_data.get("blueprints", []):
                    if str(bp.get("id")) == str(target_bp_id):
                        raw_code = bp.get("raw_vendor_code") or bp.get("normalized_vendor_code") or ""
                        masked = vcn.apply_rule(raw_code, rule) or target_masked_code
                        if masked:
                            bp["normalized_vendor_code"] = masked
                            bp["skeleton_vendor_code"] = vcn.compute_skeleton_code(masked)
                            target_masked_code = masked
                rec["kind"] = "MASK_UPDATE"
                rec["mode_update"] = rule.get("heuristic_mode", "COLOR_MASKED")
                rec["color_segment_index"] = rule.get("color_segment_index")
                if target_masked_code:
                    rec["new_normalized_code"] = target_masked_code
                _generate_retroactive_db_reconciliations(db, staging_data, rule, target_bp_id)

    # Caso 2: Conferma di MASK_UPDATE (diretto oppure origin == "DISAMBIGUATION")
    else:
        target_masked_code = None
        for item in staging_data.get("items", []):
            if item.get("article_blueprint_id") in staging_bp_ids or item.get("item_id") in incoming_item_ids:
                raw_code = item.get("vendor_code") or ""
                if rule:
                    masked = vcn.apply_rule(raw_code, rule)
                    if masked:
                        item["normalized_vendor_code"] = masked
                        if not target_masked_code:
                            target_masked_code = masked
                if target_bp_id:
                    item["article_blueprint_id"] = target_bp_id

        if target_bp_id and rule:
            for bp in staging_data.get("blueprints", []):
                if str(bp.get("id")) == str(target_bp_id):
                    raw_code = bp.get("raw_vendor_code") or bp.get("normalized_vendor_code") or ""
                    masked = vcn.apply_rule(raw_code, rule) or target_masked_code
                    if masked:
                        bp["normalized_vendor_code"] = masked
                        bp["skeleton_vendor_code"] = vcn.compute_skeleton_code(masked)

        for old_bp_id in staging_bp_ids:
            if target_bp_id and old_bp_id != target_bp_id:
                _orphan_cleanup(staging_data, old_bp_id)

        if rule:
            _generate_retroactive_db_reconciliations(db, staging_data, rule, target_bp_id)


def _generate_retroactive_db_reconciliations(
    db: Session,
    staging_data: dict,
    rule: dict,
    target_bp_id: Optional[str],
) -> None:
    """
    Ispeziona i blueprint del brand già consolidati a database alla luce della nuova regola
    confermata dall'utente. Se un blueprint esistente contiene articoli che ora divergono
    in più codici normalizzati (RETROACTIVE_SPLIT) o se due blueprint distinti collassano
    ora sullo stesso codice normalizzato (RETROACTIVE_MERGE), genera card HITL dedicate
    nella Staging Area affinché l'utente esprima il proprio consenso esplicito.
    """
    import uuid
    from collections import OrderedDict
    from sqlalchemy.orm import selectinload
    from src.models.pim import ArticleBlueprint
    from src.repositories.pim_repo import heuristic_repo
    from src.utils import vendor_code_normalizer as vcn

    brand_id = staging_data.get("brand_id")
    if not brand_id or not rule:
        return

    active_h = heuristic_repo.get_active_heuristic(db, str(brand_id))
    meta = getattr(active_h, "heuristic_metadata", None) or {}
    current_rules = list(meta.get("rules") or [])
    negatives = [list(p) for p in (meta.get("negative_constraints") or []) if len(p) == 2]

    for existing_rec in staging_data.get("heuristic_reconciliations") or []:
        if existing_rec.get("status") == "REJECTED":
            for p in existing_rec.get("negative_pairs") or []:
                if len(p) == 2 and p not in negatives and p[::-1] not in negatives:
                    negatives.append(list(p))

    new_rules = vcn.merge_rule(current_rules, rule)

    db_blueprints = (
        db.query(ArticleBlueprint)
        .options(selectinload(ArticleBlueprint.articles), selectinload(ArticleBlueprint.photos))
        .filter(ArticleBlueprint.brand_id == str(brand_id))
        .all()
    )
    if not db_blueprints:
        return

    def _photo_url_for_articles(bp_obj: ArticleBlueprint, arts: list) -> Optional[str]:
        cols = [
            str(c).strip().lower()
            for a in arts
            for c in (a.colors or [])
            if c and str(c).strip()
        ]
        for col in cols:
            for p in (bp_obj.photos or []):
                if (p.canonical_color_name or "").strip().lower() == col:
                    return f"/api/v1/ingestion/photos/{p.id}"
        if bp_obj.photos:
            return f"/api/v1/ingestion/photos/{bp_obj.photos[0].id}"
        return None

    existing_recs = staging_data.setdefault("heuristic_reconciliations", [])
    existing_split_keys = {
        (str(r.get("source_blueprint_id")), str(r.get("split_normalized_code")))
        for r in existing_recs
        if r.get("kind") == "RETROACTIVE_SPLIT"
    }
    existing_merge_keys = {
        tuple(sorted([str(r.get("merge_target_blueprint_id")), str(r.get("secondary_blueprint_id"))]))
        for r in existing_recs
        if r.get("kind") == "RETROACTIVE_MERGE"
    }

    bps_by_primary_norm: dict[str, list[tuple[ArticleBlueprint, list]]] = OrderedDict()

    for bp in db_blueprints:
        arts = [a for a in (bp.articles or []) if vcn.sterilize_separators(a.supplier_code)]
        if not arts:
            continue

        applied_by_art = {
            a.id: vcn.apply_rules(vcn.sterilize_separators(a.supplier_code), new_rules)
            for a in arts
        }
        matched_norms = [nc for nc in applied_by_art.values() if nc is not None]
        default_bp_norm = (
            matched_norms[0]
            if matched_norms
            else (bp.normalized_vendor_code or vcn.sterilize_separators(arts[0].supplier_code))
        )

        groups: dict[str, list] = OrderedDict()
        for a in arts:
            norm_c = applied_by_art[a.id] or default_bp_norm
            groups.setdefault(norm_c, []).append(a)

        group_items = list(groups.items())
        primary_norm, primary_arts = group_items[0]
        bps_by_primary_norm.setdefault(primary_norm, []).append((bp, primary_arts))

        # 1. Rilevamento Split Retroattivo (Multi-Norm dentro lo stesso blueprint storico)
        if len(group_items) > 1:
            prim_raw = vcn.sterilize_separators(primary_arts[0].supplier_code)
            prim_photo = _photo_url_for_articles(bp, primary_arts)
            primary_view = {
                "id": str(bp.id),
                "article_name": bp.article_name,
                "raw_vendor_code": prim_raw,
                "normalized_vendor_code": primary_norm,
                "photo_url": prim_photo,
                "variants": [
                    {"supplier_code": vcn.sterilize_separators(a.supplier_code), "colors": list(a.colors or [])}
                    for a in primary_arts
                ],
            }
            for split_norm, split_arts in group_items[1:]:
                s_key = (str(bp.id), split_norm)
                if s_key in existing_split_keys:
                    continue
                existing_split_keys.add(s_key)
                split_raw = vcn.sterilize_separators(split_arts[0].supplier_code)
                split_photo = _photo_url_for_articles(bp, split_arts)
                split_view = {
                    "item_id": str(split_arts[0].id),
                    "article_name": bp.article_name,
                    "vendor_code": split_raw,
                    "raw_vendor_code": split_raw,
                    "normalized_vendor_code": split_norm,
                    "colors": list(split_arts[0].colors or []),
                    "photo_url": split_photo,
                }
                existing_recs.append({
                    "reconciliation_id": str(uuid.uuid4()),
                    "kind": "RETROACTIVE_SPLIT",
                    "origin": "RETROACTIVE",
                    "status": "PENDING_USER_CONFIRMATION",
                    "requires_confirmation": True,
                    "source_blueprint_id": str(bp.id),
                    "split_source_blueprint_id": str(bp.id),
                    "merge_target_blueprint_id": str(bp.id),
                    "primary_normalized_code": primary_norm,
                    "split_normalized_code": split_norm,
                    "split_article_ids": [str(a.id) for a in split_arts],
                    "matched_db_blueprints": [primary_view],
                    "incoming_items": [split_view],
                    "rule_update": None,
                })

    # 2. Rilevamento Merge Retroattivo (Collisione sullo stesso codice normalizzato tra blueprint storici distinti)
    for norm_key, bp_entries in bps_by_primary_norm.items():
        if len(bp_entries) <= 1:
            continue

        # Privilegia come primario il target corrente del merge se presente
        sorted_entries = sorted(
            bp_entries,
            key=lambda entry: 0 if str(entry[0].id) == str(target_bp_id) else 1,
        )
        bp_primary, prim_arts = sorted_entries[0]
        prim_codes = {vcn.sterilize_separators(a.supplier_code) for a in prim_arts if vcn.sterilize_separators(a.supplier_code)}
        prim_raw = vcn.sterilize_separators(prim_arts[0].supplier_code)
        prim_photo = _photo_url_for_articles(bp_primary, prim_arts)
        primary_bp_view = {
            "id": str(bp_primary.id),
            "article_name": bp_primary.article_name,
            "raw_vendor_code": prim_raw,
            "normalized_vendor_code": norm_key,
            "photo_url": prim_photo,
            "variants": [
                {"supplier_code": vcn.sterilize_separators(a.supplier_code), "colors": list(a.colors or [])}
                for a in prim_arts
            ],
        }

        for bp_secondary, sec_arts in sorted_entries[1:]:
            sec_codes = {vcn.clean_code(a.supplier_code) for a in sec_arts if vcn.clean_code(a.supplier_code)}
            # Rispetta eventuali vincoli negativi espliciti già espressi dall'utente
            violates_neg = any(
                [ca, cb] in negatives or [cb, ca] in negatives
                for ca in prim_codes
                for cb in sec_codes
            )
            if violates_neg:
                continue

            m_key = tuple(sorted([str(bp_primary.id), str(bp_secondary.id)]))
            if m_key in existing_merge_keys:
                continue
            existing_merge_keys.add(m_key)

            sec_raw = vcn.clean_code(sec_arts[0].supplier_code)
            sec_photo = _photo_url_for_articles(bp_secondary, sec_arts)
            secondary_bp_view = {
                "item_id": str(sec_arts[0].id),
                "id": str(bp_secondary.id),
                "article_name": bp_secondary.article_name,
                "vendor_code": sec_raw,
                "raw_vendor_code": sec_raw,
                "normalized_vendor_code": norm_key,
                "colors": list(sec_arts[0].colors or []),
                "photo_url": sec_photo,
                "variants": [
                    {"supplier_code": vcn.clean_code(a.supplier_code), "colors": list(a.colors or [])}
                    for a in sec_arts
                ],
            }
            existing_recs.append({
                "reconciliation_id": str(uuid.uuid4()),
                "kind": "RETROACTIVE_MERGE",
                "origin": "RETROACTIVE",
                "status": "PENDING_USER_CONFIRMATION",
                "requires_confirmation": True,
                "merge_target_blueprint_id": str(bp_primary.id),
                "secondary_blueprint_id": str(bp_secondary.id),
                "merge_secondary_blueprint_id": str(bp_secondary.id),
                "target_normalized_code": norm_key,
                "matched_db_blueprints": [primary_bp_view],
                "incoming_items": [secondary_bp_view],
                "rule_update": None,
            })


def _apply_reject_heuristic_reconciliation(
    db: Session, staging_data: dict, op: RevisionOperation,
    op_index: int, generated_ids: Dict[int, str],
) -> None:
    """Reject a pending heuristic reconciliation proposal."""
    if not op.reconciliation_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} (reject_heuristic_reconciliation): 'reconciliation_id' is required"
        )
    rec = _find_reconciliation(staging_data, op.reconciliation_id, op_index, "reject_heuristic_reconciliation")
    rec["status"] = "REJECTED"

    from src.utils import vendor_code_normalizer as vcn

    kind = rec.get("kind")
    origin = rec.get("origin")
    fallback_norm = rec.get("fallback_normalized_code")
    staging_bp_ids = set(rec.get("staging_blueprint_ids") or [])
    incoming_item_ids = {it.get("item_id") for it in (rec.get("incoming_items") or []) if it.get("item_id")}

    # Caso Speciale 0a: Rifiuto di RETROACTIVE_SPLIT
    # Il blueprint storico resta intatto così com'era; l'articolo in ingresso e la nuova maschera restano validi.
    if kind == "RETROACTIVE_SPLIT":
        rec["rule_update"] = None
        rec["negative_pairs"] = []
        return

    # Caso Speciale 0b: Rifiuto di RETROACTIVE_MERGE
    # I due blueprint storici restano separati e viene registrato il vincolo negativo tra loro;
    # l'articolo in ingresso e la nuova maschera del brand restano validi.
    if kind == "RETROACTIVE_MERGE":
        rec["rule_update"] = None
        _record_negative_constraints_on_rejection(rec)
        return

    # Caso Speciale 1: Rifiuto di MASK_UPDATE derivato da DISAMBIGUATION (origin == "DISAMBIGUATION")
    # Principio concordato: l'unione resta valida, l'utente rifiuta solo l'allargamento della maschera retroattiva.
    if origin == "DISAMBIGUATION":
        rec["rule_update"] = None
        rec["negative_pairs"] = []
        return

    # In tutti gli altri casi (Rifiuto DISAMBIGUATION o Rifiuto MASK_UPDATE diretto):
    # L'utente dichiara che sono articoli/modelli diversi -> mantieni separati e registra negative_constraints.
    for item in staging_data.get("items", []):
        if item.get("article_blueprint_id") in staging_bp_ids or item.get("item_id") in incoming_item_ids:
            if fallback_norm:
                item["normalized_vendor_code"] = fallback_norm
            else:
                item["normalized_vendor_code"] = vcn.clean_code(item.get("vendor_code")) or item.get("normalized_vendor_code")

    # Build negative pairs (must-split constraint) between incoming codes and target DB blueprint variant codes
    _record_negative_constraints_on_rejection(rec)


def _record_negative_constraints_on_rejection(rec: dict) -> None:
    """Costruisce le coppie negative (must-split) tra i codici in ingresso e i codici del blueprint target."""
    from src.utils import vendor_code_normalizer as vcn

    inc_codes = {
        vcn.clean_code(it.get("vendor_code"))
        for it in (rec.get("incoming_items") or [])
        if vcn.clean_code(it.get("vendor_code"))
    }
    target_id = rec.get("merge_target_blueprint_id")
    target_bps = [bp for bp in (rec.get("matched_db_blueprints") or []) if bp.get("id") == target_id] if target_id else []
    if not target_bps:
        target_bps = rec.get("matched_db_blueprints") or []

    db_codes = {
        vcn.clean_code(v.get("supplier_code"))
        for bp in target_bps
        for v in (bp.get("variants") or [])
        if vcn.clean_code(v.get("supplier_code"))
    }
    neg_pairs = list(rec.get("negative_pairs") or [])
    for ic in sorted(inc_codes):
        for dc in sorted(db_codes):
            if ic != dc:
                pair = sorted([ic, dc])
                if pair not in neg_pairs and pair[::-1] not in neg_pairs:
                    neg_pairs.append(pair)
    rec["negative_pairs"] = neg_pairs


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _find_item(staging_data: dict, item_id: str, op_index: int, op_name: str) -> dict:
    """Locate an item in staging data by item_id. Raises 404 if not found."""
    for item in staging_data.get("items", []):
        if item.get("item_id") == item_id:
            return item
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Operation {op_index} ({op_name}): item '{item_id}' not found in staging data"
    )


def _find_blueprint(staging_data: dict, blueprint_id: str, op_index: int, op_name: str) -> dict:
    """Locate a blueprint in staging data by id. Raises 404 if not found."""
    for bp in staging_data.get("blueprints", []):
        if bp.get("id") == blueprint_id:
            return bp
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Operation {op_index} ({op_name}): blueprint '{blueprint_id}' not found in staging data"
    )


def _resolve_blueprint_ref(
    ref: str,
    generated_ids: Dict[int, str],
    staging_data: dict,
    db: Session,
    op_index: int,
    op_name: str,
) -> str:
    """
    Resolve a blueprint reference. Handles:
    - $N references to previously generated blueprint IDs
    - Direct IDs in staging blueprints array
    - Existing DB blueprint IDs (auto-added to staging with is_new=False)
    """
    # Handle $N cross-operation references
    if ref.startswith("$"):
        try:
            ref_index = int(ref[1:])
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Operation {op_index} ({op_name}): invalid reference '{ref}'. "
                       f"Expected format: $<operation_index>"
            )
        if ref_index not in generated_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Operation {op_index} ({op_name}): reference '{ref}' points to operation {ref_index} "
                       f"which did not generate a blueprint ID"
            )
        return generated_ids[ref_index]

    # Check staging blueprints
    for bp in staging_data.get("blueprints", []):
        if bp.get("id") == ref:
            return ref

    # Check production DB
    from src.repositories.pim_repo import pim_repo
    db_bp = pim_repo.get(db, ref)
    if db_bp:
        cat_dict = None
        sub_cat_dict = None
        if db_bp.category:
            parent_cat = db_bp.category.parent
            if not parent_cat and db_bp.category.parent_id:
                parent_cat = db.query(Category).filter(Category.id == db_bp.category.parent_id).first()

            if parent_cat:
                cat_dict = {"id": str(parent_cat.id), "description": parent_cat.description}
                sub_cat_dict = {"id": str(db_bp.category.id), "description": db_bp.category.description}
            else:
                cat_dict = {"id": str(db_bp.category.id), "description": db_bp.category.description}

        # Auto-add to staging with is_new=False and full attributes
        staging_data["blueprints"].append({
            "id": str(db_bp.id),
            "is_new": False,
            "article_name": db_bp.article_name,
            "description": db_bp.description,
            "extended_description": db_bp.extended_description,
            "category": cat_dict,
            "sub_category": sub_cat_dict,
            "tags": list(db_bp.tags or []),
            "materials": list(db_bp.materials or []),
            "normalized_vendor_code": db_bp.normalized_vendor_code,
        })
        return str(db_bp.id)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Operation {op_index} ({op_name}): blueprint '{ref}' not found in staging data or database"
    )


def _orphan_cleanup(staging_data: dict, blueprint_id: str) -> None:
    """
    Remove a blueprint from staging if it has zero items referencing it
    and is marked as is_new: True.
    """
    # Check if any items still reference this blueprint
    has_items = any(
        item.get("article_blueprint_id") == blueprint_id
        for item in staging_data.get("items", [])
    )
    if has_items:
        return

    # Only cleanup new blueprints (not existing DB ones)
    staging_data["blueprints"] = [
        bp for bp in staging_data["blueprints"]
        if not (bp.get("id") == blueprint_id and bp.get("is_new", True))
    ]


def _validate_category_in_db(db: Session, category_id: str, op_index: int, op_name: str) -> None:
    """Validate that a category ID exists in the database."""
    cat = db.query(Category).filter(Category.id == category_id).first()
    if not cat:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Operation {op_index} ({op_name}): category '{category_id}' not found in database"
        )
