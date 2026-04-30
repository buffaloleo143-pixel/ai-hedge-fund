"""
AKShare 数据提供器 —— 优先直接 HTTP API，AKShare 作为 fallback。
返回类型与 src/tools/api.py 中对应函数完全一致。
"""

import datetime
import glob
import json
import logging
import os
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


def _normalize_date(date_str: str) -> str:
    """将日期字符串规范化为 YYYY-MM-DD 格式，用于可靠的日期字符串比较。

    支持格式:
    - 'YYYYMMDD' -> 'YYYY-MM-DD'
    - 'YYYY-MM-DD' -> 'YYYY-MM-DD'（不变）
    """
    if not date_str:
        return date_str
    s = str(date_str).strip()
    # YYYYMMDD 格式（8位纯数字）-> 转换为 YYYY-MM-DD
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _report_period_sort_key(report_period: str) -> str:
    """将不同格式的 report_period 转换为可排序的日期字符串。

    支持格式:
    - '2024-12-31' (YYYY-MM-DD)
    - '20241231' (YYYYMMDD)
    - '2024-annual' (年报)
    - '2024-Q1' / '2024-Q2' / '2024-Q3' (季报)
    """
    if not report_period:
        return "0000-00-00"
    try:
        # YYYY-MM-DD 格式
        if len(report_period) >= 10 and report_period[4] == "-" and report_period[7] == "-":
            return report_period[:10]
        # YYYYMMDD 格式
        if len(report_period) == 8 and report_period.isdigit():
            return f"{report_period[:4]}-{report_period[4:6]}-{report_period[6:8]}"
        # annual / Qx 格式
        parts = report_period.split("-")
        if len(parts) == 2:
            year, suffix = parts[0], parts[1].lower()
            if suffix == "annual":
                return f"{year}-12-31"
            elif suffix == "q1":
                return f"{year}-03-31"
            elif suffix == "q2":
                return f"{year}-06-30"
            elif suffix == "q3":
                return f"{year}-09-30"
    except (ValueError, IndexError):
        pass
    return str(report_period) or "0000-00-00"


def _is_annual_report_period(report_period: str) -> bool:
    """判断报告期是否为年报（月份为12月或标记为 annual）"""
    if not report_period:
        return False
    try:
        # YYYY-MM-DD 格式
        if len(report_period) >= 7 and report_period[4] == "-" and report_period[7] == "-":
            return int(report_period[5:7]) == 12
        # YYYYMMDD 格式
        if len(report_period) == 8 and report_period.isdigit():
            return int(report_period[4:6]) == 12
        # annual 格式
        if report_period.endswith("-annual"):
            return True
    except (ValueError, IndexError):
        pass
    return False


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


def _compute_cagr(values: list, years: int) -> float | None:
    """
    计算复合年增长率 CAGR = (最新值/最早值)^(1/years) - 1

    Args:
        values: 按时间从新到旧排列的数值列表（至少需要 2 个非 None 值）
        years: 最早值与最新值之间的年数

    Returns:
        CAGR 值，或 None（数据不足 / 除零 / 负数开方等异常）
    """
    try:
        # 过滤 None，保留有效值
        valid = [v for v in values if v is not None]
        if len(valid) < 2 or years <= 0:
            return None
        latest = float(valid[0])
        earliest = float(valid[-1])
        if earliest == 0:
            return None
        # 两者同号才能计算有意义的 CAGR
        if earliest < 0 and latest < 0:
            # 都是负数时，用绝对值比计算增长率
            ratio = abs(latest) / abs(earliest)
        elif earliest > 0 and latest > 0:
            ratio = latest / earliest
        elif latest == 0:
            return -1.0  # 从正值降到 0
        else:
            # 一正一负，CAGR 无意义
            return None
        cagr = ratio ** (1.0 / years) - 1.0
        return cagr
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


def _compute_quarterly_growth(ticker: str) -> dict:
    """
    计算季报同比增长率（如 2025Q1 vs 2024Q1）。

    使用 AKShare 的 stock_financial_report_sina 获取季度利润表，
    比较最新季报与去年同期的营收和净利润。

    Returns:
        dict 包含:
        - revenue_growth_quarterly: 季报营收同比增长率
        - earnings_growth_quarterly: 季报净利润同比增长率
        获取失败时对应值为 None
    """
    result = {
        "revenue_growth_quarterly": None,
        "earnings_growth_quarterly": None,
    }
    try:
        symbol = _clean_ticker(ticker)

        # 获取季度利润表
        df = AKShareRateLimiter.call_with_retry(
            ak.stock_financial_report_sina,
            stock=symbol,
            symbol="利润表",
        )
        if df is None or df.empty:
            logger.warning(f"Quarterly growth: no income statement for {ticker}")
            return result

        # 识别日期列
        date_col = None
        for col in ("报告日", "日期", "截止日期", "报告期"):
            if col in df.columns:
                date_col = col
                break
        if date_col is None:
            logger.warning(f"Quarterly growth: no date column in income statement for {ticker}")
            return result

        # 识别营收和净利润列
        revenue_col = None
        for col_name in ("营业收入", "营业总收入"):
            if col_name in df.columns:
                revenue_col = col_name
                break

        net_income_col = None
        for col_name in ("净利润", "归属于母公司所有者的净利润"):
            if col_name in df.columns:
                net_income_col = col_name
                break

        if revenue_col is None and net_income_col is None:
            logger.warning(f"Quarterly growth: no revenue/net_income columns for {ticker}")
            return result

        # 提取日期和季度信息，按日期降序排列
        df["_report_date"] = df[date_col].astype(str).str[:10]
        df = df.sort_values("_report_date", ascending=False)

        # 解析季度：日期格式如 "2024-03-31" -> Q1, "2024-06-30" -> Q2, etc.
        def _parse_quarter(date_str: str) -> tuple[int, int] | None:
            """从报告期日期解析年份和季度，返回 (year, quarter) 或 None"""
            try:
                parts = date_str.split("-")
                month = int(parts[1])
                year = int(parts[0])
                if month <= 3:
                    return (year, 1)
                elif month <= 6:
                    return (year, 2)
                elif month <= 9:
                    return (year, 3)
                else:
                    return (year, 4)
            except (ValueError, IndexError):
                return None

        # 构建季报数据字典: (year, quarter) -> {revenue, net_income}
        quarterly_data = {}
        for _, row in df.iterrows():
            date_str = row["_report_date"]
            yq = _parse_quarter(date_str)
            if yq is None:
                continue
            if yq in quarterly_data:
                continue  # 取第一条（最新的来源）
            entry = {}
            if revenue_col and revenue_col in row.index:
                entry["revenue"] = _safe_float(row[revenue_col])
            if net_income_col and net_income_col in row.index:
                entry["net_income"] = _safe_float(row[net_income_col])
            quarterly_data[yq] = entry

        if not quarterly_data:
            return result

        # 找到最新的季度
        sorted_quarters = sorted(quarterly_data.keys(), reverse=True)
        latest_yq = sorted_quarters[0]
        prev_yq = (latest_yq[0] - 1, latest_yq[1])  # 去年同期

        if prev_yq not in quarterly_data:
            logger.info(f"Quarterly growth: no YoY data for {ticker}, latest={latest_yq}, need={prev_yq}")
            return result

        latest_data = quarterly_data[latest_yq]
        prev_data = quarterly_data[prev_yq]

        # 计算营收同比增长
        if "revenue" in latest_data and "revenue" in prev_data:
            rev_new = latest_data["revenue"]
            rev_old = prev_data["revenue"]
            if rev_new is not None and rev_old is not None and rev_old != 0:
                result["revenue_growth_quarterly"] = (rev_new - rev_old) / abs(rev_old)

        # 计算净利润同比增长
        if "net_income" in latest_data and "net_income" in prev_data:
            ni_new = latest_data["net_income"]
            ni_old = prev_data["net_income"]
            if ni_new is not None and ni_old is not None and ni_old != 0:
                result["earnings_growth_quarterly"] = (ni_new - ni_old) / abs(ni_old)

        logger.info(
            f"Quarterly growth for {ticker}: latest={latest_yq}, "
            f"revenue_growth={result['revenue_growth_quarterly']}, "
            f"earnings_growth={result['earnings_growth_quarterly']}"
        )
        return result

    except Exception as e:
        logger.warning(f"Quarterly growth computation failed for {ticker}: {e}")
        return result


