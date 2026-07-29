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

class Settings(BaseSettings):
    PROJECT_NAME: str = yaml_config.get("project_name", "")
    
    # Server configuration
    API_HOST: str = yaml_config.get("api_host", "0.0.0.0")
    API_PORT: int = int(yaml_config.get("api_port", 8000))
    
    # Database
    SQLITE_URL: str = yaml_config.get("db_connection_string", "").replace("{BASE_DIR}", str(BASE_DIR))
    
    # Staging Storage
    STAGING_DIRECTORY: str = yaml_config.get("staging_directory", "").replace("{BASE_DIR}", str(BASE_DIR))
    
    # Logging Config
    LOGS_DIR: str = logging_config.get("logs_dir", "data/logs/execution").replace("{BASE_DIR}", str(BASE_DIR))
    LOGGING_LEVEL: str = logging_config.get("logging_level", "STANDARD").upper()
    SAVING_INTERVAL: int = int(logging_config.get("saving_interval", 24))
    
    class Config:
        env_file = ".env"

settings = Settings()
