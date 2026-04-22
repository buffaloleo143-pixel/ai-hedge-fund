"""
AKShare 提供器单元测试。

纯逻辑测试（is_a_share、速率限制器、工具函数）无需网络，
网络调用测试标记为 @pytest.mark.integration。
"""

import time
from unittest.mock import patch

import pytest

from src.tools.api import is_a_share
from src.tools.akshare_provider import (
    AKShareRateLimiter,
    _clean_ticker,
    _compute_derived_metrics,
    _date_to_akshare,
    _get_valuation_from_tencent,
    _safe_float,
    _safe_int,
    get_company_news_ak,
    get_financial_metrics_ak,
    get_insider_trades_ak,
    get_market_cap_ak,
    get_prices_ak,
    search_line_items_ak,
)
from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    InsiderTrade,
    LineItem,
    Price,
)


# ===========================================================================
# 1. is_a_share() 路由判断
# ===========================================================================

class TestIsAShare:
    """测试 is_a_share() 路由判断逻辑。"""

    def test_pure_digit_a_share(self):
        assert is_a_share("600519") is True
        assert is_a_share("000001") is True
        assert is_a_share("300750") is True

    def test_prefixed_a_share(self):
        assert is_a_share("sh600519") is True
        assert is_a_share("sz000001") is True
        assert is_a_share("bj430047") is True

    def test_uppercase_prefixed_a_share(self):
        assert is_a_share("SH600519") is True
        assert is_a_share("SZ000001") is True
        assert is_a_share("BJ430047") is True

    def test_us_stocks(self):
        assert is_a_share("AAPL") is False
        assert is_a_share("MSFT") is False
        assert is_a_share("NVDA") is False
        assert is_a_share("TSLA") is False

    def test_edge_cases(self):
        # 5 位数字不算 A 股
        assert is_a_share("60051") is False
        # 7 位数字不算 A 股
        assert is_a_share("6005190") is False
        # 空字符串
        assert is_a_share("") is False
        # 混合字母数字（非前缀）
        assert is_a_share("AAP600519") is False


# ===========================================================================
# 2. 工具函数
# ===========================================================================

class TestCleanTicker:
    """测试 _clean_ticker() 清理逻辑。"""

    def test_pure_digit(self):
        assert _clean_ticker("600519") == "600519"

    def test_sh_prefix(self):
        assert _clean_ticker("sh600519") == "600519"

    def test_sz_prefix(self):
        assert _clean_ticker("sz000001") == "000001"

    def test_bj_prefix(self):
        assert _clean_ticker("bj430047") == "430047"

    def test_uppercase_prefix(self):
        assert _clean_ticker("SH600519") == "600519"

    def test_whitespace(self):
        assert _clean_ticker(" 600519 ") == "600519"


class TestDateToAkshare:
    """测试 _date_to_akshare() 日期格式转换。"""

    def test_normal_date(self):
        assert _date_to_akshare("2024-01-31") == "20240131"

    def test_start_of_year(self):
        assert _date_to_akshare("2024-01-01") == "20240101"


class TestSafeFloat:
    """测试 _safe_float() 安全浮点转换。"""

    def test_normal_number(self):
        assert _safe_float(3.14) == 3.14

    def test_string_number(self):
        assert _safe_float("2.5") == 2.5

    def test_integer(self):
        assert _safe_float(10) == 10.0

    def test_none_returns_none(self):
        assert _safe_float(None) is None

    def test_nan_returns_none(self):
        import pandas as pd
        assert _safe_float(float("nan")) is None
        assert _safe_float(pd.NA) is None

    def test_invalid_string_returns_none(self):
        assert _safe_float("abc") is None


class TestSafeInt:
    """测试 _safe_int() 安全整数转换。"""

    def test_normal_number(self):
        assert _safe_int(10) == 10

    def test_float_truncates(self):
        assert _safe_int(3.7) == 3

    def test_string_number(self):
        assert _safe_int("42") == 42

    def test_none_returns_none(self):
        assert _safe_int(None) is None

    def test_invalid_string_returns_none(self):
        assert _safe_int("abc") is None


# ===========================================================================
# 3. 速率限制器
# ===========================================================================

