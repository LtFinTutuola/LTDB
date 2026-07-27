"""
Tests for Sales module CRUD operations:
  - Seller: create, read
  - Sale: create with payment method enum
  - SaleLine: link to Article, negative quantity for returns
  - Transactional sale creation via SaleRepository
"""
import pytest
from datetime import date
from decimal import Decimal

from src.models.pim import Brand, ArticleBlueprint
from src.models.wms import Supplier, Batch, Article, ArticleStatus
from src.models.sales import Seller, Sale, SaleLine, PaymentMethod
from src.schemas.sales import SellerCreate, SaleCreate, SaleLineCreate
from src.repositories.base import BaseRepository
from src.repositories.sales_repo import SaleRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
import uuid as _uuid

def _create_article(db) -> Article:
    """Creates the full chain: Brand → Blueprint, Supplier → Batch → Article."""
    uid = _uuid.uuid4().hex[:8]

    from src.models.pim import Category
    brand = Brand(name=f"SaleBrand-{uid}")
    db.add(brand)
    
    category = Category(name=f"SaleCat-{uid}", description="Sale category")
    db.add(category)
    db.flush()

    bp = ArticleBlueprint(
        brand_id=brand.id,
        category_id=category.id,
        article_name="Sale Item Name",
        description="Sale Item",
        extended_description="For sales tests",
        tags=["test"],
    )
    db.add(bp)
    db.flush()

    sup = Supplier(company_name=f"SaleSupplier-{uid}")
    db.add(sup)
    db.flush()

    batch = Batch(
        supplier_id=sup.id,
        delivery_note_number=f"DDT-SALE-{uid}",
        document_date=date(2025, 7, 1),
    )
    db.add(batch)
    db.flush()

    article = Article(
        article_blueprint_id=bp.id,
        batch_id=batch.id,
        supplier_code=f"SALE-TEST-{uid}",
        status=ArticleStatus.AVAILABLE,
    )
    db.add(article)
    db.flush()
    return article


def _create_seller(db) -> Seller:
    seller = Seller(name="Mario Rossi")
    db.add(seller)
    db.flush()
    return seller


# ---------------------------------------------------------------------------
# Seller CRUD
# ---------------------------------------------------------------------------
class TestSellerCrud:
    def test_create_seller(self, db_session):
        repo = BaseRepository(Seller)
        seller = repo.create(db_session, obj_in=SellerCreate(name="Anna"))
        assert seller.name == "Anna"
        assert seller.id is not None

    def test_read_seller(self, db_session):
        repo = BaseRepository(Seller)
        created = repo.create(db_session, obj_in=SellerCreate(name="Luca"))
        fetched = repo.get(db_session, created.id)
        assert fetched.name == "Luca"

    def test_delete_seller(self, db_session):
        repo = BaseRepository(Seller)
        seller = repo.create(db_session, obj_in=SellerCreate(name="Temp"))
        repo.delete(db_session, id=seller.id)
        assert repo.get(db_session, seller.id) is None


# ---------------------------------------------------------------------------
# Sale + SaleLine manual creation
# ---------------------------------------------------------------------------
class TestSaleManual:
    def test_create_sale_with_line(self, db_session):
        seller = _create_seller(db_session)
        article = _create_article(db_session)

        sale = Sale(
            seller_id=seller.id,
            total_paid=Decimal("120.00"),
            payment_method=PaymentMethod.CASH,
        )
        db_session.add(sale)
        db_session.flush()

        line = SaleLine(
            sale_id=sale.id,
            article_id=article.id,
            quantity=1,
            unit_price=Decimal("120.00"),
        )
        db_session.add(line)
        db_session.commit()

        db_session.refresh(sale)
        assert len(sale.lines) == 1
        assert float(sale.lines[0].unit_price) == 120.00

    def test_sale_payment_methods(self, db_session):
        seller = _create_seller(db_session)

        for method in PaymentMethod:
            sale = Sale(
                seller_id=seller.id,
                total_paid=Decimal("10.00"),
                payment_method=method,
            )
            db_session.add(sale)
        db_session.commit()

        sales = db_session.query(Sale).all()
        methods = {s.payment_method for s in sales}
        assert methods == {PaymentMethod.CASH, PaymentMethod.CREDIT_CARD, PaymentMethod.DIGITAL_WALLET}


