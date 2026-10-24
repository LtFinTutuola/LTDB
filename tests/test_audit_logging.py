"""
tests/test_audit_logging.py
---------------------------
Verifies that AppLogger automatically enriches records with current user context
and that AccessLogMiddleware logs HTTP requests and security events correctly.
"""
import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from src.main import app
from src.core.logger import get_logger
from src.core.request_context import AuthUser, current_user_var
from src.core.config import settings

logger = get_logger()


def test_logger_enrichment_with_user_context():
    """Verifies that AppLogger automatically includes user and role when ContextVar is set."""
    user = AuthUser(id="123", username="test_operator", role="base")
    token = current_user_var.set(user)

    try:
        logger.log_execution("test_component", "test_event", "ok", custom_attr="val123")
    finally:
        current_user_var.reset(token)

    # Read the latest record in the log file
    with open(logger.file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    last_record = json.loads(lines[-1])
    assert last_record["cat"] == "EXEC"
    assert last_record["component"] == "test_component"
    assert last_record["event"] == "test_event"
    assert last_record["user"] == "test_operator"
    assert last_record["role"] == "base"
    assert last_record["custom_attr"] == "val123"


def test_log_security_category():
    """Verifies that log_security records under SEC category."""
    logger.log_security("sec_component", "sec_event", "warning", alert_reason="test")

    with open(logger.file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    last_record = json.loads(lines[-1])
    assert last_record["cat"] == "SEC"
    assert last_record["component"] == "sec_component"
    assert last_record["event"] == "sec_event"
    assert last_record["status"] == "warning"
    assert last_record["alert_reason"] == "test"


def test_access_log_middleware_captures_client_ip():
    """Verifies that AccessLogMiddleware logs requests with X-Forwarded-For IP address."""
    client = TestClient(app)
    # Perform a request with X-Forwarded-For header
    client.get(
        "/api/v1/excel-synchronization/agent-config",
        headers={"X-Forwarded-For": "203.0.113.195, 10.0.0.1"},
    )

    with open(logger.file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Find the http_access record
    matching = [
        json.loads(line) for line in lines
        if json.loads(line).get("component") == "http_access"
        and json.loads(line).get("path") == "/api/v1/excel-synchronization/agent-config"
    ]
    assert len(matching) > 0
    record = matching[-1]
    assert record["ip"] == "203.0.113.195"
    assert record["user"] == "edge-agent"
    assert record["status_code"] == 200
