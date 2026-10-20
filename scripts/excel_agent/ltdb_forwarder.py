"""
scripts/excel_agent/ltdb_forwarder.py
--------------------------------------
Lightweight standalone Edge Forwarder for LTDB.
Designed for Python 3.12 (x86) on Windows.

Dependencies:
  - openpyxl (pure-Python)
  - Python standard library (urllib, hashlib, json, logging, os, time, datetime, pathlib)
"""

import os
import sys
import time
import json
import hashlib
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
import urllib.request
import urllib.error

try:
    import openpyxl
except ImportError:
    openpyxl = None


def setup_logging(base_dir: Path) -> logging.Logger:
    """Configures rotating daily file and console logging."""
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    today_str = date.today().isoformat()
    log_file = logs_dir / f"agent_{today_str}.log"

    logger = logging.getLogger("ltdb_forwarder")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Avoid adding duplicate handlers if re-initialized
    if not logger.handlers:
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
        file_formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(file_formatter)
        logger.addHandler(console_handler)

    return logger


def load_local_config(base_dir: Path) -> Dict[str, Any]:
    """Loads config.json from the agent directory."""
    config_path = base_dir / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Missing configuration file at {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_local_config(base_dir: Path, config_data: Dict[str, Any]) -> None:
    """Saves updated configuration to config.json atomically."""
    config_path = base_dir / "config.json"
    tmp_path = base_dir / "config.json.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2, ensure_ascii=False)
    tmp_path.replace(config_path)


def fetch_remote_config(base_dir: Path, local_config: Dict[str, Any], logger: logging.Logger) -> Dict[str, Any]:
    """
    Downloads runtime configuration from LTDB cloud.
    If the cloud is unreachable (Alternative 1B), falls back to local fallback_config.
    """
    base_url = local_config.get("ltdb_base_url", "http://127.0.0.1:8000").rstrip("/")
    endpoint = local_config.get("config_endpoint", "/api/v1/excel-synchronization/agent-config")
    url = f"{base_url}{endpoint}"
    timeout = local_config.get("http_timeout_seconds", 10)
    fallback = local_config.get("fallback_config", {})

    logger.info(f"Attempting remote configuration handshake with {url}...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LTDB-Edge-Forwarder/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                body = json.loads(response.read().decode("utf-8"))
                remote_data = body.get("data", {})
                logger.info(f"Remote configuration successfully retrieved: {remote_data}")
                # Update fallback in local config cache
                local_config["fallback_config"] = remote_data
                save_local_config(base_dir, local_config)
                return remote_data
    except Exception as exc:
        logger.warning(
            f"Could not connect to LTDB at startup ({exc}). "
            f"Using cached fallback configuration (Alternative 1B): {fallback}"
        )

    return fallback


def cleanup_old_files(base_dir: Path, retention_days: int, logger: logging.Logger) -> None:
    """
    Deletes state JSON files and log files older than retention_days.
    """
    if retention_days <= 0:
        return

    cutoff_date = date.today() - timedelta(days=retention_days)
    logger.info(f"Running retention cleanup (cutoff: {cutoff_date.isoformat()}, retention: {retention_days} days)")

    # 1. State files: state/hashes_YYYY-MM-DD.json
    state_dir = base_dir / "state"
    if state_dir.exists():
        for item in state_dir.glob("hashes_*.json"):
            date_str = item.stem.replace("hashes_", "")
            try:
                item_date = date.fromisoformat(date_str)
                if item_date < cutoff_date:
                    item.unlink(missing_ok=True)
                    logger.info(f"Removed expired state file: {item.name}")
            except ValueError:
                pass

    # 2. Log files: logs/agent_YYYY-MM-DD.log
    logs_dir = base_dir / "logs"
    if logs_dir.exists():
        for item in logs_dir.glob("agent_*.log"):
            date_str = item.stem.replace("agent_", "")
            try:
                item_date = date.fromisoformat(date_str)
                if item_date < cutoff_date:
                    item.unlink(missing_ok=True)
                    logger.info(f"Removed expired log file: {item.name}")
            except ValueError:
                pass


def is_file_eligible_for_today(file_path: Path, logger: logging.Logger) -> bool:
    """
    Checks Level 1 (path existence) and Level 2 (mtime == today).
    """
    if not file_path.exists():
        logger.debug(f"Level 1: Target file does not exist at {file_path}")
        return False

    try:
        mtime_timestamp = os.path.getmtime(file_path)
        file_mtime_date = datetime.fromtimestamp(mtime_timestamp).date()
        today = date.today()
        if file_mtime_date != today:
            logger.debug(
                f"Level 2: File at {file_path} last modified on {file_mtime_date} != today ({today}). Skipping."
            )
            return False
        return True
    except Exception as exc:
        logger.error(f"Error checking file mtime for {file_path}: {exc}")
        return False


def compute_row_hash(row: Dict[str, Any]) -> str:
    """Computes a deterministic SHA-256 hash for an Excel row."""
    row_idx = row.get("excel_row_index")
    start_val = row.get("starting_price")
    sell_val = row.get("selling_price")
    col_idx = row.get("excel_file_column")
    is_exch = "1" if row.get("is_exchange") else "0"
    raw_code = (row.get("raw_article_code") or "").strip()

    start_str = f"{float(start_val):.2f}" if start_val is not None else ""
    sell_str = f"{float(sell_val):.2f}" if sell_val is not None else ""
    col_str = str(col_idx) if col_idx is not None else ""

    canonical = f"{row_idx}|{start_str}|{sell_str}|{col_str}|{is_exch}|{raw_code}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_daily_state(state_dir: Path, target_date: date) -> Dict[str, str]:
    """Loads the map of {row_index: row_hash} for the given date."""
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"hashes_{target_date.isoformat()}.json"
    if not state_file.exists():
        return {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("rows", {})
    except Exception:
        return {}


def save_daily_state(state_dir: Path, target_date: date, row_hashes: Dict[str, str]) -> None:
    """Saves the map of row hashes atomically."""
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"hashes_{target_date.isoformat()}.json"
    tmp_file = state_dir / f"hashes_{target_date.isoformat()}.json.tmp"
    payload = {
        "date": target_date.isoformat(),
        "last_updated_at": datetime.now().isoformat(),
        "rows": row_hashes
    }
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp_file.replace(state_file)


def read_excel_rows(
    file_path: Path,
    start_row: int = 2,
    code_column: str = "N",
    logger: Optional[logging.Logger] = None
) -> List[Dict[str, Any]]:
    """
    Reads rows using openpyxl (read_only=True, data_only=True).
    Streams from start_row up to 698, breaking when A-I cells are all None.
    """
    if not openpyxl:
        if logger:
            logger.error("openpyxl is not installed.")
        raise ImportError("openpyxl is required to read Excel files on edge.")

    code_col_idx = None
    max_col_to_read = 12
    if code_column:
        try:
            code_col_idx = ord(code_column.strip().upper()) - ord('A')
            max_col_to_read = max(12, code_col_idx + 1)
        except Exception:
            pass

    wb = openpyxl.load_workbook(str(file_path), read_only=True, data_only=True)
    sheet = wb.active

    extracted_rows = []

    try:
        for row_idx, row in enumerate(
            sheet.iter_rows(min_row=start_row, max_row=698, min_col=1, max_col=max_col_to_read),
            start=start_row
        ):
            # Break if A-I are all empty
            if all(cell.value is None for cell in row[0:9]):
                break

            starting_price_val = None
            selling_price_val = None
            found_category_col = None
            is_unprocessable = False

            # Starting price (Column A, index 0)
            try:
                val_a = row[0].value
                if val_a is not None:
                    starting_price_val = float(val_a)
            except (ValueError, TypeError):
                is_unprocessable = True

            # Selling price (Columns B-I, index 1-8)
            for col_idx in range(1, 9):
                cell = row[col_idx]
                if cell.value is not None:
                    found_category_col = col_idx - 1
                    try:
                        selling_price_val = float(cell.value)
                    except (ValueError, TypeError):
                        is_unprocessable = True
                    break

            # is_exchange (Column L, index 11)
            is_exchange = False
            if len(row) > 11:
                val_l = row[11].value
                if val_l is not None and str(val_l).strip().upper() == "X":
                    is_exchange = True

            # raw_article_code
            raw_article_code = None
            if code_col_idx is not None and code_col_idx < len(row):
                val_code = row[code_col_idx].value
                if val_code is not None:
                    raw_article_code = str(val_code).strip()

            extracted_rows.append({
                "excel_row_index": row_idx,
                "excel_file_column": found_category_col,
                "starting_price": starting_price_val,
                "selling_price": selling_price_val,
                "is_exchange": is_exchange,
                "raw_article_code": raw_article_code,
                "is_unprocessable": is_unprocessable
            })
    finally:
        wb.close()

    return extracted_rows


def push_batch_to_ltdb(
    base_url: str,
    push_endpoint: str,
    target_date: date,
    rows: List[Dict[str, Any]],
    timeout: int,
    logger: logging.Logger
) -> Optional[List[int]]:
    """
    Sends rows (or heartbeat if empty) to LTDB cloud API.
    Returns confirmed_row_indexes if 200 OK.
    Returns None if error occurred.
    """
    url = f"{base_url.rstrip('/')}{push_endpoint}"
    payload = {
        "date": target_date.isoformat(),
        "rows": rows
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "LTDB-Edge-Forwarder/1.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                resp_data = json.loads(response.read().decode("utf-8"))
                confirmed = resp_data.get("confirmed_row_indexes", [])
                logger.info(
                    f"Successfully pushed batch to LTDB ({len(rows)} sent, {len(confirmed)} confirmed by cloud)"
                )
                return confirmed
    except urllib.error.HTTPError as http_err:
        if http_err.code == 409:
            logger.warning(
                f"Batch rejected by LTDB (Level 3 Historical file detected): {http_err.reason}"
            )
        else:
            logger.error(f"HTTP error pushing batch to LTDB: {http_err.code} {http_err.reason}")
    except Exception as exc:
        logger.error(f"Connection error pushing batch to LTDB ({url}): {exc}")

    return None


def run_cycle(
    base_dir: Path,
    config: Dict[str, Any],
    local_config: Dict[str, Any],
    logger: logging.Logger,
    last_push_time: float
) -> float:
    """Executes a single polling iteration. Returns updated last_push_time."""
    today = date.today()
    excel_path_str = config.get("excel_file_path", "")
    if not excel_path_str:
        logger.debug("No excel_file_path configured.")
        return last_push_time

    excel_path = Path(excel_path_str)
    if not is_file_eligible_for_today(excel_path, logger):
        return last_push_time

    start_row = int(config.get("start_row", 2))
    code_col = str(config.get("excel_code_column", "N"))
    base_url = local_config.get("ltdb_base_url", "http://127.0.0.1:8000")
    push_endpoint = local_config.get("push_endpoint", "/api/v1/excel-synchronization/push-batch")
    timeout = local_config.get("http_timeout_seconds", 10)
    heartbeat_interval = local_config.get("heartbeat_interval_seconds", 180)
    state_dir = base_dir / "state"

    try:
        current_rows = read_excel_rows(excel_path, start_row=start_row, code_column=code_col, logger=logger)
    except Exception as exc:
        logger.error(f"Failed to read Excel file at {excel_path}: {exc}")
        return last_push_time

    saved_hashes = load_daily_state(state_dir, today)

    delta_rows = []
    computed_hashes_by_row = {}

    for row in current_rows:
        row_idx_str = str(row["excel_row_index"])
        h = compute_row_hash(row)
        computed_hashes_by_row[row_idx_str] = h

        if row_idx_str not in saved_hashes or saved_hashes[row_idx_str] != h:
            delta_rows.append(row)

    now = time.time()

    if delta_rows:
        logger.info(f"Detected {len(delta_rows)} modified/new row(s). Pushing delta to LTDB...")
        confirmed_indexes = push_batch_to_ltdb(
            base_url=base_url,
            push_endpoint=push_endpoint,
            target_date=today,
            rows=delta_rows,
            timeout=timeout,
            logger=logger
        )

        if confirmed_indexes is not None:
            # Update state only for confirmed rows
            for idx in confirmed_indexes:
                idx_str = str(idx)
                if idx_str in computed_hashes_by_row:
                    saved_hashes[idx_str] = computed_hashes_by_row[idx_str]
            save_daily_state(state_dir, today, saved_hashes)
            return now
    else:
        # Check heartbeat if idle for > heartbeat_interval
        if (now - last_push_time) >= heartbeat_interval:
            logger.debug("Idle threshold reached. Sending heartbeat to LTDB...")
            push_batch_to_ltdb(
                base_url=base_url,
                push_endpoint=push_endpoint,
                target_date=today,
                rows=[],
                timeout=timeout,
                logger=logger
            )
            return now

    return last_push_time


def main():
    base_dir = Path(__file__).resolve().parent
    logger = setup_logging(base_dir)
    logger.info("Starting LTDB Edge Forwarder daemon (x86 Python 3.12)...")

    local_config = load_local_config(base_dir)
    runtime_config = fetch_remote_config(base_dir, local_config, logger)

    retention_days = int(runtime_config.get("retention_days", 30))
    cleanup_old_files(base_dir, retention_days, logger)

    polling_interval = int(runtime_config.get("excel_polling_interval", 30))
    last_push_time = time.time()
    last_cleaned_date = date.today()

    logger.info(f"Entering polling loop. Polling interval: {polling_interval}s, Retention: {retention_days} days.")

    while True:
        try:
            today = date.today()
            if today != last_cleaned_date:
                cleanup_old_files(base_dir, retention_days, logger)
                last_cleaned_date = today

            last_push_time = run_cycle(base_dir, runtime_config, local_config, logger, last_push_time)
        except Exception as exc:
            logger.error(f"Unexpected error in polling loop: {exc}")

        time.sleep(polling_interval)


if __name__ == "__main__":
    main()
