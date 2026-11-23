"""
src/services/loyalty_service.py
-------------------------------
Core CRM & Loyalty Service (Phase 3):
- 4-Step atomic mathematical engine for transactions, proportional loyalty discount spreading,
  and immutable append-only loyalty_point_ledger movements.
- Retroactive birthday bonus calculation upon WhatsApp Flow onboarding completion.
- Transaction creation from LTDB UI (with optional immediate WMS reconciliation),
  transaction editing (with PRICE_CORRECTION and automatic manual_review reset),
  and grouping of existing ExcelSale rows into a Transaction.
- Customer CRM search, full profile detail with KPIs, demographic/consent updates,
  admin manual points adjustment, GDPR anonymization, and duplicate profile merge.
"""
import calendar
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.logger import get_logger
from src.models.crm import (
    Customer,
    CustomerStatus,
    LoyaltyLedgerReason,
    LoyaltyPointLedger,
    Transaction,
)
from src.models.excel_sales import ExcelSale
from src.models.pim import ArticlePhoto
from src.models.wms import Article, ArticleStatus, MovementReason
from src.repositories.crm_repo import crm_customer_repo
from src.repositories.wms_repo import wms_repo
from src.schemas.crm import (
    CustomerDetailOut,
    CustomerKpiOut,
    CustomerListItemOut,
    CustomerMergeRequest,
    CustomerPatchRequest,
    LoyaltyLedgerEntryOut,
    TransactionCreateRequest,
    TransactionFromExcelSalesRequest,
    TransactionItemIn,
    TransactionOut,
    TransactionSaleLineOut,
    TransactionUpdateRequest,
)
from src.schemas.excel_sales import ExcelSaleStatus
from src.schemas.wms import StockUpdate
from src.services.customer_onboarding_service import (
    CustomerAlreadyExistsError,
    CustomerNotFoundError,
    _add_months,
    _utc_now_naive,
    normalize_phone_number,
    normalize_title_case,
)
from src.services.search_service import invalidate_search_cache

logger = get_logger()

TWO_PLACES = Decimal("0.01")


class TransactionNotFoundError(Exception):
    """Raised when a Transaction ID is not found in the database."""

    pass


def _to_cents(value: Any) -> Decimal:
    """Normalizes a numeric value to a 2-decimal-place Decimal using ROUND_HALF_UP."""
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _birthday_in_year(birth_date: date, target_year: int) -> date:
    """Computes the birthday date in target_year, clamping Feb 29 to Feb 28 on non-leap years."""
    max_day = calendar.monthrange(target_year, birth_date.month)[1]
    return date(target_year, birth_date.month, min(birth_date.day, max_day))


def is_date_in_birthday_window(
    birth_date: Optional[date],
    target_date: date,
    window_days: Optional[int] = None,
) -> bool:
    """
    Checks whether target_date falls within [-window_days, +window_days] of the customer's birthday.
    Handles year boundaries seamlessly (e.g., Dec 30 birthday evaluated on Jan 1, or Jan 2 on Dec 31).
    """
    if birth_date is None:
        return False
    w_days = (
        int(window_days)
        if window_days is not None
        else int(settings.LOYALTY_BIRTHDAY_WINDOW_DAYS)
    )
    candidates = [
        _birthday_in_year(birth_date, target_date.year - 1),
        _birthday_in_year(birth_date, target_date.year),
        _birthday_in_year(birth_date, target_date.year + 1),
    ]
    min_distance = min(abs((target_date - bday).days) for bday in candidates)
    return min_distance <= w_days


def get_redemption_tier(discount_rule_code: str) -> Dict[str, Any]:
    """Finds a configured loyalty discount redemption tier by its code."""
    tiers = settings.LOYALTY_REDEMPTION_TIERS or []
    for tier in tiers:
        if isinstance(tier, dict) and tier.get("code") == discount_rule_code:
            return tier
    raise ValueError(f"Soglia di sconto punti non valida o inesistente: '{discount_rule_code}'")


def get_top_redeemable_tiers(points_balance: int, max_tiers: int = 3) -> List[Dict[str, Any]]:
    """
    Returns up to `max_tiers` highest-threshold loyalty discount tiers currently unlocked
    by `points_balance`, ordered from highest points_required descending.
    """
    tiers = [
        tier
        for tier in (settings.LOYALTY_REDEMPTION_TIERS or [])
        if isinstance(tier, dict) and int(tier.get("points_required", 0)) <= points_balance
    ]
    tiers.sort(key=lambda t: int(t.get("points_required", 0)), reverse=True)
    return tiers[:max_tiers]


def is_customer_eligible_for_birthday_bonus(
    db: Session,
    customer: Optional[Customer],
    tx_date: date,
) -> bool:
    """
    Evaluates the 3 conditions for the Birthday Bonus (x2 points):
    1. customer.status == ACTIVE and customer.birth_date is set.
    2. tx_date falls within [birthday - 3 days, birthday + 3 days] (including year wrap-around).
    3. In loyalty_point_ledger for tx_date.year, either no BIRTHDAY_BONUS movement exists,
       OR all existing BIRTHDAY_BONUS movements in tx_date.year belong to transactions
       with Transaction.date == tx_date (same purchase day).
    """
    if (
        not customer
        or customer.status != CustomerStatus.ACTIVE
        or customer.birth_date is None
    ):
        return False

    if not is_date_in_birthday_window(customer.birth_date, tx_date):
        return False

    used_dates = crm_customer_repo.get_birthday_bonus_transaction_dates_in_year(
        db, customer.id, tx_date.year
    )
    if not used_dates:
        return True
    return all(d == tx_date for d in used_dates)


# ---------------------------------------------------------------------------
# WMS Reconciliation Helpers for Transaction Sale Lines
# ---------------------------------------------------------------------------


def _get_or_create_movement_reason(db: Session, code: str, sign: int) -> str:
    reason = db.query(MovementReason).filter(MovementReason.code == code).first()
    if not reason:
        reason = MovementReason(code=code, sign=sign)
        db.add(reason)
        db.flush()
    return str(reason.id)


