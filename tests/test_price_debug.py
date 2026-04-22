"""Quick test to check what get_prices returns for 600690."""
from src.tools.api import get_prices
from datetime import datetime, timedelta

end_date = datetime.now().date().isoformat()
start_date = (datetime.now() - timedelta(days=30)).date().isoformat()

print(f"Fetching prices for 600690 from {start_date} to {end_date}")
prices = get_prices('600690', start_date, end_date)

if prices:
    print(f"Number of price entries: {len(prices)}")
    print(f"First price: {prices[0].close}")
    print(f"Last price: {prices[-1].close}")
    for p in prices[-5:]:
        print(f"  Date: {p.time}, Close: {p.close}, High: {p.high}, Low: {p.low}")
else:
    print("No prices returned!")
