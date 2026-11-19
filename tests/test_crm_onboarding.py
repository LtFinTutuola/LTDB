"""
tests/test_crm_onboarding.py
----------------------------
Comprehensive test suite for CRM Phase 2:
- Phone number normalization & Title Case formatting
- MockWhatsAppProvider & WhatsAppProvider adapter pattern
- Cashier onboarding endpoint (201 Created, 200 OK resend, 409 Conflict on ACTIVE/INACTIVE_EXPIRED)
- Manual resend-whatsapp endpoint
- Meta WABA webhook GET challenge & POST HMAC-SHA256 verification
- Flow completion validation (18+ age check, GDPR terms, Title Case, 60-month retention calculation)
- Mock onboarding completion endpoint
- Automated 72h pending hard-delete cleanup & 60-month ACTIVE -> INACTIVE_EXPIRED job
"""
from datetime import date, datetime, timedelta
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
    CustomerStatus,
    LoyaltyLedgerReason,
    LoyaltyPointLedger,
    Transaction,
)
from src.services.customer_onboarding_service import (
    _add_months,
    complete_onboarding_from_flow,
    normalize_phone_number,
    run_customer_lifecycle_maintenance,
)
from src.schemas.crm import WhatsAppFlowCompletionPayload
from src.services.whatsapp_service import (
    MetaCloudWhatsAppProvider,
    get_whatsapp_provider,
    mock_whatsapp_provider,
)


@pytest.fixture(autouse=True)
def _clear_mock_whatsapp_outbox():
    """Ensures the MockWhatsAppProvider outbox is clean before and after each test."""
    mock_whatsapp_provider.clear_outbox()
    yield
    mock_whatsapp_provider.clear_outbox()


class TestPhoneNormalizationAndAdapters:
    """Unit tests for phone sterilization, calendar month math, and WhatsApp provider adapter."""

    @pytest.mark.parametrize(
        "raw_input,expected",
        [
            ("+39 340-1234567", "3401234567"),
            ("0039 340 123 4567", "3401234567"),
            ("340.123.4567", "3401234567"),
            (" (347) 987-6543 ", "3479876543"),
            ("+393331122334", "3331122334"),
        ],
    )
    def test_normalize_phone_number_valid_inputs(self, raw_input: str, expected: str):
        assert normalize_phone_number(raw_input) == expected

    @pytest.mark.parametrize(
        "invalid_input",
        [
            "",
            "   ",
            "12345",  # too short (< 9 digits)
            "12345678901234567",  # too long (> 15 digits)
            "+39 340 ABC 4567",  # contains letters
            "telefono",
        ],
    )
    def test_normalize_phone_number_rejects_invalid_inputs(self, invalid_input: str):
        with pytest.raises(ValueError):
            normalize_phone_number(invalid_input)

    def test_add_months_handles_leap_year_feb_29(self):
        leap_dt = datetime(2024, 2, 29, 14, 30, 0)
        plus_60m = _add_months(leap_dt, 60)
        assert plus_60m == datetime(2029, 2, 28, 14, 30, 0)

    def test_whatsapp_provider_adapter_and_meta_stub(self, monkeypatch):
        provider = get_whatsapp_provider()
        assert provider is mock_whatsapp_provider

        res = provider.send_onboarding_flow("3401234567", "tok_test_123")
        assert res["status"] == "sent"
        assert res["message_id"].startswith("wamid.mock.")
        assert res["flow_token"] == "tok_test_123"
        assert len(mock_whatsapp_provider.outbox) == 1
        assert mock_whatsapp_provider.outbox[0]["phone_number"] == "3401234567"

        monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "meta_cloud")
        meta_provider = get_whatsapp_provider()
        assert isinstance(meta_provider, MetaCloudWhatsAppProvider)
        with pytest.raises(NotImplementedError):
            meta_provider.send_onboarding_flow("3401234567", "tok_test_123")

        monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "unsupported_provider")
        with pytest.raises(ValueError):
            get_whatsapp_provider()


