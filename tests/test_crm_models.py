"""
Tests for CRM database models, constraints, referential integrity, ExcelSale extensions,
and Alembic migration round-trip (Phase 1: Database & ORM).
"""
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, StatementError

from src.models.crm import (
    Customer,
    CustomerStatus,
    LoyaltyLedgerReason,
    LoyaltyPointLedger,
    Transaction,
)
from src.models.excel_sales import ExcelSale
from src.schemas.excel_sales import ExcelSaleStatus


class TestCustomerModel:
    """Tests for the Customer ORM model and its constraints."""

    def test_create_customer_defaults_and_timestamps(self, db_session):
        customer = Customer(phone_number="3401234567")
        db_session.add(customer)
        db_session.commit()
        db_session.refresh(customer)

        assert customer.id is not None
        assert len(customer.id) == 36
        assert customer.phone_number == "3401234567"
        assert customer.status == CustomerStatus.PENDING_ONBOARDING
        assert customer.consent_marketing is False
        assert customer.consent_profiling is False
        assert customer.first_name is None
        assert customer.last_name is None
        assert customer.birth_date is None
        assert customer.address is None
        assert customer.terms_accepted_at is None
        assert customer.consent_updated_at is None
        assert customer.whatsapp_flow_token is None
        assert customer.last_purchase_date is None
        assert customer.retention_expires_at is None
        assert customer.created_at is not None
        assert customer.updated_at is not None

    def test_customer_phone_number_unique_constraint(self, db_session):
        c1 = Customer(phone_number="3409998877")
        db_session.add(c1)
        db_session.commit()

        c2 = Customer(phone_number="3409998877")
        db_session.add(c2)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_multiple_anonymized_customers_with_null_phone(self, db_session):
        c1 = Customer(
            phone_number=None,
            first_name="ANONIMO",
            last_name="ANONIMO",
            status=CustomerStatus.ANONYMIZED,
        )
        c2 = Customer(
            phone_number=None,
            first_name="ANONIMO",
            last_name="ANONIMO",
            status=CustomerStatus.ANONYMIZED,
        )
        db_session.add_all([c1, c2])
        db_session.commit()

        anon_rows = (
            db_session.query(Customer)
            .filter(Customer.status == CustomerStatus.ANONYMIZED)
            .all()
        )
        assert len(anon_rows) == 2
        assert all(row.phone_number is None for row in anon_rows)

    def test_customer_status_enum_roundtrip(self, db_session):
        for idx, status in enumerate(CustomerStatus):
            c = Customer(
                phone_number=f"340000000{idx}" if status != CustomerStatus.ANONYMIZED else None,
                status=status,
                birth_date=date(1990, 5, 15),
                last_purchase_date=date(2026, 11, 19),
                retention_expires_at=datetime(2031, 11, 19, 12, 0, 0),
            )
            db_session.add(c)
        db_session.commit()

        stored_statuses = {c.status for c in db_session.query(Customer).all()}
        assert stored_statuses == set(CustomerStatus)

    def test_customer_invalid_enum_string_rejected(self, db_session):
        bad = Customer(phone_number="3400001234", status="INVALID_STATUS")  # type: ignore[arg-type]
        db_session.add(bad)
        with pytest.raises((StatementError, LookupError)):
            db_session.flush()
        db_session.rollback()