def _compute_derived_metrics(symbol: str, end_date: str, market_cap: float = None, current_price: float = None) -> dict:
    """从三大报表数据计算衍生财务指标（支持多期增长率与 CAGR）"""
    try:
        # 获取 5 期年报数据，用于多期增长率和 CAGR 计算
        items = search_line_items_ak(symbol, [
            "total_revenue", "cost_of_revenue", "operating_income", "net_income",
            "total_assets", "total_liabilities", "total_equity",
            "current_assets", "current_liabilities",
            "cash_and_equivalents", "inventory", "accounts_receivable",
            "operating_cash_flow", "interest_expense",
            "short_term_debt", "long_term_debt",
            "paid_in_capital", "depreciation_and_amortization"
        ], end_date, "annual", 5)

        if not items:
            return {}

        # 按报告期降序排序，确保 items[0] 是最新数据
        items.sort(key=lambda item: _report_period_sort_key(item.report_period or ""), reverse=True)

        # 过滤，确保只使用年报（12月末）数据，防止 Q1/Q2/Q3 季报数据混入
        # 导致 margin/ratio/YoY 计算偏差（单季收入不应与全年收入比较）
        annual_items = [item for item in items if _is_annual_report_period(item.report_period or "")]
        if not annual_items:
            # 如果没有年报数据，降级使用全部数据（可能是季报）
            annual_items = items
            logger.warning(f"_compute_derived_metrics: no annual reports found for {symbol}, using all items")
        else:
            logger.info(f"_compute_derived_metrics: using {len(annual_items)} annual items for {symbol}: "
                        f"{[item.report_period for item in annual_items[:3]]}")

        # 使用年报最新期作为 latest，防止 Q1 单季数据污染 margin/ratio
        latest = annual_items[0]
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
        depreciation = get_val(latest, "depreciation_and_amortization")

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

        # debt_to_equity: 有息负债口径（短期借款+长期借款+应付债券）/ 归属于母公司股东权益
        # 无有息负债数据时回退到总负债口径
        if short_term_debt is not None or long_term_debt is not None:
            interest_bearing_debt_val = (short_term_debt or 0) + (long_term_debt or 0)
            # 注意：_compute_derived_metrics 中没有 bonds_payable，跳过
            se_parent = get_val(latest, "shareholders_equity") or total_equity
            if interest_bearing_debt_val > 0 and se_parent and se_parent != 0:
                result["debt_to_equity"] = interest_bearing_debt_val / se_parent
            elif total_liabilities and total_equity and total_equity != 0:
                result["debt_to_equity"] = total_liabilities / total_equity
        elif total_liabilities and total_equity and total_equity != 0:
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

        # interest_coverage: 当利息支出<=0时（如财务费用为负表示净利息收入），
        # 利息覆盖倍数理论上为无穷大，设为999.0表示"无利息偿付压力"
        if operating_income and interest_expense is not None:
            if interest_expense > 0:
                result["interest_coverage"] = operating_income / interest_expense
            else:
                result["interest_coverage"] = 999.0  # 无利息支出，覆盖倍数极高

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

        # ====== 多期增长率计算 ======
        # 收集各期的关键指标值（从新到旧）
        revenues_list = []
        net_incomes_list = []
        operating_incomes_list = []
        equities_list = []
        eps_list = []
        ebitda_list = []

        # 用于 EPS 计算的股数推算
        base_shares = None
        if paid_in_capital and paid_in_capital != 0:
            base_shares = paid_in_capital * 1e4
        elif market_cap and current_price and current_price != 0:
            base_shares = market_cap / current_price

        # annual_items 已在前面过滤完成，直接用于多期增长率计算

        for item in annual_items:
            rev = get_val(item, "total_revenue")
            ni = get_val(item, "net_income")
            oi = get_val(item, "operating_income")
            eq = get_val(item, "total_equity")
            if eq is None:
                ta = get_val(item, "total_assets")
                tl = get_val(item, "total_liabilities")
                if ta and tl:
                    eq = ta - tl
            dep = get_val(item, "depreciation_and_amortization")

            revenues_list.append(rev)
            net_incomes_list.append(ni)
            operating_incomes_list.append(oi)
            equities_list.append(eq)

            # EPS
            if ni and base_shares and base_shares != 0:
                eps_list.append(ni / base_shares)
            else:
                eps_list.append(None)

            # EBITDA = 营业利润 + 折旧摊销
            if oi is not None:
                ebitda_val = oi + (dep if dep else 0)
                ebitda_list.append(ebitda_val)
            else:
                ebitda_list.append(None)

        # --- YoY 增长率：多级 fallback ---
        # revenue_growth: 优先从第2期 YoY，否则从第3期 fallback
        if total_revenue and len(revenues_list) >= 2 and revenues_list[1] and revenues_list[1] != 0:
            result["revenue_growth"] = (total_revenue - revenues_list[1]) / abs(revenues_list[1])
        elif total_revenue and len(revenues_list) >= 3 and revenues_list[2] and revenues_list[2] != 0:
            # fallback: 用第3期做 2 年 CAGR 并近似为 YoY
            result["revenue_growth"] = _compute_cagr(revenues_list[:3], 2)

        # earnings_growth / net_income_growth
        if net_income and len(net_incomes_list) >= 2 and net_incomes_list[1] and net_incomes_list[1] != 0:
            result["earnings_growth"] = (net_income - net_incomes_list[1]) / abs(net_incomes_list[1])
        elif net_income and len(net_incomes_list) >= 3 and net_incomes_list[2] and net_incomes_list[2] != 0:
            result["earnings_growth"] = _compute_cagr(net_incomes_list[:3], 2)

        # operating_income_growth
        if operating_income and len(operating_incomes_list) >= 2 and operating_incomes_list[1] and operating_incomes_list[1] != 0:
            result["operating_income_growth"] = (operating_income - operating_incomes_list[1]) / abs(operating_incomes_list[1])
        elif operating_income and len(operating_incomes_list) >= 3 and operating_incomes_list[2] and operating_incomes_list[2] != 0:
            result["operating_income_growth"] = _compute_cagr(operating_incomes_list[:3], 2)

        # ebitda_growth
        if len(ebitda_list) >= 2 and ebitda_list[0] and ebitda_list[1] and ebitda_list[1] != 0:
            result["ebitda_growth"] = (ebitda_list[0] - ebitda_list[1]) / abs(ebitda_list[1])
        elif len(ebitda_list) >= 3 and ebitda_list[0] and ebitda_list[2] and ebitda_list[2] != 0:
            result["ebitda_growth"] = _compute_cagr(ebitda_list[:3], 2)

        # book_value_growth
        if total_equity and len(equities_list) >= 2 and equities_list[1] and equities_list[1] != 0:
            result["book_value_growth"] = (total_equity - equities_list[1]) / abs(equities_list[1])
        elif total_equity and len(equities_list) >= 3 and equities_list[2] and equities_list[2] != 0:
            result["book_value_growth"] = _compute_cagr(equities_list[:3], 2)

        # earnings_per_share_growth
        if len(eps_list) >= 2 and eps_list[0] and eps_list[1] and eps_list[1] != 0:
            result["earnings_per_share_growth"] = (eps_list[0] - eps_list[1]) / abs(eps_list[1])
        elif len(eps_list) >= 3 and eps_list[0] and eps_list[2] and eps_list[2] != 0:
            result["earnings_per_share_growth"] = _compute_cagr(eps_list[:3], 2)

        # free_cash_flow_growth（仍使用 2 期年报数据）
        if len(annual_items) >= 2:
            prev_ocf = get_val(annual_items[1], "operating_cash_flow")
            if operating_cash_flow and prev_ocf and prev_ocf != 0:
                result["free_cash_flow_growth"] = (operating_cash_flow - prev_ocf) / abs(prev_ocf)

        # ====== 3 年 CAGR ======
        if len(revenues_list) >= 4 and revenues_list[0] is not None and revenues_list[3] is not None:
            result["revenue_cagr_3y"] = _compute_cagr(revenues_list[:4], 3)
        elif len(revenues_list) >= 2 and revenues_list[0] is not None and revenues_list[-1] is not None:
            years_span = len([v for v in revenues_list if v is not None]) - 1
            if years_span >= 2:
                result["revenue_cagr_3y"] = _compute_cagr(revenues_list, years_span)

        if len(net_incomes_list) >= 4 and net_incomes_list[0] is not None and net_incomes_list[3] is not None:
            result["earnings_cagr_3y"] = _compute_cagr(net_incomes_list[:4], 3)
        elif len(net_incomes_list) >= 2 and net_incomes_list[0] is not None and net_incomes_list[-1] is not None:
            years_span = len([v for v in net_incomes_list if v is not None]) - 1
            if years_span >= 2:
                result["earnings_cagr_3y"] = _compute_cagr(net_incomes_list, years_span)

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
# JSON 年报数据优先加载
# ---------------------------------------------------------------------------

