import threading
from datetime import date
from src.core.database import SessionLocal
from src.services.excel_ingestion_service import ingest_daily_sales
from src.core.config import settings
from src.core.logger import get_logger

logger = get_logger()

_polling_thread = None
_stop_event = threading.Event()

def _polling_loop(stop_event: threading.Event):
    logger.log_execution("excel_polling_service", "startup", "ok", message=f"Starting Excel polling loop. Interval: {settings.EXCEL_POLLING_INTERVAL}s")
    
    while not stop_event.is_set():
        # Only poll if we have a file path
        if settings.EXCEL_FILE_PATH:
            db = SessionLocal()
            try:
                # We ingest sales for today
                target_date = date.today()
                inserted_ids = ingest_daily_sales(db, settings.EXCEL_FILE_PATH, target_date)
                if inserted_ids:
                    logger.log_execution("excel_polling_service", "ingested", "ok", message=f"Polled Excel and ingested {len(inserted_ids)} new sales rows.")
            except Exception as e:
                # Catch all to prevent thread from dying
                logger.log_execution("excel_polling_service", "error", "error", exc=e)
            finally:
                db.close()
        
        # Wait for the interval, but allow immediate interruption if stop_event is set
        stop_event.wait(timeout=settings.EXCEL_POLLING_INTERVAL)
        
    logger.log_execution("excel_polling_service", "shutdown", "ok", message="Excel polling loop stopped.")

def start_polling():
    global _polling_thread
    if _polling_thread is not None and _polling_thread.is_alive():
        logger.log_execution("excel_polling_service", "start_polling", "warning", message="Polling thread is already running.")
        return
        
    _stop_event.clear()
    _polling_thread = threading.Thread(target=_polling_loop, args=(_stop_event,), daemon=True)
    _polling_thread.start()

def stop_polling():
    global _polling_thread
    if _polling_thread is not None:
        _stop_event.set()
        _polling_thread.join(timeout=5.0)
        _polling_thread = None
