"""
tests/test_phase4_excel_crm_phase1.py
-------------------------------------
Comprehensive end-to-end test suite for Phase 4 Part 1:
- Macro-Area 1: Onboarding (Flusso A), 2-Step Identification (Flusso B), Contiguous Grouping,
  60s Debounce Post-Purchase Notification with Top-3 Quick Reply Tiers, Quick Reply Webhook
  OTP Generation, and Cash Register OTP Redemption (Col Q) with Proportional Spreading.
- Section 0.2: Negative-price return auto-detection (`is_exchange = True`).
"""
from datetime import date, datetime
from decimal import Decimal
import hashlib
import hmac
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from src.core.config import settings
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


class TestPhase4FlowAOnboardingAndDeduplication:
    """Flusso A: New phone number in Col O -> deduplicated onboarding, frozen points, async Col P update."""

    def test_three_contiguous_rows_new_customer_deduplicates_onboarding_and_queues_async_completion(
        self, db_session: Session
    ):
        client = TestClient(app)

        # Push 3 contiguous rows with the same new phone number (different formatting)
        res = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 2,
                        "excel_file_column": 0,
                        "starting_price": 60.0,
                        "selling_price": 50.0,
                        "raw_article_code": "A1",
                        "phone_number": "+39 340 8880001",
                    },
                    {
                        "excel_row_index": 3,
                        "excel_file_column": 1,
                        "starting_price": 40.0,
                        "selling_price": 40.0,
                        "raw_article_code": "A2",
                        "phone_number": "3408880001",
                    },
                    {
                        "excel_row_index": 4,
                        "excel_file_column": 2,
                        "starting_price": 30.0,
                        "selling_price": 30.0,
                        "raw_article_code": "A3",
                        "phone_number": "0039 340-888-0001",
                    },
                ],
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["confirmed_row_indexes"] == [2, 3, 4]
        assert len(body["row_updates"]) == 3
        for upd in body["row_updates"]:
            assert upd["col_p_status"] == "Richiesta inviata"
            assert upd["identification_pending"] is False

        # Deduplication check: exactly 1 Customer, 1 WhatsApp Flow message, 1 Transaction
        customers = db_session.query(Customer).filter(Customer.phone_number == "3408880001").all()
        assert len(customers) == 1
        customer = customers[0]
        assert customer.status == CustomerStatus.PENDING_ONBOARDING

        onboarding_msgs = [
            m for m in mock_whatsapp_provider.outbox if m.get("type") == "onboarding_flow"
        ]
        assert len(onboarding_msgs) == 1

        txs = db_session.query(Transaction).filter(Transaction.customer_id == customer.id).all()
        assert len(txs) == 1
        assert len(txs[0].excel_sales) == 3

        # Frozen points credited immediately (50 + 40 + 30 = 120 pt)
        detail = client.get(f"/api/v1/customers/{customer.id}").json()
        assert detail["points_balance"] == 120

        # Customer completes onboarding via WhatsApp Flow
        comp_res = client.post(
            f"/api/v1/customers/{customer.id}/mock-complete-onboarding",
            json={
                "first_name": "Laura",
                "last_name": "Bianchi",
                "birth_date": "1990-04-12",
                "address": "Via Carducci 8, Trieste",
                "terms_accepted": True,
                "consent_marketing": True,
                "consent_profiling": False,
            },
        )
        assert comp_res.status_code == 200
        assert comp_res.json()["status"] == "ACTIVE"

        # Next heartbeat drains async_row_updates with "Registrazione completata" for rows 2, 3, 4
        hb_res = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={"date": "2026-11-23", "rows": []},
        )
        assert hb_res.status_code == 200
        async_updates = hb_res.json()["async_row_updates"]
        assert [(u["excel_row_index"], u["col_p_status"]) for u in async_updates] == [
            (2, "Registrazione completata"),
            (3, "Registrazione completata"),
            (4, "Registrazione completata"),
        ]

        # Queue is now empty on subsequent heartbeat
        hb_res2 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={"date": "2026-11-23", "rows": []},
        )
        assert hb_res2.json()["async_row_updates"] == []

    def test_invalid_phone_in_col_o_returns_warning_without_creating_customer(
        self, db_session: Session
    ):
        client = TestClient(app)
        res = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={
                "date": "2026-11-23",
                "rows": [
                    {
                        "excel_row_index": 2,
                        "excel_file_column": 0,
                        "starting_price": 50.0,
                        "selling_price": 50.0,
                        "phone_number": "123-ABC",
                    }
                ],
            },
        )
        assert res.status_code == 200
        updates = res.json()["row_updates"]
        assert len(updates) == 1
        assert updates[0]["col_p_status"] == "⚠️ Telefono non valido"
        assert db_session.query(Customer).count() == 0
        sale = db_session.query(ExcelSale).filter(ExcelSale.excel_row_index == 2).first()
        assert sale is not None
        assert sale.transaction_id is None