def _load_annual_report_json(ticker: str) -> dict | None:
    """尝试从 src/data/ 加载年报JSON文件，返回解析后的dict或None。"""
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    pattern = os.path.join(data_dir, f"{_clean_ticker(ticker)}_*_annual_report.json")
    files = glob.glob(pattern)
    if not files:
        return None
    files.sort(reverse=True)
    try:
        with open(files[0], 'r', encoding='utf-8') as f:
            data = json.load(f)
        logger.info(f"Loaded annual report JSON: {os.path.basename(files[0])}")
        return data
    except Exception as e:
        logger.warning(f"Failed to load annual report JSON for {ticker}: {e}")
        return None


def _load_quarterly_report_json(ticker: str) -> dict | None:
    """尝试从 src/data/ 加载季报JSON文件（如 *q1_report.json），返回解析后的dict或None。"""
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    pattern = os.path.join(data_dir, f"{_clean_ticker(ticker)}_*q*_report.json")
    files = glob.glob(pattern)
    if not files:
        return None
    files.sort(reverse=True)
    try:
        with open(files[0], 'r', encoding='utf-8') as f:
            data = json.load(f)
        logger.info(f"Loaded quarterly report JSON: {os.path.basename(files[0])}")
        return data
    except Exception as e:
        logger.warning(f"Failed to load quarterly report JSON for {ticker}: {e}")
        return None


def _extract_json_years(json_data: dict) -> list[str]:
    """从JSON数据中提取所有可用的年份，降序排列。"""
    years = set()
    for section_name in ("key_financial_indicators", "income_statement", "balance_sheet", "cash_flow_statement"):
        section = json_data.get(section_name, {})
        for field_data in section.values():
            if isinstance(field_data, dict):
                for key in field_data.keys():
                    if isinstance(key, str) and key.isdigit() and len(key) == 4:
                        years.add(key)
    return sorted(years, reverse=True)


def _json_val(section: dict, field: str, year: str) -> float | None:
    """从JSON的一个section中提取指定字段、指定年份的浮点值。"""
    sub = section.get(field, {})
    if not isinstance(sub, dict):
        return None
    return _safe_float(sub.get(year))


