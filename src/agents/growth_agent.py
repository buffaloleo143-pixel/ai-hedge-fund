from __future__ import annotations

"""Growth Agent

Implements a growth-focused valuation methodology.
"""

import json
import statistics
from langchain_core.messages import HumanMessage
from src.graph.state import AgentState, show_agent_reasoning
from src.utils.progress import progress
from src.utils.api_key import get_api_key_from_state
from src.tools.api import (
    get_financial_metrics,
    get_insider_trades,
    get_prices,
)
from datetime import datetime, timedelta

def growth_analyst_agent(state: AgentState, agent_id: str = "growth_analyst_agent"):
    """Run growth analysis across tickers and write signals back to `state`."""

    data = state["data"]
    end_date = data["end_date"]
    tickers = data["tickers"]
    api_key = get_api_key_from_state(state, "FINANCIAL_DATASETS_API_KEY")
    growth_analysis: dict[str, dict] = {}

    for ticker in tickers:
        progress.update_status(agent_id, ticker, "Fetching financial data")

        # --- Historical financial metrics ---
        financial_metrics = get_financial_metrics(
            ticker=ticker,
            end_date=end_date,
            period="ttm",
            limit=12, # 3 years of ttm data
            api_key=api_key,
        )
        if not financial_metrics or len(financial_metrics) < 4:
            progress.update_status(agent_id, ticker, "Failed: Not enough financial metrics")
            continue
        
        most_recent_metrics = financial_metrics[0]

        # Fetch current price for accurate price targets
        progress.update_status(agent_id, ticker, "Fetching current price")
        start_price_date = (datetime.fromisoformat(end_date) - timedelta(days=30)).date().isoformat()
        prices = get_prices(ticker, start_price_date, end_date, api_key=api_key, adjust="")
        current_price = prices[-1].close if prices else None

        # --- Insider Trades ---
        insider_trades = get_insider_trades(
            ticker=ticker,
            end_date=end_date,
            limit=1000,
            api_key=api_key
        )

        # ------------------------------------------------------------------
        # Tool Implementation
        # ------------------------------------------------------------------
        
        # 1. Historical Growth Analysis
        growth_trends = analyze_growth_trends(financial_metrics)
        
        # 2. Growth-Oriented Valuation
        valuation_metrics = analyze_valuation(most_recent_metrics)
        
        # 3. Margin Expansion Monitor
        margin_trends = analyze_margin_trends(financial_metrics)
        
        # 4. Insider Conviction Tracker
        insider_conviction = analyze_insider_conviction(insider_trades)
        
        # 5. Financial Health Check
        financial_health = check_financial_health(most_recent_metrics)

        # ------------------------------------------------------------------
        # Aggregate & signal
        # ------------------------------------------------------------------
        scores = {
            "growth": growth_trends['score'],
            "valuation": valuation_metrics['score'],
            "margins": margin_trends['score'],
            "insider": insider_conviction['score'],
            "health": financial_health['score']
        }
        
        weights = {
            "growth": 0.40,
            "valuation": 0.25,
            "margins": 0.15,
            "insider": 0.10,
            "health": 0.10
        }

        weighted_score = sum(scores[key] * weights[key] for key in scores)
        
        if weighted_score > 0.6:
            signal = "bullish"
        elif weighted_score < 0.4:
            signal = "bearish"
        else:
            signal = "neutral"
            
        confidence = round(abs(weighted_score - 0.5) * 2 * 100)

        reasoning = {
            "historical_growth": growth_trends,
            "growth_valuation": valuation_metrics,
            "margin_expansion": margin_trends,
            "insider_conviction": insider_conviction,
            "financial_health": financial_health,
            "final_analysis": {
                "signal": signal,
                "confidence": confidence,
                "weighted_score": round(weighted_score, 2)
            }
        }

        # Calculate price targets based on growth metrics
        price_targets = calculate_growth_price_targets(most_recent_metrics, growth_trends, signal, current_price)

        growth_analysis[ticker] = {
            "signal": signal,
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

    # ---- Emit message (for LLM tool chain) ----
    msg = HumanMessage(content=json.dumps(growth_analysis), name=agent_id)
    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(growth_analysis, "Growth Analysis Agent")

    # Add the signal to the analyst_signals list
    progress.update_status(agent_id, None, "Done")
    
    return {"messages": [msg], "data": data, "analyst_signals": {agent_id: growth_analysis}}

#############################
# Helper Functions
#############################

def _calculate_trend(data: list[float | None]) -> float:
    """Calculates the slope of the trend line for the given data."""
    clean_data = [d for d in data if d is not None]
    if len(clean_data) < 2:
        return 0.0
    
    y = clean_data
    x = list(range(len(y)))
    
    try:
        # Simple linear regression
        sum_x = sum(x)
        sum_y = sum(y)
        sum_xy = sum(i * j for i, j in zip(x, y))
        sum_x2 = sum(i**2 for i in x)
        n = len(y)
        
        slope = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x**2)
        return slope
    except ZeroDivisionError:
        return 0.0

