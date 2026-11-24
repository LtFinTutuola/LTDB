"""
tests/test_phase4_backend_agent_commands.py
-------------------------------------------
Unit and integration tests for Phase 4 — Macro-Area 4 (Backend LTDB side):
1. Remote agent command queue (`force_sync`, `update_excel_path`, `update_config`)
   and atomic draining on `POST /api/v1/excel-synchronization/push-batch`.
2. `POST /api/v1/excel-synchronization/force-sync` endpoint.
3. Automatic command queueing from `PUT /api/v1/settings` when `excel_file_path`
   or `excel_code_column` changes.
4. Edge Agent diagnostic `agent_status` recording and exposure in
   `GET /api/v1/excel-synchronization/status`.
5. End-to-end integration between FastAPI backend and `ltdb_forwarder.run_cycle`.
"""
from datetime import date
import json
from pathlib import Path
import time
from unittest.mock import patch

from fastapi.testclient import TestClient
import openpyxl
import pytest
import yaml

from scripts.excel_agent.ltdb_forwarder import (
    create_agent_state,
    load_daily_state,
    run_cycle,
    setup_logging,
)
from src.main import app
import src.routers.settings_router as settings_router
from src.services import excel_synchronization_service


@pytest.fixture(autouse=True)
def _clean_agent_queues():
    """Ensures in-memory Edge Agent command queue, async updates, and diagnostic status are clean."""
    excel_synchronization_service.clear_agent_commands()
    excel_synchronization_service.clear_async_row_updates()
    excel_synchronization_service.reset_agent_status()
    excel_synchronization_service.cancel_all_post_purchase_timers()
    yield
    excel_synchronization_service.cancel_all_post_purchase_timers()
    excel_synchronization_service.clear_agent_commands()
    excel_synchronization_service.clear_async_row_updates()
    excel_synchronization_service.reset_agent_status()


@pytest.fixture
def client(db_session):
    return TestClient(app)


@pytest.fixture
def mock_settings_yaml(tmp_path, monkeypatch):
    tmp_config = tmp_path / "config.yaml"
    tmp_gemini = tmp_path / "gemini.yaml"

    with open(tmp_config, "w", encoding="utf-8") as f:
        yaml.dump(
            {
                "db_connection_string": "sqlite:///{BASE_DIR}/data/mock.db",
                "excel_file_path": "/tmp/Stat € A.xlsx",
                "excel_code_column": "N",
                "excel_polling_interval": 5,
            },
            f,
            sort_keys=False,
        )
    with open(tmp_gemini, "w", encoding="utf-8") as f:
        yaml.dump(
            {
                "api_key": "MOCK_KEY",
                "codes_deduction_agent_model": "gemini-3.1-flash-lite",
            },
            f,
            sort_keys=False,
        )

    monkeypatch.setattr(settings_router, "CONFIG_YAML_PATH", tmp_config)
    monkeypatch.setattr(settings_router, "GEMINI_YAML_PATH", tmp_gemini)
    return tmp_config, tmp_gemini


def test_force_sync_endpoint_enqueues_and_drains_on_control_poll(client):
    """
    Verifies that `POST /api/v1/excel-synchronization/force-sync` queues `force_sync`,
    that a subsequent Control Poll (`POST /push-batch` with `rows=[]`) receives it in
    `agent_commands`, and that the queue is atomically drained so the next poll gets `[]`.
    """
    res_force = client.post("/api/v1/excel-synchronization/force-sync")
    assert res_force.status_code == 200
    body_force = res_force.json()
    assert body_force["status"] == "success"
    assert body_force["queued_command"] == {"command": "force_sync"}

    # Before poll, status shows 1 pending command
    res_status_before = client.get("/api/v1/excel-synchronization/status")
    assert res_status_before.status_code == 200
    assert res_status_before.json()["data"]["edge_agent"]["pending_commands_count"] == 1

    # Control Poll (rows=[]) receives the command and drains the queue
    res_poll1 = client.post(
        "/api/v1/excel-synchronization/push-batch",
        json={"date": date.today().isoformat(), "rows": []},
    )
    assert res_poll1.status_code == 200
    assert res_poll1.json()["agent_commands"] == [{"command": "force_sync"}]

    # Second Control Poll sees an empty queue
    res_poll2 = client.post(
        "/api/v1/excel-synchronization/push-batch",
        json={"date": date.today().isoformat(), "rows": []},
    )
    assert res_poll2.status_code == 200
    assert res_poll2.json()["agent_commands"] == []


