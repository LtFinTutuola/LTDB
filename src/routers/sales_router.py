from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from src.core.database import get_db
from src.schemas.sales import ChatMessageRequest, ChatMessageResponse
from src.core.logger import get_logger

logger = get_logger()

router = APIRouter(prefix="/api/v1/sales", tags=["Sales"])

@router.post("/chat", response_model=ChatMessageResponse)
def chat_with_gemini(
    request: ChatMessageRequest,
    db: Session = Depends(get_db)
):
    """
    Mock endpoint for chatting with Gemini.
    """
    try:
        # Mock logic
        query = request.message.lower()
        if "vendite" in query:
            reply = f"Mock: Ho controllato le vendite per la data {request.context_date}. Sembra tutto a posto!"
        elif "problemi" in query:
            reply = "Mock: Non ho rilevato alcun problema particolare nelle vendite di oggi."
        else:
            reply = f"Mock: Ricevuto il tuo messaggio '{request.message}'. (Risposta mock da Gemini)"
            
        return ChatMessageResponse(reply=reply)
    except Exception as e:
        logger.log_execution("sales_router", "chat_with_gemini", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))
