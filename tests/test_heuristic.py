import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.core.database import get_db

client = TestClient(app)

class MockSessionLocal:
    def __init__(self, session):
        self.session = session
    def __enter__(self):
        return self.session
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

def test_trigger_heuristic(db_session, monkeypatch):
    app.dependency_overrides[get_db] = lambda: db_session
    monkeypatch.setattr("src.services.heuristic_service.SessionLocal", lambda: MockSessionLocal(db_session))
    
    from src.models.pim import Brand
    test_brand = Brand(name="Heuristic Brand")
    db_session.add(test_brand)
    db_session.commit()
    
    from src.models.wms import Article
    art = Article(brand_id=test_brand.id, supplier_code="CODE-123", article_blueprint_id="fake-bp")
    db_session.add(art)
    db_session.commit()

    async def mock_aexecute(self, input_data):
        return {
            "status": "success",
            "regex_pattern": "^(?P<model_code>[A-Z]+)-.*$",
            "explanation": "Test explanation"
        }
        
    monkeypatch.setattr("src.services.heuristic_service.CodesDeductionAgent.aexecute", mock_aexecute)
    
    response = client.post(f"/api/v1/brands/{test_brand.id}/heuristic", json={"file_path": "fake.pdf"})
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data

def test_confirm_heuristic(db_session, monkeypatch):
    app.dependency_overrides[get_db] = lambda: db_session
    
    from src.models.pim import Brand
    test_brand = Brand(name="Heuristic Confirm Brand", brand_code_heuristic=".*", heuristic_confirmed=False)
    db_session.add(test_brand)
    db_session.commit()
    
    from src.models.staging import StagingArea
    import uuid
    job_id = str(uuid.uuid4())
    job = StagingArea(id=job_id, status="COMPLETED", data={"regex_pattern": ".*", "explanation": "test"}, job_type="HEURISTIC")
    db_session.add(job)
    db_session.commit()
    
    response = client.post(f"/api/v1/brands/{test_brand.id}/heuristic/{job_id}/confirm")
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    
    db_session.refresh(test_brand)
    assert test_brand.heuristic_confirmed is True

def test_confirm_heuristic_missing_job(db_session, monkeypatch):
    app.dependency_overrides[get_db] = lambda: db_session
    
    from src.models.pim import Brand
    test_brand = Brand(name="Heuristic Missing", brand_code_heuristic=None, heuristic_confirmed=False)
    db_session.add(test_brand)
    db_session.commit()
    
    import uuid
    job_id = str(uuid.uuid4())
    response = client.post(f"/api/v1/brands/{test_brand.id}/heuristic/{job_id}/confirm")
    assert response.status_code == 404