class TestPhase4FlowBTwoStepAndDebounce:
    """Flusso B: 2-step confirmation (IDENTIFICATION_PENDING -> CONFIRMED), debounce 60s, top-3 Quick Replies."""

    def test_two_step_confirmation_holds_points_until_second_save_and_triggers_debounce(
        self, db_session: Session, monkeypatch
    ):
        client = TestClient(app)
        customer = _create_active_customer(
            db_session,
            phone="3407770001",
            first_name="Mario",
            last_name="Rossi",
            initial_points=30,
        )

        # Configure 4 tiers to verify only the TOP 3 unlocked tiers are sent in Quick Replies
        monkeypatch.setattr(
            settings,
            "LOYALTY_REDEMPTION_TIERS",
            [
                {"code": "T100", "points_required": 100, "discount_type": "FIXED", "discount_value": 2.0, "label": "100 pt -> 2€"},
                {"code": "T200", "points_required": 200, "discount_type": "FIXED", "discount_value": 4.0, "label": "200 pt -> 4€"},
                {"code": "T500", "points_required": 500, "discount_type": "FIXED", "discount_value": 10.0, "label": "500 pt -> 10€"},
                {"code": "T1000", "points_required": 1000, "discount_type": "PERCENTAGE", "discount_value": 10.0, "label": "1000 pt -> 10%"},
            ],
        )

        payload = {
            "date": "2026-11-23",
            "rows": [
                {
                    "excel_row_index": 2,
                    "excel_file_column": 0,
                    "starting_price": 1000.0,
                    "selling_price": 1000.0,
                    "phone_number": "3407770001",
                }
            ],
        }

        # 1st Save (Ctrl+S): IDENTIFICATION_PENDING, 0 new points credited
        res1 = client.post("/api/v1/excel-synchronization/push-batch", json=payload)
        assert res1.status_code == 200
        upd1 = res1.json()["row_updates"][0]
        assert upd1["col_p_status"] == "M. Rossi - Salva il file per confermare"
        assert upd1["identification_pending"] is True

        tx = db_session.query(Transaction).filter(Transaction.customer_id == customer.id).first()
        assert tx is not None
        assert tx.identification_status == CustomerIdentificationStatus.IDENTIFICATION_PENDING
        assert client.get(f"/api/v1/customers/{customer.id}").json()["points_balance"] == 30

        # 2nd Save (Ctrl+S): IDENTIFICATION_CONFIRMED, +1000 points credited (total 1030 pt)
        res2 = client.post("/api/v1/excel-synchronization/push-batch", json=payload)
        assert res2.status_code == 200
        upd2 = res2.json()["row_updates"][0]
        assert upd2["col_p_status"] == "M. Rossi (1030 pt)"
        assert upd2["identification_pending"] is False

        db_session.refresh(tx)
        assert tx.identification_status == CustomerIdentificationStatus.IDENTIFICATION_CONFIRMED
        assert client.get(f"/api/v1/customers/{customer.id}").json()["points_balance"] == 1030

        # Verify debounce version concurrency guard:
        # Stale version must be ignored, current version dispatches post-purchase WhatsApp with TOP 3 tiers
        current_version = excel_synchronization_service._debounce_versions[str(tx.id)]
        assert (
            excel_synchronization_service.execute_post_purchase_notification_now(
                transaction_id=str(tx.id),
                expected_version=current_version - 1,
                db=db_session,
            )
            is None
        )
        assert len(mock_whatsapp_provider.outbox) == 0

        delivery = excel_synchronization_service.execute_post_purchase_notification_now(
            transaction_id=str(tx.id),
            expected_version=current_version,
            db=db_session,
        )
        assert delivery is not None
        assert delivery["points_balance"] == 1030
        qr_codes = [q["code"] for q in delivery["quick_replies"]]
        assert qr_codes == ["T1000", "T500", "T200"]  # Top 3 highest unlocked tiers only!


