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

class Settings(BaseSettings):
    PROJECT_NAME: str = yaml_config.get("project_name", "")
    
    # Database
    SQLITE_URL: str = yaml_config.get("db_connection_string", "").replace("{BASE_DIR}", str(BASE_DIR))
    
    class Config:
        env_file = ".env"

settings = Settings()
