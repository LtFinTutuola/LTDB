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
        if not cat_ref or not isinstance(cat_ref, dict) or "id" not in cat_ref:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): 'category' must contain an 'id'")
        
        cat = db.query(Category).filter_by(id=cat_ref["id"]).first()
        if not cat:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): category '{cat_ref['id']}' not found in database")
        
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
            if not isinstance(sub_ref, dict) or "id" not in sub_ref:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): 'sub_category' must contain an 'id' or be null")
            
            sub_cat = db.query(Category).filter_by(id=sub_ref["id"]).first()
            if not sub_cat:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): sub_category '{sub_ref['id']}' not found in database")
            
            if parent_cat_id and str(sub_cat.parent_id) != str(parent_cat_id):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Operation {op_index} (update_blueprint): sub_category '{sub_ref['id']}' does not belong to category '{parent_cat_id}'")
            
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
        # Auto-add to staging with is_new=False
        staging_data["blueprints"].append({
            "id": str(db_bp.id),
            "is_new": False,
            "article_name": db_bp.article_name,
            "description": db_bp.description,
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