class TestTransactionAndLedgerModels:
    """Tests for Transaction and immutable LoyaltyPointLedger ORM models."""

    def test_create_transaction_defaults(self, db_session):
        tx = Transaction(date=date(2026, 11, 19))
        db_session.add(tx)
        db_session.commit()
        db_session.refresh(tx)

        assert tx.id is not None
        assert tx.date == date(2026, 11, 19)
        assert tx.customer_id is None
        assert tx.loyalty_discount_amount == Decimal("0.00")
        assert tx.discount_rule_code is None
        assert tx.created_at is not None
        assert tx.updated_at is not None

    def test_ledger_is_immutable_without_updated_at(self, db_session):
        assert not hasattr(LoyaltyPointLedger, "updated_at")
        assert "updated_at" not in LoyaltyPointLedger.__table__.columns

        customer = Customer(phone_number="3471112233", status=CustomerStatus.ACTIVE)
        db_session.add(customer)
        db_session.flush()

        entry = LoyaltyPointLedger(
            customer_id=customer.id,
            transaction_id=None,
            points_delta=50,
            reason=LoyaltyLedgerReason.ADMIN_ADJUSTMENT,
        )
        db_session.add(entry)
        db_session.commit()
        db_session.refresh(entry)

        assert entry.created_at is not None
        assert entry.transaction_id is None
        assert entry.points_delta == 50

    def test_ledger_points_balance_aggregation_including_negative(self, db_session):
        customer = Customer(phone_number="3482223344", status=CustomerStatus.ACTIVE)
        db_session.add(customer)
        db_session.flush()

        tx = Transaction(
            date=date(2026, 11, 19),
            customer_id=customer.id,
            loyalty_discount_amount=Decimal("5.00"),
            discount_rule_code="TIER_500_5EUR",
        )
        db_session.add(tx)
        db_session.flush()

        entries = [
            LoyaltyPointLedger(
                customer_id=customer.id,
                transaction_id=tx.id,
                points_delta=100,
                reason=LoyaltyLedgerReason.PURCHASE_EARN,
            ),
            LoyaltyPointLedger(
                customer_id=customer.id,
                transaction_id=tx.id,
                points_delta=100,
                reason=LoyaltyLedgerReason.BIRTHDAY_BONUS,
            ),
            LoyaltyPointLedger(
                customer_id=customer.id,
                transaction_id=tx.id,
                points_delta=-500,
                reason=LoyaltyLedgerReason.DISCOUNT_REDEMPTION,
            ),
            LoyaltyPointLedger(
                customer_id=customer.id,
                transaction_id=tx.id,
                points_delta=-45,
                reason=LoyaltyLedgerReason.RETURN_REVERSAL,
            ),
            LoyaltyPointLedger(
                customer_id=customer.id,
                transaction_id=tx.id,
                points_delta=10,
                reason=LoyaltyLedgerReason.PRICE_CORRECTION,
            ),
            LoyaltyPointLedger(
                customer_id=customer.id,
                transaction_id=None,
                points_delta=-25,
                reason=LoyaltyLedgerReason.ADMIN_ADJUSTMENT,
            ),
        ]
        db_session.add_all(entries)
        db_session.commit()

        balance = (
            db_session.query(func.coalesce(func.sum(LoyaltyPointLedger.points_delta), 0))
            .filter(LoyaltyPointLedger.customer_id == customer.id)
            .scalar()
        )
        assert balance == -360

    def test_ledger_requires_valid_customer_fk(self, db_session):
        entry = LoyaltyPointLedger(
            customer_id="00000000-0000-0000-0000-999999999999",
            points_delta=10,
            reason=LoyaltyLedgerReason.ADMIN_ADJUSTMENT,
        )
        db_session.add(entry)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