def _build_metrics_from_json(ticker: str, json_data: dict, end_date: str) -> list[FinancialMetrics]:
    """从年报JSON构建FinancialMetrics列表（多期），JSON数据优先于AKShare。"""
    results = []
    years = _extract_json_years(json_data)
    kfi = json_data.get("key_financial_indicators", {})
    inc = json_data.get("income_statement", {})
    bs = json_data.get("balance_sheet", {})
    cf = json_data.get("cash_flow_statement", {})

    for year in years:
        report_period = f"{year}-annual"
        period_key = _report_period_sort_key(report_period)
        if period_key > end_date:
            continue

        # === 从 key_financial_indicators 提取比率 ===
        roe = _json_val(kfi, "roe_weighted", year)
        gross_margin = _json_val(kfi, "gross_margin", year)
        net_margin = _json_val(kfi, "net_margin", year)
        revenue_growth = _json_val(kfi, "revenue_growth_yoy", year)
        earnings_growth = _json_val(kfi, "earnings_growth_yoy", year)
        debt_to_assets = _json_val(kfi, "debt_to_asset_ratio", year)
        current_ratio = _json_val(kfi, "current_ratio", year)
        eps = _json_val(kfi, "eps_basic", year)
        bvps = _json_val(kfi, "book_value_per_share", year)

        # === 从 income_statement 提取 ===
        operating_revenue = _json_val(inc, "operating_revenue", year)
        operating_cost = _json_val(inc, "operating_cost", year)
        operating_profit = _json_val(inc, "operating_profit", year)
        net_income = _json_val(inc, "net_income_attributable_to_parent", year)
        interest_expense_raw = _json_val(inc, "financial_expenses", year)
        interest_expense = interest_expense_raw if interest_expense_raw and interest_expense_raw > 0 else 0.0

        # === 从 balance_sheet 提取 ===
        total_assets = _json_val(bs, "total_assets", year)
        total_liabilities = _json_val(bs, "total_liabilities", year)
        shareholders_equity = _json_val(bs, "shareholders_equity_attributable_to_parent", year)
        total_equity = _json_val(bs, "total_equity", year) or shareholders_equity
        current_assets = _json_val(bs, "current_assets", year)
        current_liabilities = _json_val(bs, "current_liabilities", year)
        inventory = _json_val(bs, "inventory", year)
        accounts_receivable = _json_val(bs, "accounts_receivable", year)
        cash = _json_val(bs, "cash_and_equivalents", year)
        long_term_debt = _json_val(bs, "long_term_borrowings", year)

        # === 从 cash_flow_statement 提取 ===
        operating_cash_flow = _json_val(cf, "operating_cash_flow", year)
        free_cash_flow = _json_val(cf, "free_cash_flow", year)

        # === 计算补充指标 ===
        operating_margin = None
        if operating_profit is not None and operating_revenue and operating_revenue != 0:
            operating_margin = operating_profit / operating_revenue

        # debt_to_equity: 有息负债口径（短期借款+长期借款+应付债券）/ 归属于母公司股东权益
        # 无有息负债数据时回退到总负债口径
        short_term_debt_val = _json_val(bs, "short_term_borrowings", year)
        long_term_debt_val = _json_val(bs, "long_term_borrowings", year)
        bonds_payable_val = _json_val(bs, "bonds_payable", year)
        interest_bearing_debt = (short_term_debt_val or 0) + (long_term_debt_val or 0) + (bonds_payable_val or 0)
        debt_to_equity = None
        if interest_bearing_debt > 0 and shareholders_equity and shareholders_equity != 0:
            debt_to_equity = interest_bearing_debt / shareholders_equity
        elif interest_bearing_debt > 0 and total_equity and total_equity != 0:
            debt_to_equity = interest_bearing_debt / total_equity
        elif total_liabilities is not None and total_equity and total_equity != 0:
            debt_to_equity = total_liabilities / total_equity

        return_on_assets = None
        if net_income is not None and total_assets and total_assets != 0:
            return_on_assets = net_income / total_assets

        quick_ratio = None
        if current_assets is not None and current_liabilities and current_liabilities != 0 and inventory is not None:
            quick_ratio = (current_assets - inventory) / current_liabilities

        cash_ratio = None
        if cash is not None and current_liabilities and current_liabilities != 0:
            cash_ratio = cash / current_liabilities

        operating_cash_flow_ratio = None
        if operating_cash_flow is not None and current_liabilities and current_liabilities != 0:
            operating_cash_flow_ratio = operating_cash_flow / current_liabilities

        return_on_invested_capital = None
        if operating_profit is not None and total_equity is not None:
            invested_capital = total_equity + (long_term_debt or 0)
            if invested_capital != 0:
                return_on_invested_capital = operating_profit / invested_capital

        asset_turnover = None
        if operating_revenue is not None and total_assets and total_assets != 0:
            asset_turnover = operating_revenue / total_assets

        inventory_turnover = None
        if operating_cost is not None and inventory and inventory != 0:
            inventory_turnover = operating_cost / inventory

        receivables_turnover = None
        if operating_revenue is not None and accounts_receivable and accounts_receivable != 0:
            receivables_turnover = operating_revenue / accounts_receivable

        days_sales_outstanding = None
        if receivables_turnover is not None and receivables_turnover != 0:
            days_sales_outstanding = 365.0 / receivables_turnover

        operating_cycle = None
        if inventory_turnover is not None and inventory_turnover != 0:
            days_inventory = 365.0 / inventory_turnover
            if days_sales_outstanding is not None:
                operating_cycle = days_inventory + days_sales_outstanding

        working_capital_turnover = None
        if operating_revenue is not None and current_assets is not None and current_liabilities is not None:
            working_capital = current_assets - current_liabilities
            if working_capital != 0:
                working_capital_turnover = operating_revenue / working_capital

        # interest_coverage: 当利息支出<=0时（如财务费用为负表示净利息收入），
        # 利息覆盖倍数理论上为无穷大，设为999.0表示"无利息偿付压力"
        interest_coverage = None
        if operating_profit is not None:
            if interest_expense is not None and interest_expense > 0:
                interest_coverage = operating_profit / interest_expense
            elif interest_expense is not None and interest_expense <= 0:
                interest_coverage = 999.0  # 无利息支出，覆盖倍数极高

        # === 增长率计算（YoY） ===
        prev_year = str(int(year) - 1)

        book_value_growth = None
        prev_bvps = _json_val(kfi, "book_value_per_share", prev_year)
        if bvps is not None and prev_bvps is not None and prev_bvps != 0:
            book_value_growth = (bvps - prev_bvps) / abs(prev_bvps)

        earnings_per_share_growth = None
        prev_eps = _json_val(kfi, "eps_basic", prev_year)
        if eps is not None and prev_eps is not None and prev_eps != 0:
            earnings_per_share_growth = (eps - prev_eps) / abs(prev_eps)

        free_cash_flow_growth = None
        prev_fcf = _json_val(cf, "free_cash_flow", prev_year)
        if free_cash_flow is not None and prev_fcf is not None and prev_fcf != 0:
            free_cash_flow_growth = (free_cash_flow - prev_fcf) / abs(prev_fcf)

        operating_income_growth = None
        prev_op_profit = _json_val(inc, "operating_profit", prev_year)
        if operating_profit is not None and prev_op_profit is not None and prev_op_profit != 0:
            operating_income_growth = (operating_profit - prev_op_profit) / abs(prev_op_profit)

        # === 2年CAGR计算 ===
        revenue_cagr_3y = None
        earnings_cagr_3y = None
        prev2_year = str(int(year) - 2)
        rev_prev2 = _json_val(inc, "operating_revenue", prev2_year)
        if operating_revenue is not None and rev_prev2 is not None and rev_prev2 != 0:
            revenue_cagr_3y = (operating_revenue / rev_prev2) ** (1.0 / 2) - 1
        ni_prev2 = _json_val(inc, "net_income_attributable_to_parent", prev2_year)
        if net_income is not None and ni_prev2 is not None and ni_prev2 != 0:
            earnings_cagr_3y = (net_income / ni_prev2) ** (1.0 / 2) - 1

        # === 构建 FinancialMetrics ===
        metrics = FinancialMetrics(
            ticker=ticker,
            report_period=report_period,
            period="annual",
            currency="CNY",
            market_cap=None,
            enterprise_value=None,
            price_to_earnings_ratio=None,
            price_to_book_ratio=None,
            price_to_sales_ratio=None,
            enterprise_value_to_ebitda_ratio=None,
            enterprise_value_to_revenue_ratio=None,
            free_cash_flow_yield=None,
            peg_ratio=None,
            gross_margin=gross_margin,
            operating_margin=operating_margin,
            net_margin=net_margin,
            return_on_equity=roe,
            return_on_assets=return_on_assets,
            return_on_invested_capital=return_on_invested_capital,
            asset_turnover=asset_turnover,
            inventory_turnover=inventory_turnover,
            receivables_turnover=receivables_turnover,
            days_sales_outstanding=days_sales_outstanding,
            operating_cycle=operating_cycle,
            working_capital_turnover=working_capital_turnover,
            current_ratio=current_ratio,
            quick_ratio=quick_ratio,
            cash_ratio=cash_ratio,
            operating_cash_flow_ratio=operating_cash_flow_ratio,
            debt_to_equity=debt_to_equity,
            debt_to_assets=debt_to_assets,
            interest_coverage=interest_coverage,
            revenue_growth=revenue_growth,
            earnings_growth=earnings_growth,
            book_value_growth=book_value_growth,
            earnings_per_share_growth=earnings_per_share_growth,
            free_cash_flow_growth=free_cash_flow_growth,
            operating_income_growth=operating_income_growth,
            ebitda_growth=None,
            payout_ratio=None,
            earnings_per_share=eps,
            book_value_per_share=bvps,
            free_cash_flow_per_share=None,
            revenue_cagr_3y=revenue_cagr_3y,
            earnings_cagr_3y=earnings_cagr_3y,
        )
        results.append(metrics)

    return results


