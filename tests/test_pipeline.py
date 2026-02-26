"""Tests for core.query_engine — intent routing, param binding, retired handling."""

from unittest.mock import patch, MagicMock
from core.query_engine import run, _bind_params, SUGGESTED_QUESTIONS


# --- Fallback ---

class TestFallback:
    def test_missing_intent_key(self):
        result = run({})
        assert result["fallback"] is True
        assert result["message"] == "I don't understand that question"
        assert len(result["suggestions"]) == 5

    def test_unknown_intent(self):
        result = run({"intent": "totally_made_up"})
        assert result["fallback"] is True
        assert result["suggestions"] == SUGGESTED_QUESTIONS


# --- Retired intents redirect ---

class TestRetiredRedirect:
    @patch("core.query_engine.execute_query")
    def test_projects_overdue_redirects_to_by_age(self, mock_exec):
        mock_exec.return_value = [{"Project_Code": "P001", "DaysActive": 100}]
        result = run({"intent": "projects_overdue"})
        assert result["redirected_from"] == "projects_overdue"
        assert result["intent_name"] == "projects_by_age"
        assert result["error"] is None

    @patch("core.query_engine.execute_query")
    def test_ops_overdue_redirects_to_ops_active(self, mock_exec):
        mock_exec.return_value = [{"Project_Code": "OP1", "DaysActive": 50}]
        result = run({"intent": "ops_overdue"})
        assert result["redirected_from"] == "ops_overdue"
        assert result["intent_name"] == "ops_active"


# --- Parameter binding ---

class TestParamBinding:
    def test_like_placeholder_becomes_parameterized(self):
        sql = "SELECT * FROM AMC_MASTER WHERE CustomerName LIKE '%[CUSTOMER_NAME]%'"
        bound, values, used = _bind_params(sql, {"CUSTOMER_NAME": "Hanil"}, {"CUSTOMER_NAME": ""})
        assert "?" in bound
        assert "[CUSTOMER_NAME]" not in bound
        assert values == ("%Hanil%",)
        assert "%" not in bound or bound.count("%") == 0  # wildcards moved into value

    def test_exact_placeholder_becomes_parameterized(self):
        sql = "SELECT * FROM TICKET_DETAILS WHERE Assigned_To = '[PERSON_NAME]'"
        bound, values, used = _bind_params(sql, {"PERSON_NAME": "Sidharth"}, {"PERSON_NAME": ""})
        assert "?" in bound
        assert "[PERSON_NAME]" not in bound
        assert values == ("Sidharth",)

    def test_defaults_used_when_param_missing(self):
        sql = "SELECT * FROM T WHERE col = '[PARAM]'"
        bound, values, used = _bind_params(sql, {}, {"PARAM": "default_val"})
        assert values == ("default_val",)

    def test_lowercase_key_lookup(self):
        sql = "SELECT * FROM T WHERE col = '[PERSON_NAME]'"
        bound, values, used = _bind_params(sql, {"person_name": "Srini"}, {"PERSON_NAME": ""})
        assert values == ("Srini",)

    def test_no_placeholders_returns_unchanged(self):
        sql = "SELECT COUNT(*) FROM ProSt WHERE Created_Date != '2025-04-21';"
        bound, values, used = _bind_params(sql, {}, {})
        assert bound == sql
        assert values == ()

    def test_ops_dual_placeholders(self):
        sql = (
            "SELECT * FROM OPERATIONS o\n"
            "LEFT JOIN CLIENT_MASTER c ON c.client_Code = o.Customer_Code\n"
            "WHERE o.Customer_Code = '[CUSTOMER_CODE]'\n"
            "  OR c.client_Name LIKE '%[CUSTOMER_NAME]%'"
        )
        bound, values, used = _bind_params(
            sql,
            {"CUSTOMER_CODE": "IGS", "CUSTOMER_NAME": "Inalfa"},
            {"CUSTOMER_CODE": "", "CUSTOMER_NAME": ""},
        )
        assert bound.count("?") == 2
        assert "[CUSTOMER_CODE]" not in bound
        assert "[CUSTOMER_NAME]" not in bound
        assert values[0] == "IGS"
        assert values[1] == "%Inalfa%"


# --- Caveats always included ---

class TestCaveats:
    @patch("core.query_engine.execute_query")
    def test_caveats_in_result(self, mock_exec):
        mock_exec.return_value = []
        result = run({"intent": "projects_by_stage"})
        assert isinstance(result["caveats"], list)
        assert len(result["caveats"]) > 0

    @patch("core.query_engine.execute_query")
    def test_redirected_uses_target_caveats(self, mock_exec):
        mock_exec.return_value = []
        result = run({"intent": "projects_overdue"})
        # Should have caveats from projects_by_age, not projects_overdue
        assert any("delivery" in c.lower() or "proxy" in c.lower() for c in result["caveats"])


# --- Empty results ---

class TestEmptyResults:
    @patch("core.query_engine.execute_query")
    def test_empty_result_returns_empty_list(self, mock_exec):
        mock_exec.return_value = []
        result = run({"intent": "amc_expiry"})
        assert result["rows"] == []
        assert result["error"] is None

    @patch("core.query_engine.execute_query")
    def test_none_result_returns_empty_list(self, mock_exec):
        mock_exec.return_value = None
        result = run({"intent": "amc_expiry"})
        assert result["rows"] == []


# --- Database error handling ---

class TestDbErrors:
    @patch("core.query_engine.execute_query")
    def test_db_exception_returns_error(self, mock_exec):
        mock_exec.side_effect = Exception("Connection timeout")
        result = run({"intent": "projects_by_stage"})
        assert result["error"] is not None
        assert "Connection timeout" in result["error"]
        assert result["rows"] == []
        assert result["intent_name"] == "projects_by_stage"
        assert len(result["caveats"]) > 0  # caveats still included


# --- Multi-statement SQL (tickets_by_person) ---

class TestMultiStatement:
    @patch("core.query_engine.execute_query")
    def test_summary_when_no_person(self, mock_exec):
        mock_exec.return_value = [{"Assigned_To": "Sales", "TotalTickets": 2}]
        result = run({"intent": "tickets_by_person"})
        # Should use summary query (GROUP BY)
        call_sql = mock_exec.call_args[0][0]
        assert "GROUP BY" in call_sql
        assert result["error"] is None

    @patch("core.query_engine.execute_query")
    def test_detail_when_person_provided(self, mock_exec):
        mock_exec.return_value = [{"Ticket_ID": "TKTGEN009", "Task_Title": "LS AUTO"}]
        result = run({"intent": "tickets_by_person", "PERSON_NAME": "Sidharth"})
        call_sql = mock_exec.call_args[0][0]
        assert "GROUP BY" not in call_sql
        assert "?" in call_sql
