"""Tests for the 3-category universal intent classification system.

Categories: business_health | deep_dive | agent | unknown
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from intents import INTENT_CATEGORIES


# ---------------------------------------------------------------------------
# intents/__init__.py
# ---------------------------------------------------------------------------

class TestIntentCategories:
    def test_four_categories_defined(self):
        assert len(INTENT_CATEGORIES) == 4

    def test_all_expected_categories_present(self):
        assert "business_health" in INTENT_CATEGORIES
        assert "deep_dive" in INTENT_CATEGORIES
        assert "agent" in INTENT_CATEGORIES
        assert "unknown" in INTENT_CATEGORIES

    def test_no_bizflow_specific_categories(self):
        for cat in INTENT_CATEGORIES:
            assert cat not in (
                "amc_expiry", "projects_stuck", "invoice_aging",
                "monthly_target", "ops_active", "finance_summary",
            ), f"BizFlow-specific category {cat!r} should not be in INTENT_CATEGORIES"


# ---------------------------------------------------------------------------
# core/intent_parser.py — mocked Claude API
# ---------------------------------------------------------------------------

def _mock_response(json_payload: dict):
    """Build a mock anthropic Message with given JSON as text content."""
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(json_payload))]
    return msg


@patch("core.intent_parser._client")
class TestParseBusinessHealth:
    def test_basic_health_question(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "business_health", "match_confidence": "high"}
        )
        from core.intent_parser import parse
        result = parse("How's the business doing today?")
        assert result["intent"] == "business_health"
        assert result["match_confidence"] == "high"

    def test_dashboard_overview(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "business_health", "match_confidence": "high"}
        )
        from core.intent_parser import parse
        result = parse("Give me a dashboard overview")
        assert result["intent"] == "business_health"

    def test_daily_digest(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "business_health", "match_confidence": "high"}
        )
        from core.intent_parser import parse
        result = parse("daily digest")
        assert result["intent"] == "business_health"

    def test_medium_confidence_preserved(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "business_health", "match_confidence": "medium"}
        )
        from core.intent_parser import parse
        result = parse("How are things going?")
        assert result["match_confidence"] == "medium"


@patch("core.intent_parser._client")
class TestParseDeepDive:
    def test_project_deep_dive(self, mock_client):
        mock_client.messages.create.return_value = _mock_response({
            "intent": "deep_dive",
            "entity_type": "project",
            "entity_label": "P-2024-001",
            "match_confidence": "high",
        })
        from core.intent_parser import parse
        result = parse("Tell me everything about project P-2024-001")
        assert result["intent"] == "deep_dive"
        assert result["entity_type"] == "project"
        assert result["entity_label"] == "P-2024-001"
        assert result["match_confidence"] == "high"

    def test_customer_deep_dive(self, mock_client):
        mock_client.messages.create.return_value = _mock_response({
            "intent": "deep_dive",
            "entity_type": "customer",
            "entity_label": "Acme Corp",
            "match_confidence": "high",
        })
        from core.intent_parser import parse
        result = parse("Deep dive on customer Acme Corp")
        assert result["intent"] == "deep_dive"
        assert result["entity_label"] == "Acme Corp"

    def test_order_deep_dive(self, mock_client):
        mock_client.messages.create.return_value = _mock_response({
            "intent": "deep_dive",
            "entity_type": "order",
            "entity_label": "#12345",
            "match_confidence": "high",
        })
        from core.intent_parser import parse
        result = parse("Full summary of order #12345")
        assert result["entity_type"] == "order"
        assert result["entity_label"] == "#12345"

    def test_employee_deep_dive(self, mock_client):
        mock_client.messages.create.return_value = _mock_response({
            "intent": "deep_dive",
            "entity_type": "employee",
            "entity_label": "John Smith",
            "match_confidence": "high",
        })
        from core.intent_parser import parse
        result = parse("Everything about employee John Smith")
        assert result["intent"] == "deep_dive"
        assert result["entity_label"] == "John Smith"


@patch("core.intent_parser._client")
class TestParseAgent:
    def test_overdue_invoices(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "agent", "match_confidence": "high"}
        )
        from core.intent_parser import parse
        result = parse("Which invoices are overdue?")
        assert result["intent"] == "agent"
        assert result["match_confidence"] == "high"

    def test_sales_by_region(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "agent", "match_confidence": "high"}
        )
        from core.intent_parser import parse
        result = parse("Show me sales by region this quarter")
        assert result["intent"] == "agent"

    def test_customer_count(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "agent", "match_confidence": "high"}
        )
        from core.intent_parser import parse
        result = parse("How many customers placed orders last month?")
        assert result["intent"] == "agent"

    def test_top_selling_product(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "agent", "match_confidence": "high"}
        )
        from core.intent_parser import parse
        result = parse("What's our top-selling product?")
        assert result["intent"] == "agent"

    def test_agent_is_default_for_specific_questions(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "agent", "match_confidence": "high"}
        )
        from core.intent_parser import parse
        result = parse("Who owes us money?")
        assert result["intent"] == "agent"


@patch("core.intent_parser._client")
class TestParseUnknown:
    def test_empty_string_returns_unknown_immediately(self, mock_client):
        from core.intent_parser import parse
        result = parse("")
        # Should short-circuit without calling the API
        mock_client.messages.create.assert_not_called()
        assert result["intent"] == "unknown"
        assert result["error"] == "empty_question"
        assert result["match_confidence"] == "low"

    def test_whitespace_only_returns_unknown(self, mock_client):
        from core.intent_parser import parse
        result = parse("   ")
        mock_client.messages.create.assert_not_called()
        assert result["intent"] == "unknown"

    def test_nonsensical_input(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "unknown", "original": "asdfgh", "match_confidence": "low"}
        )
        from core.intent_parser import parse
        result = parse("asdfgh")
        assert result["intent"] == "unknown"
        assert result["match_confidence"] == "low"


@patch("core.intent_parser._client")
class TestParseErrorHandling:
    def test_api_failure_returns_unknown(self, mock_client):
        mock_client.messages.create.side_effect = Exception("Connection timeout")
        from core.intent_parser import parse
        result = parse("How's the business?")
        assert result["intent"] == "unknown"
        assert result["error"] == "api_failed"
        assert result["match_confidence"] == "low"

    def test_non_json_response_returns_unknown(self, mock_client):
        msg = MagicMock()
        msg.content = [MagicMock(text="Sorry, I can't answer that.")]
        mock_client.messages.create.return_value = msg
        from core.intent_parser import parse
        result = parse("What is the meaning of life?")
        assert result["intent"] == "unknown"
        assert result["error"] == "parse_failed"

    def test_json_array_response_returns_unknown(self, mock_client):
        msg = MagicMock()
        msg.content = [MagicMock(text='["business_health"]')]
        mock_client.messages.create.return_value = msg
        from core.intent_parser import parse
        result = parse("How's the business?")
        assert result["intent"] == "unknown"
        assert result["error"] == "parse_failed"

    def test_code_fence_stripped(self, mock_client):
        msg = MagicMock()
        msg.content = [MagicMock(
            text='```json\n{"intent": "agent", "match_confidence": "high"}\n```'
        )]
        mock_client.messages.create.return_value = msg
        from core.intent_parser import parse
        result = parse("List all customers")
        assert result["intent"] == "agent"

    def test_missing_confidence_gets_defaulted_non_unknown(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "agent"}  # no match_confidence key
        )
        from core.intent_parser import parse
        result = parse("Show me open orders")
        # Non-unknown intent with missing confidence → defaults to "high"
        assert result["match_confidence"] == "high"

    def test_missing_confidence_defaults_low_for_unknown(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "unknown", "original": "xyz"}  # no match_confidence
        )
        from core.intent_parser import parse
        result = parse("xyz")
        assert result["match_confidence"] == "low"

    def test_invalid_confidence_value_corrected(self, mock_client):
        mock_client.messages.create.return_value = _mock_response(
            {"intent": "agent", "match_confidence": "very_high"}
        )
        from core.intent_parser import parse
        result = parse("Show me sales")
        # "very_high" is not valid → corrected to "high" (non-unknown)
        assert result["match_confidence"] == "high"


# ---------------------------------------------------------------------------
# Intent output always in INTENT_CATEGORIES
# ---------------------------------------------------------------------------

@patch("core.intent_parser._client")
class TestIntentAlwaysValid:
    @pytest.mark.parametrize("intent", ["business_health", "deep_dive", "agent", "unknown"])
    def test_returned_intent_is_always_a_known_category(self, mock_client, intent):
        payload = {"intent": intent, "match_confidence": "high"}
        if intent == "deep_dive":
            payload.update({"entity_type": "project", "entity_label": "P001"})
        mock_client.messages.create.return_value = _mock_response(payload)
        from core.intent_parser import parse
        result = parse("some question")
        assert result["intent"] in INTENT_CATEGORIES
