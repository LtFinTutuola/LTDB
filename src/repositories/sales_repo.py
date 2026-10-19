from datetime import datetime
from typing import List, Optional
from sqlalchemy.orm import Session, joinedload
from src.models.sales import Sale, SaleLine, SalesChatSession, SalesChatMessage
from src.schemas.sales import SaleCreate
from src.repositories.base import BaseRepository


class SaleRepository(BaseRepository[Sale, SaleCreate, SaleCreate]):
    def __init__(self):
        super().__init__(Sale)

    def create_transaction(self, db: Session, sale_in: SaleCreate) -> Sale:
        """
        Persists a transactional sale using a session rollback block.
        """
        try:
            # Create master sale record
            db_sale = Sale(
                seller_id=sale_in.seller_id,
                total_paid=sale_in.total_paid,
                payment_method=sale_in.payment_method
            )
            db.add(db_sale)
            db.flush() # get id

            # Create sale lines
            for line_in in sale_in.lines:
                db_line = SaleLine(
                    sale_id=db_sale.id,
                    article_id=line_in.article_id,
                    quantity=line_in.quantity,
                    unit_price=line_in.unit_price
                )
                db.add(db_line)
            
            db.commit()
            db.refresh(db_sale)
            return db_sale
        except Exception as e:
            db.rollback()
            raise e


class SalesChatRepository:
    """Repository for persisting and retrieving SalesChatSession and SalesChatMessage records."""

    def create_session(self, db: Session, title: str, is_active: bool = True) -> SalesChatSession:
        clean_title = (title or "Nuova conversazione").strip()
        if len(clean_title) > 80:
            clean_title = clean_title[:77] + "..."
        if is_active:
            db.query(SalesChatSession).update({SalesChatSession.is_active: False}, synchronize_session=False)
        session_obj = SalesChatSession(title=clean_title, is_active=is_active)
        db.add(session_obj)
        db.commit()
        db.refresh(session_obj)
        return session_obj

    def set_active_session(self, db: Session, session_id: Optional[str]) -> Optional[SalesChatSession]:
        """Mark session_id as the single active session, or deactivate all sessions if session_id is None."""
        if session_id:
            db.query(SalesChatSession).filter(SalesChatSession.id != session_id).update(
                {SalesChatSession.is_active: False}, synchronize_session=False
            )
            target = db.query(SalesChatSession).filter(SalesChatSession.id == session_id).first()
            if target:
                target.is_active = True
            db.commit()
            return target
        else:
            db.query(SalesChatSession).update(
                {SalesChatSession.is_active: False}, synchronize_session=False
            )
            db.commit()
            return None

    def get_active_session(
        self,
        db: Session,
        target_date: Optional[str] = None,
    ) -> Optional[SalesChatSession]:
        """
        Return the active chat session for target_date (defaulting to today UTC).
        A session is valid if its most recent message was generated on target_date.
        If multiple sessions exist on target_date, the latest updated/active one is selected.
        """
        if not target_date:
            target_date = datetime.utcnow().strftime("%Y-%m-%d")

        sessions = (
            db.query(SalesChatSession)
            .options(joinedload(SalesChatSession.messages))
            .order_by(SalesChatSession.updated_at.desc(), SalesChatSession.created_at.desc())
            .all()
        )

        def _is_message_on_date(m: SalesChatMessage) -> bool:
            if not m or not m.created_at:
                return False
            return m.created_at.strftime("%Y-%m-%d") == target_date

        # 1. Prefer an explicitly active session whose last message was generated on target_date
        for s in sessions:
            if s.is_active and s.messages:
                last_msg = s.messages[-1]
                if _is_message_on_date(last_msg):
                    return s

        # 2. Otherwise pick the most recently updated session whose last message was generated on target_date
        for s in sessions:
            if s.messages:
                last_msg = s.messages[-1]
                if _is_message_on_date(last_msg):
                    self.set_active_session(db, str(s.id))
                    return s

        return None

    def get_session_with_messages(self, db: Session, session_id: str) -> Optional[SalesChatSession]:
        return (
            db.query(SalesChatSession)
            .options(joinedload(SalesChatSession.messages))
            .filter(SalesChatSession.id == session_id)
            .first()
        )

    def list_sessions(self, db: Session, limit: int = 50) -> List[SalesChatSession]:
        return (
            db.query(SalesChatSession)
            .order_by(SalesChatSession.updated_at.desc(), SalesChatSession.created_at.desc())
            .limit(limit)
            .all()
        )

    def add_message(
        self,
        db: Session,
        session_id: str,
        role: str,
        content: str,
        context_date: Optional[str] = None,
        query_plan: Optional[dict] = None,
    ) -> SalesChatMessage:
        msg = SalesChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            context_date=context_date,
            query_plan=query_plan,
        )
        db.add(msg)
        session_obj = db.query(SalesChatSession).filter(SalesChatSession.id == session_id).first()
        if session_obj:
            session_obj.updated_at = datetime.utcnow()
            session_obj.is_active = True
            db.query(SalesChatSession).filter(SalesChatSession.id != session_id).update(
                {SalesChatSession.is_active: False}, synchronize_session=False
            )
        db.commit()
        db.refresh(msg)
        return msg

    def report_message(
        self,
        db: Session,
        message_id: str,
        reason: str,
    ) -> Optional[SalesChatMessage]:
        msg = db.query(SalesChatMessage).filter(SalesChatMessage.id == message_id).first()
        if not msg:
            return None
        msg.is_reported = True
        msg.report_reason = reason
        db.commit()
        db.refresh(msg)
        return msg


sales_repo = SaleRepository()
sales_chat_repo = SalesChatRepository()


