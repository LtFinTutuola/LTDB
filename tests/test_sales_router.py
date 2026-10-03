from fastapi.testclient import TestClient
from src.main import app

def test_chat_vendite(db_session):
    client = TestClient(app)
    payload = {
        "message": "Come vanno le vendite oggi?",
        "context_date": "2023-11-04"
    }
    response = client.post("/api/v1/sales/chat", json=payload)
    assert response.status_code == 200
    assert "vendite" in response.json()["reply"].lower()

def test_chat_problemi(db_session):
    client = TestClient(app)
    payload = {
        "message": "Ci sono problemi?",
        "context_date": "2023-11-04"
    }
    response = client.post("/api/v1/sales/chat", json=payload)
    assert response.status_code == 200
    assert "problemi" in response.json()["reply"].lower()

def test_chat_fallback(db_session):
    client = TestClient(app)
    payload = {
        "message": "Ciao",
        "context_date": "2023-11-04"
    }
    response = client.post("/api/v1/sales/chat", json=payload)
    assert response.status_code == 200
    assert "ciao" in response.json()["reply"].lower()
