from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from src.core.database import get_db
from src.schemas.sales import (
    ChatMessageRequest,
    ChatMessageResponse,
    ReportMessageRequest,
    ReportMessageResponse,
    SalesChatSessionSummary,
    SalesChatSessionDetail,
)
from src.services import sales_chat_service
from src.core.logger import get_logger

logger = get_logger()

router = APIRouter(prefix="/api/v1/sales", tags=["Sales"])


@router.post("/chat", response_model=ChatMessageResponse)
async def chat_with_gemini(
    request: ChatMessageRequest,
    db: Session = Depends(get_db),
):
    """
    Conversational hybrid RAG endpoint for the sales page.
    Creates a new chat session or reloads/continues an existing session_id.
    """
    try:
        result = await sales_chat_service.process_chat_message(
            db=db,
            message=request.message,
            context_date=request.context_date,
            session_id=request.session_id,
        )
        return ChatMessageResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.log_execution("sales_router", "chat_with_gemini", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chats", response_model=List[SalesChatSessionSummary])
def list_sales_chats(
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """
    Returns the list of past sales chat sessions ordered by most recently updated.
    """
    try:
        return sales_chat_service.list_chat_sessions(db=db, limit=limit)
    except Exception as e:
        logger.log_execution("sales_router", "list_sales_chats", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chats/{session_id}", response_model=SalesChatSessionDetail)
def get_sales_chat_detail(
    session_id: str,
    db: Session = Depends(get_db),
):
    """
    Returns a specific sales chat session with all its chronological messages so it can be reloaded and continued.
    """
    try:
        return sales_chat_service.get_chat_session_detail(db=db, session_id=session_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.log_execution("sales_router", "get_sales_chat_detail", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/report", response_model=ReportMessageResponse)
def report_sales_chat_message(
    request: ReportMessageRequest,
    db: Session = Depends(get_db),
):
    """
    Flag an AI sales chat message as reported with a user reason.
    """
    try:
        return sales_chat_service.report_chat_message(
            db=db,
            message_id=request.message_id,
            reason=request.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e).strip("'"))
    except Exception as e:
        logger.log_execution("sales_router", "report_sales_chat_message", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