def analyze_growth_trends(metrics: list) -> dict:
    """Analyzes historical growth trends.
    
    增长率取值优先级：
    1. revenue_growth（年报YoY，数据层已做fallback：AKShare原始值 -> 衍生年报YoY -> 季报同比）
    2. revenue_growth_quarterly（季报同比，作为补充fallback）
    3. None（数据不可用）
    """
    
    # 构建营收增长率序列：优先用 revenue_growth，fallback 到 revenue_growth_quarterly
    rev_growth = [m.revenue_growth or m.revenue_growth_quarterly for m in metrics]
    eps_growth = [m.earnings_per_share_growth for m in metrics]
    fcf_growth = [m.free_cash_flow_growth for m in metrics]

    rev_trend = _calculate_trend(rev_growth)
    eps_trend = _calculate_trend(eps_growth)
    fcf_trend = _calculate_trend(fcf_growth)

    # Score based on recent growth and trend
    score = 0
    
    # Revenue
    if rev_growth[0] is not None:
        if rev_growth[0] > 0.20:
            score += 0.4
        elif rev_growth[0] > 0.10:
            score += 0.2
        if rev_trend > 0:
            score += 0.1 # Accelerating
            
    # EPS
    if eps_growth[0] is not None:
        if eps_growth[0] > 0.20:
            score += 0.25
        elif eps_growth[0] > 0.10:
            score += 0.1
        if eps_trend > 0:
            score += 0.05
    
    # FCF
    if fcf_growth[0] is not None:
        if fcf_growth[0] > 0.15:
            score += 0.1
            
    score = min(score, 1.0)

    # 补充3年CAGR数据（来自最新一期 metrics）
    latest = metrics[0] if metrics else None
    revenue_cagr_3y = getattr(latest, 'revenue_cagr_3y', None) if latest else None
    earnings_cagr_3y = getattr(latest, 'earnings_cagr_3y', None) if latest else None

    return {
        "score": score,
        "revenue_growth": rev_growth[0],
        "revenue_trend": rev_trend,
        "eps_growth": eps_growth[0],
        "eps_trend": eps_trend,
        "fcf_growth": fcf_growth[0],
        "fcf_trend": fcf_trend,
        "revenue_cagr_3y": revenue_cagr_3y,
        "earnings_cagr_3y": earnings_cagr_3y,
        "data_caliber": "基于最新年报数据，季报同比作为补充参考",
    }

def analyze_valuation(metrics) -> dict:
    """Analyzes valuation from a growth perspective."""
    
    peg_ratio = metrics.peg_ratio
    ps_ratio = metrics.price_to_sales_ratio
    
    score = 0
    
    # PEG Ratio
    if peg_ratio is not None:
        if peg_ratio < 1.0:
            score += 0.5
        elif peg_ratio < 2.0:
            score += 0.25
            
    # Price to Sales Ratio
    if ps_ratio is not None:
        if ps_ratio < 2.0:
            score += 0.5
        elif ps_ratio < 5.0:
            score += 0.25
            
    score = min(score, 1.0)
    
    return {
        "score": score,
        "peg_ratio": peg_ratio,
        "price_to_sales_ratio": ps_ratio
    }

def analyze_margin_trends(metrics: list) -> dict:
    """Analyzes historical margin trends."""
    
    gross_margins = [m.gross_margin for m in metrics]
    operating_margins = [m.operating_margin for m in metrics]
    net_margins = [m.net_margin for m in metrics]

    gm_trend = _calculate_trend(gross_margins)
    om_trend = _calculate_trend(operating_margins)
    nm_trend = _calculate_trend(net_margins)
    
    score = 0
    
    # Gross Margin
    if gross_margins[0] is not None:
        if gross_margins[0] > 0.5: # Healthy margin
            score += 0.2
        if gm_trend > 0: # Expanding
            score += 0.2

    # Operating Margin
    if operating_margins[0] is not None:
        if operating_margins[0] > 0.15: # Healthy margin
            score += 0.2
        if om_trend > 0: # Expanding
            score += 0.2
            
    # Net Margin Trend
    if nm_trend > 0:
        score += 0.2
        
    score = min(score, 1.0)
    
    return {
        "score": score,
        "gross_margin": gross_margins[0],
        "gross_margin_trend": gm_trend,
        "operating_margin": operating_margins[0],
        "operating_margin_trend": om_trend,
        "net_margin": net_margins[0],
        "net_margin_trend": nm_trend
    }

