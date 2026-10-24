from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
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

from src.core.auth import get_current_user
from src.core.request_context import AuthUser

logger = get_logger()

router = APIRouter(prefix="/api/v1/sales", tags=["Sales"])


@router.post("/chat", response_model=ChatMessageResponse)
async def chat_with_gemini(
    request: ChatMessageRequest,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
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
            user_id=current_user.id,
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
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Returns the list of past sales chat sessions for the current user ordered by most recently updated.
    """
    try:
        return sales_chat_service.list_chat_sessions(db=db, limit=limit, user_id=current_user.id)
    except Exception as e:
        logger.log_execution("sales_router", "list_sales_chats", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chats/active", response_model=Optional[SalesChatSessionDetail])
def get_active_sales_chat(
    target_date: Optional[str] = Query(None, description="Data nel formato YYYY-MM-DD"),
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Returns the active sales chat session for target_date (defaulting to today UTC) for the current user,
    or null if no session has its last message generated on target_date.
    """
    try:
        return sales_chat_service.get_active_chat_session(db=db, target_date=target_date, user_id=current_user.id)
    except Exception as e:
        logger.log_execution("sales_router", "get_active_sales_chat", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/chats/{session_id}/activate", response_model=SalesChatSessionDetail)
def activate_sales_chat(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Explicitly marks a sales chat session as active and returns its details.
    """
    try:
        return sales_chat_service.activate_chat_session(db=db, session_id=session_id, user_id=current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.log_execution("sales_router", "activate_sales_chat", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chats/deactivate")
def deactivate_sales_chats(
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Deactivates all sales chat sessions for current user (e.g. when the user clicks 'Nuova chat').
    """
    try:
        sales_chat_service.deactivate_chat_sessions(db=db, user_id=current_user.id)
        return {"status": "ok"}
    except Exception as e:
        logger.log_execution("sales_router", "deactivate_sales_chats", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chats/{session_id}", response_model=SalesChatSessionDetail)
def get_sales_chat_detail(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Returns a specific sales chat session with all its chronological messages so it can be reloaded and continued.
    """
    try:
        return sales_chat_service.get_chat_session_detail(db=db, session_id=session_id, user_id=current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.log_execution("sales_router", "get_sales_chat_detail", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/report", response_model=ReportMessageResponse)
def report_sales_chat_message(
    request: ReportMessageRequest,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Flag an AI sales chat message as reported with a user reason.
    """
    try:
        return sales_chat_service.report_chat_message(
            db=db,
            message_id=request.message_id,
            reason=request.reason,
            user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e).strip("'"))
    except Exception as e:
        logger.log_execution("sales_router", "report_sales_chat_message", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


