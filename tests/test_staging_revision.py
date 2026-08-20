import pytest
from fastapi.testclient import TestClient
from typing import Generator
import uuid

from src.main import app
from src.core.database import get_db
from src.models.staging import StagingArea, JobStatus
from src.models.pim import Brand, Category, ArticleBlueprint
from src.models.wms import Article

# Define client globally within tests using dependency override if needed, 
# but best to use the fixture.

def _seed_staging_job(db_session) -> str:
    """Helper to seed a brand, category, and a completed staging job with bipartite data."""
    # Seed Brand
    brand = Brand(name="Test Brand")
    db_session.add(brand)
    db_session.commit()
    db_session.refresh(brand)

    # Seed Categories
    cat1 = Category(name="Cat 1", description="Category 1")
    cat2 = Category(name="Cat 2", description="Category 2")
    db_session.add(cat1)
    db_session.add(cat2)
    db_session.commit()
    db_session.refresh(cat1)
    db_session.refresh(cat2)

    # Seed an existing DB Blueprint
    existing_bp = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat1.id,
        article_name="Existing BP",
        description="Existing BP Desc",
        extended_description="Ext",
        tags=["old"],
        materials=["metal"]
    )
    db_session.add(existing_bp)
    db_session.commit()
    db_session.refresh(existing_bp)

    # Create a completed staging job
    job_id = str(uuid.uuid4())
    bp1_id = str(uuid.uuid4())
    bp2_id = str(uuid.uuid4())
    
    staging_data = {
        "brand_id": brand.id,
        "blueprints": [
            {
                "id": bp1_id,
                "is_new": True,
                "article_name": "New BP 1",
                "description": "Desc 1",
                "extended_description": "Ext 1",
                "category": {"id": cat1.id, "description": cat1.description},
                "tags": ["tag1"],
                "materials": ["mat1"]
            },
            {
                "id": bp2_id,
                "is_new": True,
                "article_name": "New BP 2",
                "description": "Desc 2",
                "extended_description": "Ext 2",
                "category": {"id": cat2.id, "description": cat2.description},
                "tags": ["tag2"],
                "materials": ["mat2"]
            }
        ],
        "items": [
            {
                "item_id": "item-1",
                "vendor_code": "V1",
                "barcode": "B1",
                "quantity": 10,
                "article_blueprint_id": bp1_id
            },
            {
                "item_id": "item-2",
                "vendor_code": "V2",
                "quantity": 5,
                "article_blueprint_id": bp1_id
            },
            {
                "item_id": "item-3",
                "vendor_code": "V3",
                "quantity": 1,
                "article_blueprint_id": bp2_id
            }
        ]
    }

    job = StagingArea(
        id=job_id,
        status=JobStatus.COMPLETED.value,
        file_path="/dummy/path.pdf",
        data=staging_data,
        revision_count=0
    )
    db_session.add(job)
    db_session.commit()

    return job_id, brand.id, cat1.id, cat2.id, bp1_id, bp2_id, existing_bp.id

@pytest.fixture
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# --- Tests ---

def test_update_item_success(client, db_session):
    job_id, _, _, _, _, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {"op": "update_item", "item_id": "item-1", "fields": {"vendor_code": "V1-UPDATED", "quantity": 99}}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    
    item1 = next(it for it in data["items"] if it["item_id"] == "item-1")
    assert item1["vendor_code"] == "V1-UPDATED"
    assert item1["quantity"] == 99

def test_update_item_reject_blueprint_id(client, db_session):
    job_id, _, _, _, _, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {"op": "update_item", "item_id": "item-1", "fields": {"article_blueprint_id": "something"}}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400
    assert "cannot change 'article_blueprint_id'" in response.json()["detail"]

def test_update_item_not_found(client, db_session):
    job_id, _, _, _, _, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {"op": "update_item", "item_id": "non-existent", "fields": {"quantity": 10}}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 404

def test_update_blueprint_success(client, db_session):
    job_id, _, cat1_id, cat2_id, bp1_id, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {
                "op": "update_blueprint",
                "blueprint_id": bp1_id,
                "fields": {
                    "article_name": "Changed Name",
                    "category": {"id": cat2_id, "description": "Cat 2"},
                    "tags": ["new-tag"]
                }
            }
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    
    bp1 = next(bp for bp in data["blueprints"] if bp["id"] == bp1_id)
    assert bp1["article_name"] == "Changed Name"
    assert bp1["category"]["id"] == cat2_id
    assert bp1["tags"] == ["new-tag"]

