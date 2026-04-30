from langchain_core.messages import HumanMessage
from src.graph.state import AgentState, show_agent_reasoning
from src.utils.progress import progress
import pandas as pd
import numpy as np
import json
from datetime import datetime, timedelta
from src.utils.api_key import get_api_key_from_state
from src.tools.api import get_insider_trades, get_company_news, get_prices


##### Sentiment Agent #####
def sentiment_analyst_agent(state: AgentState, agent_id: str = "sentiment_analyst_agent"):
    """Analyzes market sentiment and generates trading signals for multiple tickers."""
    data = state.get("data", {})
    end_date = data.get("end_date")
    tickers = data.get("tickers")
    api_key = get_api_key_from_state(state, "FINANCIAL_DATASETS_API_KEY")
    # Initialize sentiment analysis for each ticker
    sentiment_analysis = {}

    for ticker in tickers:
        progress.update_status(agent_id, ticker, "Fetching insider trades")

        # Get the insider trades
        insider_trades = get_insider_trades(
            ticker=ticker,
            end_date=end_date,
            limit=1000,
            api_key=api_key,
        )

        progress.update_status(agent_id, ticker, "Analyzing trading patterns")

        # Get the signals from the insider trades
        transaction_shares = pd.Series([t.transaction_shares for t in insider_trades]).dropna()
        insider_signals = np.where(transaction_shares < 0, "bearish", "bullish").tolist()

        progress.update_status(agent_id, ticker, "Fetching company news")

        # Get the company news
        company_news = get_company_news(ticker, end_date, limit=100, api_key=api_key)

        # Fetch current price for per-share price estimation
        progress.update_status(agent_id, ticker, "Fetching current price")
        start_price_date = (datetime.fromisoformat(end_date) - timedelta(days=30)).date().isoformat()
        prices_list = get_prices(ticker, start_price_date, end_date, api_key=api_key, adjust="")
        current_price = prices_list[-1].close if prices_list else None

        # Get the sentiment from the company news
        sentiment = pd.Series([n.sentiment for n in company_news]).dropna()
        news_signals = np.where(sentiment == "negative", "bearish", 
                              np.where(sentiment == "positive", "bullish", "neutral")).tolist()
        
        progress.update_status(agent_id, ticker, "Combining signals")
        # Combine signals from both sources with weights
        insider_weight = 0.3
        news_weight = 0.7
        
        # Calculate weighted signal counts
        bullish_signals = (
            insider_signals.count("bullish") * insider_weight +
            news_signals.count("bullish") * news_weight
        )
        bearish_signals = (
            insider_signals.count("bearish") * insider_weight +
            news_signals.count("bearish") * news_weight
        )

        if bullish_signals > bearish_signals:
            overall_signal = "bullish"
        elif bearish_signals > bullish_signals:
            overall_signal = "bearish"
        else:
            overall_signal = "neutral"

        # Calculate confidence level based on the weighted proportion
        total_weighted_signals = len(insider_signals) * insider_weight + len(news_signals) * news_weight
        confidence = 0  # Default confidence when there are no signals
        if total_weighted_signals > 0:
            confidence = round((max(bullish_signals, bearish_signals) / total_weighted_signals) * 100, 2)
        
        # Create structured reasoning similar to technical analysis
        reasoning = {
            "insider_trading": {
                "signal": "bullish" if insider_signals.count("bullish") > insider_signals.count("bearish") else 
                         "bearish" if insider_signals.count("bearish") > insider_signals.count("bullish") else "neutral",
                "confidence": round((max(insider_signals.count("bullish"), insider_signals.count("bearish")) / max(len(insider_signals), 1)) * 100),
                "metrics": {
                    "total_trades": len(insider_signals),
                    "bullish_trades": insider_signals.count("bullish"),
                    "bearish_trades": insider_signals.count("bearish"),
                    "weight": insider_weight,
                    "weighted_bullish": round(insider_signals.count("bullish") * insider_weight, 1),
                    "weighted_bearish": round(insider_signals.count("bearish") * insider_weight, 1),
                }
            },
            "news_sentiment": {
                "signal": "bullish" if news_signals.count("bullish") > news_signals.count("bearish") else 
                         "bearish" if news_signals.count("bearish") > news_signals.count("bullish") else "neutral",
                "confidence": round((max(news_signals.count("bullish"), news_signals.count("bearish")) / max(len(news_signals), 1)) * 100),
                "metrics": {
                    "total_articles": len(news_signals),
                    "bullish_articles": news_signals.count("bullish"),
                    "bearish_articles": news_signals.count("bearish"),
                    "neutral_articles": news_signals.count("neutral"),
                    "weight": news_weight,
                    "weighted_bullish": round(news_signals.count("bullish") * news_weight, 1),
                    "weighted_bearish": round(news_signals.count("bearish") * news_weight, 1),
                }
            },
            "combined_analysis": {
                "total_weighted_bullish": round(bullish_signals, 1),
                "total_weighted_bearish": round(bearish_signals, 1),
                "signal_determination": f"{'Bullish' if bullish_signals > bearish_signals else 'Bearish' if bearish_signals > bullish_signals else 'Neutral'} based on weighted signal comparison"
            }
        }

        # Create sentiment analysis with price predictions based on signal
        if current_price and current_price > 0:
            if overall_signal == "bullish":
                short_term_price = round(current_price * 1.03, 2)
                medium_term_price = round(current_price * 1.07, 2)
                long_term_price = round(current_price * 1.12, 2)
                target_buy_price = round(current_price * 0.95, 2)
                target_sell_price = round(current_price * 1.15, 2)
            elif overall_signal == "bearish":
                short_term_price = round(current_price * 0.97, 2)
                medium_term_price = round(current_price * 0.93, 2)
                long_term_price = round(current_price * 0.88, 2)
                target_buy_price = round(current_price * 0.85, 2)
                target_sell_price = round(current_price * 1.02, 2)
            else:
                short_term_price = round(current_price, 2)
                medium_term_price = round(current_price * 1.02, 2)
                long_term_price = round(current_price * 1.05, 2)
                target_buy_price = round(current_price * 0.90, 2)
                target_sell_price = round(current_price * 1.10, 2)
        else:
            short_term_price = None
            medium_term_price = None
            long_term_price = None
            target_buy_price = None
            target_sell_price = None

        sentiment_analysis[ticker] = {
            "signal": overall_signal,
            "confidence": confidence,
            "short_term_price": short_term_price,
            "medium_term_price": medium_term_price,
            "long_term_price": long_term_price,
            "target_buy_price": target_buy_price,
            "target_sell_price": target_sell_price,
            "reasoning": reasoning,
        }

        progress.update_status(agent_id, ticker, "Done", analysis=json.dumps(reasoning, indent=4))

    # Create the sentiment message
    message = HumanMessage(
        content=json.dumps(sentiment_analysis),
        name=agent_id,
    )

    # Print the reasoning if the flag is set
    if state["metadata"]["show_reasoning"]:
        show_agent_reasoning(sentiment_analysis, "Sentiment Analysis Agent")

    progress.update_status(agent_id, None, "Done")

    return {
        "messages": [message],
        "data": data,
        "analyst_signals": {agent_id: sentiment_analysis},
    }
