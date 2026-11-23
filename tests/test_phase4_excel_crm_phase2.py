"""
tests/test_phase4_excel_crm_phase2.py
-------------------------------------
Comprehensive end-to-end test suite for Phase 4 — Macro-Area 2
(Gestione Errori di Inserimento e Modifiche Post-Registrazione):
1. Split multi-row receipts across partial saves (`Ctrl+S`), contiguous transaction bridging,
   debounce timer reset, pre-loyalty price restoration before attaching new rows, and proportional
   OTP discount re-spreading without duplicating `DISCOUNT_REDEMPTION` or `PURCHASE_EARN`.
2. Wrong OTP in Col Q idempotency and protection against false OTP errors on already-discounted rows.
3. Phone number validation errors, pre-confirmation corrections (`IDENTIFICATION_PENDING` and
   `PENDING_ONBOARDING` unlinking), and post-confirmation customer immutability (`IDENTIFICATION_CONFIRMED`)
   with forced `col_o_phone` restoration.
4. Manual price edits on already-confirmed sales (both `ORPHAN` and `RECONCILED`), keeping
   `DISCOUNT_REDEMPTION` immutable and recording algebraic point differences via `PRICE_CORRECTION`.
"""
from datetime import date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from src.main import app
from src.models.crm import (
    Customer,
    CustomerIdentificationStatus,
    CustomerStatus,
    LoyaltyLedgerReason,
    LoyaltyPointLedger,
    Transaction,
)
from src.models.excel_sales import ExcelSale
from src.models.pim import ArticleBlueprint, Brand
from src.models.wms import Article, ArticleStatus, Batch, Supplier
from src.schemas.excel_sales import ExcelSaleStatus
from src.services import excel_synchronization_service
from src.services.whatsapp_service import mock_whatsapp_provider


@pytest.fixture(autouse=True)
def _clean_phase4_state():
    """Ensures mock WhatsApp outbox, async update queue, and debounce timers are clean per test."""
    mock_whatsapp_provider.clear_outbox()
    excel_synchronization_service.clear_async_row_updates()
    excel_synchronization_service.cancel_all_post_purchase_timers()
    yield
    excel_synchronization_service.cancel_all_post_purchase_timers()
    excel_synchronization_service.clear_async_row_updates()
    mock_whatsapp_provider.clear_outbox()