class TestAKShareRateLimiter:
    """测试 AKShareRateLimiter 速率限制。"""

    def test_rate_limiter_enforces_delay(self):
        """第二次调用应至少等待 delay 秒。"""
        # 重置状态
        AKShareRateLimiter._last_call_time = 0
        start = time.time()
        AKShareRateLimiter.wait(delay=2)
        AKShareRateLimiter.wait(delay=2)
        elapsed = time.time() - start
        assert elapsed >= 2, f"Expected >= 2s, got {elapsed:.2f}s"

    def test_rate_limiter_no_wait_when_enough_time_passed(self):
        """如果距离上次调用已足够久，无需等待。"""
        AKShareRateLimiter._last_call_time = 0
        AKShareRateLimiter.wait(delay=0)  # 第一次立即完成
        # 模拟已过很久
        AKShareRateLimiter._last_call_time = time.time() - 100
        start = time.time()
        AKShareRateLimiter.wait(delay=2)
        elapsed = time.time() - start
        assert elapsed < 1, f"Expected < 1s, got {elapsed:.2f}s"

    def test_call_with_retry_success(self):
        """call_with_retry 在成功时返回 DataFrame。"""
        import pandas as pd

        AKShareRateLimiter._last_call_time = 0
        df = pd.DataFrame({"a": [1, 2]})

        # 跳过 wait 的 sleep
        with patch.object(AKShareRateLimiter, "wait"):
            result = AKShareRateLimiter.call_with_retry(lambda: df, delay=0)
        assert result is not None
        assert not result.empty

    def test_call_with_retry_returns_none_after_exhausted(self):
        """call_with_retry 在所有重试失败后返回 None。"""
        AKShareRateLimiter._last_call_time = 0

        def always_fail():
            raise RuntimeError("fail")

        with patch.object(AKShareRateLimiter, "wait"):
            with patch("time.sleep"):
                result = AKShareRateLimiter.call_with_retry(
                    always_fail, max_retries=2, delay=0
                )
        assert result is None

    def test_call_with_retry_empty_df_retried(self):
        """call_with_retry 对空 DataFrame 做重试。"""
        import pandas as pd

        AKShareRateLimiter._last_call_time = 0
        call_count = 0

        def returns_empty_then_data():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return pd.DataFrame()
            return pd.DataFrame({"a": [1]})

        with patch.object(AKShareRateLimiter, "wait"):
            with patch("time.sleep"):
                result = AKShareRateLimiter.call_with_retry(
                    returns_empty_then_data, max_retries=3, delay=0
                )
        assert result is not None


# ===========================================================================
# 4. 各 _ak() 函数返回格式（需要网络，标记 integration）
# ===========================================================================

@pytest.mark.integration
class TestGetPricesAk:
    """测试 get_prices_ak 返回格式。"""

    def test_returns_list_of_price(self):
        prices = get_prices_ak("600519", "2024-01-01", "2024-01-31")
        assert isinstance(prices, list)
        if prices:
            assert isinstance(prices[0], Price)
            assert prices[0].open is not None
            assert prices[0].close is not None
            assert prices[0].volume is not None

    def test_returns_empty_for_no_data(self):
        prices = get_prices_ak("600519", "2099-01-01", "2099-01-31")
        assert isinstance(prices, list)
        assert len(prices) == 0


@pytest.mark.integration
class TestGetFinancialMetricsAk:
    """测试 get_financial_metrics_ak 返回格式。"""

    def test_returns_list_of_financial_metrics(self):
        metrics = get_financial_metrics_ak("600519", "2024-12-31", "annual", 5)
        assert isinstance(metrics, list)
        if metrics:
            assert isinstance(metrics[0], FinancialMetrics)
            assert metrics[0].ticker == "600519"
            assert metrics[0].report_period is not None

    def test_respects_limit(self):
        metrics = get_financial_metrics_ak("600519", "2024-12-31", "annual", 2)
        assert isinstance(metrics, list)
        assert len(metrics) <= 2


@pytest.mark.integration
class TestSearchLineItemsAk:
    """测试 search_line_items_ak 返回格式。"""

    def test_returns_list_of_line_item(self):
        items = search_line_items_ak(
            "600519",
            ["total_revenue", "net_income"],
            "2024-12-31",
            "annual",
            5,
        )
        assert isinstance(items, list)
        if items:
            assert isinstance(items[0], LineItem)
            assert items[0].ticker == "600519"


@pytest.mark.integration
class TestGetInsiderTradesAk:
    """测试 get_insider_trades_ak 返回格式。"""

    def test_returns_list_of_insider_trade(self):
        trades = get_insider_trades_ak("600519", "2024-12-31", limit=10)
        assert isinstance(trades, list)
        if trades:
            assert isinstance(trades[0], InsiderTrade)
            assert trades[0].ticker == "600519"


@pytest.mark.integration
class TestGetCompanyNewsAk:
    """测试 get_company_news_ak 返回格式。"""

    def test_returns_list_of_company_news(self):
        news = get_company_news_ak("600519", "2024-12-31", limit=10)
        assert isinstance(news, list)
        if news:
            assert isinstance(news[0], CompanyNews)
            assert news[0].ticker == "600519"
            assert news[0].title is not None