def test_update_blueprint_invalid_category(client, db_session):
    job_id, _, _, _, bp1_id, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {"op": "update_blueprint", "blueprint_id": bp1_id, "fields": {"category": {"id": "bad-id", "description": ""}}}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400
    assert "not found in database" in response.json()["detail"]

def test_update_blueprint_reject_id_field(client, db_session):
    job_id, _, _, _, bp1_id, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {"op": "update_blueprint", "blueprint_id": bp1_id, "fields": {"id": "new-id"}}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400
    assert "invalid fields" in response.json()["detail"]

def test_reassign_item_to_staged_blueprint(client, db_session):
    job_id, _, _, _, bp1_id, bp2_id, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {"op": "reassign_item", "item_id": "item-1", "target_blueprint_id": bp2_id}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    item1 = next(it for it in data["items"] if it["item_id"] == "item-1")
    assert item1["article_blueprint_id"] == bp2_id

def test_reassign_item_to_db_blueprint(client, db_session):
    job_id, _, _, _, bp1_id, bp2_id, existing_bp_id = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {"op": "reassign_item", "item_id": "item-1", "target_blueprint_id": existing_bp_id}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    item1 = next(it for it in data["items"] if it["item_id"] == "item-1")
    assert item1["article_blueprint_id"] == existing_bp_id
    
    # Verify the existing BP was auto-added to staging with is_new=False
    added_bp = next((bp for bp in data["blueprints"] if bp["id"] == existing_bp_id), None)
    assert added_bp is not None
    assert added_bp["is_new"] is False

def test_reassign_item_orphan_cleanup(client, db_session):
    job_id, _, _, _, bp1_id, bp2_id, _ = _seed_staging_job(db_session)
    # bp2 only has item-3. Reassign item-3 to bp1.
    payload = {
        "operations": [
            {"op": "reassign_item", "item_id": "item-3", "target_blueprint_id": bp1_id}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    
    # bp2 should be gone
    bp2 = next((bp for bp in data["blueprints"] if bp["id"] == bp2_id), None)
    assert bp2 is None

def test_reassign_item_no_cleanup_existing_bp(client, db_session):
    job_id, _, _, _, bp1_id, bp2_id, existing_bp_id = _seed_staging_job(db_session)
    
    # First reassign item-3 to existing DB BP (this adds it to staging)
    client.put(f"/api/v1/ingestion/staging/{job_id}", json={
        "operations": [{"op": "reassign_item", "item_id": "item-3", "target_blueprint_id": existing_bp_id}]
    })
    
    # Now reassign item-3 away from the existing DB BP
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json={
        "operations": [{"op": "reassign_item", "item_id": "item-3", "target_blueprint_id": bp1_id}]
    })
    assert response.status_code == 200
    data = response.json()["data"]
    
    # Existing BP should still be in staging blueprints array (no orphan cleanup for is_new=False)
    added_bp = next((bp for bp in data["blueprints"] if bp["id"] == existing_bp_id), None)
    assert added_bp is not None

def test_reassign_item_target_not_found(client, db_session):
    job_id, _, _, _, _, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {"op": "reassign_item", "item_id": "item-1", "target_blueprint_id": "bad-id"}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 404

def test_create_blueprint_success(client, db_session):
    job_id, _, cat1_id, _, _, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {
                "op": "create_blueprint",
                "blueprint": {
                    "article_name": "Created",
                    "description": "Desc",
                    "extended_description": "Ext",
                    "category": {"id": cat1_id, "description": "Cat"},
                    "tags": ["t1"],
                    "materials": ["m1"]
                }
            }
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["blueprints"]) == 3 # 2 original + 1 new
    new_bp = data["blueprints"][-1]
    assert new_bp["article_name"] == "Created"
    assert new_bp["is_new"] is True

def test_create_blueprint_missing_required_fields(client, db_session):
    job_id, _, cat1_id, _, _, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {
                "op": "create_blueprint",
                "blueprint": {
                    "article_name": "Created",
                    "description": "Desc",
                    "extended_description": "Ext",
                    "category": {"id": cat1_id, "description": "Cat"},
                    "tags": [], # empty not allowed
                    "materials": ["m1"]
                }
            }
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 422 # Pydantic validation catches this

