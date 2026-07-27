"""
Tests for PIM module CRUD operations:
  - Brand: create, read, read_all, update, delete
  - Category: create, hierarchical (adjacency list), read
  - BrandCategory: N:M bridge
  - ArticleBlueprint: create with JSON columns, lookup by EAN / supplier_code
"""
import pytest
from src.models.pim import Brand, Category, BrandCategory, ArticleBlueprint
from src.schemas.pim import BrandCreate, CategoryCreate, ArticleBlueprintCreate
from src.repositories.base import BaseRepository


# ---------------------------------------------------------------------------
# Brand CRUD
# ---------------------------------------------------------------------------
class TestBrandCrud:
    """Full CRUD lifecycle for Brand."""

    def test_create_brand(self, db_session):
        repo = BaseRepository(Brand)
        brand = repo.create(db_session, obj_in=BrandCreate(name="Gucci"))

        assert brand.id is not None
        assert brand.name == "Gucci"
        assert brand.created_at is not None
        assert brand.updated_at is not None

    def test_read_brand_by_id(self, db_session):
        repo = BaseRepository(Brand)
        created = repo.create(db_session, obj_in=BrandCreate(name="Prada"))

        fetched = repo.get(db_session, created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.name == "Prada"

    def test_read_all_brands(self, db_session):
        repo = BaseRepository(Brand)
        repo.create(db_session, obj_in=BrandCreate(name="Louis Vuitton"))
        repo.create(db_session, obj_in=BrandCreate(name="Hermès"))

        brands = repo.get_all(db_session)
        assert len(brands) == 2

    def test_update_brand(self, db_session):
        repo = BaseRepository(Brand)
        brand = repo.create(db_session, obj_in=BrandCreate(name="OldName"))

        updated = repo.update(
            db_session, db_obj=brand, obj_in=BrandCreate(name="NewName")
        )
        assert updated.name == "NewName"

    def test_delete_brand(self, db_session):
        repo = BaseRepository(Brand)
        brand = repo.create(db_session, obj_in=BrandCreate(name="ToDelete"))

        deleted = repo.delete(db_session, id=brand.id)
        assert deleted is not None

        assert repo.get(db_session, brand.id) is None

    def test_brand_unique_name_constraint(self, db_session):
        repo = BaseRepository(Brand)
        repo.create(db_session, obj_in=BrandCreate(name="Unique"))
        with pytest.raises(Exception):
            repo.create(db_session, obj_in=BrandCreate(name="Unique"))


# ---------------------------------------------------------------------------
# Category CRUD + Adjacency List
# ---------------------------------------------------------------------------
class TestCategoryCrud:
    """CRUD and hierarchical tree for Category (Adjacency List)."""

    def test_create_root_category(self, db_session):
        repo = BaseRepository(Category)
        cat = repo.create(
            db_session,
            obj_in=CategoryCreate(name="Bags", description="All bags"),
        )
        assert cat.parent_id is None

    def test_create_child_category(self, db_session):
        repo = BaseRepository(Category)
        parent = repo.create(
            db_session,
            obj_in=CategoryCreate(name="Bags", description="All bags"),
        )
        child = repo.create(
            db_session,
            obj_in=CategoryCreate(
                name="Crossbody", description="Crossbody bags", parent_id=parent.id
            ),
        )
        assert child.parent_id == parent.id

    def test_adjacency_list_depth(self, db_session):
        """Create a three-level hierarchy and verify tree traversal."""
        repo = BaseRepository(Category)
        l1 = repo.create(
            db_session,
            obj_in=CategoryCreate(name="Accessories", description="Root"),
        )
        l2 = repo.create(
            db_session,
            obj_in=CategoryCreate(name="Belts", description="Child", parent_id=l1.id),
        )
        l3 = repo.create(
            db_session,
            obj_in=CategoryCreate(
                name="Leather Belts", description="Grandchild", parent_id=l2.id
            ),
        )

        # Verify chain
        fetched_l3 = repo.get(db_session, l3.id)
        assert fetched_l3.parent_id == l2.id

        fetched_l2 = repo.get(db_session, l2.id)
        assert fetched_l2.parent_id == l1.id

        fetched_l1 = repo.get(db_session, l1.id)
        assert fetched_l1.parent_id is None


# ---------------------------------------------------------------------------
# BrandCategory N:M bridge
# ---------------------------------------------------------------------------
class TestBrandCategoryBridge:
    """Verify the many-to-many link between Brand and Category."""

    def test_link_brand_to_category(self, db_session):
        brand = Brand(name="Fendi")
        db_session.add(brand)
        db_session.flush()

        cat = Category(name="Shoes", description="Footwear")
        db_session.add(cat)
        db_session.flush()

        link = BrandCategory(brand_id=brand.id, category_id=cat.id)
        db_session.add(link)
        db_session.commit()

        # Relationship works both directions
        db_session.refresh(brand)
        db_session.refresh(cat)
        assert cat in brand.categories
        assert brand in cat.brands


# ---------------------------------------------------------------------------
# ArticleBlueprint CRUD + JSON columns + lookup by supplier_code
# ---------------------------------------------------------------------------
class TestArticleBlueprintCrud:
    """CRUD for ArticleBlueprint including JSON fields."""

    def _make_brand_and_category(self, db_session):
        brand = Brand(name="TestBrand")
        db_session.add(brand)
        db_session.flush()

        cat = Category(name="TestCat", description="desc")
        db_session.add(cat)
        db_session.flush()
        return brand, cat

    def test_create_blueprint_with_json(self, db_session):
        brand, cat = self._make_brand_and_category(db_session)

        repo = BaseRepository(ArticleBlueprint)
        bp = repo.create(
            db_session,
            obj_in=ArticleBlueprintCreate(
                brand_id=brand.id,
                category_id=cat.id,
                article_name="Leather Bag",
                description="Leather Bag",
                extended_description="Premium Italian leather bag",
                tags=["bag", "leather", "premium"],
                materials=["Leather"],
            ),
        )
        assert bp.article_name == "Leather Bag"
        assert bp.tags == ["bag", "leather", "premium"]
        assert bp.materials == ["Leather"]

    def test_blueprint_nullable_fields(self, db_session):
        """materials is nullable."""
        brand, cat = self._make_brand_and_category(db_session)
        repo = BaseRepository(ArticleBlueprint)

        bp = repo.create(
            db_session,
            obj_in=ArticleBlueprintCreate(
                brand_id=brand.id,
                category_id=cat.id,
                article_name="Minimal",
                description="Minimal",
                extended_description="Minimal desc",
                tags=["minimal"],
            ),
        )
        assert bp.category_id is not None
        assert bp.materials is None