class TestPhase4QuickReplyOtpAndCashRegisterRedemption:
    """Quick Reply Webhook -> OTP generation -> Col Q redemption (wrong OTP idempotency + valid OTP spread)."""

    def test_quick_reply_webhook_generates_otp_and_col_q_redeems_with_highest_price_rounding(
        self, db_session: Session
    ):
        client = TestClient(app)
        customer = _create_active_customer(
            db_session,
            phone="3406660001",
            first_name="Mario",
            last_name="Rossi",
            initial_points=580,
        )

        # 1. Customer taps Quick Reply button via Meta Cloud API interactive webhook envelope
        meta_envelope = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_ID",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messages": [
                                    {
                                        "from": "+39 3406660001",
                                        "type": "interactive",
                                        "interactive": {
                                            "type": "button_reply",
                                            "button_reply": {
                                                "id": "REDEEM_TIER:TIER_500_5EUR",
                                                "title": "500 Punti -> 5€",
                                            },
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                }
            ],
        }
        raw_bytes = json.dumps(meta_envelope).encode("utf-8")
        sig = hmac.new(
            settings.WHATSAPP_WEBHOOK_SECRET.encode("utf-8"),
            raw_bytes,
            hashlib.sha256,
        ).hexdigest()

        wh_res = client.post(
            "/api/v1/webhooks/whatsapp",
            content=raw_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={sig}",
            },
        )
        assert wh_res.status_code == 200
        wh_body = wh_res.json()
        otp_code = wh_body["redemption_otp_code"]
        assert len(otp_code) == 6 and otp_code.isdigit()
        assert wh_body["redemption_rule_code"] == "TIER_500_5EUR"

        # Points must NOT be deducted yet!
        assert client.get(f"/api/v1/customers/{customer.id}").json()["points_balance"] == 580

        # 2. Receipt with 3 purchase rows (60, 15, 15) + 1 negative-price return (-20)
        receipt_rows = [
            {"excel_row_index": 5, "excel_file_column": 0, "starting_price": 60.0, "selling_price": 60.0, "phone_number": "3406660001"},
            {"excel_row_index": 6, "excel_file_column": 1, "starting_price": 15.0, "selling_price": 15.0, "phone_number": "3406660001"},
            {"excel_row_index": 7, "excel_file_column": 2, "starting_price": 15.0, "selling_price": 15.0, "phone_number": "3406660001"},
            {"excel_row_index": 8, "excel_file_column": 0, "starting_price": -20.0, "selling_price": -20.0, "phone_number": "3406660001"},
        ]

        # 1st Save -> IDENTIFICATION_PENDING
        res_s1 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={"date": "2026-11-23", "rows": receipt_rows},
        )
        assert res_s1.status_code == 200
        assert all(u["identification_pending"] is True for u in res_s1.json()["row_updates"])

        # Verify negative-price row 8 was automatically flagged is_exchange=True
        row8 = db_session.query(ExcelSale).filter(ExcelSale.excel_row_index == 8).first()
        assert row8.is_exchange is True

        # 2nd Save with WRONG OTP ("000000") -> confirms transaction idempotently, preserves valid OTP
        wrong_otp_rows = [dict(r) for r in receipt_rows]
        wrong_otp_rows[0]["redemption_otp"] = "000000"
        res_s2 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={"date": "2026-11-23", "rows": wrong_otp_rows},
        )
        assert res_s2.status_code == 200
        for u in res_s2.json()["row_updates"]:
            # 580 + 90 (purchases) - 20 (return) = 650 pt
            assert u["col_p_status"] == "⚠️ OTP ERRATO | M. Rossi (650 pt)"
            assert u["selling_price"] is None

        db_session.refresh(customer)
        assert customer.redemption_otp_code == otp_code  # Valid OTP NOT consumed!

        # 3rd Save with VALID OTP on row 5 -> consumes OTP, spreads 5.00 EUR across rows 5, 6, 7
        valid_otp_row = [
            {
                "excel_row_index": 5,
                "excel_file_column": 0,
                "starting_price": 60.0,
                "selling_price": 60.0,
                "phone_number": "3406660001",
                "redemption_otp": otp_code,
            }
        ]
        res_s3 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={"date": "2026-11-23", "rows": valid_otp_row},
        )
        assert res_s3.status_code == 200
        updates_map = {u["excel_row_index"]: u for u in res_s3.json()["row_updates"]}

        # Cent remainder absorbed on highest-priced row (row 5: 60.00 -> 56.66; rows 6,7: 14.17; return row 8: -20.00)
        assert Decimal(str(updates_map[5]["selling_price"])) == Decimal("56.66")
        assert Decimal(str(updates_map[6]["selling_price"])) == Decimal("14.17")
        assert Decimal(str(updates_map[7]["selling_price"])) == Decimal("14.17")
        assert Decimal(str(updates_map[8]["selling_price"])) == Decimal("-20.00")

        # Balance: 580 - 500 (redemption) + 85 (net purchases) - 20 (return) = 145 pt
        assert updates_map[5]["col_p_status"] == "M. Rossi (145 pt)"

        # Verify OTP cleared and DISCOUNT_REDEMPTION entry recorded in ledger
        db_session.refresh(customer)
        assert customer.redemption_otp_code is None
        assert customer.redemption_rule_code is None

        detail = client.get(f"/api/v1/customers/{customer.id}").json()
        assert detail["points_balance"] == 145
        reasons = {e["reason"]: e["points_delta"] for e in detail["loyalty_ledger"]}
        assert reasons["DISCOUNT_REDEMPTION"] == -500

    def test_mock_quick_reply_otp_endpoint_end_to_end(self, db_session: Session):
        client = TestClient(app)
        customer = _create_active_customer(
            db_session,
            phone="3405550001",
            initial_points=600,
        )

        res = client.post(
            f"/api/v1/customers/{customer.id}/mock-quick-reply-otp",
            json={"discount_rule_code": "TIER_500_5EUR"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["customer_id"] == customer.id
        assert body["redemption_rule_code"] == "TIER_500_5EUR"
        assert len(body["redemption_otp_code"]) == 6

        otp_msgs = [m for m in mock_whatsapp_provider.outbox if m.get("type") == "redemption_otp"]
        assert len(otp_msgs) == 1
        assert otp_msgs[0]["otp_code"] == body["redemption_otp_code"]