def analyze_insider_conviction(trades: list) -> dict:
    """Analyzes insider trading activity."""
    
    buys = sum(t.transaction_value for t in trades if t.transaction_value and t.transaction_shares > 0)
    sells = sum(abs(t.transaction_value) for t in trades if t.transaction_value and t.transaction_shares < 0)
    
    if (buys + sells) == 0:
        net_flow_ratio = 0
    else:
        net_flow_ratio = (buys - sells) / (buys + sells)
    
    score = 0
    if net_flow_ratio > 0.5:
        score = 1.0
    elif net_flow_ratio > 0.1:
        score = 0.7
    elif net_flow_ratio > -0.1:
        score = 0.5 # Neutral
    else:
        score = 0.2
        
    return {
        "score": score,
        "net_flow_ratio": net_flow_ratio,
        "buys": buys,
        "sells": sells
    }

def check_financial_health(metrics) -> dict:
    """Checks the company's financial health."""
    
    debt_to_equity = metrics.debt_to_equity
    current_ratio = metrics.current_ratio
    
    score = 1.0
    
    # Debt to Equity
    if debt_to_equity is not None:
        if debt_to_equity > 1.5:
            score -= 0.5
        elif debt_to_equity > 0.8:
            score -= 0.2
            
    # Current Ratio
    if current_ratio is not None:
        if current_ratio < 1.0:
            score -= 0.5
        elif current_ratio < 1.5:
            score -= 0.2
            
    score = max(score, 0.0)
    
    return {
        "score": score,
        "debt_to_equity": debt_to_equity,
        "current_ratio": current_ratio
    }


def calculate_growth_price_targets(metrics, growth_trends: dict, signal: str, current_price: float = None) -> dict:
    """
    Estimate price targets based on growth-oriented valuation.
    Uses PEG ratio and price-to-sales combined with revenue/EPS growth rates.
    """
    eps = getattr(metrics, "earnings_per_share", None)
    pe_ratio = getattr(metrics, "price_to_earnings_ratio", None)
    ps_ratio = getattr(metrics, "price_to_sales_ratio", None)
    revenue_per_share = None

    # Try to estimate revenue per share from P/S and EPS
    if ps_ratio and pe_ratio and eps and eps > 0 and pe_ratio > 0:
        implied_price = eps * pe_ratio
        revenue_per_share = implied_price / ps_ratio if ps_ratio > 0 else None

    # 增长率取值优先级：revenue_growth（年报YoY，已fallback）-> revenue_cagr_3y -> 季报同比 -> 默认10%
    rev_growth = growth_trends.get("revenue_growth") or growth_trends.get("revenue_cagr_3y") or 0.10
    eps_growth = growth_trends.get("eps_growth") or rev_growth

    if not eps or not pe_ratio or eps <= 0 or pe_ratio <= 0:
        return {
            "short_term_price": None,
            "medium_term_price": None,
            "long_term_price": None,
            "target_buy_price": None,
            "target_sell_price": None,
        }

    current_implied = eps * pe_ratio
    # Use actual current price as baseline if available and reasonable
    if current_price and current_price > 0:
        current_implied = current_price

    # Growth-adjusted price targets
    if signal == "bullish":
        peg_pe = pe_ratio * (1 + eps_growth * 0.5)  # P/E expansion with growth
    elif signal == "bearish":
        peg_pe = pe_ratio * (1 - abs(eps_growth) * 0.3)
    else:
        peg_pe = pe_ratio

    # When we have actual current_price, use it as the base for predictions
    # (eps * pe may diverge significantly from actual price for A-shares)
    if current_price and current_price > 0:
        growth_adj = (1 + eps_growth)
        short_term_price = round(current_price * growth_adj * (peg_pe / pe_ratio), 2)
        medium_term_price = round(current_price * growth_adj ** 2 * (peg_pe / pe_ratio), 2)
        long_term_price = round(current_price * growth_adj ** 3 * (peg_pe / pe_ratio), 2)
    else:
        short_term_price = round(eps * (1 + eps_growth) * peg_pe, 2)
        medium_term_price = round(eps * (1 + eps_growth) ** 2 * peg_pe, 2)
        long_term_price = round(eps * (1 + eps_growth) ** 3 * peg_pe, 2)

    # Buy at a discount, sell at premium
    target_buy_price = round(current_implied * 0.88, 2)
    target_sell_price = round(current_implied * 1.15, 2)

    return {
        "short_term_price": short_term_price,
        "medium_term_price": medium_term_price,
        "long_term_price": long_term_price,
        "target_buy_price": target_buy_price,
        "target_sell_price": target_sell_price,
    }
