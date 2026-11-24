"""
tests/test_crm_frontend_and_daily_sales.py
------------------------------------------
Test suite for CRM Phase 5 (Frontend SPA & Daily Sales Transaction Partitioning):
1. HTML route /clienti, /customers redirect, unauthenticated login redirect, and sidebar/mobile nav links.
2. Static delivery and responsive rules for customers.css, customers_page.js, and updated vendite.html / sales_page.js.
3. Strict 3-tier RBAC enforcement across all 8 mutating endpoints in customers_router.py (base read-only vs cassa/admin).
4. Enriched GET /api/v1/excel-synchronization/daily-sales payload for single/multi-row transactions,
   2-step confirmation (IDENTIFICATION_PENDING -> IDENTIFICATION_CONFIRMED), OTP discount, and PENDING_ONBOARDING.
5. Node.js syntax check on all modified/created ES modules + unit tests for computeTransactionSpans and 409 structured errors.
"""
from datetime import date
import json
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from src.core.auth import get_current_user
from src.core.request_context import AuthUser, current_user_var
from src.main import app
from src.models.crm import Customer, CustomerStatus, LoyaltyLedgerReason, LoyaltyPointLedger
from src.services.excel_synchronization_service import cancel_all_post_purchase_timers
from src.services.whatsapp_service import mock_whatsapp_provider


@pytest.fixture(autouse=True)
def _cleanup_timers_and_outbox():
    mock_whatsapp_provider.clear_outbox()
    cancel_all_post_purchase_timers()
    yield
    cancel_all_post_purchase_timers()
    mock_whatsapp_provider.clear_outbox()


def _create_active_customer(
    db: Session,
    phone: str = "3401112233",
    first_name: str = "Mario",
    last_name: str = "Rossi",
    birth_date: date = date(1985, 6, 15),
    initial_points: int = 0,
) -> Customer:
    cust = Customer(
        phone_number=phone,
        first_name=first_name,
        last_name=last_name,
        birth_date=birth_date,
        address="Via Mazzini 10, Trieste",
        status=CustomerStatus.ACTIVE,
        consent_marketing=True,
        consent_profiling=False,
    )
    db.add(cust)
    db.flush()
    if initial_points != 0:
        db.add(
            LoyaltyPointLedger(
                customer_id=cust.id,
                transaction_id=None,
                points_delta=initial_points,
                reason=LoyaltyLedgerReason.ADMIN_ADJUSTMENT,
            )
        )
    db.commit()
    db.refresh(cust)
    return cust


class TestCrmFrontendRoutesAndAssets:
    """Verifies /clienti HTML rendering, navigation integration, and static CSS/JS assets."""

    def test_clienti_page_renders_shell_modals_and_injected_configs(self, db_session: Session):
        client = TestClient(app)
        res = client.get("/clienti")
        assert res.status_code == 200
        html = res.text

        # Viewport & mobile shell
        assert '<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">' in html
        assert 'id="mobile-topbar"' in html
        assert '<h1 class="mobile-topbar__page-title">Clienti</h1>' in html
        assert 'id="mobile-bottom-nav"' in html

        # Injected globals
        assert "window.__LTDB_CRM_CONFIG__" in html
        assert "window.__LTDB_USER__" in html
        assert '"redemption_tiers"' in html

        # Core CRM page elements & modals
        assert 'id="customers-page"' in html
        assert 'id="customers-table"' in html
        assert 'id="customers-detail-panel"' in html
        assert 'id="btn-new-customer-whatsapp"' in html
        assert 'id="modal-new-onboarding"' in html
        assert 'id="modal-mock-flow"' in html
        assert 'id="modal-confirm-irreversible"' in html

    def test_customers_alias_redirects_to_clienti(self, db_session: Session):
        client = TestClient(app)
        res = client.get("/customers", follow_redirects=False)
        assert res.status_code == 307
        assert res.headers["location"] == "/clienti"

    def test_unauthenticated_clienti_request_redirects_to_login(self, db_session: Session):
        app.dependency_overrides.pop(get_current_user, None)
        client = TestClient(app)
        res = client.get("/clienti", follow_redirects=False)
        assert res.status_code == 303
        assert res.headers["location"] == "/login?next=/clienti"

    def test_sidebar_and_mobile_nav_include_clienti_under_vendite(self, db_session: Session):
        client = TestClient(app)
        for route in ["/vendite", "/clienti", "/magazzino", "/brands", "/categories"]:
            res = client.get(route)
            assert res.status_code == 200
            html = res.text
            idx_vendite = html.find('href="/vendite"')
            idx_clienti = html.find('href="/clienti"')
            idx_magazzino = html.find('href="/magazzino"')
            assert idx_vendite != -1 and idx_clienti != -1 and idx_magazzino != -1
            assert idx_vendite < idx_clienti < idx_magazzino

    def test_crm_and_vendite_static_assets_served(self, db_session: Session):
        client = TestClient(app)

        res_css = client.get("/static/css/customers.css")
        assert res_css.status_code == 200
        assert ".customers-page" in res_css.text
        assert "@media (max-width: 768px)" in res_css.text

        res_js = client.get("/static/js/customers_page.js")
        assert res_js.status_code == 200
        assert "selectCustomer" in res_js.text
        assert "mockCompleteCustomerOnboarding" in res_js.text
        assert "mockQuickReplyOtp" in res_js.text

        res_vendite = client.get("/vendite")
        assert res_vendite.status_code == 200
        assert '<th class="col-customer">Cliente</th>' in res_vendite.text
        assert "Registra Vendita" not in res_vendite.text