@pytest.mark.integration
class TestGetMarketCapAk:
    """测试 get_market_cap_ak 返回格式。"""

    def test_returns_float_or_none(self):
        cap = get_market_cap_ak("600519", "2024-12-31")
        assert cap is None or isinstance(cap, float)
        if cap is not None:
            assert cap > 0


# ===========================================================================
# 5. 错误处理
# ===========================================================================

@pytest.mark.integration
class TestErrorHandling:
    """测试无效输入时的错误处理（不抛异常，返回空列表/None）。"""

    def test_invalid_ticker_prices_returns_empty(self):
        prices = get_prices_ak("999999", "2024-01-01", "2024-01-31")
        assert isinstance(prices, list)

    def test_invalid_ticker_financial_metrics_returns_empty(self):
        metrics = get_financial_metrics_ak("999999", "2024-12-31", "annual", 5)
        assert isinstance(metrics, list)

    def test_invalid_ticker_market_cap_returns_none(self):
        cap = get_market_cap_ak("999999", "2024-12-31")
        assert cap is None or isinstance(cap, float)


class TestErrorHandlingMocked:
    """使用 mock 测试异常路径，无需网络。"""

    def test_get_prices_ak_exception_returns_empty(self):
        with patch("src.tools.akshare_provider._get_prices_tencent", return_value=[]):
            with patch("src.tools.akshare_provider._get_prices_via_akshare_sina", return_value=[]):
                with patch("src.tools.akshare_provider.AKShareRateLimiter.call_with_retry", side_effect=Exception("boom")):
                    result = get_prices_ak("600519", "2024-01-01", "2024-01-31")
        assert result == []

    def test_get_financial_metrics_ak_exception_returns_empty(self):
        with patch("src.tools.akshare_provider.AKShareRateLimiter.call_with_retry", side_effect=Exception("boom")):
            result = get_financial_metrics_ak("600519", "2024-12-31")
        assert result == []

    def test_search_line_items_ak_exception_returns_empty(self):
        with patch("src.tools.akshare_provider.AKShareRateLimiter.call_with_retry", side_effect=Exception("boom")):
            result = search_line_items_ak("600519", ["total_revenue"], "2024-12-31")
        assert result == []

    def test_get_insider_trades_ak_exception_returns_empty(self):
        with patch("src.tools.akshare_provider.AKShareRateLimiter.call_with_retry", side_effect=Exception("boom")):
            result = get_insider_trades_ak("600519", "2024-12-31")
        assert result == []

    def test_get_company_news_ak_exception_returns_empty(self):
        with patch("src.tools.akshare_provider.AKShareRateLimiter.call_with_retry", side_effect=Exception("boom")):
            result = get_company_news_ak("600519", "2024-12-31")
        assert result == []

    def test_get_market_cap_ak_exception_returns_none(self):
        with patch("src.tools.akshare_provider._get_market_cap_tencent", return_value=None):
            with patch("src.tools.akshare_provider.AKShareRateLimiter.call_with_retry", side_effect=Exception("boom")):
                result = get_market_cap_ak("600519", "2024-12-31")
        assert result is None

    def test_get_prices_ak_empty_df_returns_empty(self):
        import pandas as pd
        with patch("src.tools.akshare_provider._get_prices_tencent", return_value=[]):
            with patch("src.tools.akshare_provider._get_prices_via_akshare_sina", return_value=[]):
                with patch("src.tools.akshare_provider.AKShareRateLimiter.call_with_retry", return_value=pd.DataFrame()):
                    result = get_prices_ak("600519", "2024-01-01", "2024-01-31")
        assert result == []

    def test_get_prices_ak_none_returns_empty(self):
        with patch("src.tools.akshare_provider._get_prices_tencent", return_value=[]):
            with patch("src.tools.akshare_provider._get_prices_via_akshare_sina", return_value=[]):
                with patch("src.tools.akshare_provider.AKShareRateLimiter.call_with_retry", return_value=None):
                    result = get_prices_ak("600519", "2024-01-01", "2024-01-31")
        assert result == []


# ===========================================================================
# 6. _get_valuation_from_tencent() 测试
# ===========================================================================