class TestCustomerOnboardingEndpoints:
    """API tests for cashier onboarding initiation, duplicate conflict checks, and manual resend."""

    def test_start_onboarding_creates_pending_customer_and_sends_mock_flow(
        self, db_session: Session
    ):
        client = TestClient(app)
        res = client.post(
            "/api/v1/customers/onboarding",
            json={"phone_number": "+39 340-1234567"},
        )
        assert res.status_code == 201
        body = res.json()
        assert body["is_resent"] is False
        assert body["customer"]["phone_number"] == "3401234567"
        assert body["customer"]["status"] == "PENDING_ONBOARDING"
        assert body["customer"]["whatsapp_flow_token"] is not None
        assert body["customer"]["retention_expires_at"] is not None
        assert body["whatsapp_delivery"]["status"] == "sent"
        assert body["whatsapp_delivery"]["message_id"].startswith("wamid.mock.")

        assert len(mock_whatsapp_provider.outbox) == 1
        assert mock_whatsapp_provider.outbox[0]["phone_number"] == "3401234567"
        assert (
            mock_whatsapp_provider.outbox[0]["flow_token"]
            == body["customer"]["whatsapp_flow_token"]
        )

    def test_start_onboarding_on_existing_pending_regenerates_token_and_returns_200(
        self, db_session: Session
    ):
        client = TestClient(app)
        res1 = client.post(
            "/api/v1/customers/onboarding",
            json={"phone_number": "3401112233"},
        )
        assert res1.status_code == 201
        first_customer = res1.json()["customer"]
        first_token = first_customer["whatsapp_flow_token"]

        # Re-trigger onboarding with different formatting of the same phone number
        res2 = client.post(
            "/api/v1/customers/onboarding",
            json={"phone_number": "+39 340 111 2233"},
        )
        assert res2.status_code == 200
        body2 = res2.json()
        assert body2["is_resent"] is True
        assert body2["customer"]["id"] == first_customer["id"]
        assert body2["customer"]["whatsapp_flow_token"] != first_token
        assert len(mock_whatsapp_provider.outbox) == 2

    def test_start_onboarding_returns_409_conflict_if_active_or_inactive_expired(
        self, db_session: Session
    ):
        active_customer = Customer(
            phone_number="3409998877",
            first_name="Mario",
            last_name="Rossi",
            status=CustomerStatus.ACTIVE,
        )
        expired_customer = Customer(
            phone_number="3405556677",
            first_name="Luigi",
            last_name="Verdi",
            status=CustomerStatus.INACTIVE_EXPIRED,
        )
        anonymized_customer = Customer(
            phone_number=None,
            first_name="ANONIMO",
            last_name="ANONIMO",
            status=CustomerStatus.ANONYMIZED,
        )
        db_session.add_all([active_customer, expired_customer, anonymized_customer])
        db_session.commit()

        client = TestClient(app)

        # 1. Conflict against ACTIVE customer
        res_active = client.post(
            "/api/v1/customers/onboarding",
            json={"phone_number": "+39 340-9998877"},
        )
        assert res_active.status_code == 409
        detail_active = res_active.json()["detail"]
        assert detail_active["existing_customer"]["id"] == active_customer.id
        assert detail_active["existing_customer"]["status"] == "ACTIVE"
        assert len(mock_whatsapp_provider.outbox) == 0

        # 2. Conflict against INACTIVE_EXPIRED customer
        res_expired = client.post(
            "/api/v1/customers/onboarding",
            json={"phone_number": "0039 340 5556677"},
        )
        assert res_expired.status_code == 409
        detail_expired = res_expired.json()["detail"]
        assert detail_expired["existing_customer"]["id"] == expired_customer.id
        assert detail_expired["existing_customer"]["status"] == "INACTIVE_EXPIRED"
        assert len(mock_whatsapp_provider.outbox) == 0

        # 3. No conflict with ANONYMIZED records -> new number succeeds
        res_new = client.post(
            "/api/v1/customers/onboarding",
            json={"phone_number": "3407778899"},
        )
        assert res_new.status_code == 201
        assert len(mock_whatsapp_provider.outbox) == 1

    def test_start_onboarding_rejects_malformed_phone(self, db_session: Session):
        client = TestClient(app)
        res = client.post(
            "/api/v1/customers/onboarding",
            json={"phone_number": "123-abc"},
        )
        assert res.status_code == 400

    def test_resend_whatsapp_endpoint(self, db_session: Session):
        client = TestClient(app)
        create_res = client.post(
            "/api/v1/customers/onboarding",
            json={"phone_number": "3471234567"},
        )
        customer_id = create_res.json()["customer"]["id"]
        old_token = create_res.json()["customer"]["whatsapp_flow_token"]

        # Valid resend on PENDING_ONBOARDING
        resend_res = client.post(f"/api/v1/customers/{customer_id}/resend-whatsapp")
        assert resend_res.status_code == 200
        resend_body = resend_res.json()
        assert resend_body["is_resent"] is True
        assert resend_body["customer"]["whatsapp_flow_token"] != old_token
        assert len(mock_whatsapp_provider.outbox) == 2

        # 404 on unknown customer
        not_found_res = client.post("/api/v1/customers/non-existent-id/resend-whatsapp")
        assert not_found_res.status_code == 404

        # 400 if customer is already ACTIVE
        customer_obj = db_session.get(Customer, customer_id)
        customer_obj.status = CustomerStatus.ACTIVE
        db_session.commit()

        active_resend_res = client.post(f"/api/v1/customers/{customer_id}/resend-whatsapp")
        assert active_resend_res.status_code == 400