class TestCrmStrictRolePermissions:
    """Verifies base (read-only) vs cassa (full customer ops) across all 8 mutating CRM endpoints."""

    def test_base_is_strictly_readonly_while_cassa_manages_full_lifecycle(
        self, db_session: Session
    ):
        client = TestClient(app)
        existing_active = _create_active_customer(
            db_session, phone="3407000001", initial_points=1000
        )

        fake_base = AuthUser(
            id="00000000-0000-0000-0000-000000000071",
            username="reader_base",
            role="base",
            token_version=1,
        )
        fake_cassa = AuthUser(
            id="00000000-0000-0000-0000-000000000072",
            username="cashier_user",
            role="cassa",
            token_version=1,
        )

        def set_user(u: AuthUser):
            def _ov():
                current_user_var.set(u)
                return u

            app.dependency_overrides[get_current_user] = _ov
            current_user_var.set(u)

        # 1. Cassa creates a PENDING_ONBOARDING customer first so we can test resend & mock-flow as 'base'
        set_user(fake_cassa)
        onb_res = client.post(
            "/api/v1/customers/onboarding", json={"phone_number": "3407000002"}
        )
        assert onb_res.status_code == 201
        pending_id = onb_res.json()["customer"]["id"]

        # 2. Switch to 'base': GETs succeed, ALL 8 mutating endpoints return 403
        set_user(fake_base)
        assert client.get("/clienti").status_code == 200
        assert client.get("/api/v1/customers").status_code == 200
        assert client.get(f"/api/v1/customers/{existing_active.id}").status_code == 200

        assert (
            client.post(
                "/api/v1/customers/onboarding", json={"phone_number": "3407000003"}
            ).status_code
            == 403
        )
        assert (
            client.post(f"/api/v1/customers/{pending_id}/resend-whatsapp").status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/customers/{pending_id}/mock-complete-onboarding",
                json={
                    "first_name": "Anna",
                    "last_name": "Bianchi",
                    "birth_date": "1990-05-10",
                    "address": "Via Giulia 5, Trieste",
                    "terms_accepted": True,
                    "consent_marketing": True,
                    "consent_profiling": False,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/customers/{existing_active.id}/mock-quick-reply-otp",
                json={"discount_rule_code": "TIER_500_5EUR"},
            ).status_code
            == 403
        )
        assert (
            client.patch(
                f"/api/v1/customers/{existing_active.id}", json={"address": "Via Nuova 1"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/customers/{existing_active.id}/points-adjustment",
                json={"points_delta": 10},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/customers/merge",
                json={
                    "source_customer_id": pending_id,
                    "target_customer_id": existing_active.id,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(f"/api/v1/customers/{existing_active.id}/anonymize").status_code
            == 403
        )

        # 3. Switch back to 'cassa': resend, mock-complete-onboarding, and mock-quick-reply-otp succeed
        set_user(fake_cassa)
        assert (
            client.post(f"/api/v1/customers/{pending_id}/resend-whatsapp").status_code
            == 200
        )
        act_res = client.post(
            f"/api/v1/customers/{pending_id}/mock-complete-onboarding",
            json={
                "first_name": "anna",
                "last_name": "bianchi",
                "birth_date": "1990-05-10",
                "address": "via giulia 5, trieste",
                "terms_accepted": True,
                "consent_marketing": True,
                "consent_profiling": True,
            },
        )
        assert act_res.status_code == 200
        assert act_res.json()["status"] == "ACTIVE"
        assert act_res.json()["first_name"] == "Anna"

        otp_res = client.post(
            f"/api/v1/customers/{existing_active.id}/mock-quick-reply-otp",
            json={"discount_rule_code": "TIER_500_5EUR"},
        )
        assert otp_res.status_code == 200
        assert len(otp_res.json()["redemption_otp_code"]) == 6


class TestDailySalesTransactionEnrichment:
    """Verifies GET /api/v1/excel-synchronization/daily-sales enriches rows with Transaction & Customer data."""

    def test_daily_sales_returns_transaction_and_customer_partition_metadata(
        self, db_session: Session
    ):
        client = TestClient(app)
        target_day = "2026-11-24"
        cust = _create_active_customer(
            db_session,
            phone="3408889990",
            first_name="Elena",
            last_name="Conti",
            birth_date=date(1988, 11, 25),  # Within +/- 3 days of 2026-11-24 -> birthday window active!
            initial_points=600,
        )

        # Generate an OTP for TIER_500_5EUR on cust
        otp_resp = client.post(
            f"/api/v1/customers/{cust.id}/mock-quick-reply-otp",
            json={"discount_rule_code": "TIER_500_5EUR"},
        )
        assert otp_resp.status_code == 200
        otp_code = otp_resp.json()["redemption_otp_code"]

        # 1st push from Excel:
        # - Row 10: anonymous sale (no phone)
        # - Rows 11 & 12: contiguous multi-row receipt for Elena Conti (1st save -> IDENTIFICATION_PENDING)
        # - Row 13: brand-new phone number -> auto PENDING_ONBOARDING (immediate confirmation with frozen points)
        push_payload_1 = {
            "date": target_day,
            "rows": [
                {
                    "excel_row_index": 10,
                    "excel_file_column": 0,
                    "starting_price": 50.0,
                    "selling_price": 50.0,
                    "is_exchange": False,
                    "raw_article_code": "ANON-1",
                },
                {
                    "excel_row_index": 11,
                    "excel_file_column": 1,
                    "starting_price": 120.0,
                    "selling_price": 100.0,
                    "is_exchange": False,
                    "raw_article_code": "ELENA-1",
                    "phone_number": "3408889990",
                    "redemption_otp": otp_code,
                },
                {
                    "excel_row_index": 12,
                    "excel_file_column": 2,
                    "starting_price": 80.0,
                    "selling_price": 80.0,
                    "is_exchange": False,
                    "raw_article_code": "ELENA-2",
                    "phone_number": "3408889990",
                },
                {
                    "excel_row_index": 13,
                    "excel_file_column": 3,
                    "starting_price": 45.0,
                    "selling_price": 45.0,
                    "is_exchange": False,
                    "raw_article_code": "NEW-CUST-1",
                    "phone_number": "3479990001",
                },
            ],
        }
        res_push_1 = client.post("/api/v1/excel-synchronization/push-batch", json=push_payload_1)
        assert res_push_1.status_code == 200

        daily_1 = client.get(
            f"/api/v1/excel-synchronization/daily-sales?target_date={target_day}"
        ).json()["data"]
        assert len(daily_1) == 4

        # Row 10: no transaction
        assert daily_1[0]["excel_row_index"] == 10
        assert daily_1[0]["transaction_id"] is None
        assert daily_1[0]["transaction"] is None

        # Rows 11 & 12: shared transaction in IDENTIFICATION_PENDING
        assert daily_1[1]["transaction_id"] is not None
        assert daily_1[1]["transaction_id"] == daily_1[2]["transaction_id"]
        tx_pending = daily_1[1]["transaction"]
        assert tx_pending["identification_status"] == "IDENTIFICATION_PENDING"
        assert tx_pending["items_count"] == 2
        assert tx_pending["customer"]["id"] == cust.id
        assert tx_pending["customer"]["display_name"] == "Elena Conti"
        assert tx_pending["customer"]["is_birthday_window_active"] is True

        # Row 13: new PENDING_ONBOARDING customer
        tx_new = daily_1[3]["transaction"]
        assert tx_new is not None
        assert tx_new["identification_status"] == "IDENTIFICATION_CONFIRMED"
        assert tx_new["points_net_delta"] == 45
        assert tx_new["customer"]["status"] == "PENDING_ONBOARDING"
        assert tx_new["customer"]["display_name"] == "In attesa di compilazione..."

        # 2nd push on rows 11 & 12 (2nd Ctrl+S confirms Elena's transaction & redeems 5 EUR OTP discount + x2 bday points!)
        res_push_2 = client.post(
            "/api/v1/excel-synchronization/push-batch",
            json={"date": target_day, "rows": push_payload_1["rows"][1:3]},
        )
        assert res_push_2.status_code == 200

        daily_2 = client.get(
            f"/api/v1/excel-synchronization/daily-sales?target_date={target_day}"
        ).json()["data"]
        tx_conf = daily_2[1]["transaction"]
        assert tx_conf["identification_status"] == "IDENTIFICATION_CONFIRMED"
        assert tx_conf["loyalty_discount_amount"] == 5.0
        assert tx_conf["net_payable"] == 175.0
        assert tx_conf["points_redeemed_total"] == 500
        # 175 EUR net -> 175 base + 175 birthday bonus = 350 earned; net delta = 350 - 500 = -150
        assert tx_conf["points_earned_total"] == 350
        assert tx_conf["points_net_delta"] == -150
        assert tx_conf["customer"]["points_balance"] == 450  # 600 - 150


class TestFrontendJsModulesAndPartitionLogic:
    """Validates JS syntax and unit-tests computeTransactionSpans & api.js 409 conflict parsing in Node."""

    def test_js_modules_syntax_and_transaction_spans_logic(self):
        node_bin = shutil.which("node")
        if not node_bin:
            pytest.skip("Node.js is not installed on system")

        for js_file in [
            "src/static/js/api.js",
            "src/static/js/customers_page.js",
            "src/static/js/sales_page.js",
        ]:
            chk = subprocess.run([node_bin, "--check", js_file], capture_output=True, text=True)
            assert chk.returncode == 0, f"SyntaxError in {js_file}: {chk.stderr}"

        node_script = """
        // Minimal browser stubs so sales_page.js & api.js can be imported in Node
        const dummyEl = {
            addEventListener() {},
            querySelector() { return null; },
            querySelectorAll() { return []; },
            style: {},
            classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
            dataset: {},
        };
        globalThis.window = {
            location: { pathname: '/vendite', search: '', href: 'http://localhost/vendite', replace() {} },
            innerWidth: 1280,
            history: { replaceState() {} },
        };
        globalThis.document = {
            getElementById() { return dummyEl; },
            querySelector() { return dummyEl; },
            querySelectorAll() { return []; },
            addEventListener() {},
            createElement() { return dummyEl; },
            body: { appendChild() {}, style: {} },
        };
        globalThis.IntersectionObserver = class { observe() {} unobserve() {} };

        const { computeTransactionSpans } = await import('./src/static/js/sales_page.js');
        const api = await import('./src/static/js/api.js');

        // 1. Test computeTransactionSpans across mixed rows
        const spans = computeTransactionSpans([
            { id: '1', transaction_id: null },
            { id: '2', transaction_id: 'tx-A' },
            { id: '3', transaction_id: 'tx-A' },
            { id: '4', transaction_id: 'tx-A' },
            { id: '5', transaction_id: 'tx-B' },
            { id: '6', transaction_id: null },
        ]);

        if (spans[0].spanCount !== 1 || spans[0].groupRole !== null) throw new Error('Row 0 failed');
        if (spans[1].spanCount !== 3 || spans[1].groupRole !== 'start' || spans[1].groupEndRow !== 3) throw new Error('Row 1 failed');
        if (spans[2].spanCount !== 0 || spans[2].groupRole !== 'mid' || spans[2].groupStartRow !== 1) throw new Error('Row 2 failed');
        if (spans[3].spanCount !== 0 || spans[3].groupRole !== 'end' || spans[3].groupEndRow !== 3) throw new Error('Row 3 failed');
        if (spans[4].spanCount !== 1 || spans[4].groupRole !== 'single') throw new Error('Row 4 failed');
        if (spans[5].spanCount !== 1 || spans[5].groupRole !== null) throw new Error('Row 5 failed');

        // 2. Test api.startCustomerOnboarding structured 409 Conflict handling
        globalThis.fetch = async () => ({
            ok: false,
            status: 409,
            headers: { get: () => 'application/json' },
            async json() {
                return {
                    detail: {
                        message: 'Esiste già un profilo cliente (ACTIVE) associato al numero 3401112233',
                        existing_customer: { id: 'cust-123', status: 'ACTIVE' },
                    },
                };
            },
        });

        let caught = null;
        try {
            await api.startCustomerOnboarding('+39 340 1112233');
        } catch (err) {
            caught = err;
        }
        if (!caught || caught.status !== 409) throw new Error('Expected 409 error status');
        if (!caught.message.includes('3401112233')) throw new Error('Expected sanitized detail.message: ' + caught.message);
        if (!caught.detailData || caught.detailData.existing_customer.id !== 'cust-123') {
            throw new Error('Expected detailData.existing_customer to be preserved');
        }
        console.log('ALL_JS_ASSERTIONS_OK');
        """
        res = subprocess.run(
            [node_bin, "--input-type=module", "-e", node_script],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"JS unit test failed: {res.stderr}"
        assert "ALL_JS_ASSERTIONS_OK" in res.stdout
