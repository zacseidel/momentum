import asyncio
from prices import PriceService
from datetime import date

async def test():
    service = PriceService()
    
    # 1. Test Date Resolution (Does it find the last trading day?)
    #    Let's pretend today is Sunday, Jan 7th, 2026 (future date)
    #    It should find the previous Friday.
    print("--- Testing Date Resolution ---")
    dates = await service.resolve_target_dates(date(2026, 1, 7))
    print(dates)
    
    # 2. Test Data Fetching (This actually hits Polygon)
    #    We'll ask for just ONE specific date to keep it fast.
    print("\n--- Testing Download ---")
    target_date = date.fromisoformat(dates['latest_trading'])
    
    # This will check DB -> fail -> fetch Polygon -> save to DB
    await service._ensure_date_data(target_date)
    
    # 3. Verify it's in the DB
    is_in_db = service._is_date_in_db(dates['latest_trading'])
    print(f"\nData saved successfully? {is_in_db}")

if __name__ == "__main__":
    asyncio.run(test())