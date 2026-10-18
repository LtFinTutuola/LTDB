"""
tests/test_pim_crud.py
-----------------------
Unit and integration tests for Brand and Category CRUD endpoints,
hierarchy validation, cascade safety, and frontend routes.
"""
import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.pim import Brand, Category, BrandCategory, ArticleBlueprint


@pytest.fixture
def client(db_session):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Brand CRUD Tests
# ---------------------------------------------------------------------------

class TestBrandEndpoints:
    def test_list_brands_details_empty(self, client):
        res = client.get("/api/v1/catalog/brands/details")
        assert res.status_code == 200
        assert res.json() == []

    def test_create_brand_success(self, client, db_session):
        # Create a macro category to associate
        cat = Category(name="Valigie", description="Desc")
        db_session.add(cat)
        db_session.commit()

        payload = {
            "name": "Samsonite",
            "category_ids": [cat.id]
        }
        res = client.post("/api/v1/catalog/brands", json=payload)
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "Samsonite"
        assert cat.id in data["category_ids"]
        assert "Valigie" in data["category_names"]
        assert data["blueprint_count"] == 0

    def test_create_brand_empty_name_rejected(self, client):
        res = client.post("/api/v1/catalog/brands", json={"name": "   "})
        assert res.status_code == 400
        assert "non può essere vuoto" in res.json()["detail"]

    def test_create_brand_duplicate_name_rejected(self, client, db_session):
        b = Brand(name="The Bridge")
        db_session.add(b)
        db_session.commit()

        res = client.post("/api/v1/catalog/brands", json={"name": "the bridge"})
        assert res.status_code == 400
        assert "esiste già" in res.json()["detail"]

    def test_update_brand_name(self, client, db_session):
        b = Brand(name="OldBrand")
        db_session.add(b)
        db_session.commit()

        res = client.put(f"/api/v1/catalog/brands/{b.id}", json={"name": "NewBrand"})
        assert res.status_code == 200
        assert res.json()["name"] == "NewBrand"

    def test_update_brand_categories(self, client, db_session):
        b = Brand(name="Piquadro")
        cat1 = Category(name="Zaini", description="Zaini desc")
        cat2 = Category(name="Trolley", description="Trolley desc")
        db_session.add_all([b, cat1, cat2])
        db_session.commit()

        # Update categories to only cat2
        res = client.put(f"/api/v1/catalog/brands/{b.id}", json={"category_ids": [cat2.id]})
        assert res.status_code == 200
        data = res.json()
        assert data["category_ids"] == [cat2.id]
        assert data["category_names"] == ["Trolley"]

    def test_update_brand_not_found(self, client):
        res = client.put("/api/v1/catalog/brands/nonexistent-id", json={"name": "Brand"})
        assert res.status_code == 404

    def test_delete_brand_success(self, client, db_session):
        b = Brand(name="BrandToDelete")
        cat = Category(name="Accessori", description="Desc")
        db_session.add_all([b, cat])
        db_session.commit()

        link = BrandCategory(brand_id=b.id, category_id=cat.id)
        db_session.add(link)
        db_session.commit()

        res = client.delete(f"/api/v1/catalog/brands/{b.id}")
        assert res.status_code == 200
        assert res.json() == {"status": "deleted", "id": b.id}

        # Verify deletion and clean link
        assert db_session.query(Brand).filter_by(id=b.id).first() is None
        assert db_session.query(BrandCategory).filter_by(brand_id=b.id).first() is None

    def test_delete_brand_with_blueprints_rejected(self, client, db_session):
        b = Brand(name="ActiveBrand")
        db_session.add(b)
        db_session.commit()

        bp = ArticleBlueprint(
            brand_id=b.id,
            article_name="Articolo Test",
            description="Desc",
            extended_description="Ext Desc",
            tags=["tag"],
            materials=["mat"]
        )
        db_session.add(bp)
        db_session.commit()

        res = client.delete(f"/api/v1/catalog/brands/{b.id}")
        assert res.status_code == 400
        assert "articoli/blueprint associati" in res.json()["detail"]

    def test_delete_brand_not_found(self, client):
        res = client.delete("/api/v1/catalog/brands/nonexistent-id")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# Category CRUD & Hierarchy Tests
# ---------------------------------------------------------------------------