def _create_active_customer(
    db: Session,
    phone: str = "3401112233",
    first_name: str = "Mario",
    last_name: str = "Rossi",
    birth_date: date = date(1985, 6, 15),
    initial_points: int = 0,
) -> Customer:
    now = datetime(2026, 11, 23, 10, 0, 0)
    customer = Customer(
        phone_number=phone,
        first_name=first_name,
        last_name=last_name,
        birth_date=birth_date,
        address="Via Roma 10, Trieste",
        status=CustomerStatus.ACTIVE,
        terms_accepted_at=now,
        consent_marketing=True,
        consent_profiling=True,
        consent_updated_at=now,
        retention_expires_at=datetime(2031, 11, 23, 10, 0, 0),
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


def _create_wms_article(db: Session, code: str = "WMS-ART-1") -> Article:
    brand = Brand(name=f"Brand-{code}")
    db.add(brand)
    db.flush()
    bp = ArticleBlueprint(
        brand_id=str(brand.id),
        article_name=f"Articolo {code}",
        description="Test description",
        extended_description="Test extended description",
    )
    db.add(bp)
    db.flush()
    supplier = Supplier(company_name=f"Supplier-{code}")
    db.add(supplier)
    db.flush()
    batch = Batch(
        supplier_id=str(supplier.id),
        delivery_note_number=f"DDT-{code}",
        document_date=date(2026, 11, 1),
    )
    db.add(batch)
    db.flush()
    article = Article(
        article_blueprint_id=str(bp.id),
        batch_id=str(batch.id),
        supplier_code=code,
        colors=["Nero"],
        status=ArticleStatus.AVAILABLE,
    )
    db.add(article)
    db.commit()
    db.refresh(article)
    return article


class TestMacroArea2SplitReceiptAndDiscountRespcoming:
    """2.1.1: Split multi-row receipts across partial saves, discount re-spreading & ledger anti-duplication."""

    def test_split_receipt_pending_onboarding_merges_without_second_onboarding_message(
        self, db_session: Session
    ):
        client = TestClient(app)

        # 1st Save (Ctrl+S): rows 2 and 3 for a new unregistered phone number
        res1 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 2,
                        "excel_file_column": 0,
                        "starting_price": 60.0,
                        "selling_price": 60.0,
                        "phone_number": "3408001001",
                    },
                    {
                        "excel_row_index": 3,
                        "excel_file_column": 1,
                        "starting_price": 40.0,
                        "selling_price": 40.0,
                        "phone_number": "3408001001",
                    },
                ],
            },
        )
        assert res1.status_code == 200
        onboarding_msgs = [
            m for m in mock_whatsapp_provider.outbox if m.get("type") == "onboarding_flow"
        ]
        assert len(onboarding_msgs) == 1

        # 2nd Save (Ctrl+S a few seconds later): cashier adds row 4 with the same phone number
        res2 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 4,
                        "excel_file_column": 2,
                        "starting_price": 50.0,
                        "selling_price": 50.0,
                        "phone_number": "3408001001",
                    }
                ],
            },
        )
        assert res2.status_code == 200
        updates_map = {u["excel_row_index"]: u for u in res2.json()["row_updates"]}
        assert set(updates_map.keys()) == {2, 3, 4}
        assert all(u["col_p_status"] == "Richiesta inviata" for u in updates_map.values())

        # Still ONLY 1 onboarding WhatsApp message and ONLY 1 Transaction containing all 3 rows
        onboarding_msgs_after = [
            m for m in mock_whatsapp_provider.outbox if m.get("type") == "onboarding_flow"
        ]
        assert len(onboarding_msgs_after) == 1

        customer = (
            db_session.query(Customer).filter(Customer.phone_number == "3408001001").one()
        )
        txs = db_session.query(Transaction).filter(Transaction.customer_id == customer.id).all()
        assert len(txs) == 1
        assert sorted(r.excel_row_index for r in txs[0].excel_sales) == [2, 3, 4]

        # Points must equal 60 + 40 + 50 = 150 (no duplicate points on rows 2 and 3!)
        detail = client.get(f"/api/v1/customers/{customer.id}").json()
        assert detail["points_balance"] == 150

    def test_split_receipt_with_already_redeemed_otp_respreads_discount_and_resets_debounce_without_duplicate_ledger(
        self, db_session: Session
    ):
        client = TestClient(app)
        customer = _create_active_customer(
            db_session,
            phone="3408001002",
            first_name="Giulia",
            last_name="Verdi",
            initial_points=600,
        )

        # Customer generates OTP for TIER_500_5EUR (500 pt -> 5.00 EUR fixed discount)
        otp_res = client.post(
            f"/api/v1/customers/{customer.id}/mock-quick-reply-otp",
            json={"discount_rule_code": "TIER_500_5EUR"},
        )
        assert otp_res.status_code == 200
        otp_code = otp_res.json()["redemption_otp_code"]

        # 1st Save: rows 10 (60.00) and 11 (40.00) -> IDENTIFICATION_PENDING
        initial_rows = [
            {
                "excel_row_index": 10,
                "excel_file_column": 0,
                "starting_price": 60.0,
                "selling_price": 60.0,
                "phone_number": "3408001002",
            },
            {
                "excel_row_index": 11,
                "excel_file_column": 0,
                "starting_price": 40.0,
                "selling_price": 40.0,
                "phone_number": "3408001002",
            },
        ]
        res1 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={"date": "2026-11-23", "rows": initial_rows},
        )
        assert res1.status_code == 200
        assert all(u["identification_pending"] is True for u in res1.json()["row_updates"])

        # 2nd Save: confirm rows 10 and 11 AND redeem OTP in Col Q
        confirm_rows = [dict(r) for r in initial_rows]
        confirm_rows[0]["redemption_otp"] = otp_code
        res2 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={"date": "2026-11-23", "rows": confirm_rows},
        )
        assert res2.status_code == 200
        upd2_map = {u["excel_row_index"]: u for u in res2.json()["row_updates"]}
        # 5.00 EUR spread across 60.00 and 40.00 -> 57.00 and 38.00 (net 95.00 -> +95 pt)
        assert Decimal(str(upd2_map[10]["selling_price"])) == Decimal("57.00")
        assert Decimal(str(upd2_map[11]["selling_price"])) == Decimal("38.00")
        # Balance: 600 - 500 + 95 = 195 pt
        assert upd2_map[10]["col_p_status"] == "G. Verdi (195 pt)"

        tx = db_session.query(Transaction).filter(Transaction.customer_id == customer.id).one()
        v_after_confirm = excel_synchronization_service._debounce_versions[str(tx.id)]

        # 3rd Save (Split Receipt): cashier adds row 12 (50.00) with same phone and presses Ctrl+S
        res3 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 12,
                        "excel_file_column": 1,
                        "starting_price": 50.0,
                        "selling_price": 50.0,
                        "phone_number": "3408001002",
                    }
                ],
            },
        )
        assert res3.status_code == 200
        upd3_map = {u["excel_row_index"]: u for u in res3.json()["row_updates"]}
        assert set(upd3_map.keys()) == {10, 11, 12}

        # Pre-loyalty prices on rows 10 (60.00) and 11 (40.00) were restored BEFORE attaching row 12 (50.00),
        # and the 5.00 EUR discount was re-spread across 150.00 total:
        # row 10: 60 - 2.00 = 58.00; row 11: 40 - 1.33 = 38.67; row 12: 50 - 1.67 = 48.33 (sum = 145.00)
        assert Decimal(str(upd3_map[10]["selling_price"])) == Decimal("58.00")
        assert Decimal(str(upd3_map[11]["selling_price"])) == Decimal("38.67")
        assert Decimal(str(upd3_map[12]["selling_price"])) == Decimal("48.33")

        # Debounce timer must have been reset (version incremented)
        v_after_split = excel_synchronization_service._debounce_versions[str(tx.id)]
        assert v_after_split > v_after_confirm

        # Ledger anti-duplication check:
        # Must contain ONLY 1 DISCOUNT_REDEMPTION (-500), 1 PURCHASE_EARN (+95), and 1 PRICE_CORRECTION (+50)
        # Total balance = 600 - 500 + 95 + 50 = 245 pt
        detail = client.get(f"/api/v1/customers/{customer.id}").json()
        assert detail["points_balance"] == 245
        tx_entries = [
            e for e in detail["loyalty_ledger"] if e["transaction_id"] == str(tx.id)
        ]
        redemption_entries = [
            e for e in tx_entries if e["reason"] == "DISCOUNT_REDEMPTION"
        ]
        earn_entries = [e for e in tx_entries if e["reason"] == "PURCHASE_EARN"]
        correction_entries = [
            e for e in tx_entries if e["reason"] == "PRICE_CORRECTION"
        ]
        assert len(redemption_entries) == 1
        assert redemption_entries[0]["points_delta"] == -500
        assert len(earn_entries) == 1
        assert earn_entries[0]["points_delta"] == 95
        assert len(correction_entries) == 1
        assert correction_entries[0]["points_delta"] == 50

    def test_bridging_two_separate_contiguous_blocks_consolidates_into_single_transaction_without_duplicate_points(
        self, db_session: Session
    ):
        client = TestClient(app)
        customer = _create_active_customer(
            db_session,
            phone="3408001003",
            first_name="Luca",
            last_name="Neri",
            initial_points=0,
        )

        # Save 1 & 2: Row 2 (50 EUR) with phone, Row 3 (30 EUR) WITHOUT phone, Row 4 (40 EUR) with phone
        rows_gap = [
            {"excel_row_index": 2, "excel_file_column": 0, "starting_price": 50.0, "selling_price": 50.0, "phone_number": "3408001003"},
            {"excel_row_index": 3, "excel_file_column": 0, "starting_price": 30.0, "selling_price": 30.0, "phone_number": None},
            {"excel_row_index": 4, "excel_file_column": 0, "starting_price": 40.0, "selling_price": 40.0, "phone_number": "3408001003"},
        ]
        client.post("/api/v1/excel-synchronization/push-batch", json={"date": "2026-11-23", "rows": rows_gap})
        client.post("/api/v1/excel-synchronization/push-batch", json={"date": "2026-11-23", "rows": rows_gap})

        # Before bridging: 2 separate transactions (50 pt + 40 pt = 90 pt)
        txs_before = db_session.query(Transaction).filter(Transaction.customer_id == customer.id).all()
        assert len(txs_before) == 2
        assert client.get(f"/api/v1/customers/{customer.id}").json()["points_balance"] == 90

        # Save 3: Cashier fills in customer's phone on intermediate Row 3 (bridging Row 2 and Row 4)
        res_bridge = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 3,
                        "excel_file_column": 0,
                        "starting_price": 30.0,
                        "selling_price": 30.0,
                        "phone_number": "3408001003",
                    }
                ],
            },
        )
        assert res_bridge.status_code == 200

        # Now consolidated into a single Transaction with rows [2, 3, 4] and exact balance 120 pt
        txs_after = db_session.query(Transaction).filter(Transaction.customer_id == customer.id).all()
        assert len(txs_after) == 1
        assert sorted(r.excel_row_index for r in txs_after[0].excel_sales) == [2, 3, 4]
        assert client.get(f"/api/v1/customers/{customer.id}").json()["points_balance"] == 120


