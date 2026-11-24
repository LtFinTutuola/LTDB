import threading
from typing import List, Dict, Any, Optional, Tuple
from datetime import date
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.logger import get_logger
from src.core.database import SessionLocal
from src.repositories.excel_synchronization_repo import excel_synchronization_repo
from src.repositories.wms_repo import wms_repo
from src.schemas.excel_sales import (
    ExcelSaleCreate,
    ExcelSaleStatus,
    ExcelRowPushItem,
    ExcelRowUpdateItem,
)
from src.schemas.wms import StockUpdate
from src.models.wms import ArticleStatus, MovementReason
from src.services.search_service import invalidate_search_cache

try:
    import openpyxl
except ImportError:
    openpyxl = None

logger = get_logger()


class HistoricalFileError(Exception):
    """Raised when incoming sales data matches a historical date pattern."""
    def __init__(self, message: str, matched_date: Optional[date] = None):
        super().__init__(message)
        self.matched_date = matched_date


def get_agent_config() -> Dict[str, Any]:
    """Reads current configuration for the standalone edge agent."""
    import yaml
    from pathlib import Path
    from src.core.config import BASE_DIR

    config_path = BASE_DIR / "src" / "config.yaml"
    cfg = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    return {
        "excel_file_path": str(cfg.get("excel_file_path", settings.EXCEL_FILE_PATH)),
        "excel_code_column": str(cfg.get("excel_code_column", settings.EXCEL_CODE_COLUMN)),
        "start_row": int(cfg.get("excel_start_row", getattr(settings, "EXCEL_START_ROW", 2))),
        "excel_polling_interval": int(cfg.get("excel_polling_interval", settings.EXCEL_POLLING_INTERVAL)),
        "retention_days": int(cfg.get("excel_retention_days", getattr(settings, "EXCEL_RETENTION_DAYS", 30))),
    }


# ---------------------------------------------------------------------------
# Phase 4 Part 1: Async Row Updates Queue & 60s Post-Purchase Debounce Manager
# ---------------------------------------------------------------------------

_async_updates_lock = threading.Lock()
_async_row_updates_queue: List[ExcelRowUpdateItem] = []

_debounce_lock = threading.Lock()
_debounce_timers: Dict[str, threading.Timer] = {}
_debounce_versions: Dict[str, int] = {}
_debounce_session_factory = SessionLocal


def enqueue_async_row_updates(updates: List[ExcelRowUpdateItem]) -> None:
    """Queues asynchronous Excel row updates (e.g., Col P after WhatsApp onboarding completion)."""
    if not updates:
        return
    with _async_updates_lock:
        _async_row_updates_queue.extend(updates)


def drain_async_row_updates() -> List[ExcelRowUpdateItem]:
    """Atomically drains and returns all queued asynchronous Excel row updates."""
    with _async_updates_lock:
        items = list(_async_row_updates_queue)
        _async_row_updates_queue.clear()
        return items


def clear_async_row_updates() -> None:
    """Clears the asynchronous Excel row updates queue (useful for test cleanup)."""
    with _async_updates_lock:
        _async_row_updates_queue.clear()


# ---------------------------------------------------------------------------
# Phase 4 Macro-Area 4: Remote Agent Commands Queue & Diagnostic Status
# ---------------------------------------------------------------------------

_agent_commands_lock = threading.Lock()
_agent_commands_queue: List[Dict[str, Any]] = []

_agent_status_lock = threading.Lock()
_latest_agent_status: Dict[str, Any] = {
    "configured_file": None,
    "monitored_file": None,
    "file_readable": False,
    "last_sync_at": None,
    "reported_at": None,
}


def enqueue_agent_command(command: Dict[str, Any]) -> None:
    """Queues a remote command (`update_excel_path`, `force_sync`, `update_config`) for the Edge Agent."""
    if not command or not isinstance(command, dict):
        return
    with _agent_commands_lock:
        _agent_commands_queue.append(dict(command))


def drain_agent_commands() -> List[Dict[str, Any]]:
    """Atomically drains and returns all queued remote commands for the Edge Agent."""
    with _agent_commands_lock:
        items = list(_agent_commands_queue)
        _agent_commands_queue.clear()
        return items


def clear_agent_commands() -> None:
    """Clears the pending Edge Agent commands queue (useful for test cleanup)."""
    with _agent_commands_lock:
        _agent_commands_queue.clear()


def get_pending_agent_commands_count() -> int:
    """Returns the number of pending commands currently waiting for the Edge Agent."""
    with _agent_commands_lock:
        return len(_agent_commands_queue)


def record_agent_status(agent_status: Optional[Dict[str, Any]]) -> None:
    """Records the latest diagnostic status reported by the standalone Edge Agent."""
    if not agent_status or not isinstance(agent_status, dict):
        return
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    with _agent_status_lock:
        _latest_agent_status.update({
            "configured_file": agent_status.get("configured_file"),
            "monitored_file": agent_status.get("monitored_file"),
            "file_readable": bool(agent_status.get("file_readable", False)),
            "last_sync_at": agent_status.get("last_sync_at"),
            "reported_at": now_iso,
        })


def reset_agent_status() -> None:
    """Resets the cached Edge Agent diagnostic status (useful for test cleanup)."""
    with _agent_status_lock:
        _latest_agent_status.update({
            "configured_file": None,
            "monitored_file": None,
            "file_readable": False,
            "last_sync_at": None,
            "reported_at": None,
        })


def _format_customer_short_name(customer: Any) -> str:
    """Formats customer display name for Excel Col P as '<Initial>. <LastName>' (e.g. 'M. Rossi')."""
    first = (getattr(customer, "first_name", None) or "").strip()
    last = (getattr(customer, "last_name", None) or "").strip()
    if first and last:
        return f"{first[0].upper()}. {last}"
    if last:
        return last
    if first:
        return first
    return getattr(customer, "phone_number", None) or "Cliente"


def execute_post_purchase_notification_now(
    transaction_id: str,
    expected_version: Optional[int] = None,
    db: Optional[Session] = None,
) -> Optional[Dict[str, Any]]:
    """
    Executes the post-purchase WhatsApp summary dispatch for a confirmed transaction.
    Verifies under lock that no newer debounce version has superseded this trigger,
    and verifies on DB that the transaction is still IDENTIFICATION_CONFIRMED.
    """
    from src.models.crm import CustomerIdentificationStatus, CustomerStatus, Transaction
    from src.repositories.crm_repo import crm_customer_repo
    from src.services.loyalty_service import get_top_redeemable_tiers
    from src.services.whatsapp_service import get_whatsapp_provider

    if expected_version is not None:
        with _debounce_lock:
            if _debounce_versions.get(transaction_id) != expected_version:
                return None
            _debounce_timers.pop(transaction_id, None)

    owns_session = db is None
    session = db if db is not None else _debounce_session_factory()
    try:
        tx = session.query(Transaction).filter(Transaction.id == transaction_id).first()
        if not tx or not tx.customer_id:
            return None
        if tx.identification_status != CustomerIdentificationStatus.IDENTIFICATION_CONFIRMED:
            return None

        customer = crm_customer_repo.get(session, tx.customer_id)
        if not customer or not customer.phone_number:
            return None
        if customer.status not in (CustomerStatus.ACTIVE, CustomerStatus.INACTIVE_EXPIRED):
            return None

        points_balance = crm_customer_repo.get_points_balance(session, customer.id)
        top_tiers = get_top_redeemable_tiers(points_balance, max_tiers=3)
        quick_replies = [
            {
                "id": f"REDEEM_TIER:{tier['code']}",
                "code": str(tier["code"]),
                "title": str(tier.get("label", tier["code"])),
                "points_required": int(tier["points_required"]),
            }
            for tier in top_tiers
        ]

        provider = get_whatsapp_provider()
        delivery = provider.send_post_purchase_summary(
            phone_number=customer.phone_number,
            customer_name=_format_customer_short_name(customer),
            points_balance=points_balance,
            transaction_id=str(tx.id),
            quick_replies=quick_replies,
        )
        logger.log_execution(
            "excel_synchronization_service",
            "post_purchase_whatsapp_sent",
            "ok",
            transaction_id=str(tx.id),
            customer_id=str(customer.id),
            points_balance=points_balance,
            quick_replies_count=len(quick_replies),
        )
        return delivery
    except Exception as exc:
        logger.log_execution(
            "excel_synchronization_service",
            "post_purchase_whatsapp_error",
            "err",
            transaction_id=transaction_id,
            exc=exc,
        )
        return None
    finally:
        if owns_session:
            session.close()


