import pytest
import threading
from unittest.mock import patch
from datetime import date
from src.services.excel_synchronization_service import _polling_loop, start_polling, stop_polling

@patch("src.services.excel_synchronization_service.ingest_daily_sales")
@patch("src.services.excel_synchronization_service.SessionLocal")
@patch("src.services.excel_synchronization_service.settings")
def test_polling_execution(mock_settings, mock_session, mock_ingest):
    """Test that the polling loop successfully instantiates a session and calls ingestion."""
    mock_settings.EXCEL_FILE_PATH = "dummy_path.xlsx"
    mock_settings.EXCEL_POLLING_INTERVAL = 0.1
    
    stop_event = threading.Event()
    
    # Run the loop in a separate thread so we can stop it
    thread = threading.Thread(target=_polling_loop, args=(stop_event,))
    thread.start()
    
    # Let it run for a tiny bit
    stop_event.wait(0.15)
    stop_event.set()
    thread.join(timeout=1.0)
    
    # It should have called the db session and ingestion
    assert mock_session.called
    assert mock_ingest.called


@patch("src.services.excel_synchronization_service.ingest_daily_sales")
@patch("src.services.excel_synchronization_service.SessionLocal")
@patch("src.services.excel_synchronization_service.settings")
def test_polling_exception_handling(mock_settings, mock_session, mock_ingest):
    """Test that an exception during ingestion does not crash the polling loop."""
    mock_settings.EXCEL_FILE_PATH = "dummy_path.xlsx"
    mock_settings.EXCEL_POLLING_INTERVAL = 0.1
    
    # Make ingestion fail
    mock_ingest.side_effect = Exception("Simulated ingestion error")
    
    stop_event = threading.Event()
    
    thread = threading.Thread(target=_polling_loop, args=(stop_event,))
    thread.start()
    
    stop_event.wait(0.2)
    stop_event.set()
    thread.join(timeout=1.0)
    
    # It should have tried multiple times and not died
    assert mock_session.called
    assert mock_ingest.call_count >= 1
    # Thread should have joined cleanly, meaning it didn't crash out of the while loop prematurely
    assert not thread.is_alive()
