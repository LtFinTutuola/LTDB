import os
import yaml
from pathlib import Path
from pydantic_settings import BaseSettings

# Define base directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Read config.yaml
config_path = BASE_DIR / "src" / "config.yaml"
with open(config_path, "r") as f:
    yaml_config = yaml.safe_load(f)

# Logging
logging_config = yaml_config.get("logging", {})

# Auth
auth_config = yaml_config.get("auth", {})

class Settings(BaseSettings):
    PROJECT_NAME: str = yaml_config.get("project_name", "")
    
    # Server configuration
    API_HOST: str = yaml_config.get("api_host", "0.0.0.0")
    API_PORT: int = int(yaml_config.get("api_port", 8000))
    
    # Database
    SQLITE_URL: str = yaml_config.get("db_connection_string", "").replace("{BASE_DIR}", str(BASE_DIR))
    
    # Staging Storage
    EXCEL_FILE_PATH: str = yaml_config.get("excel_file_path", "").replace("{BASE_DIR}", str(BASE_DIR))
    EXCEL_POLLING_INTERVAL: int = int(yaml_config.get("excel_polling_interval", 60))
    EXCEL_CODE_COLUMN: str = yaml_config.get("excel_code_column", "")
    EXCEL_START_ROW: int = int(yaml_config.get("excel_start_row", 2))
    EXCEL_RETENTION_DAYS: int = int(yaml_config.get("excel_retention_days", 30))
    
    # Logging Config
    LOGS_DIR: str = logging_config.get("logs_dir", "data/logs/execution").replace("{BASE_DIR}", str(BASE_DIR))
    LOGGING_LEVEL: str = logging_config.get("logging_level", "STANDARD").upper()
    SAVING_INTERVAL: int = int(logging_config.get("saving_interval", 24))

    # Auth Config
    AUTH_BOOTSTRAP_ADMIN_PASSWORD: str = str(auth_config.get("bootstrap_admin_password", "admin"))
    AUTH_JWT_SECRET: str = str(auth_config.get("jwt_secret", ""))
    AUTH_TOKEN_TTL_DAYS: int = int(auth_config.get("token_ttl_days", 90))
    AUTH_COOKIE_NAME: str = str(auth_config.get("cookie_name", "ltdb_token"))
    AUTH_COOKIE_SECURE: bool = bool(auth_config.get("cookie_secure", False))
    AUTH_MIN_PASSWORD_LENGTH: int = int(auth_config.get("min_password_length", 4))
    
    class Config:
        env_file = ".env"

settings = Settings()
