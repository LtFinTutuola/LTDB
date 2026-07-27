"""
Tests for WMS module CRUD operations:
  - Supplier: create, read
  - Batch: create linked to Supplier
  - Article: create linked to Blueprint+Batch, status enum
  - ArticlePrice: append-only ledger, current price retrieval
  - MovementReason: create with sign
  - ArticleMovement: append-only ledger
"""
import pytest
from datetime import date
from decimal import Decimal

from src.models.pim import Brand, Category, ArticleBlueprint
from src.models.wms import (
    Supplier,
    Batch,
    Article,
    ArticlePrice,
    MovementReason,
    ArticleMovement,
    ArticleStatus,
)
from src.schemas.wms import SupplierCreate, BatchCreate, ArticleCreate
from src.repositories.base import BaseRepository


# ---------------------------------------------------------------------------
# Helpers – create prerequisite records
# ---------------------------------------------------------------------------
def _create_blueprint(db) -> ArticleBlueprint:
    from src.models.pim import Category
    brand = Brand(name="TestBrand")
    db.add(brand)
    
    category = Category(name="TestCategory", description="Test Category")
    db.add(category)
    db.flush()

    bp = ArticleBlueprint(
        brand_id=brand.id,
        category_id=category.id,
        description="Test Article",
        extended_description="For WMS tests",
        tags=["test"],
    )
    db.add(bp)
    db.flush()
    return bp


def _create_supplier_and_batch(db):
    sup = Supplier(company_name="ACME Corp", vat_number="IT12345678901")
    db.add(sup)
    db.flush()

    batch = Batch(
        supplier_id=sup.id,
        delivery_note_number="DDT-001",
        document_date=date(2025, 6, 15),
    )
    db.add(batch)
    db.flush()
    return sup, batch


def _create_full_article(db):
    """Creates the entire dependency chain: Brand → Blueprint, Supplier → Batch → Article."""
    bp = _create_blueprint(db)
    sup, batch = _create_supplier_and_batch(db)

    article = Article(
        article_blueprint_id=bp.id,
        batch_id=batch.id,
        supplier_code="WMS-TEST",
        ean="1234567890123",
        colors=["Red"],
        status=ArticleStatus.AVAILABLE,
    )
    db.add(article)
    db.flush()
    return article, bp, sup, batch


# ---------------------------------------------------------------------------
# Supplier CRUD
# ---------------------------------------------------------------------------
class TestSupplierCrud:
    def test_create_supplier(self, db_session):
        repo = BaseRepository(Supplier)
        sup = repo.create(
            db_session,
            obj_in=SupplierCreate(company_name="ACME", vat_number="IT999"),
        )
        assert sup.company_name == "ACME"
        assert sup.vat_number == "IT999"

    def test_supplier_nullable_vat(self, db_session):
        repo = BaseRepository(Supplier)
        sup = repo.create(
            db_session, obj_in=SupplierCreate(company_name="NoVAT")
        )
        assert sup.vat_number is None

    def test_supplier_unique_vat(self, db_session):
        repo = BaseRepository(Supplier)
        repo.create(
            db_session,
            obj_in=SupplierCreate(company_name="A", vat_number="DUPLICATE"),
        )
        with pytest.raises(Exception):
            repo.create(
                db_session,
                obj_in=SupplierCreate(company_name="B", vat_number="DUPLICATE"),
            )


# ---------------------------------------------------------------------------
# Batch CRUD
# ---------------------------------------------------------------------------
class TestBatchCrud:
    def test_create_batch(self, db_session):
        sup, _ = _create_supplier_and_batch(db_session)
        repo = BaseRepository(Batch)
        batch = repo.create(
            db_session,
            obj_in=BatchCreate(
                supplier_id=sup.id,
                delivery_note_number="DDT-002",
                document_date=date(2025, 7, 1),
            ),
        )
        assert batch.delivery_note_number == "DDT-002"
        assert batch.supplier_id == sup.id

    def test_batch_supplier_relationship(self, db_session):
        sup, batch = _create_supplier_and_batch(db_session)
        db_session.refresh(sup)
        assert batch in sup.batches


# ---------------------------------------------------------------------------
# Article CRUD + status enum
# ---------------------------------------------------------------------------
class TestArticleCrud:
    def test_create_article_default_status(self, db_session):
        article, bp, _, batch = _create_full_article(db_session)
        assert article.status == ArticleStatus.AVAILABLE
        assert article.article_blueprint_id == bp.id
        assert article.batch_id == batch.id
        assert article.supplier_code == "WMS-TEST"
        assert article.ean == "1234567890123"
        assert article.colors == ["Red"]

    def test_update_article_status(self, db_session):
        article, *_ = _create_full_article(db_session)

        article.status = ArticleStatus.SOLD
        db_session.commit()
        db_session.refresh(article)
        assert article.status == ArticleStatus.SOLD

    def test_article_relationships(self, db_session):
        article, bp, _, batch = _create_full_article(db_session)
        db_session.refresh(bp)
        db_session.refresh(batch)

        assert article in bp.articles
        assert article in batch.articles

    def test_lookup_by_ean(self, db_session):
        article, *_ = _create_full_article(db_session)
        from src.repositories.wms_repo import wms_repo

        found = wms_repo.get_by_ean(db_session, "1234567890123")
        assert len(found) >= 1
        assert found[0].ean == "1234567890123"

        not_found = wms_repo.get_by_ean(db_session, "0000000000000")
        assert len(not_found) == 0

    def test_lookup_by_supplier_code(self, db_session):
        article, *_ = _create_full_article(db_session)
        from src.repositories.wms_repo import wms_repo

        found = wms_repo.get_by_supplier_code(db_session, "WMS-TEST")
        assert len(found) >= 1
        assert found[0].supplier_code == "WMS-TEST"

        not_found = wms_repo.get_by_supplier_code(db_session, "NON-EXISTENT")
        assert len(not_found) == 0