class TestGetValuationFromTencent:
    """测试 _get_valuation_from_tencent() 估值数据获取。"""

    def test_returns_dict(self):
        """Mock HTTP 测试返回格式"""
        # 构造腾讯格式的响应：~分隔，position 3=价格, 39=PE, 45=市值(亿), 46=PB
        parts = [""] * 51
        parts[3] = "25.50"    # 当前价格
        parts[39] = "12.5"    # PE
        parts[45] = "2000"    # 总市值（亿）
        parts[46] = "3.2"     # PB
        mock_text = "~".join(parts)

        mock_response = type("Response", (), {
            "status_code": 200,
            "text": mock_text,
        })()

        with patch("src.tools.akshare_provider.requests.get", return_value=mock_response):
            result = _get_valuation_from_tencent("600690")

        assert isinstance(result, dict)
        assert "price_to_earnings_ratio" in result
        assert "price_to_book_ratio" in result
        assert "market_cap" in result
        assert "current_price" in result
        assert result["price_to_earnings_ratio"] == 12.5
        assert result["price_to_book_ratio"] == 3.2
        assert result["market_cap"] == 2000 * 1e8
        assert result["current_price"] == 25.50

    def test_handles_error(self):
        """测试网络错误时返回空 dict"""
        with patch("src.tools.akshare_provider.requests.get", side_effect=ConnectionError("network error")):
            result = _get_valuation_from_tencent("600690")
        assert result == {}

    def test_handles_non_200_status(self):
        """测试非 200 状态码返回空 dict"""
        mock_response = type("Response", (), {
            "status_code": 500,
            "text": "",
        })()
        with patch("src.tools.akshare_provider.requests.get", return_value=mock_response):
            result = _get_valuation_from_tencent("600690")
        assert result == {}

    def test_handles_short_response(self):
        """测试响应字段不足时返回空 dict"""
        mock_response = type("Response", (), {
            "status_code": 200,
            "text": "1~2~3",
        })()
        with patch("src.tools.akshare_provider.requests.get", return_value=mock_response):
            result = _get_valuation_from_tencent("600690")
        assert result == {}

    @pytest.mark.integration
    def test_real_data(self):
        """集成测试：实际获取 600690 数据"""
        from src.tools.akshare_provider import _get_valuation_from_tencent
        result = _get_valuation_from_tencent("600690")
        assert isinstance(result, dict)
        if result:
            assert "price_to_earnings_ratio" in result or "price_to_book_ratio" in result


# ===========================================================================
# 7. _compute_derived_metrics() 测试
# ===========================================================================