def _build_line_items_from_json(ticker: str, json_data: dict, line_items: list[str], end_date: str, period: str) -> list[LineItem]:
    """从年报JSON构建LineItem列表（多期），JSON数据优先于AKShare。"""
    results = []
    years = _extract_json_years(json_data)
    inc = json_data.get("income_statement", {})
    bs = json_data.get("balance_sheet", {})
    cf = json_data.get("cash_flow_statement", {})

    # 收集所有需要的字段（请求字段 + 依赖字段）
    all_fields = set(line_items)
    for item in line_items:
        deps = _COMPUTED_DEPENDENCIES.get(item, [])
        for dep in deps:
            all_fields.add(dep)

    for year in years:
        report_period = f"{year}-annual"
        period_key = _report_period_sort_key(report_period)
        if period_key > end_date:
            continue

        item_dict: dict = {}

        # === 从 income_statement 提取 ===
        val = _json_val(inc, "operating_revenue", year)
        if val is not None:
            item_dict["total_revenue"] = val
        val = _json_val(inc, "operating_cost", year)
        if val is not None:
            item_dict["cost_of_revenue"] = val
        val = _json_val(inc, "gross_profit", year)
        if val is not None:
            item_dict["gross_profit"] = val
        val = _json_val(inc, "operating_profit", year)
        if val is not None:
            item_dict["operating_income"] = val
        val = _json_val(inc, "net_income_attributable_to_parent", year)
        if val is not None:
            item_dict["net_income"] = val
        val = _json_val(inc, "rd_expenses", year)
        if val is not None:
            item_dict["research_and_development"] = val
        val = _json_val(inc, "financial_expenses", year)
        if val is not None:
            # 财务费用为负表示净利息收入，此时利息支出视为0
            item_dict["interest_expense"] = val if val > 0 else 0.0
        val = _json_val(inc, "income_tax", year)
        if val is not None:
            item_dict["income_tax_expense"] = val
        # SGA = 销售费用 + 管理费用
        selling = _json_val(inc, "selling_expenses", year)
        admin = _json_val(inc, "admin_expenses", year)
        if selling is not None and admin is not None:
            item_dict["selling_general_and_administrative"] = selling + admin

        # === 从 balance_sheet 提取 ===
        for json_field, li_field in [
            ("total_assets", "total_assets"),
            ("current_assets", "current_assets"),
            ("total_liabilities", "total_liabilities"),
            ("current_liabilities", "current_liabilities"),
            ("total_equity", "total_equity"),
            ("shareholders_equity_attributable_to_parent", "shareholders_equity"),
            ("short_term_borrowings", "short_term_debt"),
            ("long_term_borrowings", "long_term_debt"),
            ("bonds_payable", "bonds_payable"),
            ("inventory", "inventory"),
            ("accounts_receivable", "accounts_receivable"),
            ("cash_and_equivalents", "cash_and_equivalents"),
            ("fixed_assets", "fixed_assets"),
            ("goodwill", "goodwill"),
            ("share_capital", "paid_in_capital"),
            ("retained_earnings", "retained_earnings"),
        ]:
            val = _json_val(bs, json_field, year)
            if val is not None:
                item_dict[li_field] = val

        # === 从 cash_flow_statement 提取 ===
        for json_field, li_field in [
            ("operating_cash_flow", "operating_cash_flow"),
            ("investing_cash_flow", "investing_cash_flow"),
            ("financing_cash_flow", "financing_cash_flow"),
            ("capital_expenditure", "capital_expenditure"),
            ("free_cash_flow", "free_cash_flow"),
        ]:
            val = _json_val(cf, json_field, year)
            if val is not None:
                item_dict[li_field] = val

        # 别名
        if "total_revenue" in item_dict and "revenue" not in item_dict:
            item_dict["revenue"] = item_dict["total_revenue"]
        if "total_equity" in item_dict and "shareholders_equity" not in item_dict:
            item_dict["shareholders_equity"] = item_dict["total_equity"]

        # 从 key_financial_indicators 提取 EPS 和 book_value_per_share
        kfi = json_data.get("key_financial_indicators", {})
        eps_val = _json_val(kfi, "eps_basic", year)
        if eps_val is not None:
            item_dict["earnings_per_share"] = eps_val
        bvps_val = _json_val(kfi, "book_value_per_share", year)
        if bvps_val is not None:
            item_dict["book_value_per_share"] = bvps_val

        # 从 balance_sheet 提取 outstanding_shares（基于 share_capital）
        sc_val = _json_val(json_data.get("balance_sheet", {}), "share_capital", year)
        if sc_val is not None and item_dict.get("outstanding_shares") is None:
            # A股面值1元，share_capital（元）/ 1 = 股数
            item_dict["outstanding_shares"] = float(sc_val)
            item_dict["paid_in_capital"] = float(sc_val)

        # 计算衍生字段（复用现有逻辑）
        item_dict = _compute_line_item_fields(item_dict)

        # JSON路径：保留所有已有字段（不过滤），确保Agent访问未显式请求的字段时不会AttributeError
        # 对于值为None的关键字段，也保留以确保Agent的hasattr/is not None检查正常工作
        _COMMON_LI_FIELDS = {
            "revenue", "net_income", "free_cash_flow", "operating_cash_flow",
            "capital_expenditure", "working_capital", "total_debt", "total_assets",
            "total_equity", "current_assets", "current_liabilities", "earnings_per_share",
            "book_value_per_share", "outstanding_shares", "depreciation_and_amortization",
            "gross_profit", "operating_income", "ebitda", "ebit",
            "short_term_debt", "long_term_debt", "cash_and_equivalents",
            "interest_expense", "shareholders_equity", "inventory",
            "accounts_receivable", "goodwill", "paid_in_capital",
            "retained_earnings", "fixed_assets", "cost_of_revenue",
            "selling_general_and_administrative", "research_and_development",
            "income_tax_expense", "total_revenue", "operating_expense",
            "gross_margin", "operating_margin", "net_margin",
            "debt_to_equity", "return_on_equity", "return_on_assets",
            "return_on_invested_capital",
            "dividends_and_other_cash_distributions",
            "issuance_or_purchase_of_equity_shares",
            "investing_cash_flow", "financing_cash_flow",
            "non_current_assets", "non_current_liabilities",
            "operating_expenses", "share_issuance", "dividends_paid",
            "minority_interest",
        }
        for field in _COMMON_LI_FIELDS:
            if field not in item_dict:
                item_dict[field] = None
        extra_fields = {k: v for k, v in item_dict.items() if v is not None or k in _COMMON_LI_FIELDS}

        line_item = LineItem(
            ticker=ticker,
            report_period=report_period,
            period="annual" if period == "ttm" else period,
            currency="CNY",
            **extra_fields,
        )
        results.append(line_item)

    return results