class TestReferentialIntegrityOnDelete:
    """Tests for ON DELETE CASCADE / SET NULL behavior under SQLite foreign keys."""

    def test_delete_customer_orm_sets_tx_null_and_cascades_ledger(self, db_session):
        customer = Customer(phone_number="3493334455")
        db_session.add(customer)
        db_session.flush()

        tx = Transaction(date=date(2026, 11, 19), customer_id=customer.id)
        db_session.add(tx)
        db_session.flush()

        sale = ExcelSale(
            date=date(2026, 11, 19),
            excel_row_index=None,
            excel_file_column=0,
            status=ExcelSaleStatus.ORPHAN,
            starting_price=100.0,
            selling_price=90.0,
            transaction_id=tx.id,
        )
        ledger = LoyaltyPointLedger(
            customer_id=customer.id,
            transaction_id=tx.id,
            points_delta=90,
            reason=LoyaltyLedgerReason.PURCHASE_EARN,
        )
        db_session.add_all([sale, ledger])
        db_session.commit()

        tx_id = tx.id
        sale_id = sale.id
        ledger_id = ledger.id

        db_session.delete(customer)
        db_session.commit()

        persisted_tx = db_session.get(Transaction, tx_id)
        persisted_sale = db_session.get(ExcelSale, sale_id)
        assert persisted_tx is not None
        assert persisted_tx.customer_id is None
        assert persisted_sale is not None
        assert persisted_sale.transaction_id == tx_id
        assert db_session.get(LoyaltyPointLedger, ledger_id) is None

    def test_delete_customer_raw_sql_enforces_db_on_delete(self, db_session):
        customer = Customer(phone_number="3493334466")
        db_session.add(customer)
        db_session.flush()

        tx = Transaction(date=date(2026, 11, 19), customer_id=customer.id)
        db_session.add(tx)
        db_session.flush()

        ledger = LoyaltyPointLedger(
            customer_id=customer.id,
            transaction_id=tx.id,
            points_delta=50,
            reason=LoyaltyLedgerReason.PURCHASE_EARN,
        )
        db_session.add(ledger)
        db_session.commit()

        customer_id = customer.id
        tx_id = tx.id
        ledger_id = ledger.id
        db_session.expunge_all()

        db_session.execute(text("DELETE FROM customers WHERE id = :id"), {"id": customer_id})
        db_session.commit()

        persisted_tx = db_session.get(Transaction, tx_id)
        assert persisted_tx is not None
        assert persisted_tx.customer_id is None
        assert db_session.get(LoyaltyPointLedger, ledger_id) is None

    def test_delete_transaction_sets_null_on_excel_sales_and_ledger(self, db_session):
        customer = Customer(phone_number="3495556677", status=CustomerStatus.ACTIVE)
        db_session.add(customer)
        db_session.flush()

        tx = Transaction(date=date(2026, 11, 19), customer_id=customer.id)
        db_session.add(tx)
        db_session.flush()

        sale = ExcelSale(
            date=date(2026, 11, 19),
            excel_row_index=12,
            excel_file_column=1,
            status=ExcelSaleStatus.ORPHAN,
            starting_price=80.0,
            selling_price=80.0,
            transaction_id=tx.id,
        )
        ledger = LoyaltyPointLedger(
            customer_id=customer.id,
            transaction_id=tx.id,
            points_delta=80,
            reason=LoyaltyLedgerReason.PURCHASE_EARN,
        )
        db_session.add_all([sale, ledger])
        db_session.commit()

        tx_id = tx.id
        sale_id = sale.id
        ledger_id = ledger.id
        db_session.expunge_all()

        db_session.execute(text("DELETE FROM transactions WHERE id = :id"), {"id": tx_id})
        db_session.commit()

        persisted_sale = db_session.get(ExcelSale, sale_id)
        persisted_ledger = db_session.get(LoyaltyPointLedger, ledger_id)
        assert persisted_sale is not None
        assert persisted_sale.transaction_id is None
        assert persisted_ledger is not None
        assert persisted_ledger.transaction_id is None


class TestExcelSaleCrmExtensions:
    """Tests for ExcelSale extensions (nullable excel_row_index, transaction_id, manual_review)."""

    def test_multiple_null_excel_row_index_allowed_same_date(self, db_session):
        tx = Transaction(date=date(2026, 11, 19))
        db_session.add(tx)
        db_session.flush()

        sales = [
            ExcelSale(
                date=date(2026, 11, 19),
                excel_row_index=None,
                excel_file_column=0,
                status=ExcelSaleStatus.ORPHAN,
                starting_price=100.0,
                selling_price=90.0,
                transaction_id=tx.id,
            ),
            ExcelSale(
                date=date(2026, 11, 19),
                excel_row_index=None,
                excel_file_column=2,
                status=ExcelSaleStatus.ORPHAN,
                starting_price=50.0,
                selling_price=45.0,
                is_exchange=True,
                transaction_id=tx.id,
            ),
            ExcelSale(
                date=date(2026, 11, 19),
                excel_row_index=None,
                excel_file_column=3,
                status=ExcelSaleStatus.ORPHAN,
                starting_price=70.0,
                selling_price=70.0,
                transaction_id=tx.id,
            ),
        ]
        db_session.add_all(sales)
        db_session.commit()
        db_session.refresh(tx)

        assert len(tx.excel_sales) == 3
        assert all(s.excel_row_index is None for s in tx.excel_sales)
        assert all(s.manual_review is False for s in tx.excel_sales)
        assert all(s.transaction is tx for s in tx.excel_sales)

    def test_unique_constraint_enforced_when_excel_row_index_present(self, db_session):
        s1 = ExcelSale(
            date=date(2026, 11, 19),
            excel_row_index=5,
            status=ExcelSaleStatus.ORPHAN,
        )
        db_session.add(s1)
        db_session.commit()

        s2 = ExcelSale(
            date=date(2026, 11, 19),
            excel_row_index=5,
            status=ExcelSaleStatus.ORPHAN,
        )
        db_session.add(s2)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_manual_review_flag_toggle(self, db_session):
        sale = ExcelSale(
            date=date(2026, 11, 19),
            excel_row_index=2,
            status=ExcelSaleStatus.ORPHAN,
        )
        db_session.add(sale)
        db_session.commit()
        assert sale.manual_review is False

        sale.manual_review = True
        db_session.commit()
        db_session.refresh(sale)
        assert sale.manual_review is True


