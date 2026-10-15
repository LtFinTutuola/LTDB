"""
tests/test_settings.py
----------------------
Unit and integration tests for Settings API endpoints,
YAML reading/writing, and frontend settings routes.
"""
import pytest
import yaml
from pathlib import Path
from fastapi.testclient import TestClient
from src.main import app
import src.routers.settings_router as settings_router


@pytest.fixture
def client(db_session):
    return TestClient(app)


@pytest.fixture
def mock_yaml_files(tmp_path, monkeypatch):
    """
    Create temporary mock config.yaml and gemini.yaml files
    and monkeypatch the router paths to use them during tests.
    """
    tmp_config = tmp_path / "config.yaml"
    tmp_gemini = tmp_path / "gemini.yaml"

    initial_config = {
        "db_connection_string": "sqlite:///{BASE_DIR}/data/mock.db",
        "project_name": "LTDB_TEST",
        "api_host": "127.0.0.1",
        "api_port": 9000,
        "excel_file_path": "/tmp/mock_sales.xlsx",
        "excel_polling_interval": 15,
        "excel_code_column": "M",
        "logging": {
            "logs_dir": "data/mock_logs",
            "logging_level": "DEBUG",
            "saving_interval": 1,
        }
    }
    with open(tmp_config, "w", encoding="utf-8") as f:
        yaml.dump(initial_config, f, sort_keys=False)

    initial_gemini = {
        "api_key": "MOCK_GEMINI_KEY_12345",
        "articles_similarity_threshold": 0.85,
        "cluster_tie_breaker_margin": 0.03,
        "db_similarity_threshold": 0.90,
        "hallucination_recognition_threshold": 0.90,
        "candidate_tolerance": 0.05,
        "codes_deduction_agent_model": "gemini-3.1-flash-lite",
    }
    with open(tmp_gemini, "w", encoding="utf-8") as f:
        yaml.dump(initial_gemini, f, sort_keys=False)

    monkeypatch.setattr(settings_router, "CONFIG_YAML_PATH", tmp_config)
    monkeypatch.setattr(settings_router, "GEMINI_YAML_PATH", tmp_gemini)

    return tmp_config, tmp_gemini


class TestSettingsEndpoints:
    def test_get_settings(self, client, mock_yaml_files):
        res = client.get("/api/v1/settings")
        assert res.status_code == 200
        data = res.json()
        assert data["db_connection_string"] == "sqlite:///{BASE_DIR}/data/mock.db"
        assert data["excel_file_path"] == "/tmp/mock_sales.xlsx"
        assert data["excel_code_column"] == "M"
        assert data["api_key"] == "MOCK_GEMINI_KEY_12345"
        assert data["codes_deduction_agent_model"] == "gemini-3.1-flash-lite"

    def test_update_settings_success(self, client, mock_yaml_files):
        tmp_config, tmp_gemini = mock_yaml_files

        payload = {
            "db_connection_string": "sqlite:///{BASE_DIR}/data/updated.db",
            "excel_file_path": "/new/path/sales.xlsx",
            "excel_code_column": "Z",
            "api_key": "NEW_TOKEN_XYZ",
            "codes_deduction_agent_model": "gemini-2.5-pro",
        }
        res = client.put("/api/v1/settings", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["db_connection_string"] == "sqlite:///{BASE_DIR}/data/updated.db"
        assert data["excel_file_path"] == "/new/path/sales.xlsx"
        assert data["excel_code_column"] == "Z"
        assert data["api_key"] == "NEW_TOKEN_XYZ"
        assert data["codes_deduction_agent_model"] == "gemini-2.5-pro"

        # Verify disk persistence and untouched keys
        with open(tmp_config, "r", encoding="utf-8") as f:
            saved_cfg = yaml.safe_load(f)
        assert saved_cfg["db_connection_string"] == "sqlite:///{BASE_DIR}/data/updated.db"
        assert saved_cfg["excel_file_path"] == "/new/path/sales.xlsx"
        assert saved_cfg["excel_code_column"] == "Z"
        # Untouched keys preserved
        assert saved_cfg["project_name"] == "LTDB_TEST"
        assert saved_cfg["logging"]["logging_level"] == "DEBUG"

        with open(tmp_gemini, "r", encoding="utf-8") as f:
            saved_gem = yaml.safe_load(f)
        assert saved_gem["api_key"] == "NEW_TOKEN_XYZ"
        assert saved_gem["codes_deduction_agent_model"] == "gemini-2.5-pro"
        # Untouched keys preserved
        assert saved_gem["articles_similarity_threshold"] == 0.85

    def test_update_settings_validation_missing_fields(self, client):
        res = client.put("/api/v1/settings", json={"db_connection_string": ""})
        assert res.status_code == 422


class TestSettingsFrontendRoutes:
    def test_settings_page(self, client):
        res = client.get("/settings")
        assert res.status_code == 200
        assert "Impostazioni" in res.text
        assert "Password Gemini" in res.text
        assert "settings-form" in res.text

    def test_impostazioni_redirect(self, client):
        res = client.get("/impostazioni", follow_redirects=False)
        assert res.status_code == 307
        assert res.headers["location"] == "/settings"
