"""
Ground truth integration tests — runs every intent through query_engine.run()
against the live database and validates results with range checks.

Usage:  python3 tests/test_queries.py
"""

import json
import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.query_engine import run

GROUND_TRUTH_PATH = os.path.join(os.path.dirname(__file__), "ground_truth.json")


def load_ground_truth():
    with open(GROUND_TRUTH_PATH) as f:
        return json.load(f)


def _check_row_count(result, spec, errors):
    """Validate row count against min/max/exact expectations."""
    rows = result["rows"]
    n = len(rows)

    if "exact_rows" in spec:
        expected = spec["exact_rows"]
        if n != expected:
            errors.append(f"Expected exactly {expected} rows, got {n}")

    if "min_rows" in spec:
        if n < spec["min_rows"]:
            errors.append(f"Expected >= {spec['min_rows']} rows, got {n}")

    if "max_rows" in spec:
        if n > spec["max_rows"]:
            errors.append(f"Expected <= {spec['max_rows']} rows, got {n}")


def _check_spot_checks(result, spec, errors):
    """Validate specific value/count pairs in result rows."""
    checks = spec.get("spot_checks", {})
    rows = result["rows"]

    for label, check in checks.items():
        field = check["field"]
        count_field = check["count_field"]
        lo, hi = check["min"], check["max"]

        matched = [r for r in rows if r.get(field) == label]
        if not matched:
            errors.append(f"Spot check '{label}': no row with {field}='{label}'")
            continue

        val = matched[0].get(count_field)
        if val is None:
            errors.append(f"Spot check '{label}': {count_field} is None")
        elif not (lo <= val <= hi):
            errors.append(f"Spot check '{label}': {count_field}={val}, expected {lo}-{hi}")


def _check_top_field(result, spec, errors):
    """Validate that the top row's field meets a minimum."""
    field = spec.get("top_field")
    top_min = spec.get("top_min")
    if not field or top_min is None:
        return

    rows = result["rows"]
    if not rows:
        errors.append(f"No rows to check top {field}")
        return

    val = rows[0].get(field)
    if val is None:
        errors.append(f"Top row {field} is None")
    elif val < top_min:
        errors.append(f"Top {field}={val}, expected >= {top_min}")


def _check_required_columns(result, spec, errors):
    """Verify required columns exist in the first row."""
    cols = spec.get("required_columns", [])
    rows = result["rows"]
    if not rows:
        if cols:
            errors.append(f"No rows — cannot verify columns: {cols}")
        return

    first = rows[0]
    for col in cols:
        if col not in first:
            errors.append(f"Missing required column: '{col}'. Got: {list(first.keys())}")


def _check_field_ranges(result, spec, errors):
    """Validate numeric fields fall within expected ranges."""
    ranges = spec.get("field_ranges", {})
    rows = result["rows"]
    if not rows:
        if ranges:
            errors.append("No rows — cannot verify field ranges")
        return

    first = rows[0]
    for field, bounds in ranges.items():
        val = first.get(field)
        if val is None:
            errors.append(f"Field '{field}' is None")
            continue
        if not (bounds["min"] <= val <= bounds["max"]):
            errors.append(f"'{field}'={val}, expected {bounds['min']}-{bounds['max']}")


def _check_positive_fields(result, spec, errors):
    """Verify fields are > 0."""
    fields = spec.get("positive_fields", [])
    rows = result["rows"]
    if not rows:
        return

    first = rows[0]
    for f in fields:
        val = first.get(f)
        if val is None or val <= 0:
            errors.append(f"'{f}'={val}, expected > 0")


def _check_non_negative_fields(result, spec, errors):
    """Verify fields are >= 0."""
    fields = spec.get("non_negative_fields", [])
    rows = result["rows"]
    if not rows:
        return

    first = rows[0]
    for f in fields:
        val = first.get(f)
        if val is None or val < 0:
            errors.append(f"'{f}'={val}, expected >= 0")


def _check_banned_pic_values(result, spec, errors):
    """Verify no row contains a banned PIC value."""
    banned = spec.get("banned_pic_values", [])
    if not banned:
        return

    for row in result["rows"]:
        pic = row.get("PIC")
        if pic in banned:
            errors.append(f"Found banned PIC='{pic}' in results")
            break


