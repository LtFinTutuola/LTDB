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

async def mock_sleep(seconds):
    pass

def test_extract_endpoint(db_session, tmp_path, monkeypatch):
    app.dependency_overrides[get_db] = lambda: db_session
    monkeypatch.setattr("src.services.data_ingestion_service.SessionLocal", lambda: MockSessionLocal(db_session))
    monkeypatch.setattr("asyncio.sleep", mock_sleep)
    
    dummy_pdf = tmp_path / "test_doc.pdf"
    dummy_pdf.write_text("dummy content")
    
    response = client.post(
        "/api/v1/ingestion/extract",
        json={"file_path": str(dummy_pdf)}
    )
    assert response.status_code == 202
    data = response.json()
    assert data.get("status_code") == 202
    assert "job_id" in data
    job_id = data["job_id"]
    
    # Verify staging area has job
    job = db_session.query(StagingArea).filter(StagingArea.id == job_id).first()
    assert job is not None
    assert job.status == JobStatus.COMPLETED.value
    
    response = client.get(f"/api/v1/ingestion/extract/{job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == JobStatus.COMPLETED.value
    assert "items" in data["data"]
    assert len(data["data"]["items"]) == 2
    
    app.dependency_overrides.clear()

def test_extract_endpoint_file_not_found(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    response = client.post(
        "/api/v1/ingestion/extract",
        json={"file_path": "/nonexistent/path/doc.pdf"}
    )
    assert response.status_code == 404
    app.dependency_overrides.clear()

def test_extract_endpoint_invalid_file_format(tmp_path, db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    dummy_txt = tmp_path / "test_doc.txt"
    dummy_txt.write_text("invalid format content")
    
    response = client.post(
        "/api/v1/ingestion/extract",
        json={"file_path": str(dummy_txt)}
    )
    assert response.status_code == 400
    assert "Invalid file format" in response.json()["message"]
    app.dependency_overrides.clear()

def test_confirm_endpoint(db_session, monkeypatch):
    app.dependency_overrides[get_db] = lambda: db_session
    
    payload = {
        "job_id": "test_job_123",
        "items": [
            {
                "supplier_code": "TEST-CODE-001",
                "description": "Test Item",
                "quantity": 5
            }
        ]
    }
    
    response = client.post(
        "/api/v1/ingestion/confirm",
        json=payload
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    
    from src.models.pim import ArticleBlueprint, Brand
    from src.models.wms import Article, ArticleMovement
    
    blueprint = db_session.query(ArticleBlueprint).filter(ArticleBlueprint.supplier_code == "TEST-CODE-001").first()
    assert blueprint is not None
    assert blueprint.description == "Test Item"
    
    brand = db_session.query(Brand).filter(Brand.id == blueprint.brand_id).first()
    assert brand.name == "DUMMY_BRAND"
    
    articles = db_session.query(Article).filter(Article.article_blueprint_id == str(blueprint.id)).all()
    assert len(articles) == 5
    
    movements = db_session.query(ArticleMovement).all()
    assert len(movements) == 5
    
    app.dependency_overrides.clear()
