"""
edges/short_circuits.py
------------------------
Conditional edge functions that bypass LLM calls when the number of
available options makes the choice trivial.

These are used as conditional edge selectors in the item sub-graph.
Each returns the name of the NEXT node to execute, allowing LangGraph
to route around macro_category_node or sub_category_node when appropriate.

Note: the actual short-circuit logic lives inside the nodes themselves
(they check option counts and return immediately without calling the LLM).
These functions exist as a routing layer that could be extended in the
future without modifying node code.
"""
from src.agents.data_ingestion_agent.state import ItemState


def should_run_macro_category(state: ItemState) -> str:
    """
    Decide whether to call the macro-category LLM node.

    Returns the next node name:
      - 'macro_category_node' if multiple macro-categories exist.
      - 'sub_category_node' to skip directly if only one (or zero) macro-cat is available.
    """
    available_macros = list(state.categories.keys())
    if len(available_macros) <= 1:
        return "sub_category_node"
    return "macro_category_node"


def should_run_sub_category(state: ItemState) -> str:
    """
    Decide whether to call the sub-category LLM node.

    Returns the next node name:
      - 'sub_category_node' if the resolved macro-category has multiple subs.
      - 'fixed_fields_node' to skip directly if only one (or zero) sub-category exists.
    """
    macro = state.macro_category
    if not macro or macro not in state.categories:
        return "fixed_fields_node"
    available_subs = list(state.categories[macro].sub_categories.keys())
    if len(available_subs) <= 1:
        return "fixed_fields_node"
    return "sub_category_node"