class TestCrmAlembicMigration:
    """Tests Alembic upgrade/downgrade/upgrade round-trip on an isolated SQLite DB."""

    def test_alembic_upgrade_downgrade_upgrade_roundtrip(self, tmp_path, monkeypatch):
        repo_root = Path(__file__).resolve().parent.parent
        db_file = tmp_path / "alembic_crm_roundtrip.db"
        sqlite_url = f"sqlite:///{db_file}"

        monkeypatch.setattr("src.core.config.settings.SQLITE_URL", sqlite_url)
        alembic_cfg = Config(str(repo_root / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(repo_root / "alembic"))

        # 1. Upgrade up to pre-CRM head
        command.upgrade(alembic_cfg, "d1e2f3a4b5c6")

        engine = sa.create_engine(sqlite_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO excel_sales (
                            id, date, excel_row_index, excel_file_column, status,
                            starting_price, selling_price, is_exchange
                        ) VALUES (
                            'legacy-sale-1', '2026-11-18', 1, 0, 'ORPHAN',
                            120.00, 100.00, 0
                        )
                        """
                    )
                )

            # 2. Upgrade to CRM head
            command.upgrade(alembic_cfg, "head")
            inspector = sa.inspect(engine)
            tables = set(inspector.get_table_names())
            assert {"customers", "transactions", "loyalty_point_ledger", "excel_sales"} <= tables

            excel_cols = {c["name"]: c for c in inspector.get_columns("excel_sales")}
            assert excel_cols["excel_row_index"]["nullable"] is True
            assert "transaction_id" in excel_cols
            assert "manual_review" in excel_cols
            assert excel_cols["manual_review"]["nullable"] is False

            ledger_cols = {c["name"] for c in inspector.get_columns("loyalty_point_ledger")}
            assert "created_at" in ledger_cols
            assert "updated_at" not in ledger_cols

            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT excel_row_index, transaction_id, manual_review FROM excel_sales WHERE id = 'legacy-sale-1'"
                    )
                ).fetchone()
                assert row == (1, None, 0)

            # 3. Downgrade back to pre-CRM revision (safe since no NULL excel_row_index rows exist)
            command.downgrade(alembic_cfg, "d1e2f3a4b5c6")
            inspector = sa.inspect(engine)
            tables_after_downgrade = set(inspector.get_table_names())
            assert "customers" not in tables_after_downgrade
            assert "transactions" not in tables_after_downgrade
            assert "loyalty_point_ledger" not in tables_after_downgrade

            excel_cols_after_downgrade = {
                c["name"]: c for c in inspector.get_columns("excel_sales")
            }
            assert excel_cols_after_downgrade["excel_row_index"]["nullable"] is False
            assert "transaction_id" not in excel_cols_after_downgrade
            assert "manual_review" not in excel_cols_after_downgrade

            # 4. Re-upgrade to head
            command.upgrade(alembic_cfg, "head")
            inspector = sa.inspect(engine)
            assert "customers" in inspector.get_table_names()
            assert "transactions" in inspector.get_table_names()
            assert "loyalty_point_ledger" in inspector.get_table_names()
        finally:
            engine.dispose()