def _reconcile_sale_line_with_wms(
    db: Session,
    sale_line: ExcelSale,
    article_id: str,
) -> None:
    """
    Links an ExcelSale line to a physical WMS Article and updates ArticleStatus + ArticleMovement.
    """
    article = wms_repo.get(db, article_id)
    if not article:
        raise ValueError(f"Articolo WMS '{article_id}' non trovato")

    if not sale_line.is_exchange and article.status != ArticleStatus.AVAILABLE:
        raise ValueError(
            f"L'articolo '{article_id}' non è disponibile per la vendita (stato attuale: {article.status.value})"
        )
    if sale_line.is_exchange and article.status != ArticleStatus.SOLD:
        raise ValueError(
            f"L'articolo '{article_id}' non risulta venduto (stato attuale: {article.status.value}), impossibile registrare un reso"
        )

    if not sale_line.is_exchange:
        article.status = ArticleStatus.SOLD
        reason_id = _get_or_create_movement_reason(db, "EXCEL_SALE", -1)
    else:
        article.status = ArticleStatus.AVAILABLE
        reason_id = _get_or_create_movement_reason(db, "EXCEL_RETURN", 1)

    db.add(article)
    db.flush()

    movement_in = StockUpdate(
        article_id=str(article.id),
        reason_id=reason_id,
        notes=f"Reconciled from Transaction ExcelSale {sale_line.id}",
    )
    wms_repo.add_movement(db, movement_in, commit_changes=False)

    sale_line.article_id = str(article.id)
    sale_line.status = ExcelSaleStatus.RECONCILED


def _unreconcile_sale_line_from_wms(
    db: Session,
    sale_line: ExcelSale,
) -> None:
    """
    Reverts WMS Article status and records a reversal movement when a reconciled ExcelSale
    line is removed or its article_id / is_exchange is changed during a transaction update.
    """
    if not sale_line.article_id or sale_line.status != ExcelSaleStatus.RECONCILED:
        sale_line.article_id = None
        sale_line.status = ExcelSaleStatus.ORPHAN
        return

    article = wms_repo.get(db, sale_line.article_id)
    if article:
        if not sale_line.is_exchange:
            # Revert sale -> back to AVAILABLE
            article.status = ArticleStatus.AVAILABLE
            reason_id = _get_or_create_movement_reason(db, "EXCEL_RETURN", 1)
        else:
            # Revert return -> back to SOLD
            article.status = ArticleStatus.SOLD
            reason_id = _get_or_create_movement_reason(db, "EXCEL_SALE", -1)
        db.add(article)
        db.flush()

        movement_in = StockUpdate(
            article_id=str(article.id),
            reason_id=reason_id,
            notes=f"Unreconciled from Transaction ExcelSale {sale_line.id}",
        )
        wms_repo.add_movement(db, movement_in, commit_changes=False)

    sale_line.article_id = None
    sale_line.status = ExcelSaleStatus.ORPHAN


# ---------------------------------------------------------------------------
# 4-Step Mathematical Engine: Loyalty Discount Spreading & Immutable Ledger
# ---------------------------------------------------------------------------


def restore_pre_loyalty_prices(transaction: Transaction) -> None:
    """
    Step 1 — Restores pre-loyalty selling_price on all purchase rows (is_exchange == False)
    if the transaction currently has an active loyalty_discount_amount > 0.
    Absorbs any rounding cent remainder on the last purchase row.
    """
    old_discount = _to_cents(transaction.loyalty_discount_amount)
    if old_discount <= Decimal("0.00"):
        return

    purchase_rows = [r for r in transaction.excel_sales if not r.is_exchange]
    if not purchase_rows:
        transaction.loyalty_discount_amount = Decimal("0.00")
        return

    current_prices = [_to_cents(r.selling_price) for r in purchase_rows]
    current_sum = sum(current_prices, Decimal("0.00"))

    if current_sum > Decimal("0.00"):
        weights = current_prices
        weight_sum = current_sum
    else:
        # Fallback if a 100% / fixed discount reduced all selling_price values to 0.00
        starting_prices = [_to_cents(r.starting_price) for r in purchase_rows]
        starting_sum = sum(starting_prices, Decimal("0.00"))
        if starting_sum > Decimal("0.00"):
            weights = starting_prices
            weight_sum = starting_sum
        else:
            weights = [Decimal("1.00") for _ in purchase_rows]
            weight_sum = Decimal(len(purchase_rows))

    # Absorb rounding cent remainder on the purchase row with the highest weight (tie-break: last row)
    remainder_idx = max(range(len(purchase_rows)), key=lambda i: (weights[i], i))
    deltas: List[Decimal] = [Decimal("0.00")] * len(purchase_rows)
    allocated_sum = Decimal("0.00")
    for idx in range(len(purchase_rows)):
        if idx == remainder_idx:
            continue
        delta_i = _to_cents(old_discount * (weights[idx] / weight_sum))
        deltas[idx] = delta_i
        allocated_sum += delta_i
    deltas[remainder_idx] = _to_cents(old_discount - allocated_sum)

    for idx, row in enumerate(purchase_rows):
        row.selling_price = float(_to_cents(row.selling_price) + deltas[idx])

    transaction.loyalty_discount_amount = Decimal("0.00")


