"""
src/services/sales_chat_service.py
----------------------------------
Service layer for the conversational sales hybrid RAG agent.
Manages SQLite chat session persistence, reloads prior messages when continuing
an existing conversation, and orchestrates SalesConversationalAgent.
"""
from datetime import date
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session

from src.repositories.sales_repo import sales_chat_repo
from src.repositories.pim_repo import pim_repo
from src.services.excel_synchronization_service import load_columns_schema
from src.agents.sales_chat_agent import SalesConversationalAgent
from src.models.sales import SalesChatSession
from src.core.logger import get_logger

logger = get_logger()


async def process_chat_message(
    db: Session,
    message: str,
    context_date: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Process a user message in the sales conversational chat:
      1. Load existing session & previous messages if `session_id` is provided, or create a new session.
      2. Persist the user's message in `sales_chat_messages`.
      3. Run `SalesConversationalAgent` with the prior `chat_history`, `columns.yaml`, and PIM taxonomy.
      4. Persist the assistant's reply and structured `query_plan` in `sales_chat_messages`.
    """
    clean_message = (message or "").strip()
    if not clean_message:
        raise ValueError("Il messaggio non può essere vuoto.")

    target_date_str = (context_date or "").strip() or date.today().isoformat()

    session_obj: Optional[SalesChatSession] = None
    chat_history: List[Dict[str, str]] = []

    if session_id and session_id.strip():
        session_obj = sales_chat_repo.get_session_with_messages(db, session_id.strip())
        if session_obj:
            chat_history = [
                {"role": m.role, "content": m.content}
                for m in (session_obj.messages or [])
            ]

    if not session_obj:
        session_obj = sales_chat_repo.create_session(db, title=clean_message)

    # Persist incoming user message
    sales_chat_repo.add_message(
        db=db,
        session_id=str(session_obj.id),
        role="user",
        content=clean_message,
        context_date=target_date_str,
    )

    columns_schema = load_columns_schema()
    taxonomy = pim_repo.get_catalog_taxonomy_for_search(db)

    agent = SalesConversationalAgent(db=db)
    agent_output = await agent.aexecute(
        {
            "query": clean_message,
            "context_date": target_date_str,
            "chat_history": chat_history,
            "columns_schema": columns_schema,
            "brands_catalog": taxonomy["brands"],
            "categories_catalog": taxonomy["categories"],
        }
    )

    reply = agent_output.get("reply", "")
    query_plan = agent_output.get("query_plan")

    # Persist assistant response + query_plan telemetry
    assistant_msg = sales_chat_repo.add_message(
        db=db,
        session_id=str(session_obj.id),
        role="assistant",
        content=reply,
        context_date=target_date_str,
        query_plan=query_plan,
    )

    logger.log_execution(
        "sales_chat_service",
        "process_chat_message",
        "ok",
        session_id=str(session_obj.id),
        context_date=target_date_str,
        prior_turns=len(chat_history),
    )

    return {
        "reply": reply,
        "session_id": str(session_obj.id),
        "session_title": session_obj.title,
        "message_id": str(assistant_msg.id),
    }


def list_chat_sessions(db: Session, limit: int = 50) -> List[SalesChatSession]:
    """Return past chat sessions ordered from most recent to oldest."""
    return sales_chat_repo.list_sessions(db, limit=limit)


def get_chat_session_detail(db: Session, session_id: str) -> SalesChatSession:
    """Return a single chat session with all its chronological messages."""
    session_obj = sales_chat_repo.get_session_with_messages(db, session_id)
    if not session_obj:
        raise ValueError(f"Chat session '{session_id}' not found.")
    return session_obj


def report_chat_message(db: Session, message_id: str, reason: str) -> Dict[str, Any]:
    """Flag a chat message as reported with a user explanation."""
    clean_id = (message_id or "").strip()
    clean_reason = (reason or "").strip()
    if not clean_id:
        raise ValueError("L'ID del messaggio non può essere vuoto.")
    if not clean_reason:
        raise ValueError("Il motivo della segnalazione non può essere vuoto.")

    updated_msg = sales_chat_repo.report_message(db=db, message_id=clean_id, reason=clean_reason)
    if not updated_msg:
        raise KeyError(f"Messaggio '{clean_id}' non trovato.")

    logger.log_execution(
        "sales_chat_service",
        "report_chat_message",
        "ok",
        message_id=clean_id,
        reason=clean_reason,
    )

    return {
        "status": "ok",
        "message_id": str(updated_msg.id),
        "is_reported": updated_msg.is_reported,
        "report_reason": updated_msg.report_reason,
    }