class TestMacroArea2PhoneErrorsAndPostConfirmationLock:
    """2.1.3: Phone errors, pre-confirmation unlinking, and post-confirmation Col O lock + forced restore."""

    def test_pre_confirmation_phone_correction_unlinks_provisional_tx_and_cleans_frozen_points(
        self, db_session: Session
    ):
        client = TestClient(app)
        cust_a = _create_active_customer(
            db_session, phone="3407001001", first_name="Anna", last_name="Alfa", initial_points=10
        )
        cust_b = _create_active_customer(
            db_session, phone="3407001002", first_name="Bruno", last_name="Beta", initial_points=20
        )

        # 1. Cashier enters a wrong unregistered number (3407009999) on row 2 (80 EUR) -> creates PENDING_ONBOARDING
        res1 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 2,
                        "excel_file_column": 0,
                        "starting_price": 80.0,
                        "selling_price": 80.0,
                        "phone_number": "3407009999",
                    }
                ],
            },
        )
        assert res1.status_code == 200
        wrong_unreg = (
            db_session.query(Customer).filter(Customer.phone_number == "3407009999").one()
        )
        assert client.get(f"/api/v1/customers/{wrong_unreg.id}").json()["points_balance"] == 80

        # 2. Cashier fixes Col O to registered Customer A (3407001001) -> unlinks from wrong_unreg, cleans frozen points!
        res2 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 2,
                        "excel_file_column": 0,
                        "starting_price": 80.0,
                        "selling_price": 80.0,
                        "phone_number": "3407001001",
                    }
                ],
            },
        )
        assert res2.status_code == 200
        upd2 = res2.json()["row_updates"][0]
        assert upd2["col_p_status"] == "A. Alfa - Salva il file per confermare"
        assert upd2["identification_pending"] is True
        # Frozen points on the mistaken PENDING_ONBOARDING customer must now be 0!
        assert client.get(f"/api/v1/customers/{wrong_unreg.id}").json()["points_balance"] == 0

        # 3. Cashier sees "A. Alfa" in Col P, realizes it's Customer B, and changes Col O to 3407001002 before confirming
        res3 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 2,
                        "excel_file_column": 0,
                        "starting_price": 80.0,
                        "selling_price": 80.0,
                        "phone_number": "3407001002",
                    }
                ],
            },
        )
        assert res3.status_code == 200
        upd3 = res3.json()["row_updates"][0]
        assert upd3["col_p_status"] == "B. Beta - Salva il file per confermare"
        assert upd3["identification_pending"] is True
        assert db_session.query(Transaction).filter(Transaction.customer_id == cust_a.id).count() == 0

        # 4. 2nd Save on Customer B -> confirms transaction (+80 pt -> 100 pt total)
        res4 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 2,
                        "excel_file_column": 0,
                        "starting_price": 80.0,
                        "selling_price": 80.0,
                        "phone_number": "3407001002",
                    }
                ],
            },
        )
        assert res4.status_code == 200
        upd4 = res4.json()["row_updates"][0]
        assert upd4["col_p_status"] == "B. Beta (100 pt)"
        assert upd4["identification_pending"] is False

    def test_post_confirmation_lock_ignores_phone_edit_or_clear_and_forces_col_o_restore(
        self, db_session: Session
    ):
        client = TestClient(app)
        customer = _create_active_customer(
            db_session, phone="3407001005", first_name="Carlo", last_name="Conti", initial_points=50
        )
        other_customer = _create_active_customer(
            db_session, phone="3407001006", first_name="Dario", last_name="Dini", initial_points=0
        )

        row_payload = [
            {
                "excel_row_index": 5,
                "excel_file_column": 0,
                "starting_price": 100.0,
                "selling_price": 100.0,
                "phone_number": "3407001005",
            }
        ]
        # 1st and 2nd save -> IDENTIFICATION_CONFIRMED on customer (3407001005)
        client.post("/api/v1/excel-synchronization/push-batch", json={"date": "2026-11-23", "rows": row_payload})
        client.post("/api/v1/excel-synchronization/push-batch", json={"date": "2026-11-23", "rows": row_payload})

        # Attempt 1: Cashier changes Col O on confirmed row 5 to other_customer's phone (3407001006)
        res_edit = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 5,
                        "excel_file_column": 0,
                        "starting_price": 100.0,
                        "selling_price": 100.0,
                        "phone_number": other_customer.phone_number,
                    }
                ],
            },
        )
        assert res_edit.status_code == 200
        upd_edit = res_edit.json()["row_updates"][0]
        assert upd_edit["excel_row_index"] == 5
        assert upd_edit["col_o_phone"] == "3407001005"
        assert upd_edit["col_p_status"] == "C. Conti (150 pt)"

        # Attempt 2: Cashier deletes Col O (empty string) on confirmed row 5
        res_clear = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 5,
                        "excel_file_column": 0,
                        "starting_price": 100.0,
                        "selling_price": 100.0,
                        "phone_number": None,
                    }
                ],
            },
        )
        assert res_clear.status_code == 200
        upd_clear = res_clear.json()["row_updates"][0]
        assert upd_clear["col_o_phone"] == "3407001005"
        assert upd_clear["col_p_status"] == "C. Conti (150 pt)"

        # Verify DB still links row 5 to customer (3407001005) and other_customer has 0 transactions
        sale5 = db_session.query(ExcelSale).filter(ExcelSale.excel_row_index == 5).one()
        assert sale5.customer_phone == "3407001005"
        assert db_session.query(Transaction).filter(Transaction.customer_id == other_customer.id).count() == 0


