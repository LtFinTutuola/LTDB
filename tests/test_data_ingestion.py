import pytest
import os
import asyncio
from fastapi.testclient import TestClient
from src.main import app
from src.core.database import get_db
from src.models.staging import StagingArea, JobStatus

client = TestClient(app)

class MockSessionLocal:
    def __init__(self, session):
        self.session = session
    def __enter__(self):
        return self.session
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def _mock_extraction_result():
    return {
        "items": [
            {"item_id": "item-1", "vendor_code": "SUP-001", "quantity": 100, "colors": ["nero"], "article_name": "Borsa Shopper", "article_description": "Borsa tote in pelle nera."},
            {"item_id": "item-2", "vendor_code": "SUP-002", "quantity": 50, "colors": ["marrone"], "article_name": "Borsa Tracolla", "article_description": "Tracolla in tessuto marrone."},
        ],
        "warnings": [],
    }


def _mock_blueprints_result():
    return {
        "items": [
            {"item_id": "item-1", "vendor_code": "SUP-001", "quantity": 100, "colors": ["nero"], "article_blueprint_id": "bp-1"},
            {"item_id": "item-2", "vendor_code": "SUP-002", "quantity": 50, "colors": ["marrone"], "article_blueprint_id": "bp-2"},
        ],
        "blueprints": [
            {
                "id": "bp-1",
                "is_new": True,
                "category": "Borse",
                "sub_category": "Tote / Shopper",
                "article_name": "Borsa Shopper",
                "description": "Borsa tote in pelle nera.",
                "extended_description": "Borsa tote capiente in pelle nera di alta qualità.",
                "tags": ["borsa", "pelle", "nero"],
                "materials": ["pelle"],
            },
            {
                "id": "bp-2",
                "is_new": True,
                "category": "Borse",
                "sub_category": "Tracolla / Crossbody",
                "article_name": "Borsa Tracolla",
                "description": "Tracolla in tessuto marrone.",
                "extended_description": "Borsa a tracolla leggera in tessuto marrone.",
                "tags": ["tracolla", "tessuto", "marrone"],
                "materials": ["tessuto"],
            },
        ],
        "warnings": [],
    }