def schedule_post_purchase_notification(
    transaction_id: str,
    delay_seconds: Optional[float] = None,
) -> int:
    """
    Starts or resets the 60-second debounce timer for a confirmed transaction.
    Returns the new monotonic debounce version for `transaction_id`.
    """
    delay = (
        float(delay_seconds)
        if delay_seconds is not None
        else float(settings.LOYALTY_POST_PURCHASE_DEBOUNCE_SECONDS)
    )
    with _debounce_lock:
        existing_timer = _debounce_timers.pop(transaction_id, None)
        if existing_timer is not None:
            existing_timer.cancel()

        version = _debounce_versions.get(transaction_id, 0) + 1
        _debounce_versions[transaction_id] = version

        timer = threading.Timer(
            delay,
            execute_post_purchase_notification_now,
            kwargs={"transaction_id": transaction_id, "expected_version": version},
        )
        timer.daemon = True
        _debounce_timers[transaction_id] = timer
        timer.start()
        return version


def cancel_post_purchase_timer(transaction_id: str) -> None:
    """Cancels and removes a single pending post-purchase debounce timer if present."""
    with _debounce_lock:
        existing_timer = _debounce_timers.pop(transaction_id, None)
        if existing_timer is not None:
            existing_timer.cancel()
        _debounce_versions.pop(transaction_id, None)


def cancel_all_post_purchase_timers() -> None:
    """Cancels and clears all pending post-purchase debounce timers (useful for shutdown/tests)."""
    with _debounce_lock:
        for timer in _debounce_timers.values():
            timer.cancel()
        _debounce_timers.clear()
        _debounce_versions.clear()


def _recalculate_or_cleanup_detached_transaction(db: Session, old_tx_id: str) -> None:
    """
    Reconciles or removes an unconfirmed/provisional transaction after one of its ExcelSale
    rows has been detached due to a pre-confirmation phone number correction in Col O.
    """
    from src.models.crm import (
        CustomerIdentificationStatus,
        LoyaltyPointLedger,
        Transaction,
    )
    from src.services.loyalty_service import _apply_loyalty_engine

    old_tx = db.query(Transaction).filter(Transaction.id == old_tx_id).first()
    if not old_tx:
        return

    db.flush()
    db.refresh(old_tx)
    remaining_sales = [s for s in old_tx.excel_sales if s.transaction_id == old_tx.id]

    if not remaining_sales:
        for entry in list(old_tx.loyalty_ledger_entries):
            db.delete(entry)
        old_tx.customer = None
        db.flush()
        cancel_post_purchase_timer(str(old_tx.id))
        db.delete(old_tx)
        db.flush()
    elif old_tx.identification_status == CustomerIdentificationStatus.IDENTIFICATION_CONFIRMED:
        _apply_loyalty_engine(
            db=db,
            transaction=old_tx,
            target_discount_rule_code=old_tx.discount_rule_code,
            previous_customer_id=None,
        )
        db.flush()


