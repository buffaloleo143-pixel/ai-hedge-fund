from langchain_core.messages import HumanMessage
from src.graph.state import AgentState, show_agent_reasoning
from src.utils.api_key import get_api_key_from_state
from src.utils.progress import progress
import json

from src.tools.api import get_financial_metrics, get_prices
from datetime import datetime, timedelta


##### Fundamental Agent #####
def fundamentals_analyst_agent(state: AgentState, agent_id: str = "fundamentals_analyst_agent"):
    """Analyzes fundamental data and generates trading signals for multiple tickers."""
    data = state["data"]
    end_date = data["end_date"]
    tickers = data["tickers"]
    api_key = get_api_key_from_state(state, "FINANCIAL_DATASETS_API_KEY")
    # Initialize fundamental analysis for each ticker
    fundamental_analysis = {}

    for ticker in tickers:
        progress.update_status(agent_id, ticker, "Fetching financial metrics")

        # Get the financial metrics
        financial_metrics = get_financial_metrics(
            ticker=ticker,
            end_date=end_date,
            period="ttm",
            limit=10,
            api_key=api_key,
        )

        if not financial_metrics:
            progress.update_status(agent_id, ticker, "Failed: No financial metrics found")
            continue

        # Pull the most recent financial metrics
        metrics = financial_metrics[0]

        # Fetch current price for accurate price targets
        progress.update_status(agent_id, ticker, "Fetching current price")
        start_price_date = (datetime.fromisoformat(end_date) - timedelta(days=30)).date().isoformat()
        prices = get_prices(ticker, start_price_date, end_date, api_key=api_key, adjust="")
        current_price = prices[-1].close if prices else None

        # Initialize signals list for different fundamental aspects
        signals = []
        reasoning = {}

        progress.update_status(agent_id, ticker, "Analyzing profitability")
        # 1. Profitability Analysis
        return_on_equity = metrics.return_on_equity
        net_margin = metrics.net_margin
        operating_margin = metrics.operating_margin

        thresholds = [
            (return_on_equity, 0.15),  # Strong ROE above 15%
            (net_margin, 0.20),  # Healthy profit margins
            (operating_margin, 0.15),  # Strong operating efficiency
        ]
        profitability_score = sum(metric is not None and metric > threshold for metric, threshold in thresholds)

        signals.append("bullish" if profitability_score >= 2 else "bearish" if profitability_score == 0 else "neutral")
        reasoning["profitability_signal"] = {
            "signal": signals[0],
            "details": (f"ROE: {return_on_equity:.2%}" if return_on_equity else "ROE: N/A") + ", " + (f"Net Margin: {net_margin:.2%}" if net_margin else "Net Margin: N/A") + ", " + (f"Op Margin: {operating_margin:.2%}" if operating_margin else "Op Margin: N/A"),
        }

        progress.update_status(agent_id, ticker, "Analyzing growth")
        # 2. Growth Analysis
        # 增长率取值优先级：revenue_growth（年报YoY，已fallback）-> revenue_growth_quarterly（季报同比）
        revenue_growth = metrics.revenue_growth or metrics.revenue_growth_quarterly
        earnings_growth = metrics.earnings_growth or metrics.earnings_growth_quarterly
        book_value_growth = metrics.book_value_growth

        thresholds = [
            (revenue_growth, 0.10),  # 10% revenue growth
            (earnings_growth, 0.10),  # 10% earnings growth
            (book_value_growth, 0.10),  # 10% book value growth
        ]
        growth_score = sum(metric is not None and metric > threshold for metric, threshold in thresholds)

        # 补充3年CAGR作为长期增长参考
        cagr_info = ""
        if metrics.revenue_cagr_3y is not None:
            cagr_info += f", Revenue 3Y CAGR: {metrics.revenue_cagr_3y:.2%}"
        if metrics.earnings_cagr_3y is not None:
            cagr_info += f", Earnings 3Y CAGR: {metrics.earnings_cagr_3y:.2%}"

        signals.append("bullish" if growth_score >= 2 else "bearish" if growth_score == 0 else "neutral")
        reasoning["growth_signal"] = {
            "signal": signals[1],
            "details": (f"Revenue Growth: {revenue_growth:.2%}" if revenue_growth else "Revenue Growth: N/A") + ", " + (f"Earnings Growth: {earnings_growth:.2%}" if earnings_growth else "Earnings Growth: N/A") + cagr_info,
            "data_caliber": "基于最新年报数据，季报同比作为补充参考",
        }

        progress.update_status(agent_id, ticker, "Analyzing financial health")
        # 3. Financial Health
        current_ratio = metrics.current_ratio
        debt_to_equity = metrics.debt_to_equity
        free_cash_flow_per_share = metrics.free_cash_flow_per_share
        earnings_per_share = metrics.earnings_per_share

        health_score = 0
        if current_ratio and current_ratio > 1.5:  # Strong liquidity
            health_score += 1
        if debt_to_equity and debt_to_equity < 0.5:  # 有息负债口径：保守债务水平
            health_score += 1
        if debt_to_equity and debt_to_equity < 1.0:  # 有息负债口径：中等债务水平也可接受
            health_score += 1
        if free_cash_flow_per_share and earnings_per_share and free_cash_flow_per_share > earnings_per_share * 0.8:  # Strong FCF conversion
            health_score += 1

        signals.append("bullish" if health_score >= 2 else "bearish" if health_score == 0 else "neutral")
        reasoning["financial_health_signal"] = {
            "signal": signals[2],
            "details": (f"Current Ratio: {current_ratio:.2f}" if current_ratio else "Current Ratio: N/A") + ", " + (f"D/E: {debt_to_equity:.2f}" if debt_to_equity else "D/E: N/A"),
        }

        progress.update_status(agent_id, ticker, "Analyzing valuation ratios")
        # 4. Price to X ratios
        pe_ratio = metrics.price_to_earnings_ratio
        pb_ratio = metrics.price_to_book_ratio
        ps_ratio = metrics.price_to_sales_ratio

        thresholds = [
            (pe_ratio, 25),  # Reasonable P/E ratio
            (pb_ratio, 3),  # Reasonable P/B ratio
            (ps_ratio, 5),  # Reasonable P/S ratio
        ]
        price_ratio_score = sum(metric is not None and metric > threshold for metric, threshold in thresholds)

        signals.append("bearish" if price_ratio_score >= 2 else "bullish" if price_ratio_score == 0 else "neutral")
        reasoning["price_ratios_signal"] = {
            "signal": signals[3],
            "details": (f"P/E: {pe_ratio:.2f}" if pe_ratio else "P/E: N/A") + ", " + (f"P/B: {pb_ratio:.2f}" if pb_ratio else "P/B: N/A") + ", " + (f"P/S: {ps_ratio:.2f}" if ps_ratio else "P/S: N/A"),
        }

        progress.update_status(agent_id, ticker, "Calculating final signal")
        # Determine overall signal
        bullish_signals = signals.count("bullish")
        bearish_signals = signals.count("bearish")

        if bullish_signals > bearish_signals:
            overall_signal = "bullish"
        elif bearish_signals > bullish_signals:
            overall_signal = "bearish"
        else:
            overall_signal = "neutral"

        # Calculate confidence level
        total_signals = len(signals)
        confidence = round(max(bullish_signals, bearish_signals) / total_signals, 2) * 100

        # Calculate price targets based on fundamental metrics
        price_targets = calculate_fundamental_price_targets(metrics, overall_signal, current_price)

        fundamental_analysis[ticker] = {
            "signal": overall_signal,
            "confidence": confidence,
            "short_term_price": price_targets["short_term_price"],
            "medium_term_price": price_targets["medium_term_price"],
            "long_term_price": price_targets["long_term_price"],
            "target_buy_price": price_targets["target_buy_price"],
            "target_sell_price": price_targets["target_sell_price"],
            "current_price": current_price,
            "reasoning": reasoning,
        }

        progress.update_status(agent_id, ticker, "Done", analysis=json.dumps(reasoning, indent=4))

    # Create the fundamental analysis message
    message = HumanMessage(
        content=json.dumps(fundamental_analysis),
        name=agent_id,
    )

    # Print the reasoning if the flag is set
    if state["metadata"]["show_reasoning"]:
        show_agent_reasoning(fundamental_analysis, "Fundamental Analysis Agent")

    progress.update_status(agent_id, None, "Done")
    
    return {
        "messages": [message],
        "data": data,
        "analyst_signals": {agent_id: fundamental_analysis},
    }