def test_extract_endpoint(db_session, tmp_path, monkeypatch):
    app.dependency_overrides[get_db] = lambda: db_session
    monkeypatch.setattr("src.services.data_ingestion_service.SessionLocal", lambda: MockSessionLocal(db_session))

    async def mock_extraction_aexecute(self, input_data):
        return _mock_extraction_result()

    async def mock_blueprints_aexecute(self, input_data):
        return _mock_blueprints_result()

    monkeypatch.setattr(
        "src.services.data_ingestion_service.DataExtractionAgent.aexecute",
        mock_extraction_aexecute,
    )
    monkeypatch.setattr(
        "src.services.data_ingestion_service.ArticleBlueprintsAgent.aexecute",
        mock_blueprints_aexecute,
    )

    monkeypatch.setattr(
        "src.services.data_ingestion_service.category_repo.get_brand_hierarchy",
        lambda db, brand: {
            "Borse": {"description": "Borse da donna", "sub_categories": {"Tote / Shopper": "Borsa grande"}}
        },
    )
    monkeypatch.setattr(
        "src.services.data_ingestion_service.pim_repo.get_embeddings_by_brand",
        lambda db, brand: [],
    )

    # Seed a brand and category for the test
    from src.models.pim import Brand, Category
    test_brand = Brand(name="Samsonite")
    db_session.add(test_brand)
    test_cat = Category(name="Borse", description="Borse da donna")
    db_session.add(test_cat)
    test_sub = Category(name="Tote / Shopper", description="Borsa grande", parent_id=test_cat.id)
    db_session.add(test_sub)
    db_session.commit()
    brand_id_str = str(test_brand.id)

    dummy_pdf = tmp_path / "test_doc.pdf"
    dummy_pdf.write_text("dummy content")

    response = client.post(
        "/api/v1/ingestion/extract",
        json={"file_path": str(dummy_pdf), "brand_id": brand_id_str},
    )
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    job_id = data["job_id"]

    # Verify staging area has job
    job = db_session.query(StagingArea).filter(StagingArea.id == job_id).first()
    assert job is not None
    assert job.status == JobStatus.COMPLETED.value

    response = client.get(f"/api/v1/ingestion/extract/{job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == JobStatus.COMPLETED.name
    assert "items" in data["data"]
    assert len(data["data"]["items"]) == 2
    assert "blueprints" in data["data"]
    assert len(data["data"]["blueprints"]) == 2

    app.dependency_overrides.clear()


def test_extract_endpoint_fail_fast_category_error(db_session, tmp_path, monkeypatch):
    app.dependency_overrides[get_db] = lambda: db_session
    monkeypatch.setattr("src.services.data_ingestion_service.SessionLocal", lambda: MockSessionLocal(db_session))

    async def mock_extraction_aexecute(self, input_data):
        return _mock_extraction_result()

    async def mock_blueprints_aexecute(self, input_data):
        res = _mock_blueprints_result()
        res["blueprints"][0]["category"] = "NonExistentCategory"
        return res

    monkeypatch.setattr(
        "src.services.data_ingestion_service.DataExtractionAgent.aexecute",
        mock_extraction_aexecute,
    )
    monkeypatch.setattr(
        "src.services.data_ingestion_service.ArticleBlueprintsAgent.aexecute",
        mock_blueprints_aexecute,
    )

    monkeypatch.setattr(
        "src.services.data_ingestion_service.category_repo.get_brand_hierarchy",
        lambda db, brand: {},
    )
    monkeypatch.setattr(
        "src.services.data_ingestion_service.pim_repo.get_embeddings_by_brand",
        lambda db, brand: [],
    )

    from src.models.pim import Brand
    from src.agents.base import AgentException
    test_brand = Brand(name="Samsonite Fail")
    db_session.add(test_brand)
    db_session.commit()
    brand_id_str = str(test_brand.id)

    dummy_pdf = tmp_path / "test_doc_fail.pdf"
    dummy_pdf.write_text("dummy content")

    with pytest.raises(AgentException, match="does not exist, extraction aborted"):
        client.post(
            "/api/v1/ingestion/extract",
            json={"file_path": str(dummy_pdf), "brand_id": brand_id_str},
        )
    
    job = db_session.query(StagingArea).filter(StagingArea.file_path == str(dummy_pdf)).first()
    assert job is not None
    assert job.status == JobStatus.ERROR.value
    assert "does not exist, extraction aborted" in job.data["error"]

    app.dependency_overrides.clear()


def test_extract_endpoint_file_not_found(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    response = client.post(
        "/api/v1/ingestion/extract",
        json={"file_path": "/nonexistent/path/doc.pdf", "brand_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code == 404
    app.dependency_overrides.clear()


def test_extract_endpoint_invalid_file_format(tmp_path, db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    dummy_txt = tmp_path / "test_doc.txt"
    dummy_txt.write_text("invalid format content")

    response = client.post(
        "/api/v1/ingestion/extract",
        json={"file_path": str(dummy_txt), "brand_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code == 400
    assert "Invalid file format" in response.json()["detail"]
    app.dependency_overrides.clear()


def test_extract_endpoint_missing_brand(tmp_path, db_session):
    """Omitting the brand_id field should return a 422 Unprocessable Entity."""
    app.dependency_overrides[get_db] = lambda: db_session
    dummy_pdf = tmp_path / "test_doc.pdf"
    dummy_pdf.write_text("dummy content")

    response = client.post(
        "/api/v1/ingestion/extract",
        json={"file_path": str(dummy_pdf)},  # missing brand_id
    )
    assert response.status_code == 422
    app.dependency_overrides.clear()


def test_confirm_endpoint(db_session, monkeypatch):
    app.dependency_overrides[get_db] = lambda: db_session

    from src.models.pim import Brand, Category
    dummy_brand = Brand(name="DUMMY_BRAND")
    db_session.add(dummy_brand)
    
    dummy_category = Category(name="Borse", description="Borse da donna")
    db_session.add(dummy_category)
    db_session.commit()

    # Pre-create a completed job in the database with brand_id in job.data
    job_id = "test_job_123"
    job = StagingArea(
        id=job_id,
        file_path="some/file.pdf",
        status=JobStatus.COMPLETED.value,
        data={
            "brand_id": str(dummy_brand.id),
            "items": [
                {
                    "VendorCode": "TEST-CODE-001",
                    "article_name": "Official Item Name",
                    "product_short_description": "Test Item",
                    "Barcode": "8888888888888",
                    "colors": ["Blue"],
                    "Quantity": 5,
                    "category": {
                        "id": str(dummy_category.id),
                        "description": dummy_category.name
                    }
                }
            ]
        }
    )
    db_session.add(job)
    db_session.commit()

    # Confirm with empty payload (defaults to saved job data)
    response = client.post(f"/api/v1/ingestion/confirm/{job_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "success"

    from src.models.pim import ArticleBlueprint, Brand
    from src.models.wms import Article, ArticleMovement

    blueprint = db_session.query(ArticleBlueprint).filter(ArticleBlueprint.article_name == "Official Item Name").first()
    assert blueprint is not None
    assert blueprint.description == "Test Item"
    assert blueprint.article_name == "Official Item Name"

    brand = db_session.query(Brand).filter(Brand.id == blueprint.brand_id).first()
    assert brand.name == "DUMMY_BRAND"

    articles = db_session.query(Article).filter(Article.article_blueprint_id == str(blueprint.id)).all()
    assert len(articles) == 5
    assert articles[0].supplier_code == "TEST-CODE-001"
    assert articles[0].ean == "8888888888888"
    assert articles[0].colors == ["Blue"]

    movements = db_session.query(ArticleMovement).all()
    assert len(movements) == 5

    # Ensure the job was deleted from the staging area
    deleted_job = db_session.query(StagingArea).filter(StagingArea.id == job_id).first()
    assert deleted_job is None

    app.dependency_overrides.clear()


def test_confirm_endpoint_job_not_found(db_session):
    app.dependency_overrides[get_db] = lambda: db_session

    response = client.post("/api/v1/ingestion/confirm/non_existent_job")
    assert response.status_code == 404
    assert "Job not found" in response.json()["detail"]
    app.dependency_overrides.clear()


def test_confirm_endpoint_job_not_completed(db_session):
    app.dependency_overrides[get_db] = lambda: db_session

    job_id = "test_job_accepted"
    job = StagingArea(id=job_id, file_path="some/file.pdf", status=JobStatus.ACCEPTED.value, data={})
    db_session.add(job)
    db_session.commit()

    response = client.post(f"/api/v1/ingestion/confirm/{job_id}")
    assert response.status_code == 400
    assert "Job is not yet completed" in response.json()["detail"]
    app.dependency_overrides.clear()


def test_confirm_endpoint_persistence_failure(db_session, monkeypatch):
    app.dependency_overrides[get_db] = lambda: db_session

    from src.models.pim import Brand, Category
    dummy_brand = Brand(name="DUMMY_BRAND")
    db_session.add(dummy_brand)
    
    dummy_category = Category(name="Borse", description="Borse da donna")
    db_session.add(dummy_category)
    db_session.commit()

    job_id = "test_job_fail"
    job = StagingArea(
        id=job_id,
        file_path="some/file.pdf",
        status=JobStatus.COMPLETED.value,
        data={
            "brand_id": str(dummy_brand.id),
            "items": [
                {
                    "VendorCode": "TEST-CODE-001",
                    "product_short_description": "Test Item",
                    "Quantity": 5,
                    "category": {
                        "id": str(dummy_category.id),
                        "description": dummy_category.name
                    }
                }
            ]
        }
    )
    db_session.add(job)
    db_session.commit()

    # Monkeypatch to force an exception during confirm_and_persist_staging
    def mock_raise(*args, **kwargs):
        raise Exception("Mock DB Failure")

    monkeypatch.setattr("src.services.data_ingestion_service.get_or_create_product", mock_raise)

    response = client.post(f"/api/v1/ingestion/confirm/{job_id}")
    assert response.status_code == 500
    assert "Mock DB Failure" in response.json()["detail"]

    # Ensure the job is NOT deleted from the staging area when confirmation fails
    remaining_job = db_session.query(StagingArea).filter(StagingArea.id == job_id).first()
    assert remaining_job is not None

    app.dependency_overrides.clear()