def _check_banned_status_values(result, spec, errors):
    """Verify no row contains a banned Status value."""
    banned = spec.get("banned_status_values", [])
    if not banned:
        return

    for row in result["rows"]:
        status = row.get("Status")
        if status in banned:
            errors.append(f"Found banned Status='{status}' in results")
            break


def _check_all_field_equals(result, spec, errors):
    """Verify every row has a specific field value."""
    checks = spec.get("all_field_equals", {})
    if not checks:
        return

    for field, expected in checks.items():
        for i, row in enumerate(result["rows"]):
            val = row.get(field)
            if val != expected:
                errors.append(f"Row {i}: {field}='{val}', expected '{expected}'")
                break


def _check_must_contain_bucket(result, spec, errors):
    """Verify a specific aging bucket label exists in results."""
    bucket = spec.get("must_contain_bucket")
    if not bucket:
        return

    found = any(
        bucket in str(v) for row in result["rows"] for v in row.values()
    )
    if not found:
        errors.append(f"Expected bucket '{bucket}' not found in results")


def _check_days_remaining_range(result, spec, errors):
    """Verify all rows' DaysRemaining fall within a range."""
    dr = spec.get("days_remaining_range")
    if not dr:
        return

    field = dr["field"]
    lo, hi = dr["min"], dr["max"]
    for i, row in enumerate(result["rows"]):
        val = row.get(field)
        if val is None:
            continue
        if not (lo <= val <= hi):
            errors.append(f"Row {i}: {field}={val}, expected {lo}-{hi}")
            break


def _check_redirect(result, spec, errors):
    """Validate retired intent redirect."""
    expected_from = spec.get("expect_redirect_from")
    expected_to = spec.get("expect_redirect_to")

    if expected_from:
        if result.get("redirected_from") != expected_from:
            errors.append(
                f"redirected_from='{result.get('redirected_from')}', "
                f"expected '{expected_from}'"
            )
    if expected_to:
        if result.get("intent_name") != expected_to:
            errors.append(
                f"intent_name='{result.get('intent_name')}', "
                f"expected '{expected_to}'"
            )


def _check_fallback(result, spec, errors):
    """Validate unknown intent returns fallback."""
    if spec.get("expect_fallback"):
        if not result.get("fallback"):
            errors.append("Expected fallback=True, got False/missing")
        if not result.get("suggestions"):
            errors.append("Expected suggestions list in fallback")


def run_test(name, spec):
    """Run a single ground truth test. Returns (passed, errors)."""
    intent_input = spec["input"]
    errors = []

    try:
        result = run(intent_input)
    except Exception as e:
        return False, [f"EXCEPTION: {e}"]

    # Check for query engine errors (unless we expect fallback/redirect-only)
    if result.get("error") and not spec.get("expect_fallback"):
        # Redirect tests still execute a query, so error matters
        if not spec.get("expect_redirect_from"):
            errors.append(f"Query error: {result['error']}")

    # Run all applicable checks
    _check_redirect(result, spec, errors)
    _check_fallback(result, spec, errors)

    # Skip data checks for fallback tests
    if spec.get("expect_fallback"):
        return len(errors) == 0, errors

    _check_row_count(result, spec, errors)
    _check_spot_checks(result, spec, errors)
    _check_top_field(result, spec, errors)
    _check_required_columns(result, spec, errors)
    _check_field_ranges(result, spec, errors)
    _check_positive_fields(result, spec, errors)
    _check_non_negative_fields(result, spec, errors)
    _check_banned_pic_values(result, spec, errors)
    _check_banned_status_values(result, spec, errors)
    _check_all_field_equals(result, spec, errors)
    _check_must_contain_bucket(result, spec, errors)
    _check_days_remaining_range(result, spec, errors)

    return len(errors) == 0, errors


def main():
    ground_truth = load_ground_truth()
    passed = 0
    failed = 0

    print(f"\n{'='*60}")
    print(f"  OptiFlow Ground Truth Tests — {len(ground_truth)} tests")
    print(f"{'='*60}\n")

    for name, spec in ground_truth.items():
        ok, errors = run_test(name, spec)

        if ok:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}")
            for e in errors:
                print(f"        -> {e}")

    print(f"\n{'='*60}")
    print(f"  Results: {passed} passed, {failed} failed, {passed + failed} total")
    print(f"{'='*60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