def _apply_loyalty_engine(
    db: Session,
    transaction: Transaction,
    target_discount_rule_code: Optional[str],
    previous_customer_id: Optional[str] = None,
    now: Optional[datetime] = None,
    preserve_manual_prices: bool = False,
) -> None:
    """
    Executes Steps 1 to 4 of the Loyalty Mathematical Engine inside the current DB transaction:
    - Step 1: Restores pre-loyalty selling prices if transaction.loyalty_discount_amount > 0
      (skipped when `preserve_manual_prices=True` for direct Excel price edits on confirmed sales).
    - Step 2: Computes net discountable total T_netto = S_acq - S_resi.
    - Step 3: Validates and spreads loyalty discount proportionally across purchase rows
      (when `preserve_manual_prices=False`).
    - Step 4: Writes immutable entries to loyalty_point_ledger (or PRICE_CORRECTION on edit),
      preventing duplicate DISCOUNT_REDEMPTION or PURCHASE_EARN entries.
    """
    current_dt = now or _utc_now_naive()
    prior_discount_rule_code = transaction.discount_rule_code

    # Step 1: Restore pre-loyalty prices if transaction still carries old_discount > 0
    if not preserve_manual_prices and _to_cents(transaction.loyalty_discount_amount) > Decimal("0.00"):
        restore_pre_loyalty_prices(transaction)

    # Fetch and validate target customer if any
    customer: Optional[Customer] = None
    if transaction.customer_id:
        customer = crm_customer_repo.get(db, transaction.customer_id)
        if not customer:
            raise CustomerNotFoundError(f"Cliente '{transaction.customer_id}' non trovato")
        if customer.status == CustomerStatus.ANONYMIZED:
            raise ValueError(
                "Impossibile associare una transazione a un profilo cliente anonimizzato"
            )

    existing_tx_entries: List[LoyaltyPointLedger] = []
    if customer:
        existing_tx_entries = (
            db.query(LoyaltyPointLedger)
            .filter(
                LoyaltyPointLedger.customer_id == customer.id,
                LoyaltyPointLedger.transaction_id == transaction.id,
            )
            .all()
        )
    existing_redemption_entries = [
        e for e in existing_tx_entries if e.reason == LoyaltyLedgerReason.DISCOUNT_REDEMPTION
    ]
    has_redemption_entry = len(existing_redemption_entries) > 0
    existing_redeemed_points = sum(
        abs(int(e.points_delta)) for e in existing_redemption_entries
    )

    purchase_rows = [r for r in transaction.excel_sales if not r.is_exchange]
    return_rows = [r for r in transaction.excel_sales if r.is_exchange]

    # Step 2: Compute Net Discountable Total (T_netto = S_acq - S_resi)
    s_acq = sum((_to_cents(r.selling_price) for r in purchase_rows), Decimal("0.00"))
    s_resi = sum((abs(_to_cents(r.selling_price)) for r in return_rows), Decimal("0.00"))
    t_netto = _to_cents(s_acq - s_resi)

    # Step 3: Validate and Spread Loyalty Discount
    tier: Optional[Dict[str, Any]] = None
    discount_amount = Decimal("0.00")

    if preserve_manual_prices:
        # Excel is the single source of truth for row prices on manual post-confirmation price edits:
        # keep existing row prices and keep any already-redeemed discount rule/amount immutable.
        if transaction.discount_rule_code:
            try:
                tier = get_redemption_tier(transaction.discount_rule_code)
            except ValueError:
                tier = None
    elif target_discount_rule_code:
        if not customer:
            raise ValueError(
                "Per applicare uno sconto punti è necessario associare un cliente alla transazione"
            )
        if customer.status != CustomerStatus.ACTIVE:
            raise ValueError(
                f"Lo sconto punti può essere riscattato solo da clienti in stato ACTIVE (stato attuale: {customer.status.value})"
            )

        tier = get_redemption_tier(target_discount_rule_code)
        points_required = int(tier["points_required"])

        is_same_already_redeemed_rule = (
            has_redemption_entry and prior_discount_rule_code == target_discount_rule_code
        )
        if not is_same_already_redeemed_rule:
            # Available points excluding any movements already recorded on this transaction
            available_points = crm_customer_repo.get_points_balance(
                db, customer.id, exclude_transaction_id=transaction.id
            )
            if available_points < points_required:
                raise ValueError(
                    f"Saldo punti insufficiente per '{tier['code']}': richiesti {points_required} pt, disponibili {available_points} pt"
                )

        if not purchase_rows or t_netto <= Decimal("0.00") or s_acq <= Decimal("0.00"):
            raise ValueError(
                "Impossibile applicare uno sconto punti: il totale scontabile netto (Acquisti - Resi) deve essere maggiore di zero e deve esistere almeno una riga di acquisto"
            )

        discount_type = str(tier["discount_type"]).upper()
        discount_value = Decimal(str(tier["discount_value"]))

        if discount_type == "PERCENTAGE":
            discount_amount = _to_cents(t_netto * (discount_value / Decimal("100")))
        elif discount_type == "FIXED":
            discount_amount = min(_to_cents(discount_value), t_netto)
        else:
            raise ValueError(f"Tipo di sconto non supportato: '{discount_type}'")

        # Proportional spreading across purchase_rows (is_exchange == False),
        # absorbing any rounding cent remainder on the highest-priced purchase row (tie-break: last row)
        row_prices = [_to_cents(r.selling_price) for r in purchase_rows]
        remainder_idx = max(range(len(purchase_rows)), key=lambda i: (row_prices[i], i))
        discounts: List[Decimal] = [Decimal("0.00")] * len(purchase_rows)
        allocated_discount = Decimal("0.00")
        for idx in range(len(purchase_rows)):
            if idx == remainder_idx:
                continue
            d_i = _to_cents(discount_amount * (row_prices[idx] / s_acq))
            discounts[idx] = d_i
            allocated_discount += d_i
        discounts[remainder_idx] = _to_cents(discount_amount - allocated_discount)

        for idx, row in enumerate(purchase_rows):
            row.selling_price = float(_to_cents(row_prices[idx] - discounts[idx]))
            db.add(row)

        transaction.loyalty_discount_amount = discount_amount
        transaction.discount_rule_code = str(tier["code"])
    else:
        transaction.loyalty_discount_amount = Decimal("0.00")
        transaction.discount_rule_code = None

    db.add(transaction)
    db.flush()

    # Step 4: Immutable writing to loyalty_point_ledger
    # 4.a If the transaction was previously linked to a different customer, neutralize their ledger balance for this tx
    if previous_customer_id and previous_customer_id != transaction.customer_id:
        old_entries = (
            db.query(LoyaltyPointLedger)
            .filter(
                LoyaltyPointLedger.customer_id == previous_customer_id,
                LoyaltyPointLedger.transaction_id == transaction.id,
            )
            .all()
        )
        old_sum = sum(int(e.points_delta) for e in old_entries)
        if old_sum != 0:
            db.add(
                LoyaltyPointLedger(
                    customer_id=previous_customer_id,
                    transaction_id=transaction.id,
                    points_delta=-old_sum,
                    reason=LoyaltyLedgerReason.PRICE_CORRECTION,
                    created_at=current_dt,
                )
            )

    if not customer:
        db.flush()
        return

    # Compute target points for the current customer on this transaction (post-discount)
    euro_per_point = Decimal(str(settings.LOYALTY_EARNING_EURO_PER_POINT))
    post_s_acq = sum((_to_cents(r.selling_price) for r in purchase_rows), Decimal("0.00"))
    post_s_resi = sum((abs(_to_cents(r.selling_price)) for r in return_rows), Decimal("0.00"))

    p_acq = int(post_s_acq // euro_per_point) if post_s_acq > Decimal("0.00") else 0
    p_resi = int(post_s_resi // euro_per_point) if post_s_resi > Decimal("0.00") else 0

    if transaction.discount_rule_code:
        if has_redemption_entry and prior_discount_rule_code == transaction.discount_rule_code:
            p_redeem = existing_redeemed_points
        elif tier is not None:
            p_redeem = int(tier["points_required"])
        else:
            p_redeem = existing_redeemed_points
    else:
        p_redeem = 0

    eligible_bday = is_customer_eligible_for_birthday_bonus(db, customer, transaction.date)
    bday_extra_multiplier = max(0, int(settings.LOYALTY_BIRTHDAY_MULTIPLIER) - 1)
    p_bday = (p_acq * bday_extra_multiplier) if (eligible_bday and p_acq > 0) else 0

    if not existing_tx_entries:
        if p_redeem > 0:
            db.add(
                LoyaltyPointLedger(
                    customer_id=customer.id,
                    transaction_id=transaction.id,
                    points_delta=-p_redeem,
                    reason=LoyaltyLedgerReason.DISCOUNT_REDEMPTION,
                    created_at=current_dt,
                )
            )
        if p_acq > 0:
            db.add(
                LoyaltyPointLedger(
                    customer_id=customer.id,
                    transaction_id=transaction.id,
                    points_delta=p_acq,
                    reason=LoyaltyLedgerReason.PURCHASE_EARN,
                    created_at=current_dt,
                )
            )
        if p_resi > 0:
            db.add(
                LoyaltyPointLedger(
                    customer_id=customer.id,
                    transaction_id=transaction.id,
                    points_delta=-p_resi,
                    reason=LoyaltyLedgerReason.RETURN_REVERSAL,
                    created_at=current_dt,
                )
            )
        if p_bday > 0:
            db.add(
                LoyaltyPointLedger(
                    customer_id=customer.id,
                    transaction_id=transaction.id,
                    points_delta=p_bday,
                    reason=LoyaltyLedgerReason.BIRTHDAY_BONUS,
                    created_at=current_dt,
                )
            )
    else:
        p_nuovo = (-p_redeem) + p_acq - p_resi + p_bday
        p_attuale = sum(int(e.points_delta) for e in existing_tx_entries)
        if p_redeem > 0 and not has_redemption_entry:
            db.add(
                LoyaltyPointLedger(
                    customer_id=customer.id,
                    transaction_id=transaction.id,
                    points_delta=-p_redeem,
                    reason=LoyaltyLedgerReason.DISCOUNT_REDEMPTION,
                    created_at=current_dt,
                )
            )
            p_attuale -= p_redeem

        delta = p_nuovo - p_attuale
        if delta != 0:
            db.add(
                LoyaltyPointLedger(
                    customer_id=customer.id,
                    transaction_id=transaction.id,
                    points_delta=delta,
                    reason=LoyaltyLedgerReason.PRICE_CORRECTION,
                    created_at=current_dt,
                )
            )

    # Update customer's last_purchase_date and 60-month rolling retention window
    if customer.last_purchase_date is None or transaction.date >= customer.last_purchase_date:
        customer.last_purchase_date = transaction.date
        if customer.status == CustomerStatus.ACTIVE:
            anchor_dt = datetime(
                transaction.date.year,
                transaction.date.month,
                transaction.date.day,
                current_dt.hour,
                current_dt.minute,
                current_dt.second,
            )
            customer.retention_expires_at = _add_months(
                anchor_dt, settings.WHATSAPP_RETENTION_MONTHS
            )
        db.add(customer)

    db.flush()


def credit_retroactive_birthday_bonus_on_activation(
    db: Session,
    customer: Customer,
    activation_date: date,
    now: Optional[datetime] = None,
) -> int:
    """
    Called when a PENDING_ONBOARDING customer completes the WhatsApp Flow and becomes ACTIVE.
    If the customer performed one or more transactions on `activation_date` while still
    PENDING_ONBOARDING and their newly registered `birth_date` falls within the birthday
    window (+/- 3 days), automatically credits the BIRTHDAY_BONUS movements for those transactions.
    Returns total bonus points credited.
    """
    current_dt = now or _utc_now_naive()
    if not is_customer_eligible_for_birthday_bonus(db, customer, activation_date):
        return 0

    same_day_txs = (
        db.query(Transaction)
        .filter(
            Transaction.customer_id == customer.id,
            Transaction.date == activation_date,
        )
        .order_by(Transaction.created_at.asc())
        .all()
    )
    if not same_day_txs:
        return 0

    euro_per_point = Decimal(str(settings.LOYALTY_EARNING_EURO_PER_POINT))
    bday_extra_multiplier = max(0, int(settings.LOYALTY_BIRTHDAY_MULTIPLIER) - 1)
    if bday_extra_multiplier <= 0:
        return 0

    total_bonus_credited = 0
    for tx in same_day_txs:
        already_has_bday = (
            db.query(LoyaltyPointLedger)
            .filter(
                LoyaltyPointLedger.customer_id == customer.id,
                LoyaltyPointLedger.transaction_id == tx.id,
                LoyaltyPointLedger.reason == LoyaltyLedgerReason.BIRTHDAY_BONUS,
            )
            .first()
        )
        if already_has_bday:
            continue

        purchase_rows = [r for r in tx.excel_sales if not r.is_exchange]
        post_s_acq = sum((_to_cents(r.selling_price) for r in purchase_rows), Decimal("0.00"))
        p_acq = int(post_s_acq // euro_per_point) if post_s_acq > Decimal("0.00") else 0
        p_bday = p_acq * bday_extra_multiplier

        if p_bday > 0:
            db.add(
                LoyaltyPointLedger(
                    customer_id=customer.id,
                    transaction_id=tx.id,
                    points_delta=p_bday,
                    reason=LoyaltyLedgerReason.BIRTHDAY_BONUS,
                    created_at=current_dt,
                )
            )
            total_bonus_credited += p_bday

    if total_bonus_credited > 0:
        db.flush()
        logger.log_execution(
            "loyalty_service",
            "retroactive_birthday_bonus_credited",
            "ok",
            customer_id=customer.id,
            activation_date=str(activation_date),
            bonus_points=total_bonus_credited,
        )

    return total_bonus_credited


# ---------------------------------------------------------------------------
# Serialization Helpers for TransactionOut & Sale Lines
# ---------------------------------------------------------------------------


def _serialize_sale_line(db: Session, row: ExcelSale) -> TransactionSaleLineOut:
    article_name: Optional[str] = None
    brand_name: Optional[str] = None
    category_name: Optional[str] = None
    photo_url: Optional[str] = None

    if row.article_id and row.article:
        blueprint = row.article.blueprint
        if blueprint:
            article_name = blueprint.article_name
            brand_name = blueprint.brand.name if blueprint.brand else None
            category_name = blueprint.category.name if blueprint.category else None

            colors_list = [str(c).lower() for c in (row.article.colors or []) if c]
            photo = None
            if colors_list:
                photo = (
                    db.query(ArticlePhoto)
                    .filter(
                        ArticlePhoto.article_blueprint_id == blueprint.id,
                        func.lower(ArticlePhoto.canonical_color_name).in_(colors_list),
                    )
                    .first()
                )
            if not photo:
                photo = (
                    db.query(ArticlePhoto)
                    .filter(ArticlePhoto.article_blueprint_id == blueprint.id)
                    .first()
                )
            if photo:
                photo_url = f"/api/v1/ingestion/photos/{photo.id}"

    return TransactionSaleLineOut(
        id=str(row.id),
        date=row.date,
        excel_row_index=row.excel_row_index,
        excel_file_column=row.excel_file_column,
        status=row.status,
        starting_price=_to_cents(row.starting_price) if row.starting_price is not None else None,
        selling_price=_to_cents(row.selling_price) if row.selling_price is not None else None,
        is_exchange=bool(row.is_exchange),
        raw_article_code=row.raw_article_code,
        article_id=row.article_id,
        transaction_id=row.transaction_id,
        manual_review=bool(row.manual_review),
        created_at=row.created_at,
        updated_at=row.updated_at,
        article_name=article_name,
        brand_name=brand_name,
        category_name=category_name,
        photo_url=photo_url,
    )


def serialize_transaction(db: Session, tx: Transaction) -> TransactionOut:
    """
    Builds a full TransactionOut representation including Approach 1 economic aggregates:
    - net_list_total = Sum(acq starting_price) - Sum(resi starting_price)
    - net_payable = Sum(acq selling_price) - Sum(resi selling_price)
    - base_cash_discount = (Sum(acq starting_price) - Sum(acq selling_price)) - loyalty_discount_amount
    """
    indexed_lines = list(enumerate(tx.excel_sales))
    indexed_lines.sort(
        key=lambda pair: (
            0 if pair[1].excel_row_index is not None else 1,
            pair[1].excel_row_index if pair[1].excel_row_index is not None else pair[0],
        )
    )
    lines = [pair[1] for pair in indexed_lines]
    items_out = [_serialize_sale_line(db, r) for r in lines]

    acq_starting = sum(
        (_to_cents(r.starting_price) for r in lines if not r.is_exchange), Decimal("0.00")
    )
    resi_starting = sum(
        (abs(_to_cents(r.starting_price)) for r in lines if r.is_exchange), Decimal("0.00")
    )
    acq_selling = sum(
        (_to_cents(r.selling_price) for r in lines if not r.is_exchange), Decimal("0.00")
    )
    resi_selling = sum(
        (abs(_to_cents(r.selling_price)) for r in lines if r.is_exchange), Decimal("0.00")
    )

    loyalty_disc = _to_cents(tx.loyalty_discount_amount)
    net_list_total = _to_cents(acq_starting - resi_starting)
    net_payable = _to_cents(acq_selling - resi_selling)
    base_cash_discount = _to_cents((acq_starting - acq_selling) - loyalty_disc)

    # Filter ledger entries belonging to the transaction's current customer (or all if unlinked)
    tx_ledger_entries = sorted(
        tx.loyalty_ledger_entries,
        key=lambda e: (e.created_at or datetime.min, str(e.id)),
    )
    ledger_out = [LoyaltyLedgerEntryOut.model_validate(e) for e in tx_ledger_entries]

    active_cust_entries = [
        e
        for e in tx_ledger_entries
        if tx.customer_id is None or e.customer_id == tx.customer_id
    ]
    points_redeemed_total = sum(
        abs(int(e.points_delta))
        for e in active_cust_entries
        if e.reason == LoyaltyLedgerReason.DISCOUNT_REDEMPTION
    )
    points_net_delta = sum(int(e.points_delta) for e in active_cust_entries)
    points_earned_total = points_net_delta + points_redeemed_total

    return TransactionOut(
        id=str(tx.id),
        date=tx.date,
        customer_id=tx.customer_id,
        loyalty_discount_amount=loyalty_disc,
        discount_rule_code=tx.discount_rule_code,
        identification_status=tx.identification_status,
        net_list_total=net_list_total,
        net_payable=net_payable,
        base_cash_discount=base_cash_discount,
        points_earned_total=points_earned_total,
        points_redeemed_total=points_redeemed_total,
        points_net_delta=points_net_delta,
        items=items_out,
        ledger_entries=ledger_out,
        created_at=tx.created_at,
        updated_at=tx.updated_at,
    )


# ---------------------------------------------------------------------------
# Transaction CRUD & ExcelSale Grouping Services
# ---------------------------------------------------------------------------


def get_transaction_detail(db: Session, transaction_id: str) -> TransactionOut:
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise TransactionNotFoundError(f"Transazione '{transaction_id}' non trovata")
    return serialize_transaction(db, tx)


def create_transaction(
    db: Session,
    payload: TransactionCreateRequest,
    now: Optional[datetime] = None,
) -> TransactionOut:
    """
    Creates a new Transaction from LTDB UI with one or more sale/return lines:
    - Each ExcelSale row is created with excel_row_index = None.
    - If article_id is provided on an item, performs immediate WMS reconciliation.
    - Runs the 4-step loyalty discount spreading and ledger engine atomically.
    """
    current_dt = now or _utc_now_naive()
    tx_date = payload.date or current_dt.date()

    tx = Transaction(
        date=tx_date,
        customer_id=payload.customer_id,
        loyalty_discount_amount=Decimal("0.00"),
        discount_rule_code=None,
    )
    db.add(tx)
    db.flush()

    reconciled_any_wms = False
    for item_in in payload.items:
        sale_row = ExcelSale(
            date=tx_date,
            excel_row_index=None,
            excel_file_column=item_in.excel_file_column,
            status=ExcelSaleStatus.ORPHAN,
            starting_price=float(_to_cents(item_in.starting_price)),
            selling_price=float(_to_cents(item_in.selling_price)),
            is_exchange=bool(item_in.is_exchange),
            raw_article_code=item_in.raw_article_code,
            transaction_id=tx.id,
            manual_review=False,
        )
        db.add(sale_row)
        db.flush()

        if item_in.article_id:
            _reconcile_sale_line_with_wms(db, sale_row, item_in.article_id)
            reconciled_any_wms = True

    db.refresh(tx)

    _apply_loyalty_engine(
        db=db,
        transaction=tx,
        target_discount_rule_code=payload.discount_rule_code,
        previous_customer_id=None,
        now=current_dt,
    )

    db.commit()
    db.refresh(tx)
    if reconciled_any_wms:
        invalidate_search_cache()

    logger.log_execution(
        "loyalty_service",
        "create_transaction",
        "ok",
        transaction_id=tx.id,
        customer_id=tx.customer_id,
        items_count=len(tx.excel_sales),
        discount_rule_code=tx.discount_rule_code,
    )
    return serialize_transaction(db, tx)


def update_transaction(
    db: Session,
    transaction_id: str,
    payload: TransactionUpdateRequest,
    now: Optional[datetime] = None,
) -> TransactionOut:
    """
    Updates an existing Transaction from LTDB:
    - Restores pre-loyalty prices on existing lines (Step 1).
    - Applies optional updates to date, customer_id, discount_rule_code, and items.
    - Resets manual_review = False on all ExcelSale lines belonging to the transaction.
    - Recalculates loyalty discount spreading and appends PRICE_CORRECTION to the ledger.
    """
    current_dt = now or _utc_now_naive()
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise TransactionNotFoundError(f"Transazione '{transaction_id}' non trovata")

    previous_customer_id = tx.customer_id
    fields_set = payload.model_fields_set

    # Step 1 upfront: restore pre-loyalty prices on existing lines before any item/discount edits
    if _to_cents(tx.loyalty_discount_amount) > Decimal("0.00"):
        restore_pre_loyalty_prices(tx)

    if "date" in fields_set and payload.date is not None:
        tx.date = payload.date
        for r in tx.excel_sales:
            r.date = payload.date

    if "customer_id" in fields_set:
        tx.customer_id = payload.customer_id

    target_discount_rule_code = (
        payload.discount_rule_code
        if "discount_rule_code" in fields_set
        else tx.discount_rule_code
    )
    # If customer is removed, discount_rule_code must also be cleared unless explicitly invalid
    if tx.customer_id is None and "discount_rule_code" not in fields_set:
        target_discount_rule_code = None

    reconciled_any_wms = False
    if "items" in fields_set and payload.items is not None:
        if not payload.items:
            raise ValueError("Una transazione deve contenere almeno una riga di vendita o reso")

        existing_by_id = {str(r.id): r for r in tx.excel_sales}
        kept_ids = set()

        for item_in in payload.items:
            if item_in.id and item_in.id in existing_by_id:
                row = existing_by_id[item_in.id]
                kept_ids.add(str(row.id))

                # Handle WMS changes if article_id or is_exchange changed
                if (
                    row.article_id != item_in.article_id
                    or bool(row.is_exchange) != bool(item_in.is_exchange)
                ):
                    if row.article_id:
                        _unreconcile_sale_line_from_wms(db, row)
                        reconciled_any_wms = True
                    row.is_exchange = bool(item_in.is_exchange)
                    if item_in.article_id:
                        _reconcile_sale_line_with_wms(db, row, item_in.article_id)
                        reconciled_any_wms = True

                row.date = tx.date
                row.excel_file_column = item_in.excel_file_column
                row.starting_price = float(_to_cents(item_in.starting_price))
                row.selling_price = float(_to_cents(item_in.selling_price))
                row.is_exchange = bool(item_in.is_exchange)
                row.raw_article_code = item_in.raw_article_code
                row.manual_review = False
                db.add(row)
            else:
                new_row = ExcelSale(
                    date=tx.date,
                    excel_row_index=None,
                    excel_file_column=item_in.excel_file_column,
                    status=ExcelSaleStatus.ORPHAN,
                    starting_price=float(_to_cents(item_in.starting_price)),
                    selling_price=float(_to_cents(item_in.selling_price)),
                    is_exchange=bool(item_in.is_exchange),
                    raw_article_code=item_in.raw_article_code,
                    transaction_id=tx.id,
                    manual_review=False,
                )
                db.add(new_row)
                db.flush()
                if item_in.article_id:
                    _reconcile_sale_line_with_wms(db, new_row, item_in.article_id)
                    reconciled_any_wms = True
                kept_ids.add(str(new_row.id))

        # Remove lines that were omitted from payload.items
        for old_id, old_row in list(existing_by_id.items()):
            if old_id not in kept_ids:
                if old_row.article_id:
                    _unreconcile_sale_line_from_wms(db, old_row)
                    reconciled_any_wms = True
                db.delete(old_row)

        db.flush()
        db.refresh(tx)

    # Always reset manual_review = False on all lines of the transaction upon a legitimate LTDB PATCH
    for row in tx.excel_sales:
        if row.manual_review:
            row.manual_review = False
            db.add(row)

    _apply_loyalty_engine(
        db=db,
        transaction=tx,
        target_discount_rule_code=target_discount_rule_code,
        previous_customer_id=previous_customer_id,
        now=current_dt,
    )

    db.commit()
    db.refresh(tx)
    if reconciled_any_wms:
        invalidate_search_cache()

    logger.log_execution(
        "loyalty_service",
        "update_transaction",
        "ok",
        transaction_id=tx.id,
        customer_id=tx.customer_id,
        discount_rule_code=tx.discount_rule_code,
    )
    return serialize_transaction(db, tx)


def create_transaction_from_excel_sales(
    db: Session,
    payload: TransactionFromExcelSalesRequest,
    now: Optional[datetime] = None,
) -> TransactionOut:
    """
    Groups one or more existing ExcelSale rows (with transaction_id == None) into a new Transaction,
    associating them with an optional customer_id and discount_rule_code.
    """
    current_dt = now or _utc_now_naive()

    # Preserve input order while deduplicating IDs
    unique_ids: List[str] = []
    for sid in payload.excel_sale_ids:
        if sid not in unique_ids:
            unique_ids.append(sid)

    rows = db.query(ExcelSale).filter(ExcelSale.id.in_(unique_ids)).all()
    rows_by_id = {str(r.id): r for r in rows}

    if len(rows_by_id) != len(unique_ids):
        missing = [sid for sid in unique_ids if sid not in rows_by_id]
        raise ValueError(f"Righe vendita ExcelSale non trovate: {missing}")

    ordered_rows = [rows_by_id[sid] for sid in unique_ids]
    for r in ordered_rows:
        if r.transaction_id is not None:
            raise ValueError(
                f"La riga vendita '{r.id}' appartiene già alla transazione '{r.transaction_id}'"
            )
        if r.status == ExcelSaleStatus.UNPROCESSABLE:
            raise ValueError(
                f"La riga vendita '{r.id}' è in stato UNPROCESSABLE e non può essere inserita in una transazione"
            )

    distinct_dates = {r.date for r in ordered_rows}
    if len(distinct_dates) > 1:
        raise ValueError(
            "Tutte le righe ExcelSale raggruppate in una transazione devono appartenere alla stessa data contabile"
        )

    tx_date = ordered_rows[0].date
    tx = Transaction(
        date=tx_date,
        customer_id=payload.customer_id,
        loyalty_discount_amount=Decimal("0.00"),
        discount_rule_code=None,
    )
    db.add(tx)
    db.flush()

    for r in ordered_rows:
        r.transaction_id = tx.id
        r.manual_review = False
        db.add(r)

    db.flush()
    db.refresh(tx)

    _apply_loyalty_engine(
        db=db,
        transaction=tx,
        target_discount_rule_code=payload.discount_rule_code,
        previous_customer_id=None,
        now=current_dt,
    )

    db.commit()
    db.refresh(tx)

    logger.log_execution(
        "loyalty_service",
        "create_transaction_from_excel_sales",
        "ok",
        transaction_id=tx.id,
        customer_id=tx.customer_id,
        grouped_sales_count=len(ordered_rows),
        discount_rule_code=tx.discount_rule_code,
    )
    return serialize_transaction(db, tx)


# ---------------------------------------------------------------------------
# Customer CRM Endpoints Business Logic
# ---------------------------------------------------------------------------


def list_customers(
    db: Session,
    q: Optional[str] = None,
    status: Optional[CustomerStatus] = None,
    birthday_this_week: bool = False,
    today: Optional[date] = None,
) -> List[CustomerListItemOut]:
    """
    Searches and filters customers, enriching each item with dynamically calculated
    `points_balance` and `is_birthday_window_active`.
    """
    current_date = today or _utc_now_naive().date()
    customers = crm_customer_repo.search_customers(db, q=q, status=status)
    balances_map = crm_customer_repo.get_points_balances_map(
        db, [str(c.id) for c in customers]
    )

    results: List[CustomerListItemOut] = []
    for c in customers:
        bday_active = (
            c.status == CustomerStatus.ACTIVE
            and is_date_in_birthday_window(c.birth_date, current_date)
        )
        if birthday_this_week and not bday_active:
            continue

        item = CustomerListItemOut.model_validate(c)
        item.points_balance = balances_map.get(str(c.id), 0)
        item.is_birthday_window_active = bday_active
        results.append(item)

    return results


def get_customer_detail(
    db: Session,
    customer_id: str,
    today: Optional[date] = None,
) -> CustomerDetailOut:
    """
    Returns full customer profile including dynamic points_balance, KPIs
    (total_spent, transactions_count, avg_ticket), transaction history with sale lines
    and PIM metadata, and immutable loyalty_point_ledger history.
    """
    current_date = today or _utc_now_naive().date()
    customer = crm_customer_repo.get(db, customer_id)
    if not customer:
        raise CustomerNotFoundError(f"Cliente '{customer_id}' non trovato")

    points_balance = crm_customer_repo.get_points_balance(db, customer.id)
    bday_active = (
        customer.status == CustomerStatus.ACTIVE
        and is_date_in_birthday_window(customer.birth_date, current_date)
    )

    tx_records = crm_customer_repo.get_customer_transactions(db, customer.id)
    transactions_out = [serialize_transaction(db, tx) for tx in tx_records]

    tx_count = len(transactions_out)
    total_spent = sum((t.net_payable for t in transactions_out), Decimal("0.00"))
    avg_ticket = (
        _to_cents(total_spent / Decimal(tx_count))
        if tx_count > 0
        else Decimal("0.00")
    )

    ledger_records = crm_customer_repo.get_customer_ledger(db, customer.id)
    ledger_out = [LoyaltyLedgerEntryOut.model_validate(e) for e in ledger_records]

    detail = CustomerDetailOut.model_validate(customer)
    detail.points_balance = points_balance
    detail.is_birthday_window_active = bday_active
    detail.kpi = CustomerKpiOut(
        points_balance=points_balance,
        total_spent=_to_cents(total_spent),
        transactions_count=tx_count,
        avg_ticket=avg_ticket,
    )
    detail.transactions = transactions_out
    detail.loyalty_ledger = ledger_out
    return detail


def update_customer(
    db: Session,
    customer_id: str,
    payload: CustomerPatchRequest,
    now: Optional[datetime] = None,
    today: Optional[date] = None,
) -> CustomerDetailOut:
    """
    Updates customer demographic data (normalized in Title Case / sterilized phone)
    and GDPR marketing/profiling consent flags.
    """
    current_dt = now or _utc_now_naive()
    current_date = today or current_dt.date()

    customer = crm_customer_repo.get(db, customer_id)
    if not customer:
        raise CustomerNotFoundError(f"Cliente '{customer_id}' non trovato")

    if customer.status == CustomerStatus.ANONYMIZED:
        raise ValueError("Impossibile modificare un profilo cliente anonimizzato")

    fields_set = payload.model_fields_set

    if "phone_number" in fields_set and payload.phone_number is not None:
        new_phone = normalize_phone_number(payload.phone_number)
        if new_phone != customer.phone_number:
            existing = crm_customer_repo.get_by_phone_number(db, new_phone)
            if existing and existing.id != customer.id:
                raise CustomerAlreadyExistsError(existing)
            customer.phone_number = new_phone

    if "first_name" in fields_set and payload.first_name is not None:
        customer.first_name = normalize_title_case(payload.first_name, "first_name")

    if "last_name" in fields_set and payload.last_name is not None:
        customer.last_name = normalize_title_case(payload.last_name, "last_name")

    if "address" in fields_set and payload.address is not None:
        customer.address = normalize_title_case(payload.address, "address")

    if "birth_date" in fields_set and payload.birth_date is not None:
        bdate = payload.birth_date
        age = (
            current_date.year
            - bdate.year
            - ((current_date.month, current_date.day) < (bdate.month, bdate.day))
        )
        if age < 18:
            raise ValueError(
                "Data di nascita non valida: il cliente deve essere maggiorenne (età >= 18 anni)"
            )
        customer.birth_date = bdate

    consents_changed = False
    if "consent_marketing" in fields_set and payload.consent_marketing is not None:
        customer.consent_marketing = bool(payload.consent_marketing)
        consents_changed = True

    if "consent_profiling" in fields_set and payload.consent_profiling is not None:
        customer.consent_profiling = bool(payload.consent_profiling)
        consents_changed = True

    if consents_changed:
        customer.consent_updated_at = current_dt

    db.add(customer)
    db.commit()
    db.refresh(customer)

    logger.log_execution(
        "loyalty_service",
        "update_customer",
        "ok",
        customer_id=customer.id,
    )
    return get_customer_detail(db, customer.id, today=current_date)


def adjust_customer_points(
    db: Session,
    customer_id: str,
    points_delta: int,
    now: Optional[datetime] = None,
) -> CustomerDetailOut:
    """
    Admin operation: appends an immutable ADMIN_ADJUSTMENT movement to loyalty_point_ledger.
    """
    current_dt = now or _utc_now_naive()
    if points_delta == 0:
        raise ValueError("La rettifica punti (points_delta) deve essere diversa da zero")

    customer = crm_customer_repo.get(db, customer_id)
    if not customer:
        raise CustomerNotFoundError(f"Cliente '{customer_id}' non trovato")

    if customer.status == CustomerStatus.ANONYMIZED:
        raise ValueError("Impossibile rettificare i punti di un profilo cliente anonimizzato")

    entry = LoyaltyPointLedger(
        customer_id=customer.id,
        transaction_id=None,
        points_delta=int(points_delta),
        reason=LoyaltyLedgerReason.ADMIN_ADJUSTMENT,
        created_at=current_dt,
    )
    db.add(entry)
    db.commit()

    logger.log_execution(
        "loyalty_service",
        "adjust_customer_points",
        "ok",
        customer_id=customer.id,
        points_delta=points_delta,
    )
    return get_customer_detail(db, customer.id, today=current_dt.date())


def anonymize_customer(
    db: Session,
    customer_id: str,
    now: Optional[datetime] = None,
) -> CustomerDetailOut:
    """
    Admin GDPR Right-to-be-Forgotten operation:
    - Anonymizes personal fields (first_name="ANONIMO", last_name="ANONIMO",
      phone_number=None, birth_date=None, address=None, status=ANONYMIZED).
    - Revokes marketing/profiling consents and clears whatsapp_flow_token.
    - Zeroes out any positive points balance via a closing ADMIN_ADJUSTMENT movement.
    - Preserves all associated Transactions and ExcelSales intact.
    """
    current_dt = now or _utc_now_naive()
    customer = crm_customer_repo.get(db, customer_id)
    if not customer:
        raise CustomerNotFoundError(f"Cliente '{customer_id}' non trovato")

    current_balance = crm_customer_repo.get_points_balance(db, customer.id)
    if current_balance > 0:
        db.add(
            LoyaltyPointLedger(
                customer_id=customer.id,
                transaction_id=None,
                points_delta=-current_balance,
                reason=LoyaltyLedgerReason.ADMIN_ADJUSTMENT,
                created_at=current_dt,
            )
        )

    customer.first_name = "ANONIMO"
    customer.last_name = "ANONIMO"
    customer.phone_number = None
    customer.birth_date = None
    customer.address = None
    customer.status = CustomerStatus.ANONYMIZED
    customer.consent_marketing = False
    customer.consent_profiling = False
    customer.consent_updated_at = current_dt
    customer.whatsapp_flow_token = None
    customer.redemption_otp_code = None
    customer.redemption_rule_code = None
    customer.retention_expires_at = None

    db.add(customer)
    db.commit()
    db.refresh(customer)

    logger.log_execution(
        "loyalty_service",
        "anonymize_customer",
        "ok",
        customer_id=customer.id,
        zeroed_points=max(0, current_balance),
    )
    return get_customer_detail(db, customer.id, today=current_dt.date())


def merge_customers(
    db: Session,
    payload: CustomerMergeRequest,
    now: Optional[datetime] = None,
) -> CustomerDetailOut:
    """
    Admin operation to merge two duplicate customer profiles (source -> target):
    - Moves all Transactions from source_customer_id to target_customer_id.
    - Moves all LoyaltyPointLedger entries from source_customer_id to target_customer_id.
    - Updates target's last_purchase_date and retention_expires_at if source had a newer purchase.
    - Deletes the source Customer profile.
    """
    current_dt = now or _utc_now_naive()
    source_id = payload.source_customer_id.strip()
    target_id = payload.target_customer_id.strip()

    if source_id == target_id:
        raise ValueError("Impossibile unire un profilo cliente con se stesso")

    source_customer = crm_customer_repo.get(db, source_id)
    if not source_customer:
        raise CustomerNotFoundError(f"Profilo cliente sorgente '{source_id}' non trovato")

    target_customer = crm_customer_repo.get(db, target_id)
    if not target_customer:
        raise CustomerNotFoundError(f"Profilo cliente destinazione '{target_id}' non trovato")

    if target_customer.status == CustomerStatus.ANONYMIZED:
        raise ValueError("Impossibile unire un profilo verso un cliente destinazione anonimizzato")

    # Reassign Transactions
    db.query(Transaction).filter(Transaction.customer_id == source_customer.id).update(
        {Transaction.customer_id: target_customer.id},
        synchronize_session=False,
    )

    # Reassign LoyaltyPointLedger entries
    db.query(LoyaltyPointLedger).filter(
        LoyaltyPointLedger.customer_id == source_customer.id
    ).update(
        {LoyaltyPointLedger.customer_id: target_customer.id},
        synchronize_session=False,
    )

    # Update target last_purchase_date if source had a more recent purchase
    latest_dates = [
        d
        for d in (
            target_customer.last_purchase_date,
            source_customer.last_purchase_date,
            crm_customer_repo.get_latest_transaction_date(db, target_customer.id),
        )
        if d is not None
    ]
    if latest_dates:
        newest_date = max(latest_dates)
        target_customer.last_purchase_date = newest_date
        if target_customer.status == CustomerStatus.ACTIVE:
            anchor_dt = datetime(
                newest_date.year,
                newest_date.month,
                newest_date.day,
                current_dt.hour,
                current_dt.minute,
                current_dt.second,
            )
            target_customer.retention_expires_at = _add_months(
                anchor_dt, settings.WHATSAPP_RETENTION_MONTHS
            )

    db.flush()
    db.refresh(source_customer)
    db.delete(source_customer)
    db.add(target_customer)
    db.commit()
    db.refresh(target_customer)

    logger.log_execution(
        "loyalty_service",
        "merge_customers",
        "ok",
        source_customer_id=source_id,
        target_customer_id=target_id,
    )
    return get_customer_detail(db, target_customer.id, today=current_dt.date())
