"""
src/routers/settings_router.py
------------------------------
REST API router for reading and updating application settings
in src/config.yaml and src/agents/gemini.yaml.
"""
from pathlib import Path
import yaml
from fastapi import APIRouter, HTTPException, status
from src.schemas.settings import SettingsResponse, SettingsUpdateRequest
from src.core.logger import get_logger
from src.core.config import settings, BASE_DIR

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


@router.get("", response_model=SettingsResponse)
def get_settings():
    """
    Retrieve current configurable settings from config.yaml and gemini.yaml.
    """
    logger.log_execution("settings_router", "get_settings", "ok")
    try:
        cfg = _read_yaml(CONFIG_YAML_PATH)
        gem = _read_yaml(GEMINI_YAML_PATH)

        return SettingsResponse(
            db_connection_string=str(cfg.get("db_connection_string", "")),
            excel_file_path=str(cfg.get("excel_file_path", "")),
            excel_code_column=str(cfg.get("excel_code_column", "")),
            api_key=str(gem.get("api_key", "")),
            codes_deduction_agent_model=str(gem.get("codes_deduction_agent_model", "")),
        )
    except Exception as exc:
        logger.log_execution("settings_router", "get_settings_error", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Impossibile leggere le impostazioni: {str(exc)}"
        )


@router.put("", response_model=SettingsResponse)
def update_settings(request: SettingsUpdateRequest):
    """
    Update configurable settings in config.yaml and gemini.yaml,
    preserving all untouched keys and updating in-memory settings.
    """
    logger.log_execution("settings_router", "update_settings_request", "ok")
    try:
        # 1. Update src/config.yaml
        cfg = _read_yaml(CONFIG_YAML_PATH)
        cfg["db_connection_string"] = request.db_connection_string.strip()
        cfg["excel_file_path"] = request.excel_file_path.strip()
        cfg["excel_code_column"] = request.excel_code_column.strip()
        _write_yaml(CONFIG_YAML_PATH, cfg)

        # 2. Update src/agents/gemini.yaml
        gem = _read_yaml(GEMINI_YAML_PATH)
        gem["api_key"] = request.api_key.strip()
        gem["codes_deduction_agent_model"] = request.codes_deduction_agent_model.strip()
        _write_yaml(GEMINI_YAML_PATH, gem)

        # 3. Synchronize in-memory settings
        settings.SQLITE_URL = cfg["db_connection_string"].replace("{BASE_DIR}", str(BASE_DIR))
        settings.EXCEL_FILE_PATH = cfg["excel_file_path"].replace("{BASE_DIR}", str(BASE_DIR))
        settings.EXCEL_CODE_COLUMN = cfg["excel_code_column"]

        logger.log_execution("settings_router", "update_settings_success", "ok")
        return SettingsResponse(
            db_connection_string=cfg["db_connection_string"],
            excel_file_path=cfg["excel_file_path"],
            excel_code_column=cfg["excel_code_column"],
            api_key=gem["api_key"],
            codes_deduction_agent_model=gem["codes_deduction_agent_model"],
        )
    except Exception as exc:
        logger.log_execution("settings_router", "update_settings_error", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Impossibile salvare le impostazioni: {str(exc)}"
        )