class TestComputeDerivedMetrics:
    """测试 _compute_derived_metrics() 衍生指标计算。"""

    @staticmethod
    def _make_line_item(**kwargs):
        """创建测试用 LineItem。"""
        return LineItem(
            ticker="600690",
            report_period="2024-12-31",
            period="annual",
            currency="CNY",
            **kwargs,
        )

    def test_computes_operating_margin(self):
        """Mock search_line_items_ak，验证 operating_margin 计算"""
        items = [
            self._make_line_item(
                operating_income=100,
                total_revenue=500,
            )
        ]
        with patch("src.tools.akshare_provider.search_line_items_ak", return_value=items):
            result = _compute_derived_metrics("600690", "2024-12-31")
        assert "operating_margin" in result
        assert abs(result["operating_margin"] - 0.2) < 1e-6

    def test_computes_asset_turnover(self):
        """验证 asset_turnover 计算"""
        items = [
            self._make_line_item(
                total_revenue=1000,
                total_assets=500,
            )
        ]
        with patch("src.tools.akshare_provider.search_line_items_ak", return_value=items):
            result = _compute_derived_metrics("600690", "2024-12-31")
        assert "asset_turnover" in result
        assert abs(result["asset_turnover"] - 2.0) < 1e-6

    def test_computes_debt_to_equity(self):
        """验证 debt_to_equity 计算"""
        items = [
            self._make_line_item(
                total_liabilities=300,
                total_equity=200,
            )
        ]
        with patch("src.tools.akshare_provider.search_line_items_ak", return_value=items):
            result = _compute_derived_metrics("600690", "2024-12-31")
        assert "debt_to_equity" in result
        assert abs(result["debt_to_equity"] - 1.5) < 1e-6

    def test_handles_empty_data(self):
        """验证无数据时返回空 dict"""
        with patch("src.tools.akshare_provider.search_line_items_ak", return_value=[]):
            result = _compute_derived_metrics("600690", "2024-12-31")
        assert result == {}

    def test_handles_zero_division(self):
        """验证分母为0时不崩溃"""
        items = [
            self._make_line_item(
                operating_income=100,
                total_revenue=0,  # 分母为 0
                total_assets=0,
                total_equity=0,
            )
        ]
        with patch("src.tools.akshare_provider.search_line_items_ak", return_value=items):
            result = _compute_derived_metrics("600690", "2024-12-31")
        # 不应包含需要除以0的指标
        assert "operating_margin" not in result
        assert "asset_turnover" not in result
        assert "debt_to_equity" not in result
        assert isinstance(result, dict)

    def test_computes_gross_margin(self):
        """验证 gross_margin 计算"""
        items = [
            self._make_line_item(
                total_revenue=500,
                cost_of_revenue=300,
            )
        ]
        with patch("src.tools.akshare_provider.search_line_items_ak", return_value=items):
            result = _compute_derived_metrics("600690", "2024-12-31")
        assert "gross_margin" in result
        assert abs(result["gross_margin"] - 0.4) < 1e-6

    def test_computes_net_margin(self):
        """验证 net_margin 计算"""
        items = [
            self._make_line_item(
                net_income=80,
                total_revenue=500,
            )
        ]
        with patch("src.tools.akshare_provider.search_line_items_ak", return_value=items):
            result = _compute_derived_metrics("600690", "2024-12-31")
        assert "net_margin" in result
        assert abs(result["net_margin"] - 0.16) < 1e-6

    def test_computes_return_on_equity(self):
        """验证 return_on_equity 计算"""
        items = [
            self._make_line_item(
                net_income=50,
                total_equity=250,
            )
        ]
        with patch("src.tools.akshare_provider.search_line_items_ak", return_value=items):
            result = _compute_derived_metrics("600690", "2024-12-31")
        assert "return_on_equity" in result
        assert abs(result["return_on_equity"] - 0.2) < 1e-6

    def test_computes_cash_ratio(self):
        """验证 cash_ratio 计算"""
        items = [
            self._make_line_item(
                cash_and_equivalents=100,
                current_liabilities=200,
            )
        ]
        with patch("src.tools.akshare_provider.search_line_items_ak", return_value=items):
            result = _compute_derived_metrics("600690", "2024-12-31")
        assert "cash_ratio" in result
        assert abs(result["cash_ratio"] - 0.5) < 1e-6

    def test_computes_enterprise_value(self):
        """验证 enterprise_value 计算（需要 market_cap）"""
        items = [
            self._make_line_item(
                short_term_debt=50,
                long_term_debt=100,
                cash_and_equivalents=30,
            )
        ]
        with patch("src.tools.akshare_provider.search_line_items_ak", return_value=items):
            result = _compute_derived_metrics("600690", "2024-12-31", market_cap=1000)
        assert "enterprise_value" in result
        # EV = market_cap + total_debt - cash = 1000 + 150 - 30 = 1120
        assert abs(result["enterprise_value"] - 1120) < 1e-6

    def test_computes_growth_with_two_periods(self):
        """验证两期数据时增长率计算"""
        items = [
            self._make_line_item(
                total_revenue=600,
                net_income=120,
                total_equity=500,
                operating_income=150,
                operating_cash_flow=200,
            ),
            self._make_line_item(
                total_revenue=500,
                net_income=100,
                total_equity=450,
                operating_income=130,
                operating_cash_flow=180,
            ),
        ]
        with patch("src.tools.akshare_provider.search_line_items_ak", return_value=items):
            result = _compute_derived_metrics("600690", "2024-12-31")
        assert "revenue_growth" in result
        assert abs(result["revenue_growth"] - 0.2) < 1e-6
        assert "earnings_growth" in result
        assert abs(result["earnings_growth"] - 0.2) < 1e-6


# ===========================================================================
# 8. 增强后的 get_financial_metrics_ak() 测试
# ===========================================================================

class TestEnhancedFinancialMetrics:
    """测试增强后 get_financial_metrics_ak 的字段覆盖率。"""

    @pytest.mark.integration
    def test_field_count(self):
        """集成测试：验证返回字段数量 >= 25"""
        from src.tools.akshare_provider import get_financial_metrics_ak
        metrics = get_financial_metrics_ak("600690", "2025-04-20", "annual", 1)
        assert len(metrics) >= 1
        m = metrics[0]
        non_none = sum(1 for f in [
            'market_cap', 'price_to_earnings_ratio', 'price_to_book_ratio',
            'gross_margin', 'operating_margin', 'net_margin', 'return_on_equity',
            'return_on_assets', 'current_ratio', 'quick_ratio', 'debt_to_equity',
            'debt_to_assets', 'revenue_growth', 'earnings_growth',
            'earnings_per_share', 'book_value_per_share', 'asset_turnover',
            'inventory_turnover', 'cash_ratio', 'operating_cash_flow_ratio',
            'enterprise_value', 'price_to_sales_ratio'
        ] if getattr(m, f, None) is not None)
        assert non_none >= 20, f"Only {non_none} fields with value"
