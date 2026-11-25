"""
src/routers/settings_router.py
------------------------------
REST API router for reading and updating application settings:
- GET/PUT /api/v1/settings: Admin-only system (DB) & Gemini AI configuration.
- GET/PUT /api/v1/settings/excel: Cash register ('cassa') & Admin Excel configuration,
  automatically queueing remote commands (`update_excel_path`, `update_config`) for the Edge Agent.
"""
from pathlib import Path
from typing import Dict, Any
import yaml
from fastapi import APIRouter, Depends, HTTPException, status

from src.core.auth import require_admin, require_cassa_or_admin
from src.core.config import settings, BASE_DIR
from src.core.logger import get_logger
from src.core.request_context import AuthUser
from src.schemas.settings import (
    ExcelSettingsResponse,
    ExcelSettingsUpdateRequest,
    SettingsResponse,
    SettingsUpdateRequest,
    normalize_excel_column_letter,
)
from src.services import excel_synchronization_service

logger = get_logger()

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])

CONFIG_YAML_PATH = BASE_DIR / "src" / "config.yaml"
GEMINI_YAML_PATH = BASE_DIR / "src" / "agents" / "gemini.yaml"


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_yaml(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _apply_excel_settings_and_enqueue_commands(
    cfg: Dict[str, Any],
    new_excel_path: str,
    new_code_col: str,
    new_phone_col: str,
    new_status_col: str,
    new_otp_col: str,
) -> None:
    """
    Mutates `cfg` with updated Excel settings, syncs in-memory `settings`, and enqueues
    `update_excel_path` / `update_config` remote commands for any modified fields.
    """
    old_excel_path = str(cfg.get("excel_file_path", "")).strip()
    old_code_col = str(cfg.get("excel_code_column", "N") or "N").strip().upper()
    old_phone_col = str(cfg.get("excel_phone_column", "O") or "O").strip().upper()
    old_status_col = str(cfg.get("excel_status_column", "P") or "P").strip().upper()
    old_otp_col = str(cfg.get("excel_otp_column", "Q") or "Q").strip().upper()

    cfg["excel_file_path"] = new_excel_path
    cfg["excel_code_column"] = new_code_col
    cfg["excel_phone_column"] = new_phone_col
    cfg["excel_status_column"] = new_status_col
    cfg["excel_otp_column"] = new_otp_col

    settings.EXCEL_FILE_PATH = new_excel_path.replace("{BASE_DIR}", str(BASE_DIR))
    settings.EXCEL_CODE_COLUMN = new_code_col
    settings.EXCEL_PHONE_COLUMN = new_phone_col
    settings.EXCEL_STATUS_COLUMN = new_status_col
    settings.EXCEL_OTP_COLUMN = new_otp_col

    if new_excel_path and new_excel_path != old_excel_path:
        excel_synchronization_service.enqueue_agent_command({
            "command": "update_excel_path",
            "new_excel_path": new_excel_path,
        })

    changed_cols: Dict[str, Any] = {}
    if new_code_col and new_code_col != old_code_col:
        changed_cols["excel_code_column"] = new_code_col
    if new_phone_col and new_phone_col != old_phone_col:
        changed_cols["excel_phone_column"] = new_phone_col
    if new_status_col and new_status_col != old_status_col:
        changed_cols["excel_status_column"] = new_status_col
    if new_otp_col and new_otp_col != old_otp_col:
        changed_cols["excel_otp_column"] = new_otp_col

    if changed_cols:
        excel_synchronization_service.enqueue_agent_command({
            "command": "update_config",
            "data": changed_cols,
        })


@router.get("", response_model=SettingsResponse)
def get_settings(
    _admin: AuthUser = Depends(require_admin),
):
    """
    Retrieve current configurable settings from config.yaml and gemini.yaml (Admin only).
    """
    logger.log_execution("settings_router", "get_settings", "ok")
    try:
        cfg = _read_yaml(CONFIG_YAML_PATH)
        gem = _read_yaml(GEMINI_YAML_PATH)

        return SettingsResponse(
            db_connection_string=str(cfg.get("db_connection_string", "")),
            excel_file_path=str(cfg.get("excel_file_path", "")),
            excel_code_column=str(cfg.get("excel_code_column", "N") or "N"),
            excel_phone_column=str(cfg.get("excel_phone_column", "O") or "O"),
            excel_status_column=str(cfg.get("excel_status_column", "P") or "P"),
            excel_otp_column=str(cfg.get("excel_otp_column", "Q") or "Q"),
            api_key=str(gem.get("api_key", "")),
            codes_deduction_agent_model=str(gem.get("codes_deduction_agent_model", "")),
        )
    except Exception as exc:
        logger.log_execution("settings_router", "get_settings_error", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Impossibile leggere le impostazioni: {str(exc)}",
        )


@router.put("", response_model=SettingsResponse)
def update_settings(
    request: SettingsUpdateRequest,
    _admin: AuthUser = Depends(require_admin),
):
    """
    Update system & Gemini settings in config.yaml and gemini.yaml (Admin only),
    preserving all untouched keys and updating in-memory settings.
    """
    logger.log_execution("settings_router", "update_settings_request", "ok")
    try:
        cfg = _read_yaml(CONFIG_YAML_PATH)

        cfg["db_connection_string"] = request.db_connection_string.strip()

        new_excel_path = (
            request.excel_file_path.strip()
            if request.excel_file_path is not None
            else str(cfg.get("excel_file_path", "")).strip()
        )
        new_code_col = (
            normalize_excel_column_letter(request.excel_code_column, "Colonna codice articolo")
            if request.excel_code_column is not None
            else str(cfg.get("excel_code_column", "N") or "N").strip().upper()
        )
        new_phone_col = (
            normalize_excel_column_letter(request.excel_phone_column, "Colonna numero di telefono")
            if request.excel_phone_column is not None
            else str(cfg.get("excel_phone_column", "O") or "O").strip().upper()
        )
        new_status_col = (
            normalize_excel_column_letter(request.excel_status_column, "Colonna info cliente")
            if request.excel_status_column is not None
            else str(cfg.get("excel_status_column", "P") or "P").strip().upper()
        )
        new_otp_col = (
            normalize_excel_column_letter(request.excel_otp_column, "Colonna codici sconto")
            if request.excel_otp_column is not None
            else str(cfg.get("excel_otp_column", "Q") or "Q").strip().upper()
        )

        _apply_excel_settings_and_enqueue_commands(
            cfg=cfg,
            new_excel_path=new_excel_path,
            new_code_col=new_code_col,
            new_phone_col=new_phone_col,
            new_status_col=new_status_col,
            new_otp_col=new_otp_col,
        )
        _write_yaml(CONFIG_YAML_PATH, cfg)

        gem = _read_yaml(GEMINI_YAML_PATH)
        gem["api_key"] = request.api_key.strip()
        gem["codes_deduction_agent_model"] = request.codes_deduction_agent_model.strip()
        _write_yaml(GEMINI_YAML_PATH, gem)

        settings.SQLITE_URL = cfg["db_connection_string"].replace("{BASE_DIR}", str(BASE_DIR))

        logger.log_execution("settings_router", "update_settings_success", "ok")
        return SettingsResponse(
            db_connection_string=cfg["db_connection_string"],
            excel_file_path=cfg["excel_file_path"],
            excel_code_column=cfg["excel_code_column"],
            excel_phone_column=cfg["excel_phone_column"],
            excel_status_column=cfg["excel_status_column"],
            excel_otp_column=cfg["excel_otp_column"],
            api_key=gem["api_key"],
            codes_deduction_agent_model=gem["codes_deduction_agent_model"],
        )
    except ValueError as val_exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(val_exc),
        )
    except Exception as exc:
        logger.log_execution("settings_router", "update_settings_error", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Impossibile salvare le impostazioni: {str(exc)}",
        )


