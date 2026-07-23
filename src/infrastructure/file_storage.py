import os
import json
from pathlib import Path
from typing import Dict, Any, Optional
from src.core.config import settings

def _get_staging_dir() -> Path:
    staging_dir = Path(settings.STAGING_DIRECTORY)
    staging_dir.mkdir(parents=True, exist_ok=True)
    return staging_dir

def save_staging_file(job_id: str, data: Dict[str, Any]) -> str:
    """Saves staging data to the staging directory."""
    staging_dir = _get_staging_dir()
    file_path = staging_dir / f"{job_id}.json"
    
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        
    return str(file_path)

def read_staging_file(job_id: str) -> Optional[Dict[str, Any]]:
    """Reads staging data and deletes the file."""
    staging_dir = _get_staging_dir()
    file_path = staging_dir / f"{job_id}.json"
    
    if not file_path.exists():
        return None
        
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    # Delete file after reading
    try:
        os.remove(file_path)
    except OSError:
        pass
        
    return data