def _build_q1_line_item_from_json(ticker: str, q1_data: dict, end_date: str) -> LineItem | None:
    """从季报JSON构建单个Q1 LineItem，用于补充年报数据。"""
    q_period = q1_data.get("report_period", "")
    if not q_period or "-Q" not in q_period:
        return None

    period_key = _report_period_sort_key(q_period)
    if period_key > end_date:
        return None

    inc = q1_data.get("income_statement", {})
    bs = q1_data.get("balance_sheet", {})
    cf = q1_data.get("cash_flow_statement", {})
    kfi = q1_data.get("key_financial_indicators", {})

    qp = q_period  # e.g., "2026-Q1"

    item_dict: dict = {}

    # === 从 income_statement 提取 ===
    val = _json_val(inc, "operating_revenue", qp)
    if val is not None: item_dict["total_revenue"] = val
    val = _json_val(inc, "operating_cost", qp)
    if val is not None: item_dict["cost_of_revenue"] = val
    val = _json_val(inc, "gross_profit", qp)
    if val is not None: item_dict["gross_profit"] = val
    val = _json_val(inc, "operating_profit", qp)
    if val is not None: item_dict["operating_income"] = val
    val = _json_val(inc, "net_income_attributable_to_parent", qp)
    if val is not None: item_dict["net_income"] = val
    val = _json_val(inc, "rd_expenses", qp)
    if val is not None: item_dict["research_and_development"] = val
    val = _json_val(inc, "financial_expenses", qp)
    if val is not None:
        item_dict["interest_expense"] = val if val > 0 else 0.0
    val = _json_val(inc, "income_tax", qp)
    if val is not None: item_dict["income_tax_expense"] = val
    selling = _json_val(inc, "selling_expenses", qp)
    admin = _json_val(inc, "admin_expenses", qp)
    if selling is not None and admin is not None:
        item_dict["selling_general_and_administrative"] = selling + admin

    # === 从 balance_sheet 提取 ===
    for json_field, li_field in [
        ("total_assets", "total_assets"),
        ("current_assets", "current_assets"),
        ("non_current_assets", "non_current_assets"),
        ("total_liabilities", "total_liabilities"),
        ("current_liabilities", "current_liabilities"),
        ("non_current_liabilities", "non_current_liabilities"),
        ("total_equity", "total_equity"),
        ("shareholders_equity_attributable_to_parent", "shareholders_equity"),
        ("short_term_borrowings", "short_term_debt"),
        ("long_term_borrowings", "long_term_debt"),
        ("bonds_payable", "bonds_payable"),
        ("inventory", "inventory"),
        ("accounts_receivable", "accounts_receivable"),
        ("cash_and_equivalents", "cash_and_equivalents"),
        ("fixed_assets", "fixed_assets"),
        ("goodwill", "goodwill"),
        ("share_capital", "paid_in_capital"),
        ("retained_earnings", "retained_earnings"),
        ("minority_interest", "minority_interest"),
    ]:
        val = _json_val(bs, json_field, qp)
        if val is not None: item_dict[li_field] = val

    # === 从 cash_flow_statement 提取 ===
    for json_field, li_field in [
        ("operating_cash_flow", "operating_cash_flow"),
        ("investing_cash_flow", "investing_cash_flow"),
        ("financing_cash_flow", "financing_cash_flow"),
        ("capital_expenditure", "capital_expenditure"),
        ("free_cash_flow", "free_cash_flow"),
    ]:
        val = _json_val(cf, json_field, qp)
        if val is not None: item_dict[li_field] = val

    # 别名
    if "total_revenue" in item_dict and "revenue" not in item_dict:
        item_dict["revenue"] = item_dict["total_revenue"]
    if "total_equity" in item_dict and "shareholders_equity" not in item_dict:
        item_dict["shareholders_equity"] = item_dict["total_equity"]

    # 从 key_financial_indicators 提取 EPS 和 book_value_per_share
    eps_val = _json_val(kfi, "eps_basic", qp)
    if eps_val is not None: item_dict["earnings_per_share"] = eps_val
    bvps_val = _json_val(kfi, "book_value_per_share", qp)
    if bvps_val is not None: item_dict["book_value_per_share"] = bvps_val

    # outstanding_shares
    sc_val = _json_val(bs, "share_capital", qp)
    if sc_val is not None and item_dict.get("outstanding_shares") is None:
        item_dict["outstanding_shares"] = float(sc_val)
        item_dict["paid_in_capital"] = float(sc_val)

    # 计算衍生字段
    # 保存JSON直接提供的EPS/BVPS（比计算值更准确：EPS用加权平均股数，BVPS用归母权益）
    _json_eps = item_dict.get("earnings_per_share")
    _json_bvps = item_dict.get("book_value_per_share")
    item_dict = _compute_line_item_fields(item_dict)
    if _json_eps is not None:
        item_dict["earnings_per_share"] = _json_eps
    if _json_bvps is not None:
        item_dict["book_value_per_share"] = _json_bvps

    # 保留所有已有字段（与年报逻辑一致）
    _COMMON_LI_FIELDS = {
        "revenue", "net_income", "free_cash_flow", "operating_cash_flow",
        "capital_expenditure", "working_capital", "total_debt", "total_assets",
        "total_equity", "current_assets", "current_liabilities", "earnings_per_share",
        "book_value_per_share", "outstanding_shares", "depreciation_and_amortization",
        "gross_profit", "operating_income", "ebitda", "ebit",
        "short_term_debt", "long_term_debt", "cash_and_equivalents",
        "interest_expense", "shareholders_equity", "inventory",
        "accounts_receivable", "goodwill", "paid_in_capital",
        "retained_earnings", "fixed_assets", "cost_of_revenue",
        "selling_general_and_administrative", "research_and_development",
        "income_tax_expense", "total_revenue", "operating_expense",
        "gross_margin", "operating_margin", "net_margin",
        "debt_to_equity", "return_on_equity", "return_on_assets",
        "return_on_invested_capital",
        "dividends_and_other_cash_distributions",
        "issuance_or_purchase_of_equity_shares",
        "investing_cash_flow", "financing_cash_flow",
        "non_current_assets", "non_current_liabilities",
        "operating_expenses", "share_issuance", "dividends_paid",
        "minority_interest",
    }
    for field in _COMMON_LI_FIELDS:
        if field not in item_dict:
            item_dict[field] = None
    extra_fields = {k: v for k, v in item_dict.items() if v is not None or k in _COMMON_LI_FIELDS}

    return LineItem(
        ticker=ticker,
        report_period=q_period,
        period="quarterly",
        currency="CNY",
        **extra_fields,
    )


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