def _process_crm_for_pushed_rows(
    db: Session,
    rows: List[ExcelRowPushItem],
    existing_by_row: Dict[int, Any],
    target_date: date,
    price_changed_row_indexes: Optional[set[int]] = None,
) -> Tuple[List[ExcelRowUpdateItem], List[str]]:
    """
    Processes Phase 4 Macro-Area 1 & Macro-Area 2 CRM/Loyalty logic for pushed Excel rows:
    - Enforces post-confirmation customer immutability (`IDENTIFICATION_CONFIRMED` on registered
      customers): ignores unauthorized Col O changes/deletions and emits `col_o_phone` to restore it.
    - Allows pre-confirmation Col O corrections (when `IDENTIFICATION_PENDING` or `PENDING_ONBOARDING`),
      detaching the row and cleaning up/recalculating provisional transaction points.
    - Groups contiguous rows sharing the same normalized phone number (Col O) into a single Transaction,
      automatically merging split receipts (`Ctrl+S` partial saves) and bridging contiguous blocks.
    - Restores pre-loyalty prices (`restore_pre_loyalty_prices`) BEFORE attaching newly added split-receipt
      rows so existing OTP discounts are re-spread accurately across all purchase rows without duplicate
      `DISCOUNT_REDEMPTION` or `PURCHASE_EARN` movements.
    - Preserves manual Excel price edits on already-confirmed rows (`preserve_manual_prices=True`),
      appending `PRICE_CORRECTION` for the algebraic point difference while keeping `DISCOUNT_REDEMPTION`
      immutable.
    Returns (row_updates, confirmed_tx_ids_to_notify).
    """
    from decimal import Decimal
    from src.models.crm import (
        Customer,
        CustomerIdentificationStatus,
        CustomerStatus,
        LoyaltyPointLedger,
        Transaction,
    )
    from src.repositories.crm_repo import crm_customer_repo
    from src.services import customer_onboarding_service
    from src.services.loyalty_service import (
        _apply_loyalty_engine,
        _to_cents,
        restore_pre_loyalty_prices,
    )

    changed_prices = price_changed_row_indexes or set()
    row_updates_by_idx: Dict[int, ExcelRowUpdateItem] = {}
    confirmed_tx_ids_to_notify: List[str] = []
    forced_col_o_restore: Dict[int, str] = {}

    # 1. Validate phone numbers, enforce post-confirmation lock, or handle pre-confirmation unlinking
    valid_crm_entries: List[Tuple[int, ExcelRowPushItem, Any, str]] = []
    sorted_items = sorted(rows, key=lambda r: r.excel_row_index)

    for item in sorted_items:
        sale = existing_by_row.get(item.excel_row_index)
        if not sale or sale.status == ExcelSaleStatus.UNPROCESSABLE:
            continue

        raw_phone = (item.phone_number or "").strip()
        parsed_phone: Optional[str] = None
        phone_invalid = False
        if raw_phone:
            try:
                parsed_phone = customer_onboarding_service.normalize_phone_number(raw_phone)
            except ValueError:
                phone_invalid = True

        old_tx: Optional[Transaction] = None
        old_customer: Optional[Customer] = None
        if sale.transaction_id:
            old_tx = (
                db.query(Transaction)
                .filter(Transaction.id == sale.transaction_id)
                .first()
            )
            if old_tx and old_tx.customer_id:
                old_customer = crm_customer_repo.get(db, old_tx.customer_id)

        is_locked_confirmed = (
            old_tx is not None
            and old_customer is not None
            and old_tx.identification_status == CustomerIdentificationStatus.IDENTIFICATION_CONFIRMED
            and old_customer.status != CustomerStatus.PENDING_ONBOARDING
        )

        if is_locked_confirmed and old_customer and old_customer.phone_number:
            locked_phone = old_customer.phone_number
            if not raw_phone or phone_invalid or parsed_phone != locked_phone:
                # Macro-Area 2.1.3: Post-confirmation lock — ignore Col O edit and force restore
                forced_col_o_restore[item.excel_row_index] = locked_phone
                parsed_phone = locked_phone
                phone_invalid = False
            sale.customer_phone = locked_phone
            db.add(sale)
            valid_crm_entries.append((item.excel_row_index, item, sale, locked_phone))
            continue

        # Row is not locked post-confirmation: check if it was previously linked to a provisional transaction
        if old_tx is not None:
            old_phone = old_customer.phone_number if old_customer else None
            if not raw_phone or phone_invalid or parsed_phone != old_phone:
                old_tx_id_str = str(old_tx.id)
                sale.transaction_id = None
                sale.customer_phone = None
                db.add(sale)
                db.flush()
                _recalculate_or_cleanup_detached_transaction(db, old_tx_id_str)

        if not raw_phone:
            if old_tx is not None:
                row_updates_by_idx[item.excel_row_index] = ExcelRowUpdateItem(
                    excel_row_index=item.excel_row_index,
                    col_p_status="",
                    identification_pending=False,
                )
            continue

        if phone_invalid or not parsed_phone:
            sale.customer_phone = None
            sale.transaction_id = None
            db.add(sale)
            row_updates_by_idx[item.excel_row_index] = ExcelRowUpdateItem(
                excel_row_index=item.excel_row_index,
                col_p_status="⚠️ Telefono non valido",
                identification_pending=False,
            )
            continue

        sale.customer_phone = parsed_phone
        db.add(sale)
        valid_crm_entries.append((item.excel_row_index, item, sale, parsed_phone))

    if not valid_crm_entries:
        return [row_updates_by_idx[k] for k in sorted(row_updates_by_idx.keys())], []

    # 2. Group contiguous rows sharing the same normalized phone number
    groups: List[Tuple[str, List[Tuple[int, ExcelRowPushItem, Any]]]] = []
    for row_idx, item, sale, norm_phone in valid_crm_entries:
        if (
            groups
            and groups[-1][0] == norm_phone
            and row_idx == groups[-1][1][-1][0] + 1
        ):
            groups[-1][1].append((row_idx, item, sale))
        else:
            groups.append((norm_phone, [(row_idx, item, sale)]))

    # 3. Deduplicated customer cache within this batch
    customer_cache: Dict[str, Customer] = {}

    def _find_matching_txs_for_customer(
        group_entries: List[Tuple[int, ExcelRowPushItem, Any]],
        customer_id: str,
    ) -> List[Transaction]:
        """
        Finds all existing Transactions on target_date belonging to `customer_id` that are
        linked either to rows inside `group_entries` or to immediately adjacent rows
        (`min_idx - 1` and `max_idx + 1`), ordered by lowest Excel row index / creation order.
        """
        found_txs: List[Transaction] = []
        seen_tx_ids: set[str] = set()

        min_idx = group_entries[0][0]
        max_idx = group_entries[-1][0]

        candidate_sales: List[Any] = []
        prev_sale = existing_by_row.get(min_idx - 1)
        if prev_sale is not None:
            candidate_sales.append(prev_sale)
        for _, _, sale_obj in group_entries:
            candidate_sales.append(sale_obj)
        next_sale = existing_by_row.get(max_idx + 1)
        if next_sale is not None:
            candidate_sales.append(next_sale)

        for cand in candidate_sales:
            if cand and cand.transaction_id and str(cand.transaction_id) not in seen_tx_ids:
                tx_obj = (
                    db.query(Transaction)
                    .filter(Transaction.id == cand.transaction_id)
                    .first()
                )
                if tx_obj and tx_obj.customer_id == customer_id:
                    seen_tx_ids.add(str(tx_obj.id))
                    found_txs.append(tx_obj)

        return found_txs

    def _consolidate_secondary_transactions(
        primary_tx: Transaction,
        secondary_txs: List[Transaction],
    ) -> None:
        """
        Merges contiguous secondary transactions of the same customer into `primary_tx`,
        re-pointing their ExcelSale rows and LoyaltyPointLedger entries without duplicating points.
        """
        for sec_tx in secondary_txs:
            if str(sec_tx.id) == str(primary_tx.id):
                continue
            if _to_cents(sec_tx.loyalty_discount_amount) > Decimal("0.00"):
                restore_pre_loyalty_prices(sec_tx)
            if not primary_tx.discount_rule_code and sec_tx.discount_rule_code:
                primary_tx.discount_rule_code = sec_tx.discount_rule_code

            for sec_sale in list(sec_tx.excel_sales):
                sec_sale.transaction = primary_tx
                db.add(sec_sale)

            for sec_entry in list(sec_tx.loyalty_ledger_entries):
                sec_entry.transaction = primary_tx
                db.add(sec_entry)

            sec_tx.customer = None
            cancel_post_purchase_timer(str(sec_tx.id))
            db.flush()
            db.delete(sec_tx)
            db.flush()
            db.refresh(primary_tx)

    for norm_phone, group_entries in groups:
        if norm_phone in customer_cache:
            customer = customer_cache[norm_phone]
        else:
            customer = crm_customer_repo.get_by_phone_number(db, norm_phone)
            if customer is None:
                # Flusso A — New customer: immediate onboarding (single WhatsApp Flow)
                customer, _, _ = customer_onboarding_service.initiate_onboarding(
                    db=db,
                    raw_phone=norm_phone,
                )
            customer_cache[norm_phone] = customer

        # Re-fetch sales in case initiate_onboarding committed the session
        refreshed_entries = []
        for r_idx, item_obj, sale_obj in group_entries:
            db.add(sale_obj)
            refreshed_entries.append((r_idx, item_obj, sale_obj))

        matching_txs = _find_matching_txs_for_customer(refreshed_entries, customer.id)
        tx = matching_txs[0] if matching_txs else None
        secondary_txs = matching_txs[1:] if len(matching_txs) > 1 else []

        if customer.status == CustomerStatus.PENDING_ONBOARDING:
            # Flusso A: Associate rows, credit frozen points immediately, Col P = "Richiesta inviata"
            if tx is None:
                tx = Transaction(
                    date=target_date,
                    customer_id=customer.id,
                    loyalty_discount_amount=Decimal("0.00"),
                    discount_rule_code=None,
                    identification_status=CustomerIdentificationStatus.IDENTIFICATION_CONFIRMED,
                )
                db.add(tx)
                db.flush()
            elif secondary_txs:
                _consolidate_secondary_transactions(tx, secondary_txs)

            for _, _, sale_obj in refreshed_entries:
                if sale_obj.transaction_id != tx.id:
                    sale_obj.transaction_id = tx.id
                    db.add(sale_obj)
            db.flush()
            db.refresh(tx)

            _apply_loyalty_engine(
                db=db,
                transaction=tx,
                target_discount_rule_code=None,
                previous_customer_id=None,
            )

            tx_rows_to_emit = [r for r in tx.excel_sales if r.excel_row_index is not None]
            for sale_obj in tx_rows_to_emit:
                r_idx = int(sale_obj.excel_row_index)
                row_updates_by_idx[r_idx] = ExcelRowUpdateItem(
                    excel_row_index=r_idx,
                    col_p_status="Richiesta inviata",
                    col_o_phone=forced_col_o_restore.get(r_idx),
                    identification_pending=False,
                )
            continue

        if customer.status in (CustomerStatus.ACTIVE, CustomerStatus.INACTIVE_EXPIRED):
            # Flusso B — Registered customer: 2-Step Confirmation + OTP Redemption + Split Receipt & Price Edits
            short_name = _format_customer_short_name(customer)

            if tx is None:
                # 1st Save (Ctrl+S): Create IDENTIFICATION_PENDING transaction, do NOT credit points yet
                tx = Transaction(
                    date=target_date,
                    customer_id=customer.id,
                    loyalty_discount_amount=Decimal("0.00"),
                    discount_rule_code=None,
                    identification_status=CustomerIdentificationStatus.IDENTIFICATION_PENDING,
                )
                db.add(tx)
                db.flush()

                for _, _, sale_obj in refreshed_entries:
                    sale_obj.transaction_id = tx.id
                    db.add(sale_obj)
                db.flush()

                for r_idx, _, _ in refreshed_entries:
                    row_updates_by_idx[r_idx] = ExcelRowUpdateItem(
                        excel_row_index=r_idx,
                        col_p_status=f"{short_name} - Salva il file per confermare",
                        col_o_phone=forced_col_o_restore.get(r_idx),
                        identification_pending=True,
                    )
                continue

            # 2nd Save (confirmation) or subsequent update (split receipt / price edit / OTP retry)
            was_already_confirmed = (
                tx.identification_status == CustomerIdentificationStatus.IDENTIFICATION_CONFIRMED
            )
            if tx.identification_status == CustomerIdentificationStatus.IDENTIFICATION_PENDING:
                tx.identification_status = CustomerIdentificationStatus.IDENTIFICATION_CONFIRMED
                db.add(tx)

            newly_attached_sales = [
                sale_obj
                for _, _, sale_obj in refreshed_entries
                if sale_obj.transaction_id != tx.id
            ]

            # Macro-Area 2.1.1: If new rows are joining an already-discounted transaction (or two txs are bridged),
            # restore pre-loyalty prices on existing rows BEFORE attaching the new rows so the discount
            # is re-spread accurately across all purchase rows.
            if (newly_attached_sales or secondary_txs) and _to_cents(
                tx.loyalty_discount_amount
            ) > Decimal("0.00"):
                restore_pre_loyalty_prices(tx)

            if secondary_txs:
                _consolidate_secondary_transactions(tx, secondary_txs)

            for sale_obj in newly_attached_sales:
                sale_obj.transaction_id = tx.id
                db.add(sale_obj)
            db.flush()
            db.refresh(tx)

            # Check for OTP code in Col Q across the group
            otp_input: Optional[str] = None
            for _, item_obj, _ in refreshed_entries:
                candidate_otp = (item_obj.redemption_otp or "").strip()
                if candidate_otp:
                    otp_input = candidate_otp
                    break

            otp_error = False
            applied_new_otp = False
            target_rule_code = tx.discount_rule_code

            if otp_input:
                stored_otp = (customer.redemption_otp_code or "").strip()
                if (
                    stored_otp
                    and otp_input.upper() == stored_otp.upper()
                    and customer.redemption_rule_code
                ):
                    target_rule_code = customer.redemption_rule_code
                    applied_new_otp = True
                elif tx.discount_rule_code is None:
                    otp_error = True

            # Macro-Area 2.1.4: When an already-confirmed transaction is updated without adding new rows,
            # merging transactions, or redeeming a new OTP, preserve the exact manual Excel prices
            # and record any point difference as PRICE_CORRECTION while keeping DISCOUNT_REDEMPTION immutable.
            preserve_manual_prices = bool(
                was_already_confirmed
                and not applied_new_otp
                and not newly_attached_sales
                and not secondary_txs
            )
            discount_was_spread = bool(
                applied_new_otp
                or (
                    tx.discount_rule_code
                    and (newly_attached_sales or secondary_txs or not was_already_confirmed)
                )
            )

            # Execute loyalty engine (idempotent if already credited; spreads discount if OTP valid / split receipt)
            try:
                _apply_loyalty_engine(
                    db=db,
                    transaction=tx,
                    target_discount_rule_code=target_rule_code,
                    previous_customer_id=None,
                    preserve_manual_prices=preserve_manual_prices,
                )
                if applied_new_otp:
                    customer.redemption_otp_code = None
                    customer.redemption_rule_code = None
                    db.add(customer)
                    db.flush()
            except ValueError as exc:
                logger.log_execution(
                    "excel_synchronization_service",
                    "otp_redemption_rejected",
                    "warning",
                    transaction_id=str(tx.id),
                    customer_id=str(customer.id),
                    error=str(exc),
                )
                otp_error = True
                applied_new_otp = False
                discount_was_spread = False
                _apply_loyalty_engine(
                    db=db,
                    transaction=tx,
                    target_discount_rule_code=tx.discount_rule_code,
                    previous_customer_id=None,
                    preserve_manual_prices=preserve_manual_prices,
                )

            balance = crm_customer_repo.get_points_balance(db, customer.id)
            col_p_text = (
                f"⚠️ OTP ERRATO | {short_name} ({balance} pt)"
                if otp_error
                else f"{short_name} ({balance} pt)"
            )

            # Emit row_updates for all Excel rows belonging to the transaction so multi-row receipts stay consistent
            tx_rows_to_emit = [r for r in tx.excel_sales if r.excel_row_index is not None]
            for sale_obj in tx_rows_to_emit:
                r_idx = int(sale_obj.excel_row_index)
                row_updates_by_idx[r_idx] = ExcelRowUpdateItem(
                    excel_row_index=r_idx,
                    col_p_status=col_p_text,
                    selling_price=(
                        float(sale_obj.selling_price)
                        if discount_was_spread and sale_obj.selling_price is not None
                        else None
                    ),
                    col_o_phone=forced_col_o_restore.get(r_idx),
                    identification_pending=False,
                )

            if str(tx.id) not in confirmed_tx_ids_to_notify:
                confirmed_tx_ids_to_notify.append(str(tx.id))

    ordered_updates = [row_updates_by_idx[k] for k in sorted(row_updates_by_idx.keys())]
    return ordered_updates, confirmed_tx_ids_to_notify