# ---------------------------------------------------------------------------
# ArticlePrice – Append-Only Ledger
# ---------------------------------------------------------------------------
class TestArticlePriceLedger:
    def test_append_price(self, db_session):
        article, *_ = _create_full_article(db_session)

        p1 = ArticlePrice(article_id=article.id, list_price=Decimal("150.00"))
        db_session.add(p1)
        db_session.commit()

        db_session.refresh(article)
        assert len(article.prices) == 1
        assert float(article.prices[0].list_price) == 150.00

    def test_price_history_append_only(self, db_session):
        """Multiple prices should coexist – never overwritten."""
        article, *_ = _create_full_article(db_session)

        for price_val in [Decimal("100.00"), Decimal("90.00"), Decimal("80.00")]:
            db_session.add(ArticlePrice(article_id=article.id, list_price=price_val))
        db_session.commit()

        db_session.refresh(article)
        assert len(article.prices) == 3

    def test_current_price_is_latest(self, db_session):
        """The current price is retrieved by the most recently inserted record."""
        article, *_ = _create_full_article(db_session)

        db_session.add(ArticlePrice(article_id=article.id, list_price=Decimal("200.00")))
        db_session.flush()
        db_session.add(ArticlePrice(article_id=article.id, list_price=Decimal("180.00")))
        db_session.commit()

        # Use SQLite rowid as tiebreaker for identical timestamps
        from sqlalchemy import text
        latest = (
            db_session.query(ArticlePrice)
            .filter(ArticlePrice.article_id == article.id)
            .order_by(text("rowid DESC"))
            .first()
        )
        assert float(latest.list_price) == 180.00


# ---------------------------------------------------------------------------
# MovementReason
# ---------------------------------------------------------------------------
class TestMovementReason:
    def test_create_reasons(self, db_session):
        reasons = [
            MovementReason(code="SALE", sign=-1),
            MovementReason(code="SUPPLIER_INTAKE", sign=1),
            MovementReason(code="INVENTORY_CHECK", sign=0),
        ]
        db_session.add_all(reasons)
        db_session.commit()

        all_reasons = db_session.query(MovementReason).all()
        assert len(all_reasons) == 3

        signs = {r.code: r.sign for r in all_reasons}
        assert signs["SALE"] == -1
        assert signs["SUPPLIER_INTAKE"] == 1
        assert signs["INVENTORY_CHECK"] == 0

    def test_reason_unique_code(self, db_session):
        db_session.add(MovementReason(code="DUP", sign=1))
        db_session.commit()
        db_session.add(MovementReason(code="DUP", sign=-1))
        with pytest.raises(Exception):
            db_session.commit()


# ---------------------------------------------------------------------------
# ArticleMovement – Append-Only Ledger
# ---------------------------------------------------------------------------
class TestArticleMovementLedger:
    def test_record_movement(self, db_session):
        article, *_ = _create_full_article(db_session)
        reason = MovementReason(code="SUPPLIER_INTAKE", sign=1)
        db_session.add(reason)
        db_session.flush()

        mov = ArticleMovement(
            article_id=article.id, reason_id=reason.id, notes="First intake"
        )
        db_session.add(mov)
        db_session.commit()

        db_session.refresh(article)
        assert len(article.movements) == 1
        assert article.movements[0].notes == "First intake"

    def test_movement_chain(self, db_session):
        """Simulate intake → sale → return via the append-only ledger."""
        article, *_ = _create_full_article(db_session)

        intake = MovementReason(code="SUPPLIER_INTAKE", sign=1)
        sale = MovementReason(code="SALE", sign=-1)
        ret = MovementReason(code="CUSTOMER_RETURN", sign=1)
        db_session.add_all([intake, sale, ret])
        db_session.flush()

        db_session.add(ArticleMovement(article_id=article.id, reason_id=intake.id))
        db_session.add(ArticleMovement(article_id=article.id, reason_id=sale.id))
        db_session.add(ArticleMovement(article_id=article.id, reason_id=ret.id))
        db_session.commit()

        db_session.refresh(article)
        assert len(article.movements) == 3

    def test_movement_reason_relationship(self, db_session):
        article, *_ = _create_full_article(db_session)
        reason = MovementReason(code="SALE", sign=-1)
        db_session.add(reason)
        db_session.flush()

        mov = ArticleMovement(article_id=article.id, reason_id=reason.id)
        db_session.add(mov)
        db_session.commit()
        db_session.refresh(mov)

        assert mov.reason.code == "SALE"
        assert mov.reason.sign == -1