def test_update_settings_enqueues_update_excel_path_and_update_config(
    client, mock_settings_yaml
):
    """
    Verifies that `PUT /api/v1/settings` queues `update_excel_path` when `excel_file_path`
    changes, queues `update_config` when `excel_code_column` changes, and queues nothing
    when those values are unchanged.
    """
    payload_changed = {
        "db_connection_string": "sqlite:///{BASE_DIR}/data/mock.db",
        "excel_file_path": "/tmp/Stat € B.xlsx",
        "excel_code_column": "M",
        "api_key": "MOCK_KEY",
        "codes_deduction_agent_model": "gemini-3.1-flash-lite",
    }
    res_put1 = client.put("/api/v1/settings", json=payload_changed)
    assert res_put1.status_code == 200

    res_poll1 = client.post(
        "/api/v1/excel-synchronization/push-batch",
        json={"date": date.today().isoformat(), "rows": []},
    )
    assert res_poll1.status_code == 200
    cmds = res_poll1.json()["agent_commands"]
    assert cmds == [
        {"command": "update_excel_path", "new_excel_path": "/tmp/Stat € B.xlsx"},
        {"command": "update_config", "data": {"excel_code_column": "M"}},
    ]

    # Re-saving identical Excel settings should NOT enqueue redundant commands
    res_put2 = client.put("/api/v1/settings", json=payload_changed)
    assert res_put2.status_code == 200
    res_poll2 = client.post(
        "/api/v1/excel-synchronization/push-batch",
        json={"date": date.today().isoformat(), "rows": []},
    )
    assert res_poll2.status_code == 200
    assert res_poll2.json()["agent_commands"] == []


def test_push_batch_records_agent_status_and_exposes_in_sync_status(client):
    """
    Verifies that diagnostic `agent_status` sent in `push-batch` (including when the Excel
    file is missing / unreadable) is stored and returned under `edge_agent` by `GET /status`.
    """
    res_poll = client.post(
        "/api/v1/excel-synchronization/push-batch",
        json={
            "date": date.today().isoformat(),
            "rows": [],
            "agent_status": {
                "configured_file": "K:\\Stat € B.xlsx",
                "monitored_file": None,
                "file_readable": False,
                "last_sync_at": "2026-11-24T10:15:00",
            },
        },
    )
    assert res_poll.status_code == 200

    res_status = client.get("/api/v1/excel-synchronization/status")
    assert res_status.status_code == 200
    edge_agent = res_status.json()["data"]["edge_agent"]
    assert edge_agent["configured_file"] == "K:\\Stat € B.xlsx"
    assert edge_agent["monitored_file"] is None
    assert edge_agent["file_readable"] is False
    assert edge_agent["last_sync_at"] == "2026-11-24T10:15:00"
    assert edge_agent["reported_at"] is not None
    assert edge_agent["pending_commands_count"] == 0


def test_push_batch_with_rows_also_drains_agent_commands(client):
    """
    Verifies that a normal Timer 1 data push (`rows` non-empty) also receives and drains
    any pending `agent_commands` alongside `row_updates` and `async_row_updates`.
    """
    excel_synchronization_service.enqueue_agent_command(
        {"command": "update_excel_path", "new_excel_path": "K:\\Stat € B.xlsx"}
    )

    res_push = client.post(
        "/api/v1/excel-synchronization/push-batch",
        json={
            "date": date.today().isoformat(),
            "rows": [
                {
                    "excel_row_index": 2,
                    "excel_file_column": 0,
                    "starting_price": 100.0,
                    "selling_price": 90.0,
                    "is_exchange": False,
                    "raw_article_code": "ART-100",
                    "is_unprocessable": False,
                }
            ],
            "agent_status": {
                "configured_file": "K:\\Stat € A.xlsx",
                "monitored_file": "K:\\Stat € A.xlsx",
                "file_readable": True,
                "last_sync_at": None,
            },
        },
    )
    assert res_push.status_code == 200
    body = res_push.json()
    assert body["confirmed_row_indexes"] == [2]
    assert body["agent_commands"] == [
        {"command": "update_excel_path", "new_excel_path": "K:\\Stat € B.xlsx"}
    ]

    # Queue is now empty
    res_status = client.get("/api/v1/excel-synchronization/status")
    assert res_status.json()["data"]["edge_agent"]["pending_commands_count"] == 0
    assert res_status.json()["data"]["edge_agent"]["file_readable"] is True
    assert res_status.json()["data"]["edge_agent"]["monitored_file"] == "K:\\Stat € A.xlsx"