class TestWhatsAppWebhooksAndActivation:
    """Tests for Meta GET challenge, HMAC-signed POST webhook, validation rules, and mock simulator."""

    def test_webhook_get_challenge_verification(self, db_session: Session):
        client = TestClient(app)

        # Valid challenge
        ok_res = client.get(
            "/api/v1/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN,
                "hub.challenge": "987654321",
            },
        )
        assert ok_res.status_code == 200
        assert ok_res.text == "987654321"

        # Wrong verify_token -> 403
        bad_tok_res = client.get(
            "/api/v1/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_secret_token",
                "hub.challenge": "987654321",
            },
        )
        assert bad_tok_res.status_code == 403

        # Wrong hub.mode -> 403
        bad_mode_res = client.get(
            "/api/v1/webhooks/whatsapp",
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN,
                "hub.challenge": "987654321",
            },
        )
        assert bad_mode_res.status_code == 403

    def test_webhook_post_rejects_missing_or_invalid_hmac_signature(self, db_session: Session):
        client = TestClient(app)
        payload = {
            "flow_token": "some_token",
            "first_name": "Mario",
            "last_name": "Rossi",
            "birth_date": "1990-01-15",
            "address": "Via Roma 1",
            "terms_accepted": True,
        }
        raw_bytes = json.dumps(payload).encode("utf-8")

        # Missing header
        res_missing = client.post(
            "/api/v1/webhooks/whatsapp",
            content=raw_bytes,
            headers={"Content-Type": "application/json"},
        )
        assert res_missing.status_code == 401

        # Invalid HMAC signature
        res_invalid = client.post(
            "/api/v1/webhooks/whatsapp",
            content=raw_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=0000000000000000000000000000000000000000000000000000000000000000",
            },
        )
        assert res_invalid.status_code == 401

    def test_webhook_post_and_mock_complete_enforces_age_18_and_terms(self, db_session: Session):
        client = TestClient(app)
        onb_res = client.post(
            "/api/v1/customers/onboarding",
            json={"phone_number": "3481122334"},
        )
        customer_id = onb_res.json()["customer"]["id"]

        # 1. Underage customer (e.g., 16 years old) -> 400 Bad Request
        underage_date = date.today().replace(year=date.today().year - 16).isoformat()
        res_minor = client.post(
            f"/api/v1/customers/{customer_id}/mock-complete-onboarding",
            json={
                "first_name": "giovanni",
                "last_name": "bianchi",
                "birth_date": underage_date,
                "address": "via milano 5, trieste",
                "terms_accepted": True,
                "consent_marketing": True,
                "consent_profiling": False,
            },
        )
        assert res_minor.status_code == 400
        assert "maggiorenni" in res_minor.json()["detail"].lower()

        # 2. Adult customer but terms_accepted == False -> 400 Bad Request
        res_no_terms = client.post(
            f"/api/v1/customers/{customer_id}/mock-complete-onboarding",
            json={
                "first_name": "giovanni",
                "last_name": "bianchi",
                "birth_date": "1985-06-20",
                "address": "via milano 5, trieste",
                "terms_accepted": False,
                "consent_marketing": True,
                "consent_profiling": False,
            },
        )
        assert res_no_terms.status_code == 400
        assert "regolamento" in res_no_terms.json()["detail"].lower()

        # Verify customer is still PENDING_ONBOARDING
        cust = db_session.get(Customer, customer_id)
        assert cust.status == CustomerStatus.PENDING_ONBOARDING

    def test_mock_complete_onboarding_activates_customer_with_title_case_and_60m_retention(
        self, db_session: Session
    ):
        client = TestClient(app)
        onb_res = client.post(
            "/api/v1/customers/onboarding",
            json={"phone_number": "+39 349-4455667"},
        )
        customer_id = onb_res.json()["customer"]["id"]

        act_res = client.post(
            f"/api/v1/customers/{customer_id}/mock-complete-onboarding",
            json={
                "first_name": "  maria   luisa ",
                "last_name": "de   santIs",
                "birth_date": "1992-03-14",
                "address": "  piazza unità d'italia 1,   trieste ",
                "terms_accepted": True,
                "consent_marketing": True,
                "consent_profiling": True,
            },
        )
        assert act_res.status_code == 200
        activated = act_res.json()
        assert activated["status"] == "ACTIVE"
        assert activated["first_name"] == "Maria Luisa"
        assert activated["last_name"] == "De Santis"
        assert activated["address"] == "Piazza Unità D'Italia 1, Trieste"
        assert activated["birth_date"] == "1992-03-14"
        assert activated["consent_marketing"] is True
        assert activated["consent_profiling"] is True
        assert activated["terms_accepted_at"] is not None
        assert activated["consent_updated_at"] is not None
        assert activated["whatsapp_flow_token"] is None

        # Retention must be ~60 months (5 years) in the future
        cust = db_session.get(Customer, customer_id)
        assert cust.retention_expires_at.year == datetime.utcnow().year + 5

        # Replay attempt on already ACTIVE customer must fail with 400
        replay_res = client.post(
            f"/api/v1/customers/{customer_id}/mock-complete-onboarding",
            json={
                "first_name": "Maria",
                "last_name": "De Santis",
                "birth_date": "1992-03-14",
                "address": "Piazza Unità 1",
                "terms_accepted": True,
            },
        )
        assert replay_res.status_code == 400

    def test_webhook_supports_meta_cloud_nested_envelope_and_existing_transaction_date(
        self, db_session: Session
    ):
        # Create a PENDING_ONBOARDING customer associated with an existing transaction
        tx_date = date(2026, 11, 10)
        customer = Customer(
            phone_number="3339988776",
            status=CustomerStatus.PENDING_ONBOARDING,
            whatsapp_flow_token="meta_envelope_token_999",
            retention_expires_at=datetime(2026, 11, 22, 12, 0, 0),
        )
        db_session.add(customer)
        db_session.flush()

        tx = Transaction(
            date=tx_date,
            customer_id=customer.id,
            loyalty_discount_amount=Decimal("0.00"),
        )
        db_session.add(tx)
        db_session.commit()

        envelope_body = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_ID",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "flow_response": {
                                    "flow_token": "meta_envelope_token_999",
                                    "first_name": "paolo",
                                    "last_name": "conte",
                                    "birth_date": "1978-07-22",
                                    "address": "corso italia 20, trieste",
                                    "terms_accepted": True,
                                    "consent_marketing": False,
                                    "consent_profiling": True,
                                }
                            },
                        }
                    ],
                }
            ],
        }
        raw_bytes = json.dumps(envelope_body).encode("utf-8")
        sig = hmac.new(
            settings.WHATSAPP_WEBHOOK_SECRET.encode("utf-8"),
            raw_bytes,
            hashlib.sha256,
        ).hexdigest()

        client = TestClient(app)
        res = client.post(
            "/api/v1/webhooks/whatsapp",
            content=raw_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={sig}",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ACTIVE"
        assert data["first_name"] == "Paolo"
        assert data["last_name"] == "Conte"
        assert data["last_purchase_date"] == "2026-11-10"
        # Retention anchored to last_purchase_date (2026-11-10 + 60 months -> 2031-11-10)
        assert data["retention_expires_at"].startswith("2031-11-10")


class TestCustomerLifecycleMaintenanceJobs:
    """Tests for the 72-hour pending cleanup job and 60-month active retention expiration job."""

    def test_72h_pending_cleanup_unlinks_transactions_deletes_ledger_and_hard_deletes_customer(
        self, db_session: Session
    ):
        ref_now = datetime(2026, 11, 19, 18, 0, 0)

        # 1. Expired PENDING_ONBOARDING customer (created > 72h ago)
        expired_pending = Customer(
            phone_number="3400000001",
            status=CustomerStatus.PENDING_ONBOARDING,
            whatsapp_flow_token="expired_tok_1",
            retention_expires_at=ref_now - timedelta(hours=1),
        )
        # 2. Valid PENDING_ONBOARDING customer (still within 72h window)
        valid_pending = Customer(
            phone_number="3400000002",
            status=CustomerStatus.PENDING_ONBOARDING,
            whatsapp_flow_token="valid_tok_2",
            retention_expires_at=ref_now + timedelta(hours=48),
        )
        db_session.add_all([expired_pending, valid_pending])
        db_session.flush()

        expired_id = expired_pending.id
        valid_id = valid_pending.id

        # Attach a Transaction and a provisional LoyaltyPointLedger entry to expired_pending
        tx = Transaction(
            date=date(2026, 11, 16),
            customer_id=expired_id,
            loyalty_discount_amount=Decimal("0.00"),
        )
        db_session.add(tx)
        db_session.flush()
        tx_id = tx.id

        ledger_entry = LoyaltyPointLedger(
            customer_id=expired_id,
            transaction_id=tx_id,
            points_delta=120,
            reason=LoyaltyLedgerReason.PURCHASE_EARN,
        )
        db_session.add(ledger_entry)
        db_session.commit()
        ledger_id = ledger_entry.id

        # Execute maintenance job
        result = run_customer_lifecycle_maintenance(db_session, now=ref_now)
        assert result.purged_pending_count == 1
        assert result.expired_active_count == 0

        # Verify expired customer was hard-deleted
        assert db_session.get(Customer, expired_id) is None
        # Verify provisional ledger entry was deleted
        assert db_session.get(LoyaltyPointLedger, ledger_id) is None
        # Verify transaction survived as anonymous occasional sale (customer_id = NULL)
        surviving_tx = db_session.get(Transaction, tx_id)
        assert surviving_tx is not None
        assert surviving_tx.customer_id is None
        # Verify valid pending customer is untouched
        assert db_session.get(Customer, valid_id) is not None

    def test_60_month_retention_job_transitions_active_to_inactive_expired(
        self, db_session: Session
    ):
        ref_now = datetime(2026, 11, 19, 18, 0, 0)

        expired_active = Customer(
            phone_number="3400000003",
            first_name="Cliente",
            last_name="Scaduto",
            status=CustomerStatus.ACTIVE,
            retention_expires_at=ref_now - timedelta(days=1),
        )
        fresh_active = Customer(
            phone_number="3400000004",
            first_name="Cliente",
            last_name="Attivo",
            status=CustomerStatus.ACTIVE,
            retention_expires_at=ref_now + timedelta(days=365),
        )
        db_session.add_all([expired_active, fresh_active])
        db_session.commit()

        result = run_customer_lifecycle_maintenance(db_session, now=ref_now)
        assert result.purged_pending_count == 0
        assert result.expired_active_count == 1

        db_session.refresh(expired_active)
        db_session.refresh(fresh_active)
        assert expired_active.status == CustomerStatus.INACTIVE_EXPIRED
        assert fresh_active.status == CustomerStatus.ACTIVE

    def test_exact_18th_birthday_today_is_accepted(self, db_session: Session):
        ref_now = datetime(2026, 11, 19, 10, 0, 0)
        ref_today = date(2026, 11, 19)

        customer = Customer(
            phone_number="3408889990",
            status=CustomerStatus.PENDING_ONBOARDING,
            whatsapp_flow_token="tok_exact_18",
            retention_expires_at=ref_now + timedelta(hours=72),
        )
        db_session.add(customer)
        db_session.commit()

        payload = WhatsAppFlowCompletionPayload(
            flow_token="tok_exact_18",
            first_name="Elena",
            last_name="Neri",
            birth_date=date(2008, 11, 19),  # Exactly 18 years old on 2026-11-19
            address="Via Carducci 12",
            terms_accepted=True,
            consent_marketing=True,
            consent_profiling=False,
        )
        activated = complete_onboarding_from_flow(
            db_session, payload, now=ref_now, today=ref_today
        )
        assert activated.status == CustomerStatus.ACTIVE