class TestCategoryEndpoints:
    def test_list_categories_empty(self, client):
        res = client.get("/api/v1/catalog/categories")
        assert res.status_code == 200
        assert res.json() == []

    def test_create_macro_category_with_brands(self, client, db_session):
        b = Brand(name="Roncato")
        db_session.add(b)
        db_session.commit()

        payload = {
            "name": "Valigeria",
            "description": "Bagagli e trolley",
            "parent_id": None,
            "brand_ids": [b.id]
        }
        res = client.post("/api/v1/catalog/categories", json=payload)
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "Valigeria"
        assert data["description"] == "Bagagli e trolley"
        assert data["parent_id"] is None
        assert b.id in data["brand_ids"]
        assert "Roncato" in data["brand_names"]

    def test_create_subcategory(self, client, db_session):
        macro = Category(name="Borse", description="Tutte le borse")
        db_session.add(macro)
        db_session.commit()

        payload = {
            "name": "Borse a Spalla",
            "description": "Modelli con tracolla o manici lunghi",
            "parent_id": macro.id
        }
        res = client.post("/api/v1/catalog/categories", json=payload)
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "Borse a Spalla"
        assert data["parent_id"] == macro.id
        assert data["parent_name"] == "Borse"

    def test_create_category_empty_name_rejected(self, client):
        res = client.post("/api/v1/catalog/categories", json={"name": "   ", "description": ""})
        assert res.status_code == 400
        assert "non può essere vuoto" in res.json()["detail"]

    def test_create_category_duplicate_name_same_parent_rejected(self, client, db_session):
        macro = Category(name="Cinture", description="Desc")
        db_session.add(macro)
        db_session.commit()

        res = client.post("/api/v1/catalog/categories", json={"name": "cinture", "parent_id": None})
        assert res.status_code == 400
        assert "esiste già sotto questo livello" in res.json()["detail"]

    def test_create_subcategory_invalid_parent(self, client):
        res = client.post("/api/v1/catalog/categories", json={"name": "Sub", "parent_id": "invalid-id"})
        assert res.status_code == 404
        assert "Categoria genitore non trovata" in res.json()["detail"]

    def test_update_category_name_and_desc(self, client, db_session):
        cat = Category(name="Zaini", description="Vecchia desc")
        db_session.add(cat)
        db_session.commit()

        res = client.put(f"/api/v1/catalog/categories/{cat.id}", json={
            "name": "Zaini e Marsupi",
            "description": "Nuova desc"
        })
        assert res.status_code == 200
        data = res.json()
        assert data["name"] == "Zaini e Marsupi"
        assert data["description"] == "Nuova desc"

    def test_update_category_self_parent_rejected(self, client, db_session):
        cat = Category(name="Pelletteria", description="Desc")
        db_session.add(cat)
        db_session.commit()

        res = client.put(f"/api/v1/catalog/categories/{cat.id}", json={"parent_id": cat.id})
        assert res.status_code == 400
        assert "non può essere genitore di se stessa" in res.json()["detail"]

    def test_update_category_circular_reference_rejected(self, client, db_session):
        macro = Category(name="RootCat", description="Desc")
        db_session.add(macro)
        db_session.commit()

        sub = Category(name="SubCat", description="Desc", parent_id=macro.id)
        db_session.add(sub)
        db_session.commit()

        # Trying to make macro a child of its own child 'sub'
        res = client.put(f"/api/v1/catalog/categories/{macro.id}", json={"parent_id": sub.id})
        assert res.status_code == 400
        assert "Riferimento circolare" in res.json()["detail"]

    def test_delete_category_success(self, client, db_session):
        cat = Category(name="ToDelete", description="Desc")
        db_session.add(cat)
        db_session.commit()

        res = client.delete(f"/api/v1/catalog/categories/{cat.id}")
        assert res.status_code == 200
        assert res.json() == {"status": "deleted", "id": cat.id}
        assert db_session.query(Category).filter_by(id=cat.id).first() is None

    def test_delete_category_with_subcategories_rejected(self, client, db_session):
        macro = Category(name="ParentCat", description="Desc")
        db_session.add(macro)
        db_session.commit()

        sub = Category(name="ChildCat", description="Desc", parent_id=macro.id)
        db_session.add(sub)
        db_session.commit()

        res = client.delete(f"/api/v1/catalog/categories/{macro.id}")
        assert res.status_code == 400
        assert "contiene 1 sottocategorie" in res.json()["detail"]

    def test_delete_category_with_blueprints_rejected(self, client, db_session):
        brand = Brand(name="CatBrand")
        cat = Category(name="UsedCat", description="Desc")
        db_session.add_all([brand, cat])
        db_session.commit()

        bp = ArticleBlueprint(
            brand_id=brand.id,
            category_id=cat.id,
            article_name="Articolo con Categoria",
            description="Desc",
            extended_description="Ext",
            tags=["t"],
            materials=["m"]
        )
        db_session.add(bp)
        db_session.commit()

        res = client.delete(f"/api/v1/catalog/categories/{cat.id}")
        assert res.status_code == 400
        assert "articoli/blueprint associati" in res.json()["detail"]

    def test_delete_category_not_found(self, client):
        res = client.delete("/api/v1/catalog/categories/nonexistent-id")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# Frontend Page Routes Tests
# ---------------------------------------------------------------------------

class TestFrontendRoutes:
    def test_brands_page(self, client):
        res = client.get("/brands")
        assert res.status_code == 200
        assert "Gestione Brand" in res.text
        assert "modal-brand" in res.text

    def test_categories_page(self, client):
        res = client.get("/categories")
        assert res.status_code == 200
        assert "Gestione Categorie" in res.text
        assert "modal-category" in res.text

    def test_categorie_redirect(self, client):
        res = client.get("/categorie", follow_redirects=False)
        assert res.status_code == 307
        assert res.headers["location"] == "/categories"

    def test_brands_always_ordered_alphabetically(self, client, db_session):
        names = ["Zebra", "alfa", "Beta", "charlie"]
        for n in names:
            db_session.add(Brand(name=n))
        db_session.commit()

        # 1. Check /api/v1/catalog/brands
        res = client.get("/api/v1/catalog/brands")
        assert res.status_code == 200
        brand_names = [b["name"] for b in res.json()]
        assert brand_names == ["alfa", "Beta", "charlie", "Zebra"]

        # 2. Check /api/v1/catalog/brands/details
        res_details = client.get("/api/v1/catalog/brands/details")
        assert res_details.status_code == 200
        detail_names = [b["name"] for b in res_details.json()]
        assert detail_names == ["alfa", "Beta", "charlie", "Zebra"]

        # 3. Check /magazzino index template injection
        res_index = client.get("/magazzino")
        assert res_index.status_code == 200
        import json
        found_injection = False
        for line in res_index.text.splitlines():
            if "window.__LTDB_BRANDS__ =" in line:
                raw_json = line.split("window.__LTDB_BRANDS__ =")[1].strip().rstrip(";")
                injected_brands = json.loads(raw_json)
                injected_names = [b["name"] for b in injected_brands]
                assert injected_names == ["alfa", "Beta", "charlie", "Zebra"]
                found_injection = True
                break
        assert found_injection is True