def ingest_pushed_sales(
    db: Session,
    rows: List[ExcelRowPushItem],
    target_date: date,
    agent_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Ingests a batch of sales pushed by the standalone edge agent.
    - Records latest diagnostic `agent_status` when provided
    - Handles heartbeats / control polls (empty rows) and drains `async_row_updates` + `agent_commands`
    - Validates against historical file patterns (Level 3 protection)
    - Auto-detects returns (is_exchange = True) when starting_price < 0 or selling_price < 0
    - Performs idempotent upsert (updates ORPHAN / UNPROCESSABLE, inserts new)
    - Processes CRM identification (Flusso A / Flusso B), OTP redemption, and 60s debounce notification
    - Returns confirmed row indexes, created/updated record IDs, row_updates, async_row_updates, and agent_commands
    """
    if agent_status is not None:
        record_agent_status(agent_status)

    # 1. Heartbeat / Control & Status Poll check
    if not rows:
        _record_sync(True, 0)
        async_updates = drain_async_row_updates()
        agent_commands = drain_agent_commands()
        logger.log_execution("excel_synchronization_service", "heartbeat_received", "ok", target_date=str(target_date))
        return {
            "confirmed_row_indexes": [],
            "inserted_ids": [],
            "row_updates": [],
            "async_row_updates": async_updates,
            "agent_commands": agent_commands,
        }

    # 2. Level 3 Safety: Historical pattern detection (>= 10 matches on a single historical date)
    matched_hist_date = excel_synchronization_repo.find_historical_pattern_date(db, target_date, rows, min_matches=10)
    if matched_hist_date:
        err_msg = f"Historical file pattern detected: matches data from {matched_hist_date}"
        _record_sync(False, error=err_msg)
        logger.log_execution("excel_synchronization_service", "historical_pattern_blocked", "warning",
                             target_date=str(target_date), matched_date=str(matched_hist_date))
        raise HistoricalFileError(err_msg, matched_date=matched_hist_date)

    # 3. Existing records for target_date
    existing_records = excel_synchronization_repo.get_all_by_date(db, target_date)
    existing_by_row = {r.excel_row_index: r for r in existing_records}

    sales_to_insert = []
    sales_to_update = []
    confirmed_row_indexes = []
    price_changed_row_indexes: set[int] = set()

    for item in rows:
        row_idx = item.excel_row_index
        confirmed_row_indexes.append(row_idx)

        status = ExcelSaleStatus.UNPROCESSABLE if item.is_unprocessable else ExcelSaleStatus.ORPHAN

        starting_price_val = None
        if item.starting_price is not None:
            try:
                starting_price_val = float(item.starting_price)
            except (ValueError, TypeError):
                status = ExcelSaleStatus.UNPROCESSABLE

        selling_price_val = None
        if item.selling_price is not None:
            try:
                selling_price_val = float(item.selling_price)
            except (ValueError, TypeError):
                status = ExcelSaleStatus.UNPROCESSABLE

        # Auto-detect returns from negative prices in addition to legacy Col L == "X"
        is_exchange_val = bool(item.is_exchange)
        if (starting_price_val is not None and starting_price_val < 0) or (
            selling_price_val is not None and selling_price_val < 0
        ):
            is_exchange_val = True

        existing_record = existing_by_row.get(row_idx)
        if existing_record:
            # Macro-Area 2.1.4: Excel is the single source of truth for prices even on RECONCILED
            # or already-confirmed transaction rows. Preserve RECONCILED status if row remains valid.
            if (
                existing_record.status == ExcelSaleStatus.RECONCILED
                and status == ExcelSaleStatus.UNPROCESSABLE
            ):
                continue

            new_starting = starting_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None
            new_selling = selling_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None

            changed = False
            price_changed = False
            old_start_f = (
                float(existing_record.starting_price)
                if existing_record.starting_price is not None
                else None
            )
            old_sell_f = (
                float(existing_record.selling_price)
                if existing_record.selling_price is not None
                else None
            )
            if old_start_f != new_starting:
                existing_record.starting_price = new_starting
                changed = True
                price_changed = True
            if old_sell_f != new_selling:
                existing_record.selling_price = new_selling
                changed = True
                price_changed = True
            if bool(existing_record.is_exchange) != bool(is_exchange_val):
                existing_record.is_exchange = is_exchange_val
                changed = True
                price_changed = True
            if existing_record.raw_article_code != item.raw_article_code:
                existing_record.raw_article_code = item.raw_article_code
                changed = True
            if existing_record.excel_file_column != item.excel_file_column:
                existing_record.excel_file_column = item.excel_file_column
                changed = True
            if existing_record.status in (
                ExcelSaleStatus.ORPHAN,
                ExcelSaleStatus.UNPROCESSABLE,
            ) and existing_record.status != status:
                existing_record.status = status
                changed = True

            if price_changed:
                price_changed_row_indexes.add(row_idx)
            if changed:
                sales_to_update.append(existing_record)
        else:
            sale_in = ExcelSaleCreate(
                date=target_date,
                excel_row_index=row_idx,
                excel_file_column=item.excel_file_column,
                status=status,
                starting_price=starting_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None,
                selling_price=selling_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None,
                is_exchange=is_exchange_val,
                raw_article_code=item.raw_article_code
            )
            sales_to_insert.append(sale_in)

    created_records = []
    if sales_to_insert:
        created_records = excel_synchronization_repo.bulk_create(
            db, sales_to_insert, commit_changes=False
        )
        for rec in created_records:
            existing_by_row[rec.excel_row_index] = rec

    db.flush()

    # 4. CRM Identification, Contiguous Grouping, Error Handling & OTP Redemption
    row_updates, confirmed_tx_ids = _process_crm_for_pushed_rows(
        db=db,
        rows=rows,
        existing_by_row=existing_by_row,
        target_date=target_date,
        price_changed_row_indexes=price_changed_row_indexes,
    )

    db.commit()

    # Schedule / reset 60s post-purchase debounce timers after commit
    for tx_id in confirmed_tx_ids:
        schedule_post_purchase_notification(tx_id)

    async_updates = drain_async_row_updates()
    agent_commands = drain_agent_commands()
    inserted_or_updated_ids = [str(r.id) for r in created_records] + [str(r.id) for r in sales_to_update]

    _record_sync(True, len(inserted_or_updated_ids))
    logger.log_execution(
        "excel_synchronization_service",
        "ingest_pushed_sales",
        "ok",
        target_date=str(target_date),
        records_inserted=len(created_records),
        records_updated=len(sales_to_update),
        confirmed_count=len(confirmed_row_indexes),
        row_updates_count=len(row_updates),
    )

    return {
        "confirmed_row_indexes": confirmed_row_indexes,
        "inserted_ids": inserted_or_updated_ids,
        "row_updates": row_updates,
        "async_row_updates": async_updates,
        "agent_commands": agent_commands,
    }

# ---------------------------------------------------------------------------
# Ingestion Logic
# ---------------------------------------------------------------------------

def ingest_daily_sales(db: Session, file_path: str, target_date: date) -> List[str]:
    """
    Ingests Excel sales data for the given date.
    Uses 'Stream-until-Empty' strategy starting from `db_count + 1` up to row 698.
    """
    if not openpyxl:
        logger.error("openpyxl is not installed.")
        raise ImportError("openpyxl is required to ingest excel files")

    # 1. Fetch existing records to allow updating ORPHANs
    existing_records = excel_synchronization_repo.get_all_by_date(db, target_date)
    existing_by_row = {r.excel_row_index: r for r in existing_records}
    
    # 2. Open workbook in read_only mode
    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        sheet = wb.active
    except Exception as e:
        logger.log_execution("excel_synchronization_service", "ingest_open_error", "err", exc=e)
        raise e

    # 3. Determine max_col based on settings
    code_col_idx = None
    max_col_to_read = 12
    if settings.EXCEL_CODE_COLUMN:
        try:
            code_col_idx = ord(settings.EXCEL_CODE_COLUMN.upper()) - ord('A')
            max_col_to_read = max(12, code_col_idx + 1)
        except TypeError:
            logger.error(f"Invalid EXCEL_CODE_COLUMN: {settings.EXCEL_CODE_COLUMN}")

    # 4. Read rows
    sales_to_insert = []
    sales_to_update = []
    
    for row_idx, row in enumerate(sheet.iter_rows(min_row=1, max_row=698, min_col=1, max_col=max_col_to_read), start=1):
        # A row is empty if all cells in A to I are None
        if all(cell.value is None for cell in row[0:9]):
            break
            
        status = ExcelSaleStatus.ORPHAN
        starting_price_val = None
        selling_price_val = None
        found_category_col = None
        
        # Parse starting price (Column A, index 0)
        try:
            val_a = row[0].value
            if val_a is not None:
                starting_price_val = float(val_a)
        except (ValueError, TypeError):
            status = ExcelSaleStatus.UNPROCESSABLE
            
        # Parse selling price (Columns B-I, index 1-8)
        for col_idx in range(1, 9):
            cell = row[col_idx]
            if cell.value is not None:
                found_category_col = col_idx - 1 # 0-based index for categories (0 for 'B', 1 for 'C', etc.)
                try:
                    selling_price_val = float(cell.value)
                except (ValueError, TypeError):
                    status = ExcelSaleStatus.UNPROCESSABLE
                break
                
        # Parse is_exchange (Column L, index 11 OR negative price)
        is_exchange = False
        val_l = row[11].value
        if val_l is not None and str(val_l).strip().upper() == "X":
            is_exchange = True
        if (starting_price_val is not None and starting_price_val < 0) or (
            selling_price_val is not None and selling_price_val < 0
        ):
            is_exchange = True

        # Parse raw_article_code
        raw_article_code = None
        if code_col_idx is not None and code_col_idx < len(row):
            val_code = row[code_col_idx].value
            if val_code is not None:
                raw_article_code = str(val_code).strip()

        existing_record = existing_by_row.get(row_idx)
        if existing_record:
            if existing_record.status in (ExcelSaleStatus.ORPHAN, ExcelSaleStatus.UNPROCESSABLE):
                new_starting = starting_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None
                new_selling = selling_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None
                
                changed = False
                if existing_record.starting_price != new_starting:
                    existing_record.starting_price = new_starting
                    changed = True
                if existing_record.selling_price != new_selling:
                    existing_record.selling_price = new_selling
                    changed = True
                if existing_record.is_exchange != is_exchange:
                    existing_record.is_exchange = is_exchange
                    changed = True
                if existing_record.raw_article_code != raw_article_code:
                    existing_record.raw_article_code = raw_article_code
                    changed = True
                if existing_record.excel_file_column != found_category_col:
                    existing_record.excel_file_column = found_category_col
                    changed = True
                if existing_record.status != status:
                    existing_record.status = status
                    changed = True
                
                if changed:
                    sales_to_update.append(existing_record)
        else:
            sale_in = ExcelSaleCreate(
                date=target_date,
                excel_row_index=row_idx,
                excel_file_column=found_category_col,
                status=status,
                starting_price=starting_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None,
                selling_price=selling_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None,
                is_exchange=is_exchange,
                raw_article_code=raw_article_code
            )
            sales_to_insert.append(sale_in)
        
    wb.close()
    
    # 5. Bulk insert and update
    if sales_to_update:
        db.commit()
        
    created_records = []
    if sales_to_insert:
        created_records = excel_synchronization_repo.bulk_create(db, sales_to_insert)
        
    if sales_to_insert or sales_to_update:
        logger.log_execution(
            "excel_synchronization_service", 
            "ingest_daily_sales", 
            "ok", 
            target_date=str(target_date),
            records_inserted=len(created_records),
            records_updated=len(sales_to_update),
            min_row_checked=1
        )
        return [str(r.id) for r in created_records] + [str(r.id) for r in sales_to_update]
    
    return []


# ---------------------------------------------------------------------------
# Reconciliation Logic
# ---------------------------------------------------------------------------

from sqlalchemy import func
from src.models.pim import ArticlePhoto

async def find_candidates_async(db: Session, search_code: str, is_exchange: bool) -> List[Dict[str, Any]]:
    """
    Finds reconciliation candidates using the unified warehouse search engine (search_service).
    Returns candidates matching the search code (EAN, vendor code, skeleton, or free text query)
    grouped by color variant with FIFO physical article assignment.
    """
    from src.services import search_service
    from src.models.wms import Article, ArticleStatus

    clean_code = (search_code or "").strip()
    if not clean_code:
        return []

    target_status = ArticleStatus.SOLD if is_exchange else ArticleStatus.AVAILABLE
    candidates: List[Dict[str, Any]] = []

    try:
        search_res = await search_service.execute_search(
            db,
            query=clean_code,
            in_stock_only=(not is_exchange),
            page_size=50,
        )
        search_items = search_res.get("results", [])

        for item in search_items:
            variant_color = item.colors[0].strip().lower() if item.colors else None

            # Find matching physical articles for this blueprint and status
            q = (
                db.query(Article)
                .filter(
                    Article.article_blueprint_id == item.blueprint_id,
                    Article.status == target_status,
                )
                .order_by(Article.created_at.asc())
            )
            all_target_articles = q.all()

            matching_articles: List[Article] = []
            for a in all_target_articles:
                a_colors = [str(c).strip().lower() for c in (a.colors or []) if c and str(c).strip()]
                if variant_color and variant_color != "unknown":
                    if variant_color in a_colors:
                        matching_articles.append(a)
                else:
                    if not a_colors or a_colors == ["unknown"]:
                        matching_articles.append(a)

            # If no color match but there are available articles and item has no specific color constraint
            if not matching_articles and not variant_color and all_target_articles:
                matching_articles = all_target_articles

            if not matching_articles:
                continue

            selected_article = matching_articles[0]  # FIFO
            inventory = len(matching_articles)
            photo_url = f"/api/v1/ingestion/photos/{item.photo_id}" if item.photo_id else None

            candidates.append({
                "article_id": str(selected_article.id),
                "article_name": item.article_name,
                "article_description": item.description,
                "brand_name": item.brand_name,
                "category_name": item.category_name,
                "colors": selected_article.colors or item.colors,
                "inventory": inventory,
                "photo_serving_endpoint_url": photo_url,
            })
    except Exception as e:
        logger.log_execution("excel_synchronization_service", "unified_search_failed", "warning", exc=e, query=clean_code)

    # Fallback to direct WMS repo query if search_service yielded 0 candidates
    if not candidates:
        legacy_articles = wms_repo.get_reconciliation_candidates(db, clean_code, is_exchange)
        grouped = {}
        inventory_counts = {}

        for a in legacy_articles:
            colors_list = [c.lower() for c in (a.colors or [])]
            color_key = ", ".join(colors_list) if colors_list else "unknown"

            if color_key not in grouped:
                grouped[color_key] = a  # FIFO relies on query order_by created_at asc
                inventory_counts[color_key] = 1
            else:
                inventory_counts[color_key] += 1

        for color_key, article in grouped.items():
            colors_list = [c.lower() for c in (article.colors or [])]
            photo = None
            if colors_list:
                photo = db.query(ArticlePhoto).filter(
                    ArticlePhoto.article_blueprint_id == article.article_blueprint_id,
                    func.lower(ArticlePhoto.canonical_color_name).in_(colors_list),
                ).first()

            if not photo:
                photo = db.query(ArticlePhoto).filter(
                    ArticlePhoto.article_blueprint_id == article.article_blueprint_id
                ).first()

            photo_url = f"/api/v1/ingestion/photos/{photo.id}" if photo else None
            blueprint = article.blueprint
            candidates.append({
                "article_id": str(article.id),
                "article_name": blueprint.article_name if blueprint else None,
                "article_description": blueprint.description if blueprint else None,
                "brand_name": blueprint.brand.name if blueprint and blueprint.brand else None,
                "category_name": blueprint.category.name if blueprint and blueprint.category else None,
                "colors": article.colors,
                "inventory": inventory_counts[color_key],
                "photo_serving_endpoint_url": photo_url,
            })

    return candidates


def find_candidates(db: Session, search_code: str, is_exchange: bool) -> List[Dict[str, Any]]:
    """
    Synchronous wrapper for find_candidates_async, providing backward compatibility for sync callers.
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, find_candidates_async(db, search_code, is_exchange)).result()
    else:
        return asyncio.run(find_candidates_async(db, search_code, is_exchange))

