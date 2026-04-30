import sys

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph
from colorama import Fore, Style, init
import questionary
from src.agents.portfolio_manager import portfolio_management_agent
from src.agents.risk_manager import risk_management_agent
from src.graph.state import AgentState
from src.utils.display import print_trading_output
from src.utils.analysts import ANALYST_ORDER, get_analyst_nodes
from src.utils.progress import progress
from src.utils.visualize import save_graph_as_png
from src.cli.input import (
    parse_cli_inputs,
)

import argparse
from datetime import datetime
from dateutil.relativedelta import relativedelta
import json
import os

# Load environment variables from .env file
load_dotenv()

init(autoreset=True)


def parse_hedge_fund_response(response):
    """Parses a JSON string and returns a dictionary."""
    try:
        return json.loads(response)
    except json.JSONDecodeError as e:
        print(f"JSON decoding error: {e}\nResponse: {repr(response)}")
        return None
    except TypeError as e:
        print(f"Invalid response type (expected string, got {type(response).__name__}): {e}")
        return None
    except Exception as e:
        print(f"Unexpected error while parsing response: {e}\nResponse: {repr(response)}")
        return None


##### Run the Hedge Fund #####
def run_hedge_fund(
    tickers: list[str],
    start_date: str,
    end_date: str,
    portfolio: dict,
    show_reasoning: bool = False,
    selected_analysts: list[str] = [],
    model_name: str = "gpt-4.1",
    model_provider: str = "OpenAI",
):
    # Start progress tracking
    progress.start()

    try:
        # Build workflow (default to all analysts when none provided)
        workflow = create_workflow(selected_analysts if selected_analysts else None)
        agent = workflow.compile()

        final_state = agent.invoke(
            {
                "messages": [
                    HumanMessage(
                        content="Make trading decisions based on the provided data.",
                    )
                ],
                "data": {
                    "tickers": tickers,
                    "portfolio": portfolio,
                    "start_date": start_date,
                    "end_date": end_date,
                },
                "metadata": {
                    "show_reasoning": show_reasoning,
                    "model_name": model_name,
                    "model_provider": model_provider,
                },
                "analyst_signals": {},
            },
        )

        return {
            "decisions": parse_hedge_fund_response(final_state["messages"][-1].content),
            "analyst_signals": final_state["analyst_signals"],
        }
    finally:
        # Stop progress tracking
        progress.stop()


def start(state: AgentState):
    """Initialize the workflow with the input message."""
    return state


# Analyst layer definitions for parallel execution
LAYER1_ANALYSTS = [
    "technical_analyst",
    "fundamentals_analyst",
    "valuation_analyst",
    "growth_analyst",
    "sentiment_analyst",
    "news_sentiment_analyst",
]
LAYER2_ANALYSTS = [
    "ben_graham",
    "michael_burry",
    "peter_lynch",
    "phil_fisher",
    "aswath_damodaran",
]
LAYER3_ANALYSTS = [
    "warren_buffett",
    "charlie_munger",
    "bill_ackman",
    "cathie_wood",
    "mohnish_pabrai",
    "stanley_druckenmiller",
    "nassim_taleb",
    "rakesh_jhunjhunwala",
]


def layer_join(state: AgentState):
    """No-op join node to synchronize layer completion.
    不返回analyst_signals，避免覆盖已累积的Agent信号。
    """
    return {"messages": [], "data": {}, "metadata": {}}


def create_workflow(selected_analysts=None):
    """Create the workflow with selected analysts."""
    workflow = StateGraph(AgentState)
    workflow.add_node("start_node", start)

    # Get analyst nodes from the configuration
    analyst_nodes = get_analyst_nodes()

    # Default to all analysts if none selected
    if selected_analysts is None:
        selected_analysts = list(analyst_nodes.keys())
    selected_analysts = [k for k in selected_analysts if k in analyst_nodes]

    # Add selected analyst nodes
    for analyst_key in selected_analysts:
        node_name, node_func = analyst_nodes[analyst_key]
        workflow.add_node(node_name, node_func)

    # Always add risk and portfolio management
    workflow.add_node("risk_management_agent", risk_management_agent)
    workflow.add_node("portfolio_manager", portfolio_management_agent)

    # Check if parallel execution is disabled (fallback to sequential)
    parallel = os.getenv("PARALLEL_ANALYSTS", "true").lower() != "false"

    if not parallel:
        # Fallback to sequential execution for backwards compatibility
        for analyst_key in selected_analysts:
            node_name = analyst_nodes[analyst_key][0]
            workflow.add_edge("start_node", node_name)
            workflow.add_edge(node_name, "risk_management_agent")
    else:
        # Layered parallel execution
        layer1 = [k for k in LAYER1_ANALYSTS if k in selected_analysts]
        layer2 = [k for k in LAYER2_ANALYSTS if k in selected_analysts]
        layer3 = [k for k in LAYER3_ANALYSTS if k in selected_analysts]

        def connect_via_join(prev_nodes, next_keys, join_name):
            """Connect previous nodes to next layer via a join node."""
            if not next_keys:
                return prev_nodes
            workflow.add_node(join_name, layer_join)
            for node in prev_nodes:
                workflow.add_edge(node, join_name)
            for key in next_keys:
                workflow.add_edge(join_name, analyst_nodes[key][0])
            return [analyst_nodes[key][0] for key in next_keys]

        # Layer 1: start -> layer1 agents (parallel)
        current_nodes = ["start_node"]
        if layer1:
            for key in layer1:
                workflow.add_edge("start_node", analyst_nodes[key][0])
            current_nodes = [analyst_nodes[key][0] for key in layer1]

        # Layer 1 -> Layer 2
        if layer2:
            current_nodes = connect_via_join(current_nodes, layer2, "layer1_join")

        # Layer 2 -> Layer 3
        if layer3:
            current_nodes = connect_via_join(current_nodes, layer3, "layer2_join")

        # Last layer -> risk manager
        if current_nodes == ["start_node"]:
            # No analysts selected
            workflow.add_edge("start_node", "risk_management_agent")
        else:
            workflow.add_node("final_join", layer_join)
            for node in current_nodes:
                workflow.add_edge(node, "final_join")
            workflow.add_edge("final_join", "risk_management_agent")

    workflow.add_edge("risk_management_agent", "portfolio_manager")
    workflow.add_edge("portfolio_manager", END)

    workflow.set_entry_point("start_node")
    return workflow


if __name__ == "__main__":
    inputs = parse_cli_inputs(
        description="Run the hedge fund trading system",
        require_tickers=True,
        default_months_back=None,
        include_graph_flag=True,
        include_reasoning_flag=True,
    )

    tickers = inputs.tickers
    selected_analysts = inputs.selected_analysts

    # Construct portfolio here
    portfolio = {
        "cash": inputs.initial_cash,
        "margin_requirement": inputs.margin_requirement,
        "margin_used": 0.0,
        "positions": {
            ticker: {
                "long": 0,
                "short": 0,
                "long_cost_basis": 0.0,
                "short_cost_basis": 0.0,
                "short_margin_used": 0.0,
            }
            for ticker in tickers
        },
        "realized_gains": {
            ticker: {
                "long": 0.0,
                "short": 0.0,
            }
            for ticker in tickers
        },
    }

    result = run_hedge_fund(
        tickers=tickers,
        start_date=inputs.start_date,
        end_date=inputs.end_date,
        portfolio=portfolio,
        show_reasoning=inputs.show_reasoning,
        selected_analysts=inputs.selected_analysts,
        model_name=inputs.model_name,
        model_provider=inputs.model_provider,
    )
    print_trading_output(result)