def _format_report_period(report_date: str) -> str:
    """将报告日期转换为带数据来源标记的格式，如 '2024-annual' 或 '2025-Q1'。

    根据 report_date 中的月份判断报告类型：
    - 12月 → annual（年报）
    - 3月  → Q1（一季报）
    - 6月  → Q2（半年报）
    - 9月  → Q3（三季报）
    """
    try:
        month = int(report_date[5:7])
        year = report_date[:4]
        if month == 12:
            return f"{year}-annual"
        elif month <= 3:
            return f"{year}-Q1"
        elif month <= 6:
            return f"{year}-Q2"
        elif month <= 9:
            return f"{year}-Q3"
        else:
            return f"{year}-annual"
    except (ValueError, IndexError):
        return report_date


def get_financial_metrics_ak(
    ticker: str,
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
) -> list[FinancialMetrics]:
    """
    使用 AKShare 获取财务指标。
    当存在年报 JSON 文件时，优先使用 JSON 数据，不再调用 AKShare。

    Returns: list[FinancialMetrics]，与 api.get_financial_metrics 返回类型一致。
    """
    try:
        symbol = _clean_ticker(ticker)

        # === JSON 年报数据优先：有 JSON 则直接使用，不调用 AKShare ===
        json_data = _load_annual_report_json(ticker)
        if json_data:
            json_metrics = _build_metrics_from_json(ticker, json_data, end_date)
            if json_metrics:
                logger.info(f"[JSON] Using annual report JSON data for {ticker}, {len(json_metrics)} periods")
                results = json_metrics

                # 补充市值和估值数据（需要实时数据，从腾讯API获取）
                try:
                    valuation = _get_valuation_from_tencent(symbol)
                    for metric in results:
                        if metric.price_to_earnings_ratio is None and valuation.get("price_to_earnings_ratio"):
                            metric.price_to_earnings_ratio = valuation["price_to_earnings_ratio"]
                        if metric.price_to_book_ratio is None and valuation.get("price_to_book_ratio"):
                            metric.price_to_book_ratio = valuation["price_to_book_ratio"]
                        if metric.market_cap is None and valuation.get("market_cap"):
                            metric.market_cap = valuation["market_cap"]
                except Exception as e:
                    logger.warning(f"[JSON] Failed to supplement valuation data for {ticker}: {e}")

                # 对前几个 metric 做衍生计算
                try:
                    for idx, metric in enumerate(results[:3]):
                        report_date = metric.report_period or end_date
                        mc = metric.market_cap
                        cp = None
                        derived = _compute_derived_metrics(symbol, report_date, market_cap=mc, current_price=cp)
                        for field, value in derived.items():
                            if hasattr(metric, field) and getattr(metric, field) is None and value is not None:
                                setattr(metric, field, value)
                        # peg_ratio 计算
                        if metric.peg_ratio is None and metric.price_to_earnings_ratio and metric.earnings_growth:
                            if metric.earnings_growth > 0:
                                metric.peg_ratio = metric.price_to_earnings_ratio / (metric.earnings_growth * 100)
                except Exception as e:
                    logger.warning(f"[JSON] Failed to compute derived metrics for {ticker}: {e}")

                # 补充季报同比增长率：优先使用Q1季报JSON，回退到AKShare
                try:
                    if results:
                        q1_data = _load_quarterly_report_json(ticker)
                        if q1_data:
                            # Q1 JSON数据优先（更准确、更及时）
                            q1_metrics = q1_data.get("financial_metrics", {})
                            metric = results[0]
                            rev_g = q1_metrics.get("revenue_growth")
                            earn_g = q1_metrics.get("earnings_growth")
                            if rev_g is not None:
                                metric.revenue_growth_quarterly = rev_g
                            if earn_g is not None:
                                metric.earnings_growth_quarterly = earn_g
                            logger.info(f"[JSON] Supplemented Q1 growth from quarterly JSON for {ticker}: "
                                        f"revenue={rev_g}, earnings={earn_g}")
                        else:
                            # 回退到AKShare计算
                            quarterly_growth = _compute_quarterly_growth(ticker)
                            metric = results[0]
                            if metric.revenue_growth_quarterly is None and quarterly_growth.get("revenue_growth_quarterly") is not None:
                                metric.revenue_growth_quarterly = quarterly_growth["revenue_growth_quarterly"]
                            if metric.earnings_growth_quarterly is None and quarterly_growth.get("earnings_growth_quarterly") is not None:
                                metric.earnings_growth_quarterly = quarterly_growth["earnings_growth_quarterly"]
                except Exception as e:
                    logger.warning(f"[JSON] Failed to supplement quarterly growth for {ticker}: {e}")

                # 格式化 report_period
                for metric in results:
                    if metric.report_period and "-" in metric.report_period and len(metric.report_period) >= 10:
                        metric.report_period = _format_report_period(metric.report_period)

                return results[:limit]

        # === 原有 AKShare 逻辑（无 JSON 数据时回退） ===
        start_year = str(int(end_date[:4]) - limit)

        df = AKShareRateLimiter.call_with_retry(
            ak.stock_financial_analysis_indicator,
            symbol=symbol,
            start_year=start_year,
        )
        if df is None or df.empty:
            logger.warning(f"AKShare: no financial metrics for {ticker}")
            return []

        # 按日期降序排列 DataFrame，确保最新数据在前
        if "日期" in df.columns:
            df = df.sort_values("日期", ascending=False)

        results: list[FinancialMetrics] = []
        for _, row in df.iterrows():
            try:
                report_date = str(row.get("日期", ""))[:10]
                if not report_date:
                    continue

                # 规范化日期格式（YYYYMMDD -> YYYY-MM-DD），确保字符串比较正确
                report_date_norm = _normalize_date(report_date)
                if report_date_norm > end_date:
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

        # 按报告期降序排序，优先选取年报数据
        if results:
            results.sort(key=lambda m: _report_period_sort_key(m.report_period or ""), reverse=True)
            annual = [m for m in results if _is_annual_report_period(m.report_period)]
            quarterly = [m for m in results if not _is_annual_report_period(m.report_period)]
            results = annual + quarterly
            logger.info(
                f"Financial metrics sorted: {len(annual)} annual, {len(quarterly)} quarterly, "
                f"first period={results[0].report_period if results else 'N/A'}"
            )

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
            first_derived: dict = {}  # 保存第一个 metric 的衍生值，用于增长率 fallback
            for idx, metric in enumerate(results[:3]):
                report_date = metric.report_period or end_date
                mc = metric.market_cap or valuation.get("market_cap")
                cp = valuation.get("current_price")
                derived = _compute_derived_metrics(symbol, report_date, market_cap=mc, current_price=cp)

                # 保存第一个 metric 的衍生计算结果，用于后续增长率 fallback
                if idx == 0:
                    first_derived = derived

                for field, value in derived.items():
                    if hasattr(metric, field) and getattr(metric, field) is None and value is not None:
                        setattr(metric, field, value)

                # peg_ratio 计算
                if metric.peg_ratio is None and metric.price_to_earnings_ratio and metric.earnings_growth:
                    if metric.earnings_growth > 0:
                        metric.peg_ratio = metric.price_to_earnings_ratio / (metric.earnings_growth * 100)

            # 补充季报同比增长率（只需对第一个 metric 计算）
            if results:
                quarterly_growth = _compute_quarterly_growth(ticker)
                metric = results[0]
                if metric.revenue_growth_quarterly is None and quarterly_growth.get("revenue_growth_quarterly") is not None:
                    metric.revenue_growth_quarterly = quarterly_growth["revenue_growth_quarterly"]
                if metric.earnings_growth_quarterly is None and quarterly_growth.get("earnings_growth_quarterly") is not None:
                    metric.earnings_growth_quarterly = quarterly_growth["earnings_growth_quarterly"]

            # 增长率 fallback：如果 AKShare 原始增长率为 None，使用衍生计算值填充
            if results and first_derived:
                growth_fields = [
                    'revenue_growth', 'earnings_growth', 'net_income_growth',
                    'operating_income_growth', 'ebitda_growth',
                    'book_value_growth', 'earnings_per_share_growth'
                ]
                for field in growth_fields:
                    if not hasattr(results[0], field):
                        continue
                    current_val = getattr(results[0], field, None)
                    derived_val = first_derived.get(field)
                    if current_val is None and derived_val is not None:
                        setattr(results[0], field, derived_val)
                        logger.info(f"Growth fallback for {ticker}: {field} = {derived_val:.4f} (from derived metrics)")

            # 格式化 report_period：将日期标记为年报或季报（如 '2024-annual', '2025-Q1'）
            # 注意：此步骤放在所有日期相关计算之后，避免影响日期比较逻辑
            for metric in results:
                if metric.report_period and "-" in metric.report_period and len(metric.report_period) >= 10:
                    metric.report_period = _format_report_period(metric.report_period)

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
    "debt_to_equity": ["short_term_debt", "long_term_debt", "bonds_payable", "shareholders_equity", "total_liabilities", "total_equity"],
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

    # total_debt = 短期借款 + 长期借款 + 应付债券（有息负债总额）
    std = item_dict.get("short_term_debt")
    ltd = item_dict.get("long_term_debt")
    bp = item_dict.get("bonds_payable")
    if std is not None or ltd is not None or bp is not None:
        item_dict["total_debt"] = (float(std) if std else 0) + (float(ltd) if ltd else 0) + (float(bp) if bp else 0)

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

    # debt_to_equity = 有息负债 / 归属于母公司股东的权益
    # 有息负债 = 短期借款 + 长期借款 + 应付债券
    # 注意：使用 total_liabilities/total_equity 会包含经营性负债（应付账款等），
    # 导致制造业企业 D/E 偏高，不符合投资分析中"有息负债率"的口径
    tl = item_dict.get("total_liabilities")
    te = item_dict.get("total_equity")
    se_parent = item_dict.get("shareholders_equity")  # 归属于母公司股东权益
    interest_bearing_debt = (float(std) if std else 0) + (float(ltd) if ltd else 0) + (float(bp) if bp else 0)
    # 优先使用有息负债口径，如果无有息负债数据则回退到总负债口径
    if interest_bearing_debt > 0 and se_parent is not None and float(se_parent) != 0:
        item_dict["debt_to_equity"] = interest_bearing_debt / float(se_parent)
    elif interest_bearing_debt > 0 and te is not None and te != 0:
        item_dict["debt_to_equity"] = interest_bearing_debt / te
    elif tl is not None and te is not None and te != 0:
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
    当存在年报 JSON 文件时，优先使用 JSON 数据，不再调用 AKShare。

    Returns: list[LineItem]，与 api.search_line_items 返回类型一致。
    """
    try:
        symbol = _clean_ticker(ticker)

        # === JSON 年报数据优先：有 JSON 则直接使用，不调用 AKShare ===
        json_data = _load_annual_report_json(ticker)
        if json_data:
            json_line_items = _build_line_items_from_json(ticker, json_data, line_items, end_date, period)
            if json_line_items:
                # 也尝试加载Q1季报数据，排在最前面（最新）
                q1_data = _load_quarterly_report_json(ticker)
                q1_line_item = None
                if q1_data:
                    q1_line_item = _build_q1_line_item_from_json(ticker, q1_data, end_date)

                if q1_line_item:
                    # 当请求 period="annual" 时，不将 Q1 季报数据混入结果
                    # 防止单季数据与全年数据做 YoY 比较导致增长率失真
                    if period == "annual":
                        results = json_line_items
                        logger.info(f"[JSON] period=annual, skipping Q1 for {ticker}, "
                                    f"got {len(results)} annual periods")
                    else:
                        results = [q1_line_item] + json_line_items
                        logger.info(f"[JSON] Using annual + Q1 JSON line items for {ticker}, "
                                    f"got {len(results)} periods (Q1={q1_line_item.report_period})")
                else:
                    results = json_line_items
                    logger.info(f"[JSON] Using annual report JSON line items for {ticker}, got {len(results)} periods")

                return results[:limit]

        # === 原有 AKShare 逻辑（无 JSON 数据时回退） ===
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

        # 按日期降序排列 DataFrame，确保最新数据在前
        _date_col = None
        for dc in ("报告日", "日期", "截止日期", "报告期"):
            if dc in primary_df.columns:
                _date_col = dc
                break
        if _date_col:
            primary_df = primary_df.sort_values(_date_col, ascending=False)

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

                # 规范化日期格式（YYYYMMDD -> YYYY-MM-DD），确保字符串比较正确
                report_date_norm = _normalize_date(report_date)
                if report_date_norm > end_date:
                    continue

                # 如果 period 指定为 "annual"，则只保留年报（12月末）数据，
                # 过滤掉 Q1/Q2/Q3 季报累计数，避免混入非全年数据导致 YoY 计算偏差
                if period == "annual" and not _is_annual_report_period(report_date):
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

        # 按报告期降序排序，确保最新数据排在前面
        if results:
            results.sort(key=lambda item: _report_period_sort_key(item.report_period or ""), reverse=True)

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
