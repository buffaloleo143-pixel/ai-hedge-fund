import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tools.api import get_prices

outpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_output.txt")
with open(outpath, "w") as f:
    f.write("Starting test...\n")
    try:
        prices = get_prices('600690', '2026-03-20', '2026-04-20')
        if prices:
            f.write(f"Got {len(prices)} prices\n")
            f.write(f"Last close: {prices[-1].close}\n")
            f.write(f"First close: {prices[0].close}\n")
        else:
            f.write("No prices returned\n")
    except Exception as e:
        f.write(f"Error: {type(e).__name__}: {e}\n")
    f.write("Done.\n")
