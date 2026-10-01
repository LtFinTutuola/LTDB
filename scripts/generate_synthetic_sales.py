import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
import random
from sqlalchemy.orm import Session
from src.core.database import SessionLocal
from src.models.excel_sales import ExcelSale, ExcelSaleStatus

def generate_synthetic_data(days=60):
    db = SessionLocal()
    try:
        today = date.today()
        
        list_prices = {
            0: 240, 1: 210, 2: 230, 3: 240,
            4: 230, 5: 230, 6: 230, 7: 240
        }
        
        total_created = 0
        
        for d in range(days):
            target_date = today - timedelta(days=d)
            daily_sales = random.randint(5, 20)
            
            # Find max row index
            max_idx = db.query(ExcelSale).filter(ExcelSale.date == target_date).count()
            
            for i in range(daily_sales):
                col = random.choices(
                    list(range(8)), 
                    weights=[40, 10, 15, 20, 10, 1, 2, 2], 
                    k=1
                )[0]
                
                lp = list_prices.get(col, 200)
                discount_perc = random.uniform(0.1, 0.5)
                sp = round(lp * (1 - discount_perc), 2)
                is_exchange = random.random() < 0.05
                
                sale = ExcelSale(
                    date=target_date,
                    excel_row_index=max_idx + i + 1,
                    excel_file_column=col,
                    status=ExcelSaleStatus.ORPHAN,
                    starting_price=lp,
                    selling_price=sp,
                    is_exchange=is_exchange,
                    raw_article_code=f"SYNTH-{col}-{random.randint(1000,9999)}"
                )
                db.add(sale)
                total_created += 1
                
        db.commit()
        print(f"Generated {total_created} synthetic sales across {days} days.")
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    generate_synthetic_data()
