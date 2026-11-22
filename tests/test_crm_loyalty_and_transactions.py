"""
tests/test_crm_loyalty_and_transactions.py
------------------------------------------
Comprehensive test suite for CRM Phase 3:
- Loyalty Service 4-step mathematical engine (pre-discount restoration, net discountable total,
  FIXED and PERCENTAGE proportional discount spreading with cent remainder absorption, immutable ledger)
- Base point earning (floor rounding), return reversals (negative balance support), and discount redemptions
- Birthday Bonus (x2) window calculation (including Dec/Jan year wrap-around, multiple same-day
  purchases vs different-day restriction, and retroactive crediting upon WhatsApp Flow activation)
- Transaction creation from LTDB (excel_row_index=None, immediate WMS Article reconciliation),
  transaction PATCH updates (PRICE_CORRECTION delta, customer reassignment, automatic manual_review=False reset),
  and grouping existing ExcelSale rows via POST /api/v1/transactions/from-excel-sales
- Customer CRM endpoints: search & filters (q, status, birthday_this_week), full detail with KPIs,
  demographic & consent PATCH, admin points adjustment, GDPR anonymization, duplicate profile merge, and RBAC.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from src.core.auth import get_current_user
from src.core.request_context import AuthUser, current_user_var
from src.main import app
from src.models.crm import (
    Customer,
    CustomerStatus,
    LoyaltyLedgerReason,
    LoyaltyPointLedger,
    Transaction,
)
from src.models.excel_sales import ExcelSale
from src.models.pim import ArticleBlueprint, Brand, Category
from src.models.wms import Article, ArticleMovement, ArticleStatus, Batch, Supplier
from src.schemas.excel_sales import ExcelSaleStatus
from src.services.whatsapp_service import mock_whatsapp_provider


@pytest.fixture(autouse=True)
def _clean_whatsapp_outbox():
    mock_whatsapp_provider.clear_outbox()
    yield
    mock_whatsapp_provider.clear_outbox()


def _create_active_customer(
    db: Session,
    phone: str = "3401112233",
    first_name: str = "Mario",
    last_name: str = "Rossi",
    birth_date: date = date(1985, 6, 15),
    initial_points: int = 0,
) -> Customer:
    now = datetime(2026, 11, 19, 10, 0, 0)
    customer = Customer(
        phone_number=phone,
        first_name=first_name,
        last_name=last_name,
        birth_date=birth_date,
        address="Via Mazzini 1, Trieste",
        status=CustomerStatus.ACTIVE,
        terms_accepted_at=now,
        consent_marketing=True,
        consent_profiling=True,
        consent_updated_at=now,
        retention_expires_at=datetime(2031, 11, 19, 10, 0, 0),
    )
    db.add(customer)
    db.flush()

    if initial_points != 0:
        db.add(
            LoyaltyPointLedger(
                customer_id=customer.id,
                transaction_id=None,
                points_delta=initial_points,
                reason=LoyaltyLedgerReason.ADMIN_ADJUSTMENT,
                created_at=now,
            )
        )
    db.commit()
    db.refresh(customer)
    return customer


def _create_wms_article(
    db: Session,
    status: ArticleStatus = ArticleStatus.AVAILABLE,
    code: str = "ART-WMS-01",
) -> Article:
    brand = Brand(name=f"Brand-{code}")
    category = Category(name=f"Cat-{code}", description="Categoria test")
    supplier = Supplier(company_name=f"Supplier-{code}", vat_number=f"IT{code[-6:].zfill(11)}")
    db.add_all([brand, category, supplier])
    db.flush()

    blueprint = ArticleBlueprint(
        brand_id=brand.id,
        category_id=category.id,
        article_name=f"Borsa {code}",
        description="Borsa in pelle",
        extended_description="Borsa in vera pelle test",
        tags=["borsa", "pelle"],
    )
    batch = Batch(
        supplier_id=supplier.id,
        delivery_note_number="DDT-100",
        document_date=date(2026, 11, 1),
    )
    db.add_all([blueprint, batch])
    db.flush()

    article = Article(
        article_blueprint_id=blueprint.id,
        batch_id=batch.id,
        supplier_code=code,
        colors=["nero"],
        status=status,
    )
    db.add(article)
    db.commit()
    db.refresh(article)
    return article


class TestLoyaltyMathematicalEngineAndDiscounts:
    """Tests for point earning, returns, proportional discount spreading, and validation guards."""

    def test_base_points_floor_rounding_and_return_reversal_negative_balance(
        self, db_session: Session
    ):
        client = TestClient(app)
        customer = _create_active_customer(
            db_session, phone="3401000001", birth_date=date(1990, 5, 10)
        )

        # 1. Purchase of 99.95 EUR outside birthday window -> earns floor(99.95) = 99 points
        res1 = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": customer.id,
                "items": [
                    {
                        "starting_price": 120.00,
                        "selling_price": 99.95,
                        "excel_file_column": 0,
                        "is_exchange": False,
                        "raw_article_code": "A001",
                    }
                ],
            },
        )
        assert res1.status_code == 201
        tx1 = res1.json()
        assert tx1["points_earned_total"] == 99
        assert tx1["points_net_delta"] == 99
        assert tx1["items"][0]["excel_row_index"] is None

        # 2. Pure return of 150.00 EUR -> deducts 150 points, bringing total balance to -51
        res2 = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": customer.id,
                "items": [
                    {
                        "starting_price": 150.00,
                        "selling_price": 150.00,
                        "excel_file_column": 0,
                        "is_exchange": True,
                        "raw_article_code": "A002-RESO",
                    }
                ],
            },
        )
        assert res2.status_code == 201
        tx2 = res2.json()
        assert tx2["points_net_delta"] == -150
        assert tx2["ledger_entries"][0]["reason"] == "RETURN_REVERSAL"
        assert tx2["ledger_entries"][0]["points_delta"] == -150

        detail = client.get(f"/api/v1/customers/{customer.id}").json()
        assert detail["points_balance"] == -51

    def test_fixed_and_percentage_discount_proportional_spreading_with_cent_remainder(
        self, db_session: Session
    ):
        client = TestClient(app)
        customer = _create_active_customer(
            db_session,
            phone="3401000002",
            birth_date=date(1988, 4, 10),
            initial_points=1600,
        )

        # Case 1: TIER_500_5EUR (5.00 EUR fixed) spread across 3 equal rows of 30.00 EUR (S_acq = 90.00)
        # 5.00 * (30/90) = 1.67, 1.67, and last row absorbs remainder: 5.00 - 3.34 = 1.66
        # Resulting selling_prices: 28.33, 28.33, 28.34 (sum = 85.00)
        res_fixed = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": customer.id,
                "discount_rule_code": "TIER_500_5EUR",
                "items": [
                    {
                        "starting_price": 30.00,
                        "selling_price": 30.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                        "raw_article_code": "P1",
                    },
                    {
                        "starting_price": 30.00,
                        "selling_price": 30.00,
                        "excel_file_column": 1,
                        "is_exchange": False,
                        "raw_article_code": "P2",
                    },
                    {
                        "starting_price": 30.00,
                        "selling_price": 30.00,
                        "excel_file_column": 2,
                        "is_exchange": False,
                        "raw_article_code": "P3",
                    },
                ],
            },
        )
        assert res_fixed.status_code == 201
        tx_fixed = res_fixed.json()
        assert Decimal(tx_fixed["loyalty_discount_amount"]) == Decimal("5.00")
        prices = [Decimal(item["selling_price"]) for item in tx_fixed["items"]]
        assert prices == [Decimal("28.33"), Decimal("28.33"), Decimal("28.34")]
        assert sum(prices) == Decimal("85.00")
        assert tx_fixed["points_redeemed_total"] == 500
        assert tx_fixed["points_earned_total"] == 85
        assert tx_fixed["points_net_delta"] == -415

        # Case 2: Mixed transaction (2 purchases + 1 return) with TIER_1000_10PCT (10%)
        # Purchases: 100.00 + 50.00 = 150.00; Return: 30.00 -> T_netto = 120.00
        # Discount D = 10% of 120.00 = 12.00 EUR, spread ONLY over purchase rows:
        # Row 1 (100/150 of 12.00) = 8.00 -> selling_price = 92.00
        # Row 2 (12.00 - 8.00) = 4.00 -> selling_price = 46.00
        # Return row (is_exchange=True) MUST remain untouched at 30.00!
        res_pct = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": customer.id,
                "discount_rule_code": "TIER_1000_10PCT",
                "items": [
                    {
                        "starting_price": 110.00,
                        "selling_price": 100.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                        "raw_article_code": "ACQ-100",
                    },
                    {
                        "starting_price": 60.00,
                        "selling_price": 50.00,
                        "excel_file_column": 1,
                        "is_exchange": False,
                        "raw_article_code": "ACQ-50",
                    },
                    {
                        "starting_price": 30.00,
                        "selling_price": 30.00,
                        "excel_file_column": 0,
                        "is_exchange": True,
                        "raw_article_code": "RESO-30",
                    },
                ],
            },
        )
        assert res_pct.status_code == 201
        tx_pct = res_pct.json()
        assert Decimal(tx_pct["loyalty_discount_amount"]) == Decimal("12.00")
        assert Decimal(tx_pct["net_list_total"]) == Decimal("140.00")  # (110+60) - 30
        assert Decimal(tx_pct["net_payable"]) == Decimal("108.00")  # (92+46) - 30
        assert Decimal(tx_pct["base_cash_discount"]) == Decimal("20.00")  # (170 - 138) - 12

        item_prices = [Decimal(i["selling_price"]) for i in tx_pct["items"]]
        assert item_prices == [Decimal("92.00"), Decimal("46.00"), Decimal("30.00")]

        reasons = {e["reason"]: e["points_delta"] for e in tx_pct["ledger_entries"]}
        assert reasons["DISCOUNT_REDEMPTION"] == -1000
        assert reasons["PURCHASE_EARN"] == 138  # 92 + 46
        assert reasons["RETURN_REVERSAL"] == -30

    def test_discount_redemption_blocking_validations(self, db_session: Session):
        client = TestClient(app)

        # 1. PENDING_ONBOARDING customer can earn frozen points, but CANNOT redeem discounts
        onboard_res = client.post(
            "/api/v1/customers/onboarding", json={"phone_number": "3401000003"}
        )
        pending_id = onboard_res.json()["customer"]["id"]

        # Give pending customer 600 points via transaction
        tx_pending = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": pending_id,
                "items": [
                    {
                        "starting_price": 600.00,
                        "selling_price": 600.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )
        assert tx_pending.status_code == 201

        # Attempt redemption while PENDING_ONBOARDING -> 400 Bad Request
        res_pending_disc = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": pending_id,
                "discount_rule_code": "TIER_500_5EUR",
                "items": [
                    {
                        "starting_price": 50.00,
                        "selling_price": 50.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )
        assert res_pending_disc.status_code == 400
        assert "ACTIVE" in res_pending_disc.json()["detail"]

        # 2. ACTIVE customer with insufficient points -> 400 Bad Request
        active_low = _create_active_customer(
            db_session, phone="3401000004", initial_points=300
        )
        res_low = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": active_low.id,
                "discount_rule_code": "TIER_500_5EUR",
                "items": [
                    {
                        "starting_price": 50.00,
                        "selling_price": 50.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )
        assert res_low.status_code == 400
        assert "insufficiente" in res_low.json()["detail"].lower()

        # 3. ACTIVE customer with enough points, but T_netto <= 0 (return >= purchase) -> 400 Bad Request
        active_rich = _create_active_customer(
            db_session, phone="3401000005", initial_points=1000
        )
        res_neg_net = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": active_rich.id,
                "discount_rule_code": "TIER_500_5EUR",
                "items": [
                    {
                        "starting_price": 50.00,
                        "selling_price": 50.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    },
                    {
                        "starting_price": 50.00,
                        "selling_price": 50.00,
                        "excel_file_column": 0,
                        "is_exchange": True,
                    },
                ],
            },
        )
        assert res_neg_net.status_code == 400


class TestBirthdayBonusAndRetroactiveActivation:
    """Tests for birthday window (+/- 3 days, year wrap-around, single day per year, and retroactive onboarding)."""

    def test_birthday_bonus_same_day_multiple_purchases_vs_different_day_block(
        self, db_session: Session
    ):
        client = TestClient(app)
        # Customer born Nov 21 -> window on 2026 is [Nov 18 .. Nov 24]
        customer = _create_active_customer(
            db_session, phone="3402000001", birth_date=date(1992, 11, 21)
        )

        # 1st purchase on Nov 19 (inside +/- 3 days window) -> gets PURCHASE_EARN (+100) + BIRTHDAY_BONUS (+100)
        res1 = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": customer.id,
                "items": [
                    {
                        "starting_price": 100.00,
                        "selling_price": 100.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )
        assert res1.status_code == 201
        reasons1 = {e["reason"]: e["points_delta"] for e in res1.json()["ledger_entries"]}
        assert reasons1 == {"PURCHASE_EARN": 100, "BIRTHDAY_BONUS": 100}

        # 2nd purchase on the SAME day (Nov 19) -> also gets BIRTHDAY_BONUS (+50)!
        res2 = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": customer.id,
                "items": [
                    {
                        "starting_price": 50.00,
                        "selling_price": 50.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )
        assert res2.status_code == 201
        reasons2 = {e["reason"]: e["points_delta"] for e in res2.json()["ledger_entries"]}
        assert reasons2 == {"PURCHASE_EARN": 50, "BIRTHDAY_BONUS": 50}

        # 3rd purchase on a DIFFERENT day still within the birthday window (Nov 21) -> NO BIRTHDAY_BONUS
        res3 = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-21",
                "customer_id": customer.id,
                "items": [
                    {
                        "starting_price": 80.00,
                        "selling_price": 80.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )
        assert res3.status_code == 201
        reasons3 = {e["reason"]: e["points_delta"] for e in res3.json()["ledger_entries"]}
        assert reasons3 == {"PURCHASE_EARN": 80}

    def test_birthday_window_across_december_january_year_boundary(
        self, db_session: Session
    ):
        client = TestClient(app)
        # Born Dec 30 -> purchase on Jan 1 (2 days after Dec 30) is inside the +/- 3 days window
        cust_dec = _create_active_customer(
            db_session, phone="3402000002", birth_date=date(1985, 12, 30)
        )
        res_jan = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-01-01",
                "customer_id": cust_dec.id,
                "items": [
                    {
                        "starting_price": 70.00,
                        "selling_price": 70.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )
        assert res_jan.status_code == 201
        reasons_jan = {e["reason"]: e["points_delta"] for e in res_jan.json()["ledger_entries"]}
        assert reasons_jan == {"PURCHASE_EARN": 70, "BIRTHDAY_BONUS": 70}

        # Born Jan 2 -> purchase on Dec 31 (2 days before Jan 2) is inside the +/- 3 days window
        cust_jan = _create_active_customer(
            db_session, phone="3402000003", birth_date=date(1991, 1, 2)
        )
        res_dec = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-12-31",
                "customer_id": cust_jan.id,
                "items": [
                    {
                        "starting_price": 60.00,
                        "selling_price": 60.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )
        assert res_dec.status_code == 201
        reasons_dec = {e["reason"]: e["points_delta"] for e in res_dec.json()["ledger_entries"]}
        assert reasons_dec == {"PURCHASE_EARN": 60, "BIRTHDAY_BONUS": 60}

    def test_retroactive_birthday_bonus_on_same_day_whatsapp_onboarding_completion(
        self, db_session: Session
    ):
        client = TestClient(app)
        today = date.today()
        bdate_same_window = date(1990, today.month, today.day)

        # 1. Cashier creates PENDING_ONBOARDING customer
        onboard_res = client.post(
            "/api/v1/customers/onboarding", json={"phone_number": "3402000004"}
        )
        assert onboard_res.status_code == 201
        cust_id = onboard_res.json()["customer"]["id"]

        # 2. Cashier immediately records a 120 EUR purchase for the PENDING_ONBOARDING customer
        tx_res = client.post(
            "/api/v1/transactions",
            json={
                "date": today.isoformat(),
                "customer_id": cust_id,
                "items": [
                    {
                        "starting_price": 150.00,
                        "selling_price": 120.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )
        assert tx_res.status_code == 201
        # Only PURCHASE_EARN (+120) exists while PENDING_ONBOARDING
        assert len(tx_res.json()["ledger_entries"]) == 1
        assert tx_res.json()["ledger_entries"][0]["reason"] == "PURCHASE_EARN"

        # 3. Customer completes the WhatsApp Flow on the same day with birthday = today
        complete_res = client.post(
            f"/api/v1/customers/{cust_id}/mock-complete-onboarding",
            json={
                "first_name": "elena",
                "last_name": "verdi",
                "birth_date": bdate_same_window.isoformat(),
                "address": "corso italia 5, trieste",
                "terms_accepted": True,
                "consent_marketing": True,
                "consent_profiling": False,
            },
        )
        assert complete_res.status_code == 200
        assert complete_res.json()["status"] == "ACTIVE"

        # 4. Verify retroactive BIRTHDAY_BONUS (+120) was credited automatically!
        detail = client.get(f"/api/v1/customers/{cust_id}").json()
        assert detail["points_balance"] == 240
        reasons = [e["reason"] for e in detail["loyalty_ledger"]]
        assert "PURCHASE_EARN" in reasons
        assert "BIRTHDAY_BONUS" in reasons


class TestTransactionPatchWmsAndGrouping:
    """Tests for PATCH /transactions/{id}, PRICE_CORRECTION, manual_review reset, WMS reconciliation, and from-excel-sales."""

    def test_patch_transaction_restores_prices_writes_price_correction_and_resets_manual_review(
        self, db_session: Session
    ):
        client = TestClient(app)
        customer = _create_active_customer(
            db_session,
            phone="3403000001",
            birth_date=date(1985, 5, 10),
            initial_points=1200,
        )

        # Create transaction with 2 items of 100 EUR and TIER_1000_10PCT (discount 20 EUR -> 90 + 90 = 180)
        # Ledger: DISCOUNT_REDEMPTION (-1000) + PURCHASE_EARN (+180) = -820 (balance = 380)
        create_res = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": customer.id,
                "discount_rule_code": "TIER_1000_10PCT",
                "items": [
                    {
                        "starting_price": 100.00,
                        "selling_price": 100.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                        "raw_article_code": "ITEM-1",
                    },
                    {
                        "starting_price": 100.00,
                        "selling_price": 100.00,
                        "excel_file_column": 1,
                        "is_exchange": False,
                        "raw_article_code": "ITEM-2",
                    },
                ],
            },
        )
        assert create_res.status_code == 201
        tx_data = create_res.json()
        tx_id = tx_data["id"]

        # Simulate an unauthorized Excel edit conflict flagging manual_review = True on the DB rows
        db_rows = (
            db_session.query(ExcelSale)
            .filter(ExcelSale.transaction_id == tx_id)
            .all()
        )
        for r in db_rows:
            r.manual_review = True
        db_session.commit()

        # PATCH transaction: remove loyalty discount (discount_rule_code = None)
        # Step 1 restores row prices back to 100.00 and 100.00!
        # New target points for tx = +200. Current ledger sum for tx = -820.
        # Expected PRICE_CORRECTION delta = 200 - (-820) = +1020!
        patch_res = client.patch(
            f"/api/v1/transactions/{tx_id}",
            json={"discount_rule_code": None},
        )
        assert patch_res.status_code == 200
        patched = patch_res.json()
        assert Decimal(patched["loyalty_discount_amount"]) == Decimal("0.00")
        assert patched["discount_rule_code"] is None
        assert [Decimal(i["selling_price"]) for i in patched["items"]] == [
            Decimal("100.00"),
            Decimal("100.00"),
        ]
        # Verify manual_review was automatically reset to False on all rows
        assert all(i["manual_review"] is False for i in patched["items"])

        # Verify immutable ledger now has PRICE_CORRECTION of +1020
        correction_entries = [
            e for e in patched["ledger_entries"] if e["reason"] == "PRICE_CORRECTION"
        ]
        assert len(correction_entries) == 1
        assert correction_entries[0]["points_delta"] == 1020

        detail = client.get(f"/api/v1/customers/{customer.id}").json()
        assert detail["points_balance"] == 1400  # 1200 initial + 200 earned

    def test_transaction_wms_reconciliation_and_unreconciliation_on_patch(
        self, db_session: Session
    ):
        client = TestClient(app)
        art1 = _create_wms_article(db_session, status=ArticleStatus.AVAILABLE, code="WMS-A1")
        art2 = _create_wms_article(db_session, status=ArticleStatus.AVAILABLE, code="WMS-A2")

        # 1. Create transaction with art1 -> art1 becomes SOLD and ExcelSale is RECONCILED
        res = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "items": [
                    {
                        "starting_price": 200.00,
                        "selling_price": 180.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                        "article_id": art1.id,
                        "raw_article_code": "WMS-A1",
                    }
                ],
            },
        )
        assert res.status_code == 201
        tx_data = res.json()
        tx_id = tx_data["id"]
        line_id = tx_data["items"][0]["id"]
        assert tx_data["items"][0]["status"] == "RECONCILED"
        assert tx_data["items"][0]["article_name"] == "Borsa WMS-A1"

        db_session.refresh(art1)
        assert art1.status == ArticleStatus.SOLD

        # 2. PATCH transaction replacing art1 with art2 -> art1 reverts to AVAILABLE, art2 becomes SOLD
        patch_res = client.patch(
            f"/api/v1/transactions/{tx_id}",
            json={
                "items": [
                    {
                        "id": line_id,
                        "starting_price": 200.00,
                        "selling_price": 180.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                        "article_id": art2.id,
                        "raw_article_code": "WMS-A2",
                    }
                ]
            },
        )
        assert patch_res.status_code == 200
        db_session.refresh(art1)
        db_session.refresh(art2)
        assert art1.status == ArticleStatus.AVAILABLE
        assert art2.status == ArticleStatus.SOLD

        movements_art1 = (
            db_session.query(ArticleMovement)
            .filter(ArticleMovement.article_id == art1.id)
            .all()
        )
        assert len(movements_art1) == 2

    def test_create_transaction_from_existing_excel_sales(self, db_session: Session):
        client = TestClient(app)
        customer = _create_active_customer(
            db_session,
            phone="3403000002",
            birth_date=date(1980, 3, 15),
            initial_points=600,
        )

        # Insert 2 orphan ExcelSale rows originating from Excel (excel_row_index = 2, 3)
        sale1 = ExcelSale(
            date=date(2026, 11, 19),
            excel_row_index=2,
            excel_file_column=0,
            status=ExcelSaleStatus.ORPHAN,
            starting_price=80.00,
            selling_price=60.00,
            is_exchange=False,
            raw_article_code="EX-1",
        )
        sale2 = ExcelSale(
            date=date(2026, 11, 19),
            excel_row_index=3,
            excel_file_column=1,
            status=ExcelSaleStatus.ORPHAN,
            starting_price=40.00,
            selling_price=40.00,
            is_exchange=False,
            raw_article_code="EX-2",
        )
        db_session.add_all([sale1, sale2])
        db_session.commit()

        # Group them into a Transaction with customer and TIER_500_5EUR (5.00 EUR discount)
        # S_acq = 100.00 -> row1 gets 3.00 discount (57.00), row2 gets 2.00 discount (38.00)
        res = client.post(
            "/api/v1/transactions/from-excel-sales",
            json={
                "excel_sale_ids": [sale1.id, sale2.id],
                "customer_id": customer.id,
                "discount_rule_code": "TIER_500_5EUR",
            },
        )
        assert res.status_code == 201
        tx = res.json()
        assert Decimal(tx["loyalty_discount_amount"]) == Decimal("5.00")
        assert [Decimal(i["selling_price"]) for i in tx["items"]] == [
            Decimal("57.00"),
            Decimal("38.00"),
        ]
        assert [i["excel_row_index"] for i in tx["items"]] == [2, 3]
        assert tx["points_redeemed_total"] == 500
        assert tx["points_earned_total"] == 95

        # Calling from-excel-sales again on already grouped rows -> 400 Bad Request
        dup_res = client.post(
            "/api/v1/transactions/from-excel-sales",
            json={"excel_sale_ids": [sale1.id]},
        )
        assert dup_res.status_code == 400

    def test_reassign_and_unlink_customer_on_transaction_neutralizes_previous_customer_ledger(
        self, db_session: Session
    ):
        client = TestClient(app)
        cust_a = _create_active_customer(
            db_session, phone="3403000003", birth_date=date(1985, 5, 10)
        )
        cust_b = _create_active_customer(
            db_session, phone="3403000004", birth_date=date(1985, 5, 10)
        )

        # Create transaction for cust_a (+100 points)
        res = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": cust_a.id,
                "items": [
                    {
                        "starting_price": 100.00,
                        "selling_price": 100.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )
        assert res.status_code == 201
        tx_id = res.json()["id"]
        assert client.get(f"/api/v1/customers/{cust_a.id}").json()["points_balance"] == 100

        # Reassign transaction to cust_b -> cust_a gets PRICE_CORRECTION (-100), cust_b gets PURCHASE_EARN (+100)
        patch_res = client.patch(
            f"/api/v1/transactions/{tx_id}",
            json={"customer_id": cust_b.id},
        )
        assert patch_res.status_code == 200
        assert client.get(f"/api/v1/customers/{cust_a.id}").json()["points_balance"] == 0
        assert client.get(f"/api/v1/customers/{cust_b.id}").json()["points_balance"] == 100

        # Unlink customer completely (customer_id: null) -> cust_b gets PRICE_CORRECTION (-100)
        unlink_res = client.patch(
            f"/api/v1/transactions/{tx_id}",
            json={"customer_id": None},
        )
        assert unlink_res.status_code == 200
        assert client.get(f"/api/v1/customers/{cust_b.id}").json()["points_balance"] == 0

        # GET /api/v1/transactions/{tx_id} returns the transaction detail
        get_tx = client.get(f"/api/v1/transactions/{tx_id}")
        assert get_tx.status_code == 200
        assert get_tx.json()["customer_id"] is None
        assert client.get("/api/v1/transactions/non-existent-tx-id").status_code == 404


class TestCustomerCrmEndpointsAdminActionsAndRbac:
    """Tests for customer search, KPIs, PATCH, points-adjustment, anonymize, merge, and RBAC."""

    def test_customer_search_filters_kpis_patch_and_admin_adjustment(
        self, db_session: Session
    ):
        client = TestClient(app)
        today = date.today()
        bday_today = date(1993, today.month, today.day)

        c1 = _create_active_customer(
            db_session,
            phone="3404000001",
            first_name="Alessandro",
            last_name="Manzoni",
            birth_date=bday_today,
            initial_points=100,
        )
        c2 = _create_active_customer(
            db_session,
            phone="3404000002",
            first_name="Giovanni",
            last_name="Verga",
            birth_date=date(1975, ((today.month + 5) % 12) + 1, 10),
            initial_points=50,
        )

        # Search by query 'Manzoni'
        search_res = client.get("/api/v1/customers?q=Manzoni")
        assert search_res.status_code == 200
        items = search_res.json()
        assert len(items) == 1
        assert items[0]["id"] == c1.id
        assert items[0]["points_balance"] == 100
        assert items[0]["is_birthday_window_active"] is True

        # Filter by birthday_this_week=true
        bday_res = client.get("/api/v1/customers?birthday_this_week=true")
        assert bday_res.status_code == 200
        bday_ids = [item["id"] for item in bday_res.json()]
        assert c1.id in bday_ids
        assert c2.id not in bday_ids

        # PATCH customer demographics & consents
        patch_res = client.patch(
            f"/api/v1/customers/{c2.id}",
            json={
                "first_name": "giovanni carlo",
                "address": "piazza unita 1, trieste",
                "consent_marketing": False,
            },
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["first_name"] == "Giovanni Carlo"
        assert patch_res.json()["address"] == "Piazza Unita 1, Trieste"
        assert patch_res.json()["consent_marketing"] is False

        # Admin manual points adjustment
        adj_res = client.post(
            f"/api/v1/customers/{c2.id}/points-adjustment",
            json={"points_delta": -20},
        )
        assert adj_res.status_code == 200
        assert adj_res.json()["points_balance"] == 30
        assert adj_res.json()["loyalty_ledger"][0]["reason"] == "ADMIN_ADJUSTMENT"
        assert adj_res.json()["loyalty_ledger"][0]["points_delta"] == -20

    def test_customer_merge_and_gdpr_anonymization(self, db_session: Session):
        client = TestClient(app)
        source_cust = _create_active_customer(
            db_session,
            phone="3405000001",
            first_name="Marco",
            last_name="Duplicato",
            birth_date=date(1982, 4, 12),
            initial_points=0,
        )
        target_cust = _create_active_customer(
            db_session,
            phone="3405000002",
            first_name="Marco",
            last_name="Ufficiale",
            birth_date=date(1982, 4, 12),
            initial_points=200,
        )

        # Create a transaction on source_cust (+150 points) and one on target_cust (+100 points)
        client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-18",
                "customer_id": source_cust.id,
                "items": [
                    {
                        "starting_price": 150.00,
                        "selling_price": 150.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )
        client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-11-19",
                "customer_id": target_cust.id,
                "items": [
                    {
                        "starting_price": 100.00,
                        "selling_price": 100.00,
                        "excel_file_column": 0,
                        "is_exchange": False,
                    }
                ],
            },
        )

        # 1. Merge source_cust -> target_cust
        merge_res = client.post(
            "/api/v1/customers/merge",
            json={
                "source_customer_id": source_cust.id,
                "target_customer_id": target_cust.id,
            },
        )
        assert merge_res.status_code == 200
        merged = merge_res.json()
        assert merged["id"] == target_cust.id
        assert merged["points_balance"] == 450  # 200 + 150 + 100
        assert merged["kpi"]["transactions_count"] == 2
        assert Decimal(merged["kpi"]["total_spent"]) == Decimal("250.00")
        assert Decimal(merged["kpi"]["avg_ticket"]) == Decimal("125.00")

        # Verify source_cust was deleted
        assert client.get(f"/api/v1/customers/{source_cust.id}").status_code == 404

        # 2. GDPR Anonymize target_cust
        anon_res = client.post(f"/api/v1/customers/{target_cust.id}/anonymize")
        assert anon_res.status_code == 200
        anon = anon_res.json()
        assert anon["status"] == "ANONYMIZED"
        assert anon["first_name"] == "ANONIMO"
        assert anon["last_name"] == "ANONIMO"
        assert anon["phone_number"] is None
        assert anon["birth_date"] is None
        assert anon["address"] is None
        assert anon["points_balance"] == 0
        # Transactions and sales are preserved intact
        assert anon["kpi"]["transactions_count"] == 2
        assert Decimal(anon["kpi"]["total_spent"]) == Decimal("250.00")

    def test_crm_three_tier_rbac_base_vs_cassa_vs_admin(self, db_session: Session):
        """
        Verifies the 3-tier RBAC matrix across Customer CRM & System endpoints:
        - 'base': can read customers, initiate onboarding, and record transactions;
          FORBIDDEN (403) from PATCH, points-adjustment, merge, anonymize, users, settings.
        - 'cassa': can perform ALL customer operations (read, onboarding, PATCH, points-adjustment,
          merge, anonymize -> 200 OK); FORBIDDEN (403) from /api/v1/users and /api/v1/settings.
        - 'admin': full access to customer operations, users IAM, and system settings.
        """
        client = TestClient(app)
        c1 = _create_active_customer(db_session, phone="3406000001", initial_points=100)
        c2 = _create_active_customer(db_session, phone="3406000002", initial_points=100)
        c3 = _create_active_customer(db_session, phone="3406000003", initial_points=50)

        fake_base_user = AuthUser(
            id="00000000-0000-0000-0000-000000000099",
            username="venditore_base",
            role="base",
            token_version=1,
        )
        fake_cassa_user = AuthUser(
            id="00000000-0000-0000-0000-000000000088",
            username="operatore_cassa",
            role="cassa",
            token_version=1,
        )
        fake_admin_user = AuthUser(
            id="00000000-0000-0000-0000-000000000001",
            username="admin_test",
            role="admin",
            token_version=1,
        )

        def set_role_user(auth_user: AuthUser):
            def _override():
                current_user_var.set(auth_user)
                return auth_user

            app.dependency_overrides[get_current_user] = _override
            current_user_var.set(auth_user)

        # --- 1. Role 'base' ---
        set_role_user(fake_base_user)
        assert client.get("/api/v1/customers").status_code == 200
        assert client.get(f"/api/v1/customers/{c1.id}").status_code == 200
        assert (
            client.post(
                "/api/v1/customers/onboarding", json={"phone_number": "3406000099"}
            ).status_code
            == 201
        )
        assert (
            client.patch(
                f"/api/v1/customers/{c1.id}", json={"first_name": "Luigi"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/customers/{c1.id}/points-adjustment",
                json={"points_delta": 50},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/customers/merge",
                json={"source_customer_id": c1.id, "target_customer_id": c2.id},
            ).status_code
            == 403
        )
        assert (
            client.post(f"/api/v1/customers/{c1.id}/anonymize").status_code == 403
        )
        assert client.get("/api/v1/users").status_code == 403
        assert client.get("/api/v1/settings").status_code == 403

        # --- 2. Role 'cassa' ---
        set_role_user(fake_cassa_user)
        assert client.get("/api/v1/customers").status_code == 200
        patch_res = client.patch(
            f"/api/v1/customers/{c1.id}", json={"first_name": "Luigi"}
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["first_name"] == "Luigi"

        adj_res = client.post(
            f"/api/v1/customers/{c1.id}/points-adjustment",
            json={"points_delta": 50},
        )
        assert adj_res.status_code == 200
        assert adj_res.json()["points_balance"] == 150

        merge_res = client.post(
            "/api/v1/customers/merge",
            json={"source_customer_id": c3.id, "target_customer_id": c1.id},
        )
        assert merge_res.status_code == 200
        assert merge_res.json()["points_balance"] == 200

        anon_res = client.post(f"/api/v1/customers/{c1.id}/anonymize")
        assert anon_res.status_code == 200
        assert anon_res.json()["status"] == "ANONYMIZED"
        assert anon_res.json()["points_balance"] == 0

        # Cassa CANNOT access admin-only IAM or System Settings
        assert client.get("/api/v1/users").status_code == 403
        assert client.get("/api/v1/settings").status_code == 403

        # --- 3. Role 'admin' ---
        set_role_user(fake_admin_user)
        assert client.patch(
            f"/api/v1/customers/{c2.id}", json={"first_name": "Giulia"}
        ).status_code == 200
        assert (
            client.post(
                f"/api/v1/customers/{c2.id}/points-adjustment",
                json={"points_delta": 25},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/users").status_code == 200
        assert client.get("/api/v1/settings").status_code == 200