def test_end_to_end_backend_force_sync_and_forwarder_execution(client, tmp_path):
    """
    End-to-end integration test connecting `ltdb_forwarder.run_cycle` directly to the FastAPI
    `TestClient` backend:
    1. Tick 1 pushes row 2 from Excel and records its hash locally.
    2. Admin triggers `POST /api/v1/excel-synchronization/force-sync`.
    3. Tick 2 (Timer 2 Control Poll) receives `force_sync` from LTDB and clears local hash cache.
    4. Tick 3 (0.5s later, mtime unchanged) performs a full rescan and re-pushes row 2 to LTDB.
    """
    excel_file = tmp_path / "Stat € B.xlsx"
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.cell(row=2, column=1, value=120.0)
    sheet.cell(row=2, column=2, value=110.0)
    sheet.cell(row=2, column=14, value="E2E-ART-1")
    wb.save(str(excel_file))
    wb.close()

    local_cfg = {
        "target_os": "mac",
        "ltdb_base_url": "http://testserver",
        "push_endpoint": "/api/v1/excel-synchronization/push-batch",
        "http_timeout_seconds": 5,
        "control_polling_interval_seconds": 5,
        "fallback_config": {
            "excel_file_path": str(excel_file),
            "excel_code_column": "N",
            "start_row": 2,
            "excel_polling_interval": 5,
            "retention_days": 30,
        },
    }
    (tmp_path / "config.json").write_text(json.dumps(local_cfg), encoding="utf-8")
    runtime_cfg = dict(local_cfg["fallback_config"])
    logger = setup_logging(tmp_path)
    agent_state = create_agent_state()

    pushed_batches = []

    def bridge_push_to_testclient(
        base_url,
        push_endpoint,
        target_date,
        rows,
        timeout,
        logger,
        out_row_updates=None,
        agent_status=None,
        out_agent_commands=None,
    ):
        payload = {"date": target_date.isoformat(), "rows": rows}
        if agent_status is not None:
            payload["agent_status"] = agent_status
        pushed_batches.append(payload)
        resp = client.post(push_endpoint, json=payload)
        assert resp.status_code == 200
        data = resp.json()
        if out_row_updates is not None:
            out_row_updates.extend(data.get("row_updates", []))
            out_row_updates.extend(data.get("async_row_updates", []))
        if out_agent_commands is not None:
            out_agent_commands.extend(data.get("agent_commands", []))
        return data["confirmed_row_indexes"]

    with patch(
        "scripts.excel_agent.ltdb_forwarder.push_batch_to_ltdb",
        side_effect=bridge_push_to_testclient,
    ):
        # 1. Tick 1: Initial push of row 2
        run_cycle(tmp_path, runtime_cfg, local_cfg, logger, agent_state=agent_state)
        assert len(pushed_batches) == 1
        assert len(pushed_batches[0]["rows"]) == 1
        assert "2" in load_daily_state(tmp_path / "state", date.today())

        # 2. Admin calls POST /force-sync on backend
        res_force = client.post("/api/v1/excel-synchronization/force-sync")
        assert res_force.status_code == 200

        # 3. Tick 2: Timer 2 Control Poll retrieves `force_sync` and clears local hash cache
        agent_state["last_control_poll_time"] = time.time() - 10.0
        run_cycle(tmp_path, runtime_cfg, local_cfg, logger, agent_state=agent_state)
        assert len(pushed_batches) == 2
        assert pushed_batches[1]["rows"] == []
        assert load_daily_state(tmp_path / "state", date.today()) == {}
        assert agent_state["force_sync_requested"] is True

        # 4. Tick 3 (0.5s later, mtime unchanged): full rescan re-pushes row 2 to backend!
        run_cycle(tmp_path, runtime_cfg, local_cfg, logger, agent_state=agent_state)
        assert len(pushed_batches) == 3
        assert len(pushed_batches[2]["rows"]) == 1
        assert pushed_batches[2]["rows"][0]["raw_article_code"] == "E2E-ART-1"
        assert agent_state["force_sync_requested"] is False
