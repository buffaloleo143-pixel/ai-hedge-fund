from typing_extensions import Annotated, Sequence, TypedDict

import operator
from langchain_core.messages import BaseMessage


import json


def merge_dicts(a: dict[str, any], b: dict[str, any]) -> dict[str, any]:
    return {**a, **b}


def merge_analyst_signals(existing: dict[str, any], new: dict[str, any]) -> dict[str, any]:
    """Custom reducer for analyst_signals that merges dicts instead of overwriting.

    When parallel agents return signals, each returns {agent_id: signal_data}.
    This reducer merges them so all agents' signals are preserved.
    """
    if not existing and not new:
        return {}
    if not existing:
        return new.copy() if new else {}
    if not new:
        return existing.copy()
    # Each agent writes to a unique key (its agent_id), so a simple update is safe.
    # No key collision occurs because agent_ids are unique.
    merged = existing.copy()
    merged.update(new)
    return merged


# Define agent state
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]
    data: Annotated[dict[str, any], merge_dicts]
    metadata: Annotated[dict[str, any], merge_dicts]
    analyst_signals: Annotated[dict[str, any], merge_analyst_signals]


def show_agent_reasoning(output, agent_name):
    print(f"\n{'=' * 10} {agent_name.center(28)} {'=' * 10}")

    def convert_to_serializable(obj):
        if hasattr(obj, "to_dict"):  # Handle Pandas Series/DataFrame
            return obj.to_dict()
        elif hasattr(obj, "__dict__"):  # Handle custom objects
            return obj.__dict__
        elif isinstance(obj, (int, float, bool, str)):
            return obj
        elif isinstance(obj, (list, tuple)):
            return [convert_to_serializable(item) for item in obj]
        elif isinstance(obj, dict):
            return {key: convert_to_serializable(value) for key, value in obj.items()}
        else:
            return str(obj)  # Fallback to string representation

    if isinstance(output, (dict, list)):
        # Convert the output to JSON-serializable format
        serializable_output = convert_to_serializable(output)
        print(json.dumps(serializable_output, indent=2))
    else:
        try:
            # Parse the string as JSON and pretty print it
            parsed_output = json.loads(output)
            print(json.dumps(parsed_output, indent=2))
        except json.JSONDecodeError:
            # Fallback to original string if not valid JSON
            print(output)

    print("=" * 48)
