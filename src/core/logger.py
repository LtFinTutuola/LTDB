import os
import json
import uuid
import datetime
import traceback
from typing import Any
from pathlib import Path

from src.core.config import settings

class AppLogger:
    """
    Singleton logger for the LTDB application.
    Produces single-line JSON records for execution, agents, and LLM telemetry.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AppLogger, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        self.session_id = str(uuid.uuid4())
        self.logging_level = settings.LOGGING_LEVEL
        
        # Absolute path for logs_dir
        if settings.LOGS_DIR.startswith("/"):
            self.logs_dir = Path(settings.LOGS_DIR)
        else:
            # if somehow it's not absolute, resolve against base dir
            from src.core.config import BASE_DIR
            self.logs_dir = BASE_DIR / settings.LOGS_DIR
            
        self.saving_interval = datetime.timedelta(hours=settings.SAVING_INTERVAL)

        # Ensure directory exists
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        # 1. Purge old files
        scanned, deleted = self._purge_old_files()

        # 2. Open new log file for this session
        # Use an environment variable to share the filename between master and worker processes
        run_timestamp = os.environ.get("LTDB_RUN_TIMESTAMP")
        if not run_timestamp:
            now = datetime.datetime.now(datetime.timezone.utc)
            run_timestamp = now.strftime('%Y%m%d_%H%M%S')
            os.environ["LTDB_RUN_TIMESTAMP"] = run_timestamp
            
        self.file_path = self.logs_dir / f"{run_timestamp}_log.txt"
        
        # Log startup configuration
        self.log_execution(
            component="AppLogger",
            event="startup",
            status="ok",
            config={
                "logs_dir": str(self.logs_dir),
                "logging_level": self.logging_level,
                "saving_interval_hours": settings.SAVING_INTERVAL,
                "purged_files": deleted,
                "scanned_files": scanned
            }
        )

    def _purge_old_files(self) -> tuple[int, int]:
        now = datetime.datetime.now(datetime.timezone.utc)
        scanned = 0
        deleted = 0
        try:
            for file_path in self.logs_dir.glob("*_log.txt"):
                scanned += 1
                # filename format: YYYYMMDD_HHMMSS_log.txt
                parts = file_path.name.split("_")
                if len(parts) >= 2:
                    try:
                        file_dt = datetime.datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S")
                        file_dt = file_dt.replace(tzinfo=datetime.timezone.utc)
                        if now - file_dt > self.saving_interval:
                            file_path.unlink()
                            deleted += 1
                    except ValueError:
                        pass # Ignore files that don't match the exact pattern
        except Exception:
            pass # Failsafe, don't crash app if purge fails
        return scanned, deleted

    def _write_record(self, cat: str, component: str, event: str, status: str, kwargs: dict):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        # Extract exception traceback if present
        exc = kwargs.pop("exc", None)
        if exc is not None and isinstance(exc, Exception):
            # Format traceback into a single line escaped string
            tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            kwargs["traceback"] = tb_str.replace("\n", "\\n")
            if "exc_type" not in kwargs:
                kwargs["exc_type"] = type(exc).__name__
            if "exc_message" not in kwargs:
                kwargs["exc_message"] = str(exc)
        
        record = {
            "session": self.session_id,
            "ts": now,
            "cat": cat,
            "component": component,
            "event": event,
            "status": status,
        }
        record.update(kwargs)
        
        try:
            # We use default=str to handle non-serializable objects (like datetime, pydantic models if they slip through)
            line = json.dumps(record, default=str)
            with open(self.file_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            # We don't want logging errors to crash the application
            pass

    def log_execution(self, component: str, event: str, status: str, **kwargs):
        """Always logs execution events."""
        self._write_record("EXEC", component, event, status, kwargs)

    def log_agent(self, component: str, event: str, status: str, **kwargs):
        """Logs agent events only if LOGGING_LEVEL is DEBUG."""
        if self.logging_level == "DEBUG":
            self._write_record("AGENT", component, event, status, kwargs)

    def log_llm(self, component: str, event: str, status: str, **kwargs):
        """Logs LLM events only if LOGGING_LEVEL is DEBUG."""
        if self.logging_level == "DEBUG":
            self._write_record("LLM", component, event, status, kwargs)

# Expose a getter to match idiomatic usage
def get_logger() -> AppLogger:
    return AppLogger()
