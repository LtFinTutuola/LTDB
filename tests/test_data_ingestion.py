import pytest
import os
from fastapi.testclient import TestClient
from src.main import app
from src.core.config import settings

client = TestClient(app)

def test_extract_endpoint(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STAGING_DIRECTORY", str(tmp_path / "staging"))
    
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
    
    staging_file = tmp_path / "staging" / f"{job_id}.json"
    assert staging_file.exists()
    
    response = client.get(f"/api/v1/ingestion/extract/{job_id}")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert len(data["items"]) == 2
    
    assert not staging_file.exists()

def test_extract_endpoint_file_not_found():
    response = client.post(
        "/api/v1/ingestion/extract",
        json={"file_path": "/nonexistent/path/doc.pdf"}
    )
    assert response.status_code == 404

def test_extract_endpoint_invalid_file_format(tmp_path):
    dummy_txt = tmp_path / "test_doc.txt"
    dummy_txt.write_text("invalid format content")
    
    response = client.post(
        "/api/v1/ingestion/extract",
        json={"file_path": str(dummy_txt)}
    )
    assert response.status_code == 400
    assert "Invalid file format" in response.json()["message"]

def test_confirm_endpoint(db_session):
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
    
    from src.core.database import get_db
    app.dependency_overrides[get_db] = lambda: db_session
    
    from sqlalchemy import text
    tables = db_session.execute(text("SELECT name FROM sqlite_master WHERE type='table';")).fetchall()
    print("TABLES:", tables)
    
    response = client.post(
        "/api/v1/ingestion/confirm",
        json=payload
    )
    if response.status_code != 200:
        print(response.json())
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