# ---------------------------------------------------------------------------
# Negative quantity (returns / exchanges)
# ---------------------------------------------------------------------------
class TestSaleReturns:
    def test_return_via_negative_quantity(self, db_session):
        """
        A return is modeled as a SaleLine with quantity = -1 and a negative
        unit_price, keeping the receipt balanced.
        """
        seller = _create_seller(db_session)
        sold_article = _create_article(db_session)
        new_article = _create_article(db_session)

        sale = Sale(
            seller_id=seller.id,
            total_paid=Decimal("30.00"),  # net: new item 150 – returned item 120
            payment_method=PaymentMethod.CREDIT_CARD,
        )
        db_session.add(sale)
        db_session.flush()

        # Return line (negative)
        return_line = SaleLine(
            sale_id=sale.id,
            article_id=sold_article.id,
            quantity=-1,
            unit_price=Decimal("120.00"),
        )
        # New purchase line (positive)
        purchase_line = SaleLine(
            sale_id=sale.id,
            article_id=new_article.id,
            quantity=1,
            unit_price=Decimal("150.00"),
        )
        db_session.add_all([return_line, purchase_line])
        db_session.commit()

        db_session.refresh(sale)
        assert len(sale.lines) == 2

        quantities = [line.quantity for line in sale.lines]
        assert -1 in quantities
        assert 1 in quantities


# ---------------------------------------------------------------------------
# Transactional Sale via SaleRepository
# ---------------------------------------------------------------------------
class TestSaleRepository:
    def test_create_transaction_success(self, db_session):
        seller = _create_seller(db_session)
        article = _create_article(db_session)

        repo = SaleRepository()
        sale = repo.create_transaction(
            db_session,
            sale_in=SaleCreate(
                seller_id=seller.id,
                total_paid=99.99,
                payment_method=PaymentMethod.DIGITAL_WALLET,
                lines=[
                    SaleLineCreate(
                        article_id=article.id, quantity=1, unit_price=99.99
                    )
                ],
            ),
        )
        assert sale.id is not None
        assert len(sale.lines) == 1

    def test_create_transaction_rollback_on_bad_fk(self, db_session):
        """If a SaleLine references a non-existent article, the whole transaction rolls back."""
        seller = _create_seller(db_session)

        repo = SaleRepository()
        with pytest.raises(Exception):
            repo.create_transaction(
                db_session,
                sale_in=SaleCreate(
                    seller_id=seller.id,
                    total_paid=50.00,
                    payment_method=PaymentMethod.CASH,
                    lines=[
                        SaleLineCreate(
                            article_id="non-existent-uuid",
                            quantity=1,
                            unit_price=50.00,
                        )
                    ],
                ),
            )

        # Verify nothing was persisted
        assert db_session.query(Sale).count() == 0


# ---------------------------------------------------------------------------
# Unit price lock-in
# ---------------------------------------------------------------------------
class TestUnitPriceLockIn:
    def test_unit_price_independent_of_article_price(self, db_session):
        """
        The unit_price on SaleLine captures the exact checkout value.
        It must NOT change even if the article's price ledger gets updated later.
        """
        seller = _create_seller(db_session)
        article = _create_article(db_session)

        sale = Sale(
            seller_id=seller.id,
            total_paid=Decimal("100.00"),
            payment_method=PaymentMethod.CASH,
        )
        db_session.add(sale)
        db_session.flush()

        line = SaleLine(
            sale_id=sale.id,
            article_id=article.id,
            quantity=1,
            unit_price=Decimal("100.00"),
        )
        db_session.add(line)
        db_session.commit()

        # Simulate a future price change on the article (via the price ledger)
        from src.models.wms import ArticlePrice
        db_session.add(ArticlePrice(article_id=article.id, list_price=Decimal("80.00")))
        db_session.commit()

        # The sale line price must remain frozen at checkout value
        db_session.refresh(line)
        assert float(line.unit_price) == 100.00
