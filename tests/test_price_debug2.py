"""Quick test to check what get_prices returns for 600690."""
import sys
from src.tools.api import get_prices
from datetime import datetime, timedelta

end_date = datetime.now().date().isoformat()
start_date = (datetime.now() - timedelta(days=30)).date().isoformat()

with open("tests/price_debug_output.txt", "w", encoding="utf-8") as f:
    f.write(f"Fetching prices for 600690 from {start_date} to {end_date}\n")
    try:
        prices = get_prices('600690', start_date, end_date)
        if prices:
            f.write(f"Number of price entries: {len(prices)}\n")
            f.write(f"First price: {prices[0].close}\n")
            f.write(f"Last price: {prices[-1].close}\n")
            for p in prices[-5:]:
                f.write(f"  Date: {p.time}, Close: {p.close}, High: {p.high}, Low: {p.low}\n")
        else:
            f.write("No prices returned!\n")
    except Exception as e:
        f.write(f"Error: {e}\n")
    
    f.write("Done.\n")

print("Output written to tests/price_debug_output.txt")