@router.get("/excel", response_model=ExcelSettingsResponse)
def get_excel_settings(
    _user: AuthUser = Depends(require_cassa_or_admin),
):
    """
    Retrieve Excel file & column mapping settings (accessible to 'cassa' and 'admin').
    Never exposes DB connection strings or Gemini API credentials.
    """
    logger.log_execution("settings_router", "get_excel_settings", "ok")
    try:
        cfg = _read_yaml(CONFIG_YAML_PATH)
        return ExcelSettingsResponse(
            excel_file_path=str(cfg.get("excel_file_path", "")),
            excel_code_column=str(cfg.get("excel_code_column", "N") or "N").strip().upper(),
            excel_phone_column=str(cfg.get("excel_phone_column", "O") or "O").strip().upper(),
            excel_status_column=str(cfg.get("excel_status_column", "P") or "P").strip().upper(),
            excel_otp_column=str(cfg.get("excel_otp_column", "Q") or "Q").strip().upper(),
        )
    except Exception as exc:
        logger.log_execution("settings_router", "get_excel_settings_error", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Impossibile leggere le impostazioni Excel: {str(exc)}",
        )


@router.put("/excel", response_model=ExcelSettingsResponse)
def update_excel_settings(
    request: ExcelSettingsUpdateRequest,
    _user: AuthUser = Depends(require_cassa_or_admin),
):
    """
    Update Excel file path & column letters in config.yaml (accessible to 'cassa' and 'admin')
    and automatically enqueue remote commands for the standalone Edge Forwarder.
    """
    logger.log_execution("settings_router", "update_excel_settings_request", "ok")
    try:
        cfg = _read_yaml(CONFIG_YAML_PATH)
        _apply_excel_settings_and_enqueue_commands(
            cfg=cfg,
            new_excel_path=request.excel_file_path,
            new_code_col=request.excel_code_column,
            new_phone_col=request.excel_phone_column,
            new_status_col=request.excel_status_column,
            new_otp_col=request.excel_otp_column,
        )
        _write_yaml(CONFIG_YAML_PATH, cfg)

        logger.log_execution("settings_router", "update_excel_settings_success", "ok")
        return ExcelSettingsResponse(
            excel_file_path=cfg["excel_file_path"],
            excel_code_column=cfg["excel_code_column"],
            excel_phone_column=cfg["excel_phone_column"],
            excel_status_column=cfg["excel_status_column"],
            excel_otp_column=cfg["excel_otp_column"],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.log_execution("settings_router", "update_excel_settings_error", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Impossibile salvare le impostazioni Excel: {str(exc)}",
        )