def test_create_blueprint_invalid_category(client, db_session):
    job_id, _, _, _, _, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {
                "op": "create_blueprint",
                "blueprint": {
                    "article_name": "Created",
                    "description": "Desc",
                    "extended_description": "Ext",
                    "category": {"id": "bad-cat", "description": "Cat"},
                    "tags": ["t1"],
                    "materials": ["m1"]
                }
            }
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400
    assert "not found in database" in response.json()["detail"]

def test_create_and_reassign_composition(client, db_session):
    job_id, _, cat1_id, _, _, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {
                "op": "create_blueprint",
                "blueprint": {
                    "article_name": "Composition",
                    "description": "Desc",
                    "extended_description": "Ext",
                    "category": {"id": cat1_id, "description": "Cat"},
                    "tags": ["t1"],
                    "materials": ["m1"]
                }
            },
            {
                "op": "reassign_item",
                "item_id": "item-1",
                "target_blueprint_id": "$0"
            }
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    
    new_bp = data["blueprints"][-1]
    assert new_bp["article_name"] == "Composition"
    
    item1 = next(it for it in data["items"] if it["item_id"] == "item-1")
    assert item1["article_blueprint_id"] == new_bp["id"]

def test_merge_blueprints_success(client, db_session):
    job_id, _, _, _, bp1_id, bp2_id, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {
                "op": "merge_blueprints",
                "source_blueprint_ids": [bp1_id, bp2_id],
                "target_blueprint_id": bp1_id
            }
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    
    # bp2 should be gone
    assert len(data["blueprints"]) == 1
    assert data["blueprints"][0]["id"] == bp1_id
    
    # item-3 should now point to bp1
    item3 = next(it for it in data["items"] if it["item_id"] == "item-3")
    assert item3["article_blueprint_id"] == bp1_id

def test_merge_blueprints_too_few_sources(client, db_session):
    job_id, _, _, _, bp1_id, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {
                "op": "merge_blueprints",
                "source_blueprint_ids": [bp1_id],
                "target_blueprint_id": bp1_id
            }
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400

def test_merge_blueprints_target_not_in_sources(client, db_session):
    job_id, _, _, _, bp1_id, bp2_id, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {
                "op": "merge_blueprints",
                "source_blueprint_ids": [bp1_id, bp2_id],
                "target_blueprint_id": "other-id"
            }
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400