class TestMacroArea2PriceCorrectionOnConfirmedAndReconciledSales:
    """2.1.4: Manual Excel price edits on already-confirmed ORPHAN and RECONCILED sales -> PRICE_CORRECTION."""

    def test_manual_price_modification_on_reconciled_and_discounted_sale_writes_price_correction_and_preserves_reconciled_status(
        self, db_session: Session
    ):
        client = TestClient(app)
        customer = _create_active_customer(
            db_session,
            phone="3406009001",
            first_name="Elena",
            last_name="Fermi",
            initial_points=600,
        )
        article = _create_wms_article(db_session, code="BORSA-LUX-1")

        # Generate OTP for TIER_500_5EUR (500 pt -> 5.00 EUR discount)
        otp_res = client.post(
            f"/api/v1/customers/{customer.id}/mock-quick-reply-otp",
            json={"discount_rule_code": "TIER_500_5EUR"},
        )
        otp_code = otp_res.json()["redemption_otp_code"]

        # 1st & 2nd Save: Row 2 (100.00 EUR) confirmed with OTP -> selling_price becomes 95.00 EUR
        row_100 = [
            {
                "excel_row_index": 2,
                "excel_file_column": 0,
                "starting_price": 100.0,
                "selling_price": 100.0,
                "raw_article_code": "BORSA-LUX-1",
                "phone_number": "3406009001",
                "redemption_otp": otp_code,
            }
        ]
        client.post("/api/v1/excel-synchronization/push-batch", json={"date": "2026-11-23", "rows": row_100})
        res2 = client.post("/api/v1/excel-synchronization/push-batch", json={"date": "2026-11-23", "rows": row_100})
        assert res2.status_code == 200
        assert Decimal(str(res2.json()["row_updates"][0]["selling_price"])) == Decimal("95.00")
        # Balance: 600 - 500 + 95 = 195 pt
        assert res2.json()["row_updates"][0]["col_p_status"] == "E. Fermi (195 pt)"

        # Reconcile Row 2 with physical WMS article -> status becomes RECONCILED
        sale_row = db_session.query(ExcelSale).filter(ExcelSale.excel_row_index == 2).one()
        rec_res = excel_synchronization_service.reconcile_sale(
            db_session, str(sale_row.id), str(article.id)
        )
        assert rec_res["status"] == "ok"
        db_session.refresh(sale_row)
        assert sale_row.status == ExcelSaleStatus.RECONCILED

        # Now cashier manually modifies the price of Row 2 in Excel from 95.00 to 125.00 (+30 EUR)
        # while the already-redeemed OTP string is still in Col Q!
        res_edit_up = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 2,
                        "excel_file_column": 0,
                        "starting_price": 130.0,
                        "selling_price": 125.0,
                        "raw_article_code": "BORSA-LUX-1",
                        "phone_number": "3406009001",
                        "redemption_otp": otp_code,
                    }
                ],
            },
        )
        assert res_edit_up.status_code == 200
        upd_up = res_edit_up.json()["row_updates"][0]
        # No false "OTP ERRATO", and balance increased by +30 pt (195 + 30 = 225 pt)
        assert upd_up["col_p_status"] == "E. Fermi (225 pt)"

        db_session.refresh(sale_row)
        assert sale_row.status == ExcelSaleStatus.RECONCILED
        assert sale_row.article_id == str(article.id)
        assert float(sale_row.starting_price) == 130.0
        assert float(sale_row.selling_price) == 125.0

        # Cashier then manually lowers the price in Excel from 125.00 to 80.00 (-45 EUR)
        res_edit_down = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 2,
                        "excel_file_column": 0,
                        "starting_price": 90.0,
                        "selling_price": 80.0,
                        "raw_article_code": "BORSA-LUX-1",
                        "phone_number": "3406009001",
                    }
                ],
            },
        )
        assert res_edit_down.status_code == 200
        upd_down = res_edit_down.json()["row_updates"][0]
        # Balance decreased by -45 pt (225 - 45 = 180 pt)
        assert upd_down["col_p_status"] == "E. Fermi (180 pt)"

        # Verify immutable ledger: DISCOUNT_REDEMPTION (-500) untouched, PURCHASE_EARN (+95),
        # PRICE_CORRECTION (+30) and PRICE_CORRECTION (-45)
        detail = client.get(f"/api/v1/customers/{customer.id}").json()
        assert detail["points_balance"] == 180
        tx_entries = [
            e for e in detail["loyalty_ledger"] if e["transaction_id"] == str(sale_row.transaction_id)
        ]
        redemptions = [e for e in tx_entries if e["reason"] == "DISCOUNT_REDEMPTION"]
        corrections = [e["points_delta"] for e in tx_entries if e["reason"] == "PRICE_CORRECTION"]
        assert len(redemptions) == 1
        assert redemptions[0]["points_delta"] == -500
        assert sorted(corrections) == [-45, 30]