def calculate_fundamental_price_targets(metrics, signal: str, current_price: float = None) -> dict:
    """
    Estimate price targets based on fundamental valuation metrics.
    Uses EPS and P/E ratios to derive target prices for different time horizons.
    """
    eps = getattr(metrics, "earnings_per_share", None)
    pe_ratio = getattr(metrics, "price_to_earnings_ratio", None)
    # 增长率取值优先级：年报YoY（已fallback）-> 季报同比
    revenue_growth = getattr(metrics, "revenue_growth", None) or getattr(metrics, "revenue_growth_quarterly", None)
    earnings_growth = getattr(metrics, "earnings_growth", None) or getattr(metrics, "earnings_growth_quarterly", None)

    # Cannot estimate without EPS and P/E
    if not eps or not pe_ratio or eps <= 0 or pe_ratio <= 0:
        return {
            "short_term_price": None,
            "medium_term_price": None,
            "long_term_price": None,
            "target_buy_price": None,
            "target_sell_price": None,
        }

    # Current implied price from fundamentals
    current_implied = eps * pe_ratio
    # Use actual current price as baseline if available and reasonable
    if current_price and current_price > 0:
        current_implied = current_price

    # Growth adjustments by signal
    if signal == "bullish":
        growth_short = 1 + (earnings_growth or revenue_growth or 0.10)
        growth_medium = growth_short ** 1.5
        growth_long = growth_short ** 3.0
        pe_adjustment = 1.05  # slight P/E expansion for bullish
    elif signal == "bearish":
        growth_short = 1 + min(earnings_growth or revenue_growth or -0.05, 0)
        growth_medium = growth_short ** 1.5
        growth_long = growth_short ** 3.0
        pe_adjustment = 0.95  # slight P/E compression for bearish
    else:
        growth_short = 1 + (earnings_growth or revenue_growth or 0.05)
        growth_medium = growth_short ** 1.5
        growth_long = growth_short ** 3.0
        pe_adjustment = 1.0

    # When we have actual current_price, use it as the base for predictions
    # (eps * pe may diverge significantly from actual price for A-shares)
    if current_price and current_price > 0:
        short_term_price = round(current_price * growth_short * pe_adjustment, 2)
        medium_term_price = round(current_price * growth_medium * pe_adjustment, 2)
        long_term_price = round(current_price * growth_long * pe_adjustment, 2)
    else:
        target_pe = pe_ratio * pe_adjustment
        short_term_price = round(eps * growth_short * target_pe, 2)
        medium_term_price = round(eps * growth_medium * target_pe, 2)
        long_term_price = round(eps * growth_long * target_pe, 2)

    # Buy/sell targets: 10% discount / 15% premium from current implied
    target_buy_price = round(current_implied * 0.90, 2)
    target_sell_price = round(current_implied * 1.15, 2)

    return {
        "short_term_price": short_term_price,
        "medium_term_price": medium_term_price,
        "long_term_price": long_term_price,
        "target_buy_price": target_buy_price,
        "target_sell_price": target_sell_price,
    }
