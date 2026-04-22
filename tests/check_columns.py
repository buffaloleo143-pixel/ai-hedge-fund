import akshare as ak
from src.tools.akshare_provider import AKShareRateLimiter

symbol = "600690"
for report_type in ["资产负债表", "利润表", "现金流量表"]:
    df = AKShareRateLimiter.call_with_retry(
        ak.stock_financial_report_sina,
        stock=symbol,
        symbol=report_type,
    )
    if df is not None and not df.empty:
        print(f"\n{report_type} columns:")
        for col in df.columns:
            print(f"  {col}")
        print(f"  Rows: {len(df)}")
    else:
        print(f"\n{report_type}: No data")
