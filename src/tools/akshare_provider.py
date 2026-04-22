"""
AKShare 数据提供器 —— 优先直接 HTTP API，AKShare 作为 fallback。
返回类型与 src/tools/api.py 中对应函数完全一致。
"""

import datetime
import json
import logging
import random
import subprocess
import time
from urllib.parse import urlencode

import akshare as ak
import pandas as pd
import requests

from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    InsiderTrade,
    LineItem,
    Price,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 速率限制器
# ---------------------------------------------------------------------------

class AKShareRateLimiter:
    """简单的速率限制器，防止 AKShare 调用过于频繁被封。"""

    _last_call_time: float = 0
    _default_delay: float = 2  # 秒，降低默认延迟

    @classmethod
    def wait(cls, delay: float | None = None) -> None:
        d = delay or cls._default_delay
        elapsed = time.time() - cls._last_call_time
        if elapsed < d:
            sleep_time = d - elapsed + random.uniform(0, 0.3)
            time.sleep(sleep_time)
        cls._last_call_time = time.time()

    @classmethod
    def call_with_retry(
        cls,
        func,
        *args,
        max_retries: int = 2,  # 降到 2 次，避免在注定失败的调用上浪费时间
        delay: float = 2,
        **kwargs,
    ):
        """带重试的 AKShare 调用。返回 DataFrame / None。"""
        for attempt in range(max_retries):
            try:
                cls.wait(delay)
                result = func(*args, **kwargs)
                if result is not None and not (hasattr(result, "empty") and result.empty):
                    return result
                # 空结果也做重试
                if attempt < max_retries - 1:
                    time.sleep(delay * (2 ** attempt))
            except (ConnectionError, ConnectionResetError) as e:
                logger.warning(f"AKShare connection error (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    backoff = delay * (2 ** attempt) + random.uniform(1, 3)
                    logger.info(f"RemoteDisconnected backoff: sleeping {backoff:.1f}s")
                    time.sleep(backoff)
                else:
                    logger.error(f"AKShare call ultimately failed due to connection error: {e}")
            except Exception as e:
                error_str = str(e).lower()
                if any(err in error_str for err in ["remote", "disconnect", "connection", "reset"]):
                    logger.warning(f"AKShare remote/connection error (attempt {attempt + 1}): {e}")
                    if attempt < max_retries - 1:
                        backoff = delay * (2 ** attempt) + random.uniform(1, 3)
                        logger.info(f"RemoteDisconnected backoff: sleeping {backoff:.1f}s")
                        time.sleep(backoff)
                    else:
                        logger.error(f"AKShare call ultimately failed: {e}")
                else:
                    logger.warning(f"AKShare call failed (attempt {attempt + 1}): {e}")
                    if attempt < max_retries - 1:
                        time.sleep(delay * (2 ** attempt))
                    else:
                        logger.error(f"AKShare call ultimately failed: {e}")
        return None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _clean_ticker(ticker: str) -> str:
    """将 ticker 清理为纯 6 位数字（AKShare 内部自动识别沪深）。"""
    cleaned = ticker.strip().lower()
    for prefix in ("sh", "sz", "bj"):
        cleaned = cleaned.removeprefix(prefix)
    return cleaned.strip()


def _date_to_akshare(date_str: str) -> str:
    """'YYYY-MM-DD' -> 'YYYYMMDD'"""
    return date_str.replace("-", "")


def _safe_float(val) -> float | None:
    """尝试转换为 float，失败返回 None。"""
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> int | None:
    """尝试转换为 int，失败返回 None。"""
    try:
        f = _safe_float(val)
        if f is None:
            return None
        return int(f)
    except (ValueError, TypeError):
        return None


def _ticker_to_prefix(symbol: str) -> str:
    """根据股票代码返回 sh/sz/bj 前缀。"""
    if symbol.startswith("6"):
        return "sh"
    elif symbol.startswith(("0", "3")):
        return "sz"
    elif symbol.startswith(("4", "8")):
        return "bj"
    return "sh"


# ---------------------------------------------------------------------------
# 直接 HTTP 数据源（优先级最高）
# ---------------------------------------------------------------------------

def _get_prices_tencent(symbol: str, start_date: str, end_date: str, adjust: str = "hfq") -> list[Price]:
    """
    直接请求腾讯财经 API 获取 K 线数据。

    adjust: qfq (前复权), hfq (后复权), 其他 (不复权)
    """
    prefix = _ticker_to_prefix(symbol)
    full_symbol = f"{prefix}{symbol}"

    # 腾讯 API: param=symbol,day,start,end,count,adjust
    # count 设大些，确保覆盖整个区间
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={full_symbol},day,{start_date},{end_date},500,{adjust}"

    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        stock_data = data.get("data", {}).get(full_symbol, {})

        # 根据复权类型选择数据字段
        if adjust == "qfq":
            klines = stock_data.get("qfqday", [])
        elif adjust == "hfq":
            klines = stock_data.get("hfqday", [])
        else:
            klines = stock_data.get("day", [])

        # 如果指定复权没有数据，尝试不复权
        if not klines:
            klines = stock_data.get("day", [])

        if not klines:
            logger.warning(f"Tencent API: no klines for {full_symbol}")
            return []

        prices = []
        for k in klines:
            try:
                # 腾讯格式: [日期, 开盘, 收盘, 最高, 最低, 成交量]
                if len(k) < 5:
                    continue
                trade_date = k[0]
                # 过滤日期范围（腾讯可能返回比请求范围更多的数据）
                if trade_date < start_date or trade_date > end_date:
                    continue

                prices.append(Price(
                    open=float(k[1]),
                    close=float(k[2]),
                    high=float(k[3]),
                    low=float(k[4]),
                    volume=int(float(k[5])) if len(k) > 5 else 0,
                    time=trade_date,
                ))
            except Exception as e:
                logger.warning(f"Tencent API: skip row for {full_symbol}: {e}")
                continue

        logger.info(f"Tencent API: got {len(prices)} prices for {full_symbol}")
        return prices
    except Exception as e:
        logger.warning(f"Tencent API failed for {full_symbol}: {e}")
        return []


def _get_market_cap_tencent(symbol: str) -> float | None:
    """通过腾讯实时行情 API 获取总市值（元）。"""
    prefix = _ticker_to_prefix(symbol)
    full_symbol = f"{prefix}{symbol}"
    url = f"https://qt.gtimg.cn/q={full_symbol}"

    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        # 格式: v_sh600690="1~名称~代码~最新价~...~总市值(亿)~..."
        text = r.text
        if "~" not in text:
            return None

        parts = text.split("~")
        if len(parts) > 45:
            market_cap_yi = _safe_float(parts[45])
            if market_cap_yi is not None:
                return market_cap_yi * 1e8  # 亿 -> 元
    except Exception as e:
        logger.warning(f"Tencent market cap API failed for {symbol}: {e}")
    return None


def _get_valuation_from_tencent(symbol: str) -> dict:
    """从腾讯财经获取估值指标：PE、PB、市值、当前价格"""
    try:
        prefix = "sh" if symbol.startswith("6") else "sz"
        url = f"https://qt.gtimg.cn/q={prefix}{symbol}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return {}
        parts = r.text.split("~")
        if len(parts) < 50:
            return {}
        result = {}
        # 当前价格 at position 3
        if parts[3] and parts[3] != "0" and parts[3] != "":
            try:
                result["current_price"] = float(parts[3])
            except (ValueError, TypeError):
                pass
        # PE ratio at position 39
        if parts[39] and parts[39] != "0" and parts[39] != "":
            try:
                result["price_to_earnings_ratio"] = float(parts[39])
            except (ValueError, TypeError):
                pass
        # PB ratio at position 46
        if parts[46] and parts[46] != "0" and parts[46] != "":
            try:
                result["price_to_book_ratio"] = float(parts[46])
            except (ValueError, TypeError):
                pass
        # 总市值 at position 45 (单位：亿)
        if parts[45] and parts[45] != "0" and parts[45] != "":
            try:
                result["market_cap"] = float(parts[45]) * 1e8
            except (ValueError, TypeError):
                pass
        return result
    except Exception as e:
        logger.warning(f"Tencent valuation failed for {symbol}: {e}")
        return {}


def _compute_derived_metrics(symbol: str, end_date: str, market_cap: float = None, current_price: float = None) -> dict:
    """从三大报表数据计算衍生财务指标"""
    try:
        items = search_line_items_ak(symbol, [
            "total_revenue", "cost_of_revenue", "operating_income", "net_income",
            "total_assets", "total_liabilities", "total_equity",
            "current_assets", "current_liabilities",
            "cash_and_equivalents", "inventory", "accounts_receivable",
            "operating_cash_flow", "interest_expense",
            "short_term_debt", "long_term_debt",
            "paid_in_capital"
        ], end_date, "annual", 2)

        if not items:
            return {}

        latest = items[0]
        result = {}

        # 安全获取属性值
        def get_val(item, field):
            v = getattr(item, field, None)
            if v is None:
                v = getattr(item, field.replace("_", ""), None)
            return float(v) if v is not None else None

        total_revenue = get_val(latest, "total_revenue")
        cost_of_revenue = get_val(latest, "cost_of_revenue")
        operating_income = get_val(latest, "operating_income")
        net_income = get_val(latest, "net_income")
        total_assets = get_val(latest, "total_assets")
        total_liabilities = get_val(latest, "total_liabilities")
        total_equity = get_val(latest, "total_equity")
        current_assets = get_val(latest, "current_assets")
        current_liabilities = get_val(latest, "current_liabilities")
        cash = get_val(latest, "cash_and_equivalents")
        inventory = get_val(latest, "inventory")
        accounts_receivable = get_val(latest, "accounts_receivable")
        operating_cash_flow = get_val(latest, "operating_cash_flow")
        interest_expense = get_val(latest, "interest_expense")
        short_term_debt = get_val(latest, "short_term_debt") or 0
        long_term_debt = get_val(latest, "long_term_debt") or 0
        paid_in_capital = get_val(latest, "paid_in_capital")

        # 如果 total_equity 不可直接获取，从总资产减总负债推算
        if total_equity is None and total_assets and total_liabilities:
            total_equity = total_assets - total_liabilities

        # gross_margin = (total_revenue - cost_of_revenue) / total_revenue
        if total_revenue and cost_of_revenue and total_revenue != 0:
            result["gross_margin"] = (total_revenue - cost_of_revenue) / total_revenue

        # operating_margin
        if operating_income and total_revenue and total_revenue != 0:
            result["operating_margin"] = operating_income / total_revenue

        # net_margin
        if net_income and total_revenue and total_revenue != 0:
            result["net_margin"] = net_income / total_revenue

        # return_on_equity
        if net_income and total_equity and total_equity != 0:
            result["return_on_equity"] = net_income / total_equity

        # return_on_assets
        if net_income and total_assets and total_assets != 0:
            result["return_on_assets"] = net_income / total_assets

        # asset_turnover
        if total_revenue and total_assets and total_assets != 0:
            result["asset_turnover"] = total_revenue / total_assets

        # inventory_turnover
        if cost_of_revenue and inventory and inventory != 0:
            result["inventory_turnover"] = cost_of_revenue / inventory

        # receivables_turnover
        if total_revenue and accounts_receivable and accounts_receivable != 0:
            result["receivables_turnover"] = total_revenue / accounts_receivable

        # days_sales_outstanding
        if "receivables_turnover" in result and result["receivables_turnover"] != 0:
            result["days_sales_outstanding"] = 365.0 / result["receivables_turnover"]

        # operating_cycle (if inventory_turnover available)
        if "inventory_turnover" in result and result["inventory_turnover"] != 0:
            days_inventory = 365.0 / result["inventory_turnover"]
            if "days_sales_outstanding" in result:
                result["operating_cycle"] = days_inventory + result["days_sales_outstanding"]

        # working_capital_turnover
        if total_revenue and current_assets and current_liabilities:
            working_capital = current_assets - current_liabilities
            if working_capital != 0:
                result["working_capital_turnover"] = total_revenue / working_capital

        # debt_to_equity
        if total_liabilities and total_equity and total_equity != 0:
            result["debt_to_equity"] = total_liabilities / total_equity

        # debt_to_assets
        if total_liabilities and total_assets and total_assets != 0:
            result["debt_to_assets"] = total_liabilities / total_assets

        # cash_ratio
        if cash and current_liabilities and current_liabilities != 0:
            result["cash_ratio"] = cash / current_liabilities

        # operating_cash_flow_ratio
        if operating_cash_flow and current_liabilities and current_liabilities != 0:
            result["operating_cash_flow_ratio"] = operating_cash_flow / current_liabilities

        # interest_coverage
        if operating_income and interest_expense and interest_expense != 0:
            result["interest_coverage"] = operating_income / interest_expense

        # free_cash_flow_per_share (FCF = operating_cash_flow, 简化)
        if operating_cash_flow and paid_in_capital and paid_in_capital != 0:
            fcf = operating_cash_flow  # 简化：不减capex因为可能没有该数据
            result["free_cash_flow_per_share"] = fcf / (paid_in_capital * 1e4)  # 股本单位可能是万股
        elif operating_cash_flow and market_cap and current_price and current_price != 0:
            # 回退：用市值和当前价推算总股数
            total_shares = market_cap / current_price
            if total_shares != 0:
                result["free_cash_flow_per_share"] = operating_cash_flow / total_shares

        # earnings_per_share (net_income / shares; 简化用 paid_in_capital 推算)
        if net_income and paid_in_capital and paid_in_capital != 0:
            result["earnings_per_share"] = net_income / (paid_in_capital * 1e4)
        elif net_income and market_cap and current_price and current_price != 0:
            # 回退：用市值和当前价推算总股数
            total_shares = market_cap / current_price
            if total_shares != 0:
                result["earnings_per_share"] = net_income / total_shares

        # book_value_per_share
        if total_equity and paid_in_capital and paid_in_capital != 0:
            result["book_value_per_share"] = total_equity / (paid_in_capital * 1e4)
        elif total_equity and market_cap and current_price and current_price != 0:
            # 回退：用市值和当前价推算总股数
            total_shares = market_cap / current_price
            if total_shares != 0:
                result["book_value_per_share"] = total_equity / total_shares

        # enterprise_value (需要市值)
        if market_cap:
            total_debt = short_term_debt + long_term_debt
            cash_val = cash or 0
            result["enterprise_value"] = market_cap + total_debt - cash_val

            # price_to_sales_ratio
            if total_revenue and total_revenue != 0:
                result["price_to_sales_ratio"] = market_cap / total_revenue

            # enterprise_value_to_revenue_ratio
            if total_revenue and total_revenue != 0:
                result["enterprise_value_to_revenue_ratio"] = result["enterprise_value"] / total_revenue

            # free_cash_flow_yield = FCF / EV
            if operating_cash_flow and result["enterprise_value"] != 0:
                result["free_cash_flow_yield"] = operating_cash_flow / result["enterprise_value"]

        # ROIC (简化: operating_income / (total_equity + long_term_debt))
        invested_capital = (total_equity or 0) + long_term_debt
        if operating_income and invested_capital != 0:
            result["return_on_invested_capital"] = operating_income / invested_capital

        # 增长率计算（需要两期数据）
        if len(items) >= 2:
            prev = items[1]
            prev_equity = get_val(prev, "total_equity")
            # 如果 prev_equity 不可直接获取，从总资产减总负债推算
            if prev_equity is None:
                prev_assets = get_val(prev, "total_assets")
                prev_liabilities = get_val(prev, "total_liabilities")
                if prev_assets and prev_liabilities:
                    prev_equity = prev_assets - prev_liabilities
            prev_operating_income = get_val(prev, "operating_income")
            prev_net_income = get_val(prev, "net_income")
            prev_ocf = get_val(prev, "operating_cash_flow")
            prev_revenue = get_val(prev, "total_revenue")

            if total_equity and prev_equity and prev_equity != 0:
                result["book_value_growth"] = (total_equity - prev_equity) / abs(prev_equity)

            if operating_income and prev_operating_income and prev_operating_income != 0:
                result["operating_income_growth"] = (operating_income - prev_operating_income) / abs(prev_operating_income)

            if operating_cash_flow and prev_ocf and prev_ocf != 0:
                result["free_cash_flow_growth"] = (operating_cash_flow - prev_ocf) / abs(prev_ocf)

            # revenue_growth
            if total_revenue and prev_revenue and prev_revenue != 0:
                result["revenue_growth"] = (total_revenue - prev_revenue) / abs(prev_revenue)

            # earnings_growth
            if net_income and prev_net_income and prev_net_income != 0:
                result["earnings_growth"] = (net_income - prev_net_income) / abs(prev_net_income)

            # earnings_per_share_growth (如果两期EPS都可计算)
            latest_shares = None
            if paid_in_capital and paid_in_capital != 0:
                latest_shares = paid_in_capital * 1e4
            elif market_cap and current_price and current_price != 0:
                latest_shares = market_cap / current_price
            if latest_shares and latest_shares != 0:
                latest_eps = net_income / latest_shares if net_income else None
                prev_eps = prev_net_income / latest_shares if prev_net_income else None
                if latest_eps and prev_eps and prev_eps != 0:
                    result["earnings_per_share_growth"] = (latest_eps - prev_eps) / abs(prev_eps)

        return result
    except Exception as e:
        logger.warning(f"Derived metrics computation failed for {symbol}: {e}")
        return {}


# ---------------------------------------------------------------------------
# AKShare Sina fallback
# ---------------------------------------------------------------------------

def _get_prices_via_akshare_sina(symbol: str, start_date: str, end_date: str, adjust: str = "hfq") -> list[Price]:
    """
    使用 AKShare 新浪数据源获取价格数据（备选方案，绕过东方财富反爬虫）。
    """
    try:
        prefix = _ticker_to_prefix(symbol)
        sina_symbol = f"{prefix}{symbol}"

        df = ak.stock_zh_a_daily(symbol=sina_symbol, adjust=adjust if adjust else "")
        if df is None or df.empty:
            logger.warning(f"AKShare sina fallback: no data for {symbol}")
            return []

        # 过滤日期范围
        df["date"] = pd.to_datetime(df["date"])
        mask = (df["date"] >= start_date) & (df["date"] <= end_date)
        df = df[mask]
        if df.empty:
            logger.warning(f"AKShare sina fallback: no data in range {start_date}~{end_date} for {symbol}")
            return []

        prices = []
        for _, row in df.iterrows():
            try:
                prices.append(Price(
                    open=float(row["open"]),
                    close=float(row["close"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    volume=int(float(row["volume"])),
                    time=str(row["date"])[:10],
                ))
            except Exception as e:
                logger.warning(f"AKShare sina fallback: skip row for {symbol}: {e}")
        return prices
    except Exception as e:
        logger.error(f"AKShare sina fallback (prices) failed for {symbol}: {e}")
        return []


def _get_market_cap_via_akshare_sina(symbol: str) -> float | None:
    """
    使用 AKShare 新浪数据源估算市值（收盘价 × 总股本）。
    """
    try:
        prefix = _ticker_to_prefix(symbol)
        sina_symbol = f"{prefix}{symbol}"

        df = ak.stock_zh_a_daily(symbol=sina_symbol, adjust="")
        if df is None or df.empty:
            return None

        latest = df.iloc[-1]
        close = _safe_float(latest.get("close"))
        shares = _safe_float(latest.get("outstanding_share"))
        if close and shares:
            return close * shares
    except Exception as e:
        logger.error(f"AKShare sina fallback (market cap) failed for {symbol}: {e}")
    return None


def _get_financial_data_via_curl(symbol: str) -> dict | None:
    """
    通过 subprocess curl 获取东方财富财务摘要数据（实验性）。
    """
    url = (
        f"https://datacenter.eastmoney.com/securities/api/data/get"
        f"?type=RPT_F10_FINANCE_MAINFINADATA"
        f"&sty=ALL"
        f"&filter=(SECURITY_CODE=%22{symbol}%22)"
        f"&p=1&ps=10&sr=-1&st=REPORT_DATE"
        f"&token=894050c76af8597a853f5b408b759f5d"
    )

    cmd = [
        "curl.exe", "-s",
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-H", "Referer: https://emweb.securities.eastmoney.com/",
        url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)
    except Exception as e:
        logger.error(f"curl financial data failed for {symbol}: {e}")
        return None


# ---------------------------------------------------------------------------
# 1. get_prices_ak
# ---------------------------------------------------------------------------

def get_prices_ak(
    ticker: str,
    start_date: str,
    end_date: str,
    adjust: str = None,
) -> list[Price]:
    """
    获取 A 股日 K 线。

    Args:
        adjust: "hfq" (后复权, 默认), "qfq" (前复权), "" (不复权)

    策略: 1) 腾讯直接 HTTP → 2) AKShare Sina → 3) 失败返回 []
    """
    try:
        symbol = _clean_ticker(ticker)
        effective_adjust = adjust if adjust is not None else "hfq"

        # 策略 1: 腾讯财经 API（最快，不受东财反爬影响）
        prices = _get_prices_tencent(symbol, start_date, end_date, adjust=effective_adjust)
        if prices:
            return prices

        # 策略 2: AKShare Sina 数据源
        logger.info(f"Tencent API failed for {symbol}, trying AKShare sina fallback...")
        prices = _get_prices_via_akshare_sina(symbol, start_date, end_date, adjust=effective_adjust)
        if prices:
            return prices

        logger.error(f"All data sources failed for prices: {symbol}")
        return []

    except Exception as e:
        logger.error(f"get_prices_ak failed for {ticker}: {e}")
        return []


# ---------------------------------------------------------------------------
# 2. get_financial_metrics_ak
# ---------------------------------------------------------------------------

# AKShare stock_financial_analysis_indicator 列名 -> FinancialMetrics 字段
_FIN_METRICS_COLUMN_MAP = {
    "毛利率": "gross_margin",
    "净利率": "net_margin",
    "净资产收益率": "return_on_equity",
    "总资产净利润率": "return_on_assets",
    "流动比率": "current_ratio",
    "速动比率": "quick_ratio",
    "每股收益": "earnings_per_share",
    "每股净资产": "book_value_per_share",
    "营业收入同比增长率": "revenue_growth",
    "净利润同比增长率": "earnings_growth",
    "总资产同比增长率": None,
    "摊薄每股收益": "earnings_per_share",
    "资产负债比率": "debt_to_assets",
}


def get_financial_metrics_ak(
    ticker: str,
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
) -> list[FinancialMetrics]:
    """
    使用 AKShare 获取财务指标。

    Returns: list[FinancialMetrics]，与 api.get_financial_metrics 返回类型一致。
    """
    try:
        symbol = _clean_ticker(ticker)
        start_year = str(int(end_date[:4]) - limit)

        df = AKShareRateLimiter.call_with_retry(
            ak.stock_financial_analysis_indicator,
            symbol=symbol,
            start_year=start_year,
        )
        if df is None or df.empty:
            logger.warning(f"AKShare: no financial metrics for {ticker}")
            return []

        results: list[FinancialMetrics] = []
        for _, row in df.iterrows():
            try:
                report_date = str(row.get("日期", ""))[:10]
                if not report_date:
                    continue

                if report_date > end_date:
                    continue

                eps = _safe_float(row.get("每股收益") or row.get("摊薄每股收益"))
                bvps = _safe_float(row.get("每股净资产"))
                gross_margin = _safe_float(row.get("毛利率"))
                net_margin = _safe_float(row.get("净利率"))
                roe = _safe_float(row.get("净资产收益率"))
                roa = _safe_float(row.get("总资产净利润率"))
                current_ratio = _safe_float(row.get("流动比率"))
                quick_ratio = _safe_float(row.get("速动比率"))
                revenue_growth = _safe_float(row.get("营业收入同比增长率"))
                earnings_growth = _safe_float(row.get("净利润同比增长率"))
                debt_to_assets = _safe_float(row.get("资产负债比率"))

                peg_ratio = None
                if eps and eps != 0 and earnings_growth is not None:
                    pass

                metrics = FinancialMetrics(
                    ticker=ticker,
                    report_period=report_date,
                    period="annual" if period == "ttm" else period,
                    currency="CNY",
                    market_cap=None,
                    enterprise_value=None,
                    price_to_earnings_ratio=None,
                    price_to_book_ratio=None,
                    price_to_sales_ratio=None,
                    enterprise_value_to_ebitda_ratio=None,
                    enterprise_value_to_revenue_ratio=None,
                    free_cash_flow_yield=None,
                    peg_ratio=peg_ratio,
                    gross_margin=gross_margin,
                    operating_margin=None,
                    net_margin=net_margin,
                    return_on_equity=roe,
                    return_on_assets=roa,
                    return_on_invested_capital=None,
                    asset_turnover=None,
                    inventory_turnover=None,
                    receivables_turnover=None,
                    days_sales_outstanding=None,
                    operating_cycle=None,
                    working_capital_turnover=None,
                    current_ratio=current_ratio,
                    quick_ratio=quick_ratio,
                    cash_ratio=None,
                    operating_cash_flow_ratio=None,
                    debt_to_equity=None,
                    debt_to_assets=debt_to_assets,
                    interest_coverage=None,
                    revenue_growth=revenue_growth,
                    earnings_growth=earnings_growth,
                    book_value_growth=None,
                    earnings_per_share_growth=None,
                    free_cash_flow_growth=None,
                    operating_income_growth=None,
                    ebitda_growth=None,
                    payout_ratio=None,
                    earnings_per_share=eps,
                    book_value_per_share=bvps,
                    free_cash_flow_per_share=None,
                )
                results.append(metrics)
            except Exception as e:
                logger.warning(f"AKShare: skip one metrics row for {ticker}: {e}")
                continue

            if len(results) >= limit:
                break

        # 在构建 metrics_list 之后，补充每个 metric 的缺失字段
        if results:
            # 获取腾讯估值（只需调一次）
            valuation = _get_valuation_from_tencent(symbol)

            for metric in results:
                # 补充 PE/PB/市值
                if metric.price_to_earnings_ratio is None and valuation.get("price_to_earnings_ratio"):
                    metric.price_to_earnings_ratio = valuation["price_to_earnings_ratio"]
                if metric.price_to_book_ratio is None and valuation.get("price_to_book_ratio"):
                    metric.price_to_book_ratio = valuation["price_to_book_ratio"]
                if metric.market_cap is None and valuation.get("market_cap"):
                    metric.market_cap = valuation["market_cap"]

            # 对前 2-3 个 metric 做衍生计算，避免重复调用 API
            for idx, metric in enumerate(results[:3]):
                report_date = metric.report_period or end_date
                mc = metric.market_cap or valuation.get("market_cap")
                cp = valuation.get("current_price")
                derived = _compute_derived_metrics(symbol, report_date, market_cap=mc, current_price=cp)

                for field, value in derived.items():
                    if hasattr(metric, field) and getattr(metric, field) is None and value is not None:
                        setattr(metric, field, value)

                # peg_ratio 计算
                if metric.peg_ratio is None and metric.price_to_earnings_ratio and metric.earnings_growth:
                    if metric.earnings_growth > 0:
                        metric.peg_ratio = metric.price_to_earnings_ratio / (metric.earnings_growth * 100)

        return results[:limit]

    except Exception as e:
        logger.error(f"AKShare get_financial_metrics_ak failed for {ticker}: {e}")
        return []


# ---------------------------------------------------------------------------
# 3. search_line_items_ak
# ---------------------------------------------------------------------------

_REPORT_TYPE_MAP = {
    "资产负债表": "资产负债表",
    "利润表": "利润表",
    "现金流量表": "现金流量表",
}

_LINE_ITEM_ALIAS: dict[str, list[str]] = {
    "total_revenue": ["营业收入", "营业总收入"],
    "operating_revenue": ["营业收入"],
    "revenue": ["营业收入", "营业总收入"],
    "cost_of_revenue": ["营业成本", "营业总成本"],
    "operating_expenses": ["营业总成本", "营业成本"],
    "operating_expense": ["营业总成本", "营业成本"],
    "gross_profit": ["__COMPUTED__"],
    "operating_income": ["营业利润"],
    "net_income": ["净利润", "归属于母公司所有者的净利润"],
    "total_assets": ["资产总计", "总资产"],
    "total_liabilities": ["负债合计", "负债总计"],
    "total_equity": ["所有者权益合计", "股东权益合计"],
    "shareholders_equity": ["所有者权益合计", "股东权益合计"],
    "cash_and_equivalents": ["货币资金"],
    "accounts_receivable": ["应收账款"],
    "inventory": ["存货"],
    "fixed_assets": ["固定资产"],
    "current_assets": ["流动资产合计"],
    "current_liabilities": ["流动负债合计"],
    "long_term_debt": ["长期借款"],
    "short_term_debt": ["短期借款"],
    "total_debt": ["__COMPUTED__"],
    "operating_cash_flow": ["经营活动产生的现金流量净额", "经营活动现金流净额"],
    "investing_cash_flow": ["投资活动产生的现金流量净额", "投资活动现金流净额"],
    "financing_cash_flow": ["筹资活动产生的现金流量净额", "筹资活动现金流净额"],
    "depreciation_and_amortization": ["固定资产折旧", "资产减值准备"],
    "research_and_development": ["研发费用"],
    "selling_general_and_administrative": ["销售费用", "管理费用"],
    "interest_expense": ["利息支出", "利息费用"],
    "income_tax_expense": ["所得税费用", "所得税"],
    "dividends_paid": [
        "分配股利、利润或偿付利息支付的现金",
        "分配股利、利润或偿付利息所支付的现金",
        "分配股利利润或偿付利息支付的现金",
        "分配股利利润或偿付利息所支付的现金",
    ],
    "dividends_and_other_cash_distributions": [
        "分配股利、利润或偿付利息支付的现金",
        "分配股利、利润或偿付利息所支付的现金",
        "分配股利利润或偿付利息支付的现金",
        "分配股利利润或偿付利息所支付的现金",
    ],
    "share_issuance": ["吸收投资收到的现金"],
    "issuance_or_purchase_of_equity_shares": ["吸收投资收到的现金"],
    "share_repurchase": ["__COMPUTED__"],
    "retained_earnings": ["未分配利润"],
    "paid_in_capital": ["实收资本", "股本"],
    "goodwill": ["商誉"],
    "intangible_assets": ["无形资产"],
    "capital_expenditure": [
        "购建固定资产、无形资产和其他长期资产支付的现金",
        "购建固定资产、无形资产和其他长期资产所支付的现金",
        "购建固定资产等支付的现金",
        "购建固定资产等所支付的现金",
        "构建固定资产、无形资产和其他长期资产支付的现金",
    ],
    "free_cash_flow": ["__COMPUTED__"],
    "ebitda": ["__COMPUTED__"],
    "ebit": ["__COMPUTED__"],
    "working_capital": ["__COMPUTED__"],
    "outstanding_shares": ["__COMPUTED__"],
    "gross_margin": ["__COMPUTED__"],
    "operating_margin": ["__COMPUTED__"],
    "net_margin": ["__COMPUTED__"],
    "debt_to_equity": ["__COMPUTED__"],
    "return_on_equity": ["__COMPUTED__"],
    "return_on_assets": ["__COMPUTED__"],
    "return_on_invested_capital": ["__COMPUTED__"],
    "earnings_per_share": ["__COMPUTED__"],
    "book_value_per_share": ["__COMPUTED__"],
}


_COMPUTED_DEPENDENCIES: dict[str, list[str]] = {
    "free_cash_flow": ["operating_cash_flow", "capital_expenditure"],
    "ebitda": ["operating_income", "depreciation_and_amortization"],
    "working_capital": ["current_assets", "current_liabilities"],
    "ebit": ["operating_income", "interest_expense"],
    "total_debt": ["short_term_debt", "long_term_debt"],
    "gross_profit": ["total_revenue", "cost_of_revenue"],
    "revenue": ["total_revenue"],
    "outstanding_shares": ["paid_in_capital"],
    "shareholders_equity": ["total_equity"],
    "dividends_and_other_cash_distributions": ["dividends_paid"],
    "issuance_or_purchase_of_equity_shares": ["share_issuance"],
    "operating_expense": ["operating_expenses", "cost_of_revenue"],
    "gross_margin": ["total_revenue", "cost_of_revenue"],
    "operating_margin": ["operating_income", "total_revenue"],
    "net_margin": ["net_income", "total_revenue"],
    "debt_to_equity": ["total_liabilities", "total_equity"],
    "return_on_equity": ["net_income", "total_equity"],
    "return_on_assets": ["net_income", "total_assets"],
    "return_on_invested_capital": ["operating_income", "total_equity", "long_term_debt"],
    "earnings_per_share": ["net_income", "paid_in_capital"],
    "book_value_per_share": ["total_equity", "paid_in_capital"],
}


def _compute_line_item_fields(item_dict: dict) -> dict:
    """对需要计算的 line_items 进行推导"""

    # revenue 别名
    if "revenue" not in item_dict and "total_revenue" in item_dict:
        item_dict["revenue"] = item_dict["total_revenue"]

    # shareholders_equity 别名
    if "shareholders_equity" not in item_dict and "total_equity" in item_dict:
        item_dict["shareholders_equity"] = item_dict["total_equity"]

    # dividends_and_other_cash_distributions 别名
    if "dividends_and_other_cash_distributions" not in item_dict and "dividends_paid" in item_dict:
        item_dict["dividends_and_other_cash_distributions"] = item_dict["dividends_paid"]

    # issuance_or_purchase_of_equity_shares 别名
    if "issuance_or_purchase_of_equity_shares" not in item_dict and "share_issuance" in item_dict:
        item_dict["issuance_or_purchase_of_equity_shares"] = item_dict["share_issuance"]

    # operating_expense 别名
    if "operating_expense" not in item_dict and "operating_expenses" in item_dict:
        item_dict["operating_expense"] = item_dict["operating_expenses"]
    elif "operating_expense" not in item_dict and "cost_of_revenue" in item_dict:
        item_dict["operating_expense"] = item_dict["cost_of_revenue"]

    # outstanding_shares 从实收资本推算（A股面值1元，实收资本≈股数，单位通常为万元）
    pic = item_dict.get("paid_in_capital")
    if pic is not None and item_dict.get("outstanding_shares") is None:
        item_dict["outstanding_shares"] = float(pic) * 10000

    # gross_profit = 营业收入 - 营业成本
    rev = item_dict.get("total_revenue")
    cos = item_dict.get("cost_of_revenue")
    if rev is not None and cos is not None:
        item_dict["gross_profit"] = float(rev) - float(cos)

    # free_cash_flow = 经营活动现金流 - 资本支出
    ocf = item_dict.get("operating_cash_flow")
    capex = item_dict.get("capital_expenditure")
    if ocf is not None and capex is not None:
        item_dict["free_cash_flow"] = float(ocf) - abs(float(capex))
    elif ocf is not None:
        item_dict["free_cash_flow"] = float(ocf)  # 无capex时用ocf近似

    # working_capital = 流动资产 - 流动负债
    ca = item_dict.get("current_assets")
    cl = item_dict.get("current_liabilities")
    if ca is not None and cl is not None:
        item_dict["working_capital"] = float(ca) - float(cl)

    # ebitda = 营业利润 + 折旧摊销
    oi = item_dict.get("operating_income")
    da = item_dict.get("depreciation_and_amortization")
    if oi is not None:
        item_dict["ebitda"] = float(oi) + (float(da) if da else 0)

    # ebit = 营业利润 + 利息支出（近似）
    ie = item_dict.get("interest_expense")
    if oi is not None:
        item_dict["ebit"] = float(oi) + (abs(float(ie)) if ie else 0)

    # total_debt = 短期借款 + 长期借款
    std = item_dict.get("short_term_debt")
    ltd = item_dict.get("long_term_debt")
    if std is not None or ltd is not None:
        item_dict["total_debt"] = (float(std) if std else 0) + (float(ltd) if ltd else 0)

    # gross_margin = gross_profit / total_revenue
    gp = item_dict.get("gross_profit")
    if gp is not None and rev is not None and rev != 0:
        item_dict["gross_margin"] = gp / float(rev)

    # operating_margin = operating_income / total_revenue
    if oi is not None and rev is not None and rev != 0:
        item_dict["operating_margin"] = oi / float(rev)

    # net_margin = net_income / total_revenue
    ni = item_dict.get("net_income")
    if ni is not None and rev is not None and rev != 0:
        item_dict["net_margin"] = ni / float(rev)

    # debt_to_equity = total_liabilities / total_equity
    tl = item_dict.get("total_liabilities")
    te = item_dict.get("total_equity")
    if tl is not None and te is not None and te != 0:
        item_dict["debt_to_equity"] = tl / te

    # return_on_equity = net_income / total_equity
    if ni is not None and te is not None and te != 0:
        item_dict["return_on_equity"] = ni / te

    # return_on_assets = net_income / total_assets
    ta = item_dict.get("total_assets")
    if ni is not None and ta is not None and ta != 0:
        item_dict["return_on_assets"] = ni / ta

    # return_on_invested_capital = operating_income / (total_equity + long_term_debt)
    if oi is not None and te is not None:
        invested_capital = te + (ltd if ltd else 0)
        if invested_capital != 0:
            item_dict["return_on_invested_capital"] = oi / invested_capital

    # earnings_per_share = net_income / outstanding_shares
    shares = item_dict.get("outstanding_shares")
    if ni is not None and shares is not None and shares != 0:
        item_dict["earnings_per_share"] = ni / shares

    # book_value_per_share = total_equity / outstanding_shares
    if te is not None and shares is not None and shares != 0:
        item_dict["book_value_per_share"] = te / shares

    return item_dict


def _fetch_report(symbol: str, report_type: str) -> pd.DataFrame | None:
    """获取三大报表之一。"""
    try:
        df = AKShareRateLimiter.call_with_retry(
            ak.stock_financial_report_sina,
            stock=symbol,
            symbol=report_type,
        )
        return df
    except Exception as e:
        logger.warning(f"AKShare: failed to fetch {report_type} for {symbol}: {e}")
        return None


def search_line_items_ak(
    ticker: str,
    line_items: list[str],
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
) -> list[LineItem]:
    """
    使用 AKShare 获取三大报表，按请求的 line_items 提取字段。

    Returns: list[LineItem]，与 api.search_line_items 返回类型一致。
    """
    try:
        symbol = _clean_ticker(ticker)

        balance_df = _fetch_report(symbol, "资产负债表")
        income_df = _fetch_report(symbol, "利润表")
        cashflow_df = _fetch_report(symbol, "现金流量表")

        if income_df is not None and not income_df.empty:
            primary_df = income_df
        elif balance_df is not None and not balance_df.empty:
            primary_df = balance_df
        elif cashflow_df is not None and not cashflow_df.empty:
            primary_df = cashflow_df
        else:
            logger.warning(f"AKShare: no reports available for {ticker}")
            return []

        results: list[LineItem] = []
        for _, row in primary_df.iterrows():
            try:
                report_date = None
                for date_col in ("报告日", "日期", "截止日期", "报告期"):
                    if date_col in row.index:
                        report_date = str(row[date_col])[:10]
                        break
                if not report_date:
                    continue

                if report_date > end_date:
                    continue

                # 计算需要额外获取的依赖字段
                all_fields_to_fetch = set(line_items)
                for item in line_items:
                    deps = _COMPUTED_DEPENDENCIES.get(item, [])
                    for dep in deps:
                        all_fields_to_fetch.add(dep)

                # 收集所有字段的原始值
                item_dict: dict = {}
                for item_name in all_fields_to_fetch:
                    value = None
                    aliases = _LINE_ITEM_ALIAS.get(item_name, [item_name])
                    for alias in aliases:
                        for df in (income_df, balance_df, cashflow_df):
                            if df is not None and alias in df.columns:
                                for _, r in df.iterrows():
                                    r_date = None
                                    for dc in ("报告日", "日期", "截止日期", "报告期"):
                                        if dc in r.index:
                                            r_date = str(r[dc])[:10]
                                            break
                                    if r_date == report_date:
                                        value = _safe_float(r.get(alias))
                                        break
                                if value is not None:
                                    break
                        if value is not None:
                            break
                    item_dict[item_name] = value

                # 计算衍生字段
                item_dict = _compute_line_item_fields(item_dict)

                # 只返回请求字段
                extra_fields = {k: v for k, v in item_dict.items() if k in line_items}

                line_item = LineItem(
                    ticker=ticker,
                    report_period=report_date,
                    period="annual" if period == "ttm" else period,
                    currency="CNY",
                    **extra_fields,
                )
                results.append(line_item)
            except Exception as e:
                logger.warning(f"AKShare: skip one line_item row for {ticker}: {e}")
                continue

            if len(results) >= limit:
                break

        return results[:limit]

    except Exception as e:
        logger.error(f"AKShare search_line_items_ak failed for {ticker}: {e}")
        return []


# ---------------------------------------------------------------------------
# 4. get_insider_trades_ak
# ---------------------------------------------------------------------------

def get_insider_trades_ak(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 50,
) -> list[InsiderTrade]:
    """
    使用 AKShare 获取内部人交易数据。

    Returns: list[InsiderTrade]，与 api.get_insider_trades 返回类型一致。
    """
    try:
        symbol = _clean_ticker(ticker)

        df = AKShareRateLimiter.call_with_retry(
            ak.stock_inner_trade_xq,
        )

        if df is None or df.empty:
            logger.info(f"AKShare: stock_inner_trade_xq failed or empty for {ticker}, trying stock_shareholder_change_ths")
            df = AKShareRateLimiter.call_with_retry(
                ak.stock_shareholder_change_ths,
                symbol=symbol,
            )
        else:
            if "股票代码" in df.columns:
                df = df[df["股票代码"].astype(str).str.contains(symbol)]
            elif "代码" in df.columns:
                df = df[df["代码"].astype(str).str.contains(symbol)]
        
        if df is None or df.empty:
            logger.warning(f"AKShare: no insider trade data for {ticker}")
            return []

        trades: list[InsiderTrade] = []
        for _, row in df.iterrows():
            try:
                trade_date = str(
                    row.get("变动日期")
                    or row.get("交易日期")
                    or row.get("日期")
                    or row.get("公告日期", "")
                )[:10]

                if trade_date > end_date:
                    continue
                if start_date and trade_date < start_date:
                    continue

                name = str(
                    row.get("股东名称")
                    or row.get("变动人")
                    or row.get("变动股东")
                    or row.get("姓名")
                    or ""
                )

                shares_val = row.get("变动股数") or row.get("成交股数") or row.get("变动数量")
                if shares_val is None:
                    transaction_shares = None
                elif isinstance(shares_val, (int, float)):
                    transaction_shares = float(shares_val)
                else:
                    shares_str = str(shares_val)
                    try:
                        if "万" in shares_str:
                            num_str = ''.join(c for c in shares_str if c.isdigit() or c == '.')
                            transaction_shares = float(num_str) * 10000 if num_str else None
                        else:
                            transaction_shares = _safe_float(shares_val)
                    except (ValueError, TypeError):
                        transaction_shares = None

                price_val = row.get("成交均价") or row.get("成交价格") or row.get("均价") or row.get("交易均价")
                if price_val is None or str(price_val) in ["未披露", "-", ""]:
                    transaction_price = None
                else:
                    transaction_price = _safe_float(price_val)

                transaction_value = None
                if transaction_shares is not None and transaction_price is not None:
                    transaction_value = transaction_shares * transaction_price

                trade = InsiderTrade(
                    ticker=ticker,
                    issuer=None,
                    name=name if name else None,
                    title=str(row.get("职务") or row.get("职位") or "") or None,
                    is_board_director=None,
                    transaction_date=trade_date or None,
                    transaction_shares=transaction_shares,
                    transaction_price_per_share=transaction_price,
                    transaction_value=transaction_value,
                    shares_owned_before_transaction=None,
                    shares_owned_after_transaction=None,
                    security_title=None,
                    filing_date=trade_date,
                )
                trades.append(trade)
            except Exception as e:
                logger.warning(f"AKShare: skip one insider trade row for {ticker}: {e}")
                continue

            if len(trades) >= limit:
                break

        return trades[:limit]

    except Exception as e:
        logger.error(f"AKShare get_insider_trades_ak failed for {ticker}: {e}")
        return []


# ---------------------------------------------------------------------------
# 5. get_company_news_ak
# ---------------------------------------------------------------------------

def get_company_news_ak(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 100,
) -> list[CompanyNews]:
    """
    使用 AKShare 获取公司新闻。

    Returns: list[CompanyNews]，与 api.get_company_news 返回类型一致。
    """
    try:
        symbol = _clean_ticker(ticker)

        df = AKShareRateLimiter.call_with_retry(
            ak.stock_news_em,
            symbol=symbol,
        )
        if df is None or df.empty:
            logger.warning(f"AKShare: no news data for {ticker}")
            return []

        news_list: list[CompanyNews] = []
        for _, row in df.iterrows():
            try:
                news_date = str(
                    row.get("发布时间")
                    or row.get("时间")
                    or row.get("日期", "")
                )[:10]

                if news_date > end_date:
                    continue
                if start_date and news_date < start_date:
                    continue

                title = str(row.get("新闻标题") or row.get("标题", ""))
                source = str(row.get("文章来源") or row.get("来源", ""))
                url = str(row.get("新闻链接") or row.get("链接") or row.get("url", ""))

                news = CompanyNews(
                    ticker=ticker,
                    title=title,
                    author=None,
                    source=source,
                    date=news_date,
                    url=url,
                    sentiment=None,
                )
                news_list.append(news)
            except Exception as e:
                logger.warning(f"AKShare: skip one news row for {ticker}: {e}")
                continue

            if len(news_list) >= limit:
                break

        return news_list[:limit]

    except Exception as e:
        logger.error(f"AKShare get_company_news_ak failed for {ticker}: {e}")
        return []


# ---------------------------------------------------------------------------
# 6. get_market_cap_ak
# ---------------------------------------------------------------------------

def get_market_cap_ak(
    ticker: str,
    end_date: str,
) -> float | None:
    """
    获取总市值。

    策略: 1) 腾讯实时行情 → 2) AKShare → 3) AKShare Sina fallback
    """
    try:
        symbol = _clean_ticker(ticker)

        # 策略 1: 腾讯实时行情 API（最快）
        cap = _get_market_cap_tencent(symbol)
        if cap is not None:
            return cap

        # 策略 2: AKShare stock_individual_info_em
        df = AKShareRateLimiter.call_with_retry(
            ak.stock_individual_info_em,
            symbol=symbol,
        )
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                item = str(row.iloc[0] if len(row) > 0 else "")
                if "总市值" in item:
                    val = _safe_float(row.iloc[1] if len(row) > 1 else None)
                    if val is not None:
                        return val

        # 策略 3: 从实时行情 spot 获取
        try:
            spot_df = AKShareRateLimiter.call_with_retry(
                ak.stock_zh_a_spot_em,
            )
            if spot_df is not None and not spot_df.empty:
                row = spot_df[spot_df["代码"] == symbol]
                if row.empty:
                    row = spot_df[spot_df["代码"] == ticker]
                if not row.empty:
                    if "总市值" in row.columns:
                        val = _safe_float(row.iloc[0]["总市值"])
                        if val is not None:
                            return val
        except Exception as e:
            logger.warning(f"AKShare: fallback spot market cap failed for {ticker}: {e}")

        # 策略 4: AKShare Sina fallback
        sina_cap = _get_market_cap_via_akshare_sina(symbol)
        if sina_cap is not None:
            return sina_cap

        logger.warning(f"AKShare: could not determine market cap for {ticker}")
        return None

    except Exception as e:
        logger.error(f"AKShare get_market_cap_ak failed for {ticker}: {e}")
        return None