def get_pending_sales(db: Session, target_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """
    Returns a list of orphan/unprocessable records for the specified date (defaults to today).
    """
    date_to_use = target_date or date.today()
    records = excel_synchronization_repo.get_pending_records(db, target_date=date_to_use)
    
    results = []
    for r in records:
        results.append({
            "id": str(r.id),
            "date": str(r.date),
            "excel_row_index": r.excel_row_index,
            "excel_file_column": r.excel_file_column,
            "status": r.status.value,
            "starting_price": float(r.starting_price) if r.starting_price is not None else None,
            "selling_price": float(r.selling_price) if r.selling_price is not None else None,
            "is_exchange": r.is_exchange,
            "raw_article_code": r.raw_article_code
        })
    return results

def get_daily_sales(db: Session, target_date: date) -> List[Dict[str, Any]]:
    """
    Returns all sales records for the specified date, including photo URL, article name, brand and category for reconciled items.
    """
    records = excel_synchronization_repo.get_all_by_date(db, target_date)
    
    results = []
    for r in records:
        photo_url = None
        article_name = None
        brand_name = None
        category_name = None
        
        if r.status == ExcelSaleStatus.RECONCILED and r.article:
            blueprint = r.article.blueprint
            article_name = blueprint.article_name if blueprint else None
            brand_name = blueprint.brand.name if blueprint and blueprint.brand else None
            category_name = blueprint.category.name if blueprint and blueprint.category else None
            
            colors_list = [c.lower() for c in (r.article.colors or [])]
            photo = None
            if colors_list:
                photo = db.query(ArticlePhoto).filter(
                    ArticlePhoto.article_blueprint_id == r.article.article_blueprint_id,
                    func.lower(ArticlePhoto.canonical_color_name).in_(colors_list)
                ).first()
            if not photo:
                photo = db.query(ArticlePhoto).filter(
                    ArticlePhoto.article_blueprint_id == r.article.article_blueprint_id
                ).first()
                
            photo_url = f"/api/v1/ingestion/photos/{photo.id}" if photo else None
            
        results.append({
            "id": str(r.id),
            "date": str(r.date),
            "excel_row_index": r.excel_row_index,
            "excel_file_column": r.excel_file_column,
            "status": r.status,
            "starting_price": float(r.starting_price) if r.starting_price is not None else None,
            "selling_price": float(r.selling_price) if r.selling_price is not None else None,
            "is_exchange": r.is_exchange,
            "raw_article_code": r.raw_article_code,
            "article_name": article_name,
            "brand_name": brand_name,
            "category_name": category_name,
            "photo_url": photo_url
        })
    return results


def get_sale_detail(db: Session, sale_id: str) -> Dict[str, Any]:
    from src.models.wms import ArticleStatus
    
    sale = excel_synchronization_repo.get(db, sale_id)
    if not sale:
        raise ValueError("Sale not found")
        
    if sale.status != ExcelSaleStatus.RECONCILED or not sale.article:
        raise ValueError("Sale is not reconciled or missing article")
        
    blueprint = sale.article.blueprint
    if not blueprint:
        raise ValueError("Blueprint not found")
        
    colors_list = [c.lower() for c in (sale.article.colors or [])]
    photo = None
    if colors_list:
        photo = db.query(ArticlePhoto).filter(
            ArticlePhoto.article_blueprint_id == sale.article.article_blueprint_id,
            func.lower(ArticlePhoto.canonical_color_name).in_(colors_list)
        ).first()
    if not photo:
        photo = db.query(ArticlePhoto).filter(
            ArticlePhoto.article_blueprint_id == sale.article.article_blueprint_id
        ).first()
        
    photo_url = f"/api/v1/ingestion/photos/{photo.id}" if photo else None
    
    inventory = db.query(func.count(sale.article.__class__.id)).filter(
        sale.article.__class__.article_blueprint_id == blueprint.id,
        sale.article.__class__.status == ArticleStatus.AVAILABLE
    ).scalar() or 0

    return {
        "article_name": blueprint.article_name,
        "photo_url": photo_url,
        "inventory": inventory,
        "blueprint_id": blueprint.id,
        "colors": sale.article.colors or [],
        "is_exchange": sale.is_exchange
    }
def _get_or_create_reason(db: Session, code: str, sign: int) -> str:
    reason = db.query(MovementReason).filter(MovementReason.code == code).first()
    if not reason:
        reason = MovementReason(code=code, sign=sign)
        db.add(reason)
        db.flush()
    return str(reason.id)

def reconcile_sale(db: Session, excel_sale_id: str, article_id: str) -> Dict[str, Any]:
    """
    Executes the dual-write atomic transaction to reconcile an ExcelSale with a physical WMS Article.
    """
    # 1. Fetch and validate the ExcelSale
    excel_sale = excel_synchronization_repo.get(db, excel_sale_id)
    if not excel_sale:
        raise ValueError("ExcelSale record not found.")
        
    if excel_sale.status == ExcelSaleStatus.RECONCILED:
        return {"status": "ok", "message": "Already reconciled."}
        
    if excel_sale.status == ExcelSaleStatus.UNPROCESSABLE:
        raise ValueError("Cannot reconcile an unprocessable record.")

    # 2. Fetch and validate the Article
    article = wms_repo.get(db, article_id)
    if not article:
        raise ValueError("Article not found.")
        
    # Check if the article's status is compatible with the transaction
    if not excel_sale.is_exchange and article.status != ArticleStatus.AVAILABLE:
        raise ValueError("Article is not AVAILABLE for sale.")
    if excel_sale.is_exchange and article.status != ArticleStatus.SOLD:
        raise ValueError("Article is not SOLD, cannot process exchange return.")

    # 3. Dual-Write Transaction
    try:
        if not excel_sale.is_exchange:
            # Sale
            article.status = ArticleStatus.SOLD
            reason_id = _get_or_create_reason(db, "EXCEL_SALE", -1)
        else:
            # Return
            article.status = ArticleStatus.AVAILABLE
            reason_id = _get_or_create_reason(db, "EXCEL_RETURN", 1)
            
        movement_in = StockUpdate(
            article_id=str(article.id),
            reason_id=reason_id,
            notes=f"Reconciled from ExcelSale {excel_sale.id}"
        )
        wms_repo.add_movement(db, movement_in, commit_changes=False)
        
        excel_sale.article_id = str(article.id)
        excel_sale.status = ExcelSaleStatus.RECONCILED
        
        # Commit the atomic transaction
        db.commit()
        invalidate_search_cache()
        
        logger.log_execution("excel_synchronization_service", "reconcile_sale", "ok",
                             excel_sale_id=excel_sale_id, article_id=article_id)
        return {"status": "ok", "message": "Successfully reconciled."}
        
    except Exception as e:
        logger.log_execution("excel_synchronization_service", "reconcile_sale", "err", exc=e)
        raise e

# ---------------------------------------------------------------------------
# Polling Daemon Logic
# ---------------------------------------------------------------------------

_polling_thread = None
_stop_event = threading.Event()
_sync_lock = threading.Lock()
_sync_state = {
    "last_attempt_at": None,
    "last_success_at": None,
    "last_inserted_count": 0,
    "last_error": None
}

def _record_sync(success: bool, inserted_count: int = 0, error: str = None):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _sync_lock:
        _sync_state["last_attempt_at"] = now
        if success:
            _sync_state["last_success_at"] = now
            _sync_state["last_inserted_count"] = inserted_count
            _sync_state["last_error"] = None
        else:
            _sync_state["last_error"] = error

def get_sync_status() -> dict:
    with _sync_lock:
        sync_snapshot = dict(_sync_state)
    with _agent_status_lock:
        agent_snapshot = dict(_latest_agent_status)
    agent_snapshot["pending_commands_count"] = get_pending_agent_commands_count()
    return {
        **sync_snapshot,
        "polling_interval_seconds": settings.EXCEL_POLLING_INTERVAL,
        "edge_agent": agent_snapshot,
    }

def reset_sync_status():
    with _sync_lock:
        _sync_state.update({
            "last_attempt_at": None,
            "last_success_at": None,
            "last_inserted_count": 0,
            "last_error": None
        })

def _polling_loop(stop_event: threading.Event):
    logger.log_execution("excel_synchronization_service", "startup", "ok", message=f"Starting Excel polling loop. Interval: {settings.EXCEL_POLLING_INTERVAL}s")
    
    while not stop_event.is_set():
        if settings.EXCEL_FILE_PATH:
            db = SessionLocal()
            try:
                target_date = date.today()
                inserted_ids = ingest_daily_sales(db, settings.EXCEL_FILE_PATH, target_date)
                if inserted_ids:
                    logger.log_execution("excel_synchronization_service", "ingested", "ok", message=f"Polled Excel and ingested {len(inserted_ids)} new sales rows.")
                _record_sync(True, len(inserted_ids) if inserted_ids else 0)
            except Exception as e:
                logger.log_execution("excel_synchronization_service", "error", "error", exc=e)
                _record_sync(False, error=str(e))
            finally:
                db.close()
        
        stop_event.wait(timeout=settings.EXCEL_POLLING_INTERVAL)
        
    logger.log_execution("excel_synchronization_service", "shutdown", "ok", message="Excel polling loop stopped.")

def start_polling():
    global _polling_thread
    if _polling_thread is not None and _polling_thread.is_alive():
        logger.log_execution("excel_synchronization_service", "start_polling", "warning", message="Polling thread is already running.")
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


# ---------------------------------------------------------------------------
# Aggregations Logic
# ---------------------------------------------------------------------------

import yaml
from pathlib import Path
from src.models.excel_sales import ExcelSale

def _calculate_pnl(revenue_by_col: dict, pl_by_col: dict, constants: dict) -> dict:
    vat_rate = constants.get('vat_rate', 1.22)
    base_cost = constants.get('base_cost', 100)
    list_prices = constants.get('list_prices', {})
    sg_media_giorno = constants.get('sg_media_giorno', 500)
    costo_lavoro = constants.get('costo_lavoro', 350)
    imposizione_mutuo = constants.get('imposizione_mutuo', 180)
    
    total_costo_merce = 0.0
    for col_idx in range(8):
        list_price_total = pl_by_col.get(col_idx, 0.0)
        list_price = list_prices.get(col_idx, 0)
        if list_price and list_price > 0:
            total_costo_merce += (list_price_total / list_price) * base_cost
            
    grand_total_revenue = sum(revenue_by_col.values())
    pl_revenue = sum(pl_by_col.values())
    
    discount = pl_revenue - grand_total_revenue
    net_discounted = grand_total_revenue
    vat_amount = net_discounted - (net_discounted / vat_rate)
    net_no_vat = net_discounted - vat_amount
    gross_margin = net_no_vat - total_costo_merce
    residuo = gross_margin - sg_media_giorno - costo_lavoro
    net_margin = residuo - imposizione_mutuo
    
    return {
        "total_list_price": round(pl_revenue, 2),
        "discount": round(discount, 2),
        "net_discounted": round(net_discounted, 2),
        "vat_amount": round(vat_amount, 2),
        "net_no_vat": round(net_no_vat, 2),
        "cost_of_goods": round(total_costo_merce, 2),
        "gross_margin": round(gross_margin, 2),
        "sg_media_giorno": sg_media_giorno,
        "costo_lavoro": costo_lavoro,
        "residuo": round(residuo, 2),
        "imposizione_mutuo": imposizione_mutuo,
        "net_margin": round(net_margin, 2)
    }

def get_daily_aggregations(db: Session, target_date: date) -> Dict[str, Any]:
    const_path = Path(__file__).parent.parent / 'const.yaml'
    with open(const_path, 'r') as cf:
        constants = yaml.safe_load(cf)

    # 1. Today's stats
    today_stats = db.query(
        ExcelSale.excel_file_column,
        func.sum(ExcelSale.selling_price).label("total_revenue"),
        func.count(ExcelSale.selling_price).label("total_sales"),
        func.sum(ExcelSale.starting_price).label("total_list_price")
    ).filter(
        ExcelSale.date == target_date,
        ExcelSale.selling_price.isnot(None),
        ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE
    ).group_by(ExcelSale.excel_file_column).all()

    today_revenue_by_col = {row.excel_file_column: float(row.total_revenue or 0) for row in today_stats}
    today_pl_by_col = {row.excel_file_column: float(row.total_list_price or 0) for row in today_stats}
    today_sales_by_col = {row.excel_file_column: row.total_sales for row in today_stats}
    
    # 2. General Historical stats
    general_days = db.query(func.count(func.distinct(ExcelSale.date))).filter(
        ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
        ExcelSale.selling_price.isnot(None)
    ).scalar() or 1
    
    general_stats = db.query(
        ExcelSale.excel_file_column,
        func.sum(ExcelSale.selling_price).label("total_revenue"),
        func.sum(ExcelSale.starting_price).label("total_list_price")
    ).filter(
        ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
        ExcelSale.selling_price.isnot(None)
    ).group_by(ExcelSale.excel_file_column).all()
    
    general_revenue_by_col = {row.excel_file_column: float(row.total_revenue or 0) / general_days for row in general_stats}
    general_pl_by_col = {row.excel_file_column: float(row.total_list_price or 0) / general_days for row in general_stats}
    
    # 3. DOW Historical stats
    target_dow = target_date.strftime('%w')
    dow_days = db.query(func.count(func.distinct(ExcelSale.date))).filter(
        ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
        ExcelSale.selling_price.isnot(None),
        func.strftime('%w', ExcelSale.date) == target_dow
    ).scalar() or 1

    dow_stats = db.query(
        ExcelSale.excel_file_column,
        func.sum(ExcelSale.selling_price).label("total_revenue"),
        func.sum(ExcelSale.starting_price).label("total_list_price")
    ).filter(
        ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
        ExcelSale.selling_price.isnot(None),
        func.strftime('%w', ExcelSale.date) == target_dow
    ).group_by(ExcelSale.excel_file_column).all()

    dow_revenue_by_col = {row.excel_file_column: float(row.total_revenue or 0) / dow_days for row in dow_stats}
    dow_pl_by_col = {row.excel_file_column: float(row.total_list_price or 0) / dow_days for row in dow_stats}

    # Calculate P&L for all 3 projections
    today_pnl = _calculate_pnl(today_revenue_by_col, today_pl_by_col, constants)
    dow_pnl = _calculate_pnl(dow_revenue_by_col, dow_pl_by_col, constants)
    general_pnl = _calculate_pnl(general_revenue_by_col, general_pl_by_col, constants)

    # Prepare Column results for today
    results = []
    for col_idx in range(8):
        revenue = today_revenue_by_col.get(col_idx, 0.0)
        pl = today_pl_by_col.get(col_idx, 0.0)
        discount = pl - revenue
        sales = today_sales_by_col.get(col_idx, 0)
        avg_price = revenue / sales if sales > 0 else 0.0
        results.append({
            "col_index": col_idx,
            "total_revenue": revenue,
            "total_list_price": pl,
            "discount": discount,
            "total_sales": sales,
            "avg_price": round(avg_price, 2)
        })

    # Prepare PL column results
    pl_revenue = sum(today_revenue_by_col.values()) # wait, total_revenue should be revenue, pl should be pl
    pl_list_price = sum(today_pl_by_col.values())
    pl_discount = pl_list_price - pl_revenue
    pl_count = sum(today_sales_by_col.values())
    pl_avg = pl_revenue / pl_count if pl_count > 0 else 0.0
    pl_column = {
        "col_index": -1,
        "total_revenue": pl_revenue,
        "total_list_price": pl_list_price,
        "discount": pl_discount,
        "total_sales": pl_count,
        "avg_price": round(pl_avg, 2)
    }
    
    # Prepare comparative PnL
    comparisons = {}
    constant_keys = {"sg_media_giorno", "costo_lavoro", "imposizione_mutuo"}
    
    for key in today_pnl.keys():
        t_val = today_pnl[key]
        d_val = dow_pnl[key]
        g_val = general_pnl[key]
        is_const = key in constant_keys
        
        comparisons[key] = {
            "today": round(t_val, 2),
            "dow_avg": round(d_val, 2),
            "dow_diff": round(t_val - d_val, 2),
            "general_avg": round(g_val, 2),
            "general_diff": round(t_val - g_val, 2),
            "is_constant": is_const
        }

    return {
        "columns": results, 
        "pl_column": pl_column, 
        "pnl_summary": comparisons,
        "target_date_dow": int(target_dow)
    }


def load_columns_schema() -> List[Dict[str, Any]]:
    """Loads the Excel sales columns schema (index, header, description) from src/columns.yaml."""
    cols_path = Path(__file__).parent.parent / "columns.yaml"
    with open(cols_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    columns = data.get("columns", [])
    return sorted(columns, key=lambda c: int(c["index"]))


def get_category_headers() -> List[str]:
    """Returns the ordered list of Excel category column headers from src/columns.yaml."""
    return [str(col["header"]) for col in load_columns_schema()]


CATEGORY_HEADERS = get_category_headers()


def get_historical_aggregations(db: Session, target_date: date) -> Dict[str, Any]:
    """
    Returns historical sales data anchored at target_date:
    - last_7_days: daily total and breakdown across the 8 Excel category columns (0..7)
      for [target_date - 6 days .. target_date].
    - last_30_days: daily total revenue for [target_date - 29 days .. target_date].
    """
    from datetime import timedelta

    start_date_30 = target_date - timedelta(days=29)
    start_date_7 = target_date - timedelta(days=6)

    rows = (
        db.query(
            ExcelSale.date,
            ExcelSale.excel_file_column,
            func.sum(ExcelSale.selling_price).label("total_revenue"),
        )
        .filter(
            ExcelSale.date >= start_date_30,
            ExcelSale.date <= target_date,
            ExcelSale.selling_price.isnot(None),
            ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
        )
        .group_by(ExcelSale.date, ExcelSale.excel_file_column)
        .all()
    )

    revenue_by_day_col: Dict[date, Dict[Optional[int], float]] = {}
    for row in rows:
        day_map = revenue_by_day_col.setdefault(row.date, {})
        day_map[row.excel_file_column] = float(row.total_revenue or 0.0)

    # Historical averages (General & Day-of-Week) across all valid ExcelSale records
    dow_hist_rows = (
        db.query(
            func.strftime("%w", ExcelSale.date).label("dow"),
            func.count(func.distinct(ExcelSale.date)).label("days_count"),
            func.sum(ExcelSale.selling_price).label("total_revenue"),
        )
        .filter(
            ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
            ExcelSale.selling_price.isnot(None),
        )
        .group_by(func.strftime("%w", ExcelSale.date))
        .all()
    )

    dow_averages: Dict[int, float] = {}
    total_general_days = 0
    total_general_revenue = 0.0
    for r in dow_hist_rows:
        if r.dow is None:
            continue
        d_idx = int(r.dow)
        d_cnt = int(r.days_count or 0)
        d_rev = float(r.total_revenue or 0.0)
        dow_averages[d_idx] = round(d_rev / d_cnt, 2) if d_cnt > 0 else 0.0
        total_general_days += d_cnt
        total_general_revenue += d_rev

    general_avg = (
        round(total_general_revenue / total_general_days, 2)
        if total_general_days > 0
        else 0.0
    )

    last_30_days = []
    for offset in range(29, -1, -1):
        d = target_date - timedelta(days=offset)
        day_map = revenue_by_day_col.get(d, {})
        day_total = round(sum(day_map.values()), 2)
        dow_idx = int(d.strftime("%w"))
        last_30_days.append({
            "date": d.isoformat(),
            "dow": dow_idx,
            "total_revenue": day_total,
            "dow_avg": dow_averages.get(dow_idx, 0.0),
        })

    last_7_days = []
    for offset in range(0, 7):
        d = target_date - timedelta(days=offset)
        day_map = revenue_by_day_col.get(d, {})
        categories = []
        for col_idx, header in enumerate(CATEGORY_HEADERS):
            cat_rev = round(day_map.get(col_idx, 0.0), 2)
            categories.append({
                "col_index": col_idx,
                "header": header,
                "total_revenue": cat_rev,
            })
        day_total = round(sum(day_map.values()), 2)
        dow_idx = int(d.strftime("%w"))
        last_7_days.append({
            "date": d.isoformat(),
            "dow": dow_idx,
            "total_revenue": day_total,
            "dow_avg": dow_averages.get(dow_idx, 0.0),
            "categories": categories,
        })

    return {
        "target_date": target_date.isoformat(),
        "start_date_7": start_date_7.isoformat(),
        "start_date_30": start_date_30.isoformat(),
        "general_avg": general_avg,
        "dow_averages": dow_averages,
        "last_7_days": last_7_days,
        "last_30_days": last_30_days,
    }

