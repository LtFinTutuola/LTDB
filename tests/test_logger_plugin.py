"""
LTDB Test Logger Plugin
=======================
A pytest plugin that automatically generates a detailed Markdown log file
for every test run. It reads the output path from tests/tests.yaml and
produces a timestamped file with the naming convention:
    [YYYYMMDD_hhmmss]_tests_log.md

For each test it logs:
  - Test name (the function's docstring or qualified name)
  - Input parameters (extracted from the test source code)
  - Expected outcome (extracted from assert statements)
  - Actual outcome (PASSED / FAILED with details)

This plugin hooks into pytest's lifecycle automatically (no registration needed).
"""
import ast
import inspect
import textwrap
from datetime import datetime
from pathlib import Path

import yaml
import pytest


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _load_output_path() -> Path:
    """Read the output directory from tests/tests.yaml."""
    config_path = Path(__file__).parent / "tests.yaml"
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)
    output_dir = Path(cfg["output_path"])
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


# ---------------------------------------------------------------------------
# Source-code introspection helpers
# ---------------------------------------------------------------------------
def _get_source_lines(item) -> str:
    """Return the dedented source code of the test function."""
    try:
        source = inspect.getsource(item.obj)
        return textwrap.dedent(source)
    except (OSError, TypeError):
        return ""


def _extract_input_params(source: str) -> str:
    """
    Parse the test function's AST to extract meaningful input parameters.
    Looks for variable assignments, function call arguments, and object
    instantiations within the test body.
    """
    if not source:
        return "N/A"

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "N/A"

    params = []
    for node in ast.walk(tree):
        # Capture keyword arguments in function calls (e.g. name="Gucci")
        if isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            elif isinstance(node.func, ast.Name):
                func_name = node.func.id

            # Focus on Create schemas, model constructors, and repo methods
            interesting_funcs = {
                "create", "update", "delete", "get", "get_by_ean",
                "get_by_supplier_code", "create_transaction",
            }
            interesting_classes = {
                "BrandCreate", "CategoryCreate", "ArticleBlueprintCreate",
                "SupplierCreate", "BatchCreate", "ArticleCreate",
                "SellerCreate", "SaleCreate", "SaleLineCreate",
                "Brand", "Category", "ArticleBlueprint", "BrandCategory",
                "Supplier", "Batch", "Article", "ArticlePrice",
                "MovementReason", "ArticleMovement",
                "Seller", "Sale", "SaleLine", "StockUpdate",
            }

            if func_name in interesting_funcs or func_name in interesting_classes:
                kw_parts = []
                for kw in node.keywords:
                    try:
                        val = ast.literal_eval(kw.value)
                        kw_parts.append(f"{kw.arg}={val!r}")
                    except (ValueError, TypeError):
                        kw_parts.append(f"{kw.arg}=<dynamic>")
                for arg in node.args:
                    try:
                        val = ast.literal_eval(arg)
                        kw_parts.append(repr(val))
                    except (ValueError, TypeError):
                        pass

                if kw_parts:
                    params.append(f"`{func_name}({', '.join(kw_parts)})`")

    if not params:
        return "N/A (no explicit schema/model inputs detected)"

    return " · ".join(params)


def _extract_assertions(source: str) -> str:
    """
    Parse the test function's AST to extract all assert statements
    and render them as human-readable expected outcomes.
    """
    if not source:
        return "N/A"

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "N/A"

    assertions = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            try:
                assertion_code = ast.get_source_segment(source, node.test)
                if assertion_code:
                    assertions.append(f"`{assertion_code.strip()}`")
            except (TypeError, AttributeError):
                # Fallback: unparse the AST node
                try:
                    assertions.append(f"`{ast.unparse(node.test)}`")
                except Exception:
                    assertions.append("*(assertion could not be parsed)*")

        # Also capture pytest.raises blocks
        if isinstance(node, ast.With):
            for wi in node.items:
                if isinstance(wi.context_expr, ast.Call):
                    func = wi.context_expr.func
                    if isinstance(func, ast.Attribute) and func.attr == "raises":
                        for arg in wi.context_expr.args:
                            try:
                                exc_name = ast.unparse(arg)
                                assertions.append(f"`pytest.raises({exc_name})`")
                            except Exception:
                                assertions.append("`pytest.raises(...)`")

    if not assertions:
        return "N/A"

    return "<br>".join(assertions)


# ---------------------------------------------------------------------------
# Storage for test results (session-scoped)
# ---------------------------------------------------------------------------
_test_results: list[dict] = []


# ---------------------------------------------------------------------------
# Pytest hooks
# ---------------------------------------------------------------------------
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_makereport(item, call):
    """
    Called after each test phase (setup / call / teardown).
    We only care about the 'call' phase (the actual test execution).
    """
    if call.when != "call":
        return

    source = _get_source_lines(item)
    docstring = (item.obj.__doc__ or "").strip()
    test_name = docstring if docstring else item.nodeid

    input_params = _extract_input_params(source)
    expected = _extract_assertions(source)

    if call.excinfo is None:
        actual_outcome = "✅ **PASSED**"
    else:
        exc_repr = str(call.excinfo.getrepr(style="short"))
        # Truncate very long tracebacks
        if len(exc_repr) > 500:
            exc_repr = exc_repr[:500] + "\n... (truncated)"
        actual_outcome = f"❌ **FAILED**\n```\n{exc_repr}\n```"

    _test_results.append({
        "nodeid": item.nodeid,
        "test_name": test_name,
        "input_params": input_params,
        "expected_outcome": expected,
        "actual_outcome": actual_outcome,
    })


def pytest_sessionfinish(session, exitstatus):
    """
    Called once after the entire test session ends.
    Writes the collected results to a timestamped Markdown log file.
    """
    if not _test_results:
        return

    output_dir = _load_output_path()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = output_dir / f"{timestamp}_tests_log.md"

    passed = sum(1 for r in _test_results if "PASSED" in r["actual_outcome"])
    failed = sum(1 for r in _test_results if "FAILED" in r["actual_outcome"])
    total = len(_test_results)

    lines = [
        f"# LTDB Test Log — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"> **Total:** {total} | **Passed:** {passed} ✅ | **Failed:** {failed} ❌",
        "",
        "---",
        "",
    ]

    for i, result in enumerate(_test_results, start=1):
        status_icon = "✅" if "PASSED" in result["actual_outcome"] else "❌"
        lines.extend([
            f"## {i}. {status_icon} {result['test_name']}",
            "",
            f"**Test ID:** `{result['nodeid']}`",
            "",
            f"**Input Parameters:**<br>{result['input_params']}",
            "",
            f"**Expected Outcome:**<br>{result['expected_outcome']}",
            "",
            f"**Actual Outcome:**<br>{result['actual_outcome']}",
            "",
            "---",
            "",
        ])

    log_path.write_text("\n".join(lines), encoding="utf-8")

    # Clear for potential re-runs in the same process
    _test_results.clear()
