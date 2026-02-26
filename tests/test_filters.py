"""Tests for core.filter_injector — mandatory data quality filters."""

from core.filter_injector import inject_filters


# --- ProSt filters ---

class TestProStBareQuery:
    """A bare SELECT on ProSt should get all 3 mandatory filters."""

    def test_adds_migration_batch_filter(self):
        sql = "SELECT * FROM ProSt;"
        result = inject_filters(sql, "ProSt")
        assert "Created_Date != '2025-04-21'" in result

    def test_adds_pic_garbage_filter(self):
        sql = "SELECT * FROM ProSt;"
        result = inject_filters(sql, "ProSt")
        assert "PIC NOT IN ('XXX','NONE','66','25','64')" in result

    def test_adds_pic_not_null_filter(self):
        sql = "SELECT * FROM ProSt;"
        result = inject_filters(sql, "ProSt")
        assert "PIC IS NOT NULL" in result


class TestProStNoDoubling:
    """Templates already have filters — injector must not duplicate them."""

    TEMPLATE_SQL = (
        "SELECT Project_Code, Project_Title, PIC, Project_Status,\n"
        "       DATEDIFF(DAY, Created_Date, GETDATE()) AS DaysActive\n"
        "FROM ProSt\n"
        "WHERE Project_Status NOT IN ('Plant','Lost','Adjusted','Held','NON PREFERRED')\n"
        "  AND Created_Date != '2025-04-21'\n"
        "  AND PIC IS NOT NULL\n"
        "  AND PIC NOT IN ('XXX','NONE','66','25','64')\n"
        "ORDER BY DaysActive DESC;"
    )

    def test_returns_unchanged(self):
        result = inject_filters(self.TEMPLATE_SQL, "ProSt")
        assert result == self.TEMPLATE_SQL

    def test_no_duplicate_migration_filter(self):
        result = inject_filters(self.TEMPLATE_SQL, "ProSt")
        count = result.count("2025-04-21")
        assert count == 1


class TestProStPartialFilters:
    """Query has some filters but not all — only missing ones added."""

    def test_adds_only_missing_pic_null_check(self):
        sql = (
            "SELECT * FROM ProSt\n"
            "WHERE Created_Date != '2025-04-21'\n"
            "  AND PIC NOT IN ('XXX','NONE','66','25','64')\n"
            "ORDER BY Project_Code;"
        )
        result = inject_filters(sql, "ProSt")
        assert "PIC IS NOT NULL" in result
        assert result.count("2025-04-21") == 1
        assert result.count("PIC NOT IN") == 1


# --- AMC_MASTER filters ---

class TestAmcBareQuery:
    """A bare SELECT on AMC_MASTER should get status filters."""

    def test_adds_status_not_null(self):
        sql = "SELECT * FROM AMC_MASTER;"
        result = inject_filters(sql, "AMC_MASTER")
        assert "Status IS NOT NULL" in result

    def test_adds_status_not_empty(self):
        sql = "SELECT * FROM AMC_MASTER;"
        result = inject_filters(sql, "AMC_MASTER")
        assert "Status != ''" in result


class TestAmcNoDoubling:
    """AMC template already has filters — must not duplicate."""

    TEMPLATE_SQL = (
        "SELECT Status, COUNT(*) AS Count, SUM(TotalAmount) AS TotalValue\n"
        "FROM AMC_MASTER\n"
        "WHERE Status IS NOT NULL AND Status != ''\n"
        "GROUP BY Status\n"
        "ORDER BY Count DESC;"
    )

    def test_returns_unchanged(self):
        result = inject_filters(self.TEMPLATE_SQL, "AMC_MASTER")
        assert result == self.TEMPLATE_SQL


# --- Tables without mandatory filters ---

class TestUnaffectedTables:
    """Tables not in the filter map should pass through untouched."""

    def test_invoice_details_unchanged(self):
        sql = "SELECT * FROM INVOICE_DETAILS WHERE Line_Status = 'Invoiced';"
        result = inject_filters(sql, "INVOICE_DETAILS")
        assert result == sql

    def test_operations_unchanged(self):
        sql = "SELECT * FROM OPERATIONS WHERE Status IS NOT NULL;"
        result = inject_filters(sql, "OPERATIONS")
        assert result == sql

    def test_empty_sql_unchanged(self):
        assert inject_filters("", "ProSt") == ""

    def test_empty_table_unchanged(self):
        sql = "SELECT 1;"
        assert inject_filters(sql, "") == sql


# --- Case insensitivity ---

class TestCaseInsensitive:
    """Table name matching should be case-insensitive."""

    def test_lowercase_prost(self):
        sql = "SELECT * FROM ProSt;"
        result = inject_filters(sql, "prost")
        assert "Created_Date != '2025-04-21'" in result

    def test_uppercase_amc(self):
        sql = "SELECT * FROM AMC_MASTER;"
        result = inject_filters(sql, "amc_master")
        assert "Status IS NOT NULL" in result
