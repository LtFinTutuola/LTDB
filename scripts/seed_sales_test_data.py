"""
scripts/seed_sales_test_data.py
--------------------------------
Generates rich, realistic test data for the Vendite (Sales) page in the local database.
Includes:
- Today's sales:
  * Reconciled articles with photos, blueprints, and brands
  * Orphan sales (with and without article codes)
  * Exchange (Reso) transaction to verify the 'Reso' chip
  * Unprocessable sale with error state
  * Full distribution across category columns (B, Sam/AT, V, PP, C, CAR, BORSELLI, O+A)
- Historical sales for recent days to populate the 'Giornata' metrics and 'Storico' charts.
"""
import sys
import os
from datetime import date, timedelta
import random

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.database import SessionLocal
from src.models.excel_sales import ExcelSale, ExcelSaleStatus
from src.models.wms import Article
from src.models.pim import ArticleBlueprint, ArticlePhoto


def seed_test_sales():
    db = SessionLocal()
    try:
        today = date.today()
        print(f"Targeting today's date: {today}")

        # 1. Fetch available articles that have photos
        articles_with_photos = (
            db.query(Article)
            .join(ArticleBlueprint, Article.article_blueprint_id == ArticleBlueprint.id)
            .join(ArticlePhoto, ArticlePhoto.article_blueprint_id == ArticleBlueprint.id)
            .limit(10)
            .all()
        )

        if not articles_with_photos:
            print("Warning: No articles with photos found in database. Querying any articles...")
            articles_with_photos = db.query(Article).limit(10).all()

        print(f"Found {len(articles_with_photos)} sample articles for reconciliation.")

        # 2. Clear existing sales for today to avoid duplicate key conflicts
        deleted_today = db.query(ExcelSale).filter(ExcelSale.date == today).delete()
        if deleted_today:
            print(f"Cleared {deleted_today} existing records for today.")

        # 3. Create today's test sales
        today_sales = [
            # 1. Reconciled Borsa
            {
                "row": 2, "col": 0, "status": ExcelSaleStatus.RECONCILED,
                "start": 260.00, "sell": 245.00, "exchange": False,
                "code": articles_with_photos[0].supplier_code if len(articles_with_photos) > 0 else "CE0746000",
                "art_id": articles_with_photos[0].id if len(articles_with_photos) > 0 else None
            },
            # 2. Reconciled Shopping Bag
            {
                "row": 3, "col": 1, "status": ExcelSaleStatus.RECONCILED,
                "start": 185.00, "sell": 169.00, "exchange": False,
                "code": articles_with_photos[1].supplier_code if len(articles_with_photos) > 1 else "CE0736000",
                "art_id": articles_with_photos[1].id if len(articles_with_photos) > 1 else None
            },
            # 3. Orphan with known code (quick search test)
            {
                "row": 4, "col": 2, "status": ExcelSaleStatus.ORPHAN,
                "start": 220.00, "sell": 198.00, "exchange": False,
                "code": articles_with_photos[2].supplier_code if len(articles_with_photos) > 2 else "CE0076000",
                "art_id": None
            },
            # 4. Reconciled Exchange (Reso)
            {
                "row": 5, "col": 3, "status": ExcelSaleStatus.RECONCILED,
                "start": 125.00, "sell": 110.00, "exchange": True,
                "code": articles_with_photos[2].supplier_code if len(articles_with_photos) > 2 else "CE0076000",
                "art_id": articles_with_photos[2].id if len(articles_with_photos) > 2 else None
            },
            # 5. Orphan with code
            {
                "row": 6, "col": 4, "status": ExcelSaleStatus.ORPHAN,
                "start": 59.90, "sell": 49.00, "exchange": False,
                "code": articles_with_photos[3].supplier_code if len(articles_with_photos) > 3 else "CE0596000",
                "art_id": None
            },
            # 6. Orphan without code
            {
                "row": 7, "col": 5, "status": ExcelSaleStatus.ORPHAN,
                "start": 75.00, "sell": 70.00, "exchange": False,
                "code": None,
                "art_id": None
            },
            # 7. Reconciled Borselli
            {
                "row": 8, "col": 6, "status": ExcelSaleStatus.RECONCILED,
                "start": 195.00, "sell": 180.00, "exchange": False,
                "code": articles_with_photos[4].supplier_code if len(articles_with_photos) > 4 else "CD0836000",
                "art_id": articles_with_photos[4].id if len(articles_with_photos) > 4 else None
            },
            # 8. Orphan Ombrelli + Accessori
            {
                "row": 9, "col": 7, "status": ExcelSaleStatus.ORPHAN,
                "start": 45.00, "sell": 39.00, "exchange": False,
                "code": None,
                "art_id": None
            },
            # 9. Reconciled Hobo Bag
            {
                "row": 10, "col": 0, "status": ExcelSaleStatus.RECONCILED,
                "start": 280.00, "sell": 265.00, "exchange": False,
                "code": articles_with_photos[5].supplier_code if len(articles_with_photos) > 5 else "CE0756000",
                "art_id": articles_with_photos[5].id if len(articles_with_photos) > 5 else None
            },
            # 10. Unprocessable line (error)
            {
                "row": 11, "col": None, "status": ExcelSaleStatus.UNPROCESSABLE,
                "start": None, "sell": None, "exchange": False,
                "code": "ERR_CORRUPTED",
                "art_id": None
            },
            # 11. Orphan Portafogli
            {
                "row": 12, "col": 3, "status": ExcelSaleStatus.ORPHAN,
                "start": 35.00, "sell": 30.00, "exchange": False,
                "code": None,
                "art_id": None
            },
            # 12. Orphan Valigeria
            {
                "row": 13, "col": 1, "status": ExcelSaleStatus.ORPHAN,
                "start": 150.00, "sell": 135.00, "exchange": False,
                "code": None,
                "art_id": None
            },
        ]

        for s in today_sales:
            sale = ExcelSale(
                date=today,
                excel_row_index=s["row"],
                excel_file_column=s["col"],
                status=s["status"],
                starting_price=s["start"],
                selling_price=s["sell"],
                is_exchange=s["exchange"],
                raw_article_code=s["code"],
                article_id=s["art_id"]
            )
            db.add(sale)

        print(f"Added {len(today_sales)} test sales for today ({today}).")

        # 4. Generate historical data for past 14 days if needed
        # Check if past days have data
        historical_days = 14
        added_historical = 0
        list_prices = [240, 210, 230, 180, 70, 90, 190, 45]

        for d in range(1, historical_days + 1):
            past_date = today - timedelta(days=d)
            existing_count = db.query(ExcelSale).filter(ExcelSale.date == past_date).count()
            if existing_count == 0:
                count_to_add = random.randint(6, 14)
                for i in range(count_to_add):
                    col = random.choices(list(range(8)), weights=[35, 15, 10, 20, 10, 3, 4, 3], k=1)[0]
                    lp = list_prices[col]
                    discount = random.uniform(0.05, 0.25)
                    sp = round(lp * (1 - discount), 2)
                    is_ex = random.random() < 0.08
                    
                    # 50% chance of being reconciled with an article
                    reconciled_art = None
                    reconciled_status = ExcelSaleStatus.ORPHAN
                    if articles_with_photos and random.random() < 0.5:
                        chosen = random.choice(articles_with_photos)
                        reconciled_art = chosen.id
                        reconciled_status = ExcelSaleStatus.RECONCILED
                        raw_c = chosen.supplier_code
                    else:
                        raw_c = f"ART-{col}-{random.randint(100, 999)}"

                    sale = ExcelSale(
                        date=past_date,
                        excel_row_index=i + 2,
                        excel_file_column=col,
                        status=reconciled_status,
                        starting_price=lp,
                        selling_price=sp,
                        is_exchange=is_ex,
                        raw_article_code=raw_c,
                        article_id=reconciled_art
                    )
                    db.add(sale)
                    added_historical += 1

        db.commit()
        if added_historical:
            print(f"Added {added_historical} historical sales across the past {historical_days} days.")
        print("Database seed completed successfully!")

    except Exception as e:
        print(f"Error during seeding: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_test_sales()