def test_split_blueprint_success(client, db_session):
    job_id, _, _, _, bp1_id, _, _ = _seed_staging_job(db_session)
    # bp1 has item-1 and item-2
    payload = {
        "operations": [
            {
                "op": "split_blueprint",
                "source_blueprint_id": bp1_id,
                "item_ids": ["item-2"],
                "blueprint_overrides": {"article_name": "Split Group 2"}
            }
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    
    # should be 3 blueprints now (bp1 + new split bp + bp2)
    assert len(data["blueprints"]) == 3
    
    bp1 = next(bp for bp in data["blueprints"] if bp["id"] == bp1_id)
    assert bp1["article_name"] == "New BP 1" # Unchanged
    
    item2 = next(it for it in data["items"] if it["item_id"] == "item-2")
    new_bp_id = item2["article_blueprint_id"]
    assert new_bp_id != bp1_id
    new_bp = next(bp for bp in data["blueprints"] if bp["id"] == new_bp_id)
    assert new_bp["article_name"] == "Split Group 2"
    assert new_bp["is_new"] is True

def test_split_blueprint_move_all_items(client, db_session):
    job_id, _, _, _, bp1_id, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {
                "op": "split_blueprint",
                "source_blueprint_id": bp1_id,
                "item_ids": ["item-1", "item-2"]
            }
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400
    assert "cannot move all items" in response.json()["detail"]

def test_split_blueprint_foreign_item(client, db_session):
    job_id, _, _, _, bp1_id, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {
                "op": "split_blueprint",
                "source_blueprint_id": bp1_id,
                "item_ids": ["item-3"] # belongs to bp2
            }
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400
    assert "do not belong to source blueprint" in response.json()["detail"]

def test_delete_item_success(client, db_session):
    job_id, _, _, _, bp1_id, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {"op": "delete_item", "item_id": "item-1"}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    item1 = next((it for it in data["items"] if it["item_id"] == "item-1"), None)
    assert item1 is None

def test_delete_item_orphan_cleanup(client, db_session):
    job_id, _, _, _, _, bp2_id, _ = _seed_staging_job(db_session)
    # bp2 only has item-3
    payload = {
        "operations": [
            {"op": "delete_item", "item_id": "item-3"}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    
    # bp2 should be gone
    bp2 = next((bp for bp in data["blueprints"] if bp["id"] == bp2_id), None)
    assert bp2 is None

def test_revision_job_not_found(client, db_session):
    payload = {"operations": [{"op": "delete_item", "item_id": "item-3"}]}
    response = client.put(f"/api/v1/ingestion/staging/non-existent", json=payload)
    assert response.status_code == 404

def test_revision_job_not_completed(client, db_session):
    job_id, _, _, _, _, _, _ = _seed_staging_job(db_session)
    job = db_session.query(StagingArea).filter_by(id=job_id).first()
    job.status = JobStatus.ACCEPTED.value
    db_session.commit()
    
    payload = {"operations": [{"op": "delete_item", "item_id": "item-3"}]}
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400
    assert "not yet completed" in response.json()["detail"]

def test_revision_count_incremented(client, db_session):
    job_id, _, _, _, _, _, _ = _seed_staging_job(db_session)
    payload = {"operations": [{"op": "delete_item", "item_id": "item-3"}]}
    client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    
    db_session.expire_all()
    job = db_session.query(StagingArea).filter_by(id=job_id).first()
    assert job.revision_count == 1
    
    payload2 = {"operations": [{"op": "update_item", "item_id": "item-1", "fields": {"quantity": 99}}]}
    client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload2)
    
    db_session.expire_all()
    job = db_session.query(StagingArea).filter_by(id=job_id).first()
    assert job.revision_count == 2

def test_confirm_after_revision(client, db_session):
    job_id, _, _, _, bp1_id, _, _ = _seed_staging_job(db_session)
    
    # Revise: update item-1 quantity
    payload = {
        "operations": [
            {"op": "update_item", "item_id": "item-1", "fields": {"quantity": 99}}
        ]
    }
    client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    
    # Confirm
    response = client.post(f"/api/v1/ingestion/confirm/{job_id}")
    assert response.status_code == 200
    
    # Verify DB
    # The blueprint bp1 was "New BP 1", so a real ArticleBlueprint should be created.
    bps = db_session.query(ArticleBlueprint).all()
    # 1 seeded + 2 new (from bp1, bp2)
    assert len(bps) == 3
    
    # Verify articles
    articles = db_session.query(Article).all()
    # 99 (item-1) + 5 (item-2) + 1 (item-3) = 105 items
    assert len(articles) == 105

def test_update_blueprint_invalid_fields(client, db_session):
    job_id, _, _, _, bp1_id, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {"op": "update_blueprint", "blueprint_id": bp1_id, "fields": {"sub-category": "something"}}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400
    assert "invalid fields" in response.json()["detail"]

def test_update_blueprint_empty_materials_tags(client, db_session):
    job_id, _, _, _, bp1_id, _, _ = _seed_staging_job(db_session)
    payload = {
        "operations": [
            {"op": "update_blueprint", "blueprint_id": bp1_id, "fields": {"materials": []}}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400
    assert "must be a non-empty list" in response.json()["detail"]

def test_update_blueprint_invalid_sub_category_parent(client, db_session):
    job_id, _, cat1_id, cat2_id, bp1_id, _, _ = _seed_staging_job(db_session)
    
    # Create a subcategory that belongs to cat2
    sub_cat2 = Category(name="SubCat2", description="Desc", parent_id=cat2_id)
    db_session.add(sub_cat2)
    db_session.commit()
    
    # Try to assign it to bp1, which has category cat1
    payload = {
        "operations": [
            {"op": "update_blueprint", "blueprint_id": bp1_id, "fields": {"sub_category": {"id": str(sub_cat2.id)}}}
        ]
    }
    response = client.put(f"/api/v1/ingestion/staging/{job_id}", json=payload)
    assert response.status_code == 400
    assert "does not belong to category" in response.json()["detail"]
