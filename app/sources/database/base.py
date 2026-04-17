"""
DatabaseSource — shared base class for all SQL database sources.

Provides:
  - Semantic metadata extraction (column role, table type, grain, relationships)
  - Schema file I/O — writes enriched .md files per table + relationships.md
  - Table description derivation (rule-based, no AI)
  - Shared properties (name, source_type, description, get_database_name)

Each subclass (MSSQLSource, PostgreSQLSource, MySQLSource) implements:
  - connect(server, database, user, password) → (conn, driver, error)
  - execute_query(sql) → list[dict]  (async)
  - discover_schema(conn, db_name, server) → dict
  - get_system_prompt_section() → str  (dialect-specific SQL rules)
  - get_db_type() → str
  - validate_credentials() → dict
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Table description helpers ─────────────────────────────────────────────────

_TABLE_NAME_OVERRIDES: dict = {}

_DESCRIPTION_PATTERNS = [
    (re.compile(r"project",                re.I), "Project records and tracking"),
    (re.compile(r"customer|client",        re.I), "Customer and client data"),
    (re.compile(r"order|invoice",          re.I), "Order and invoice records"),
    (re.compile(r"product|item|sku",       re.I), "Product and item catalogue"),
    (re.compile(r"employee|staff",         re.I), "Employee and staff records"),
    (re.compile(r"sales|revenue",          re.I), "Sales and revenue data"),
    (re.compile(r"target|budget|forecast", re.I), "Target, budget, and forecast data"),
    (re.compile(r"master",                 re.I), "Master reference data"),
    (re.compile(r"lookup|ref(?:erence)?",  re.I), "Reference and lookup table"),
    (re.compile(r"log|audit|history|event",re.I), "Log and audit records"),
    (re.compile(r"report|summary",         re.I), "Report and summary data"),
    (re.compile(r"payment|finance|account",re.I), "Financial and payment records"),
    (re.compile(r"status|state|stage",     re.I), "Status and stage tracking"),
    (re.compile(r"categor|type|class",     re.I), "Category and classification data"),
    (re.compile(r"contact|address|location",re.I),"Contact and address information"),
    (re.compile(r"notif|alert",            re.I), "Notifications and alerts"),
    (re.compile(r"user|admin|role|permission",re.I),"User and access management"),
]

_KEY_COL_SKIP_TYPES = frozenset({
    "image", "varbinary", "binary", "text", "ntext", "xml",
    "geography", "geometry",
})
_KEY_COL_SKIP_WORDS = (
    "attach", "file", "photo", "image", "blob", "thumb", "icon", "logo",
    "content", "body", "remark", "comment",
)
_KEY_COL_SCORE = {
    "id": 5, "code": 5, "no": 5, "num": 5, "number": 5,
    "name": 4, "title": 4,
    "status": 3, "type": 3, "stage": 3, "state": 3,
    "date": 3, "time": 3, "year": 2, "month": 2,
    "amount": 3, "total": 3, "value": 3, "price": 3, "qty": 2, "count": 2,
    "customer": 3, "client": 3, "project": 3, "item": 2, "product": 2,
    "user": 2, "owner": 2, "manager": 2, "pic": 2,
}


def _derive_table_description(table_name: str, columns: list) -> str:
    if table_name in _TABLE_NAME_OVERRIDES:
        return _TABLE_NAME_OVERRIDES[table_name]
    for pattern, description in _DESCRIPTION_PATTERNS:
        if pattern.search(table_name):
            return description
    col_names_lower = [c["name"].lower() for c in columns]
    if any("project" in n or "proj" in n for n in col_names_lower):
        return "Project-related records"
    if any("customer" in n or "client" in n for n in col_names_lower):
        return "Customer-related records"
    if any("invoice" in n or "order" in n for n in col_names_lower):
        return "Order or invoice records"
    return "Business data table"


def _score_key_column(col_name: str, col_type: str) -> int:
    base_type = col_type.split("(")[0].lower()
    if base_type in _KEY_COL_SKIP_TYPES:
        return -1
    col_lower = col_name.lower()
    if any(skip in col_lower for skip in _KEY_COL_SKIP_WORDS):
        return -1
    score = 0
    for part in re.split(r"[_\s]", col_lower):
        score += _KEY_COL_SCORE.get(part, 0)
    for keyword, pts in _KEY_COL_SCORE.items():
        if pts >= 3 and keyword in col_lower:
            score = max(score, pts)
    return score


def _select_key_columns(columns: list, max_cols: int = 5) -> list:
    scored = [(s, col["name"]) for col in columns
              if (s := _score_key_column(col["name"], col["type"])) >= 0]
    scored.sort(key=lambda x: -x[0])
    result = [name for score, name in scored if score > 0][:max_cols]
    if not result and scored:
        result = [scored[0][1]]
    return result


# ── Semantic classification ───────────────────────────────────────────────────

_DATE_TYPES    = frozenset({"datetime", "datetime2", "date", "time", "smalldatetime", "timestamp"})
_MONEY_TYPES   = frozenset({"decimal", "numeric", "money", "smallmoney", "float", "real"})
_INT_TYPES     = frozenset({"int", "bigint", "smallint", "tinyint"})
_STRING_TYPES  = frozenset({"varchar", "nvarchar", "char", "nchar"})
_SKIP_TYPES    = frozenset({"image", "varbinary", "binary", "text", "ntext", "xml", "geography", "geometry"})

_DATE_RE    = re.compile(r"date$|time$|month$|year$|_at$|_on$|created|updated|modified|inserted", re.I)
_MEASURE_RE = re.compile(r"amount|total|qty|quantity|price|cost|rate|sum|balance|target|achieved|backlog|revenue|value|fee|tax|gst|discount|score|percent|pct|weight", re.I)
_STATUS_RE  = re.compile(r"status|state|_type$|category|level|stage|phase|mode|flag$|indicator|class$|kind$|group$", re.I)
_ID_RE      = re.compile(r"_id$|_pk$|_fk$|_code$|_no$|_number$|_num$|_key$|_ref$|^id$|^pk_|^fk_", re.I)
_TEXT_RE    = re.compile(r"name$|title$|description|_desc$|_text$|note$|comment$|address|email|phone|mobile|subject|message$|remark$|detail$|_info$|label$", re.I)


def _classify_column_role(col_name: str, col_type: str, cardinality: int, row_count: int) -> str:
    """Classify a column's semantic role using heuristics on name, type, and cardinality."""
    base_type = col_type.split("(")[0].lower()

    if base_type in _SKIP_TYPES:
        return "other"

    # date_column: datetime types or name patterns
    if base_type in _DATE_TYPES:
        return "date_column"
    if _DATE_RE.search(col_name):
        return "date_column"

    # identifier: ID/code/PK/FK name patterns
    if _ID_RE.search(col_name):
        return "identifier"

    # measure: money types OR numeric with financial keywords
    if base_type in _MONEY_TYPES:
        return "measure"
    if base_type in _INT_TYPES and _MEASURE_RE.search(col_name):
        return "measure"

    # status: string + low cardinality (≤ 20 distinct) + status-indicating name
    if base_type in _STRING_TYPES and 0 < cardinality <= 20 and _STATUS_RE.search(col_name):
        return "status"

    # name/text: string + descriptive name keywords
    if base_type in _STRING_TYPES and _TEXT_RE.search(col_name):
        return "name_text"

    # dimension: string + low-medium cardinality (≤ 50 distinct)
    if base_type in _STRING_TYPES and 0 < cardinality <= 50:
        return "dimension"

    return "other"


def _classify_table_type(
    table_name: str,
    columns: list,
    row_count: int,
    pk_columns: list,
    all_relationships: list,
) -> str:
    """Classify a table as transaction, reference, junction, reporting, or configuration."""
    name_lower = table_name.lower()

    # Configuration: very low row count + settings/config name
    if row_count < 50 and any(w in name_lower for w in ("setting", "config", "param")):
        return "configuration"

    # Junction: composite PK of 2+ columns
    if len(pk_columns) >= 2:
        return "junction"

    col_roles = [col.get("role", "other") for col in columns]
    has_dates   = "date_column" in col_roles
    has_measures = "measure" in col_roles

    # Is this table referenced by other tables (i.e., it is the "one" / reference side)
    is_referenced = any(r["to_table"] == table_name for r in all_relationships)

    # Reporting/target tables
    if any(w in name_lower for w in ("target", "budget", "forecast", "summary", "report")):
        return "reporting"

    # Transaction: has both dates and measures
    if has_dates and has_measures:
        return "transaction"

    # Reference/master: referenced by others, or has master/ref/lookup in name, or only dimensions
    if is_referenced or any(w in name_lower for w in ("master", "lookup", "dict")):
        return "reference"

    return "transaction" if has_dates else "reference"


def _detect_grain(table_name: str, pk_columns: list, table_type: str) -> str:
    """Generate a plain-English description of what one row represents."""
    name_lower = table_name.lower()

    # Use table name keywords for well-known patterns
    if "detail" in name_lower:
        subject = re.sub(r"_?details?$", "", table_name, flags=re.I).strip("_")
        return f"One row = one {subject} line item"
    if "master" in name_lower:
        subject = re.sub(r"_?master$", "", table_name, flags=re.I).strip("_")
        return f"One row = one {subject} record"
    if any(w in name_lower for w in ("log", "audit", "history")):
        return "One row = one event or log entry"
    if "target" in name_lower:
        return "One row = one periodic target/goal entry"

    if len(pk_columns) > 1:
        return f"One row = one combination of ({', '.join(pk_columns)})"
    if pk_columns:
        return f"One row = one {table_name} record (identified by {pk_columns[0]})"
    return f"One row = one {table_name} record"


def _infer_relationships(tables_data: list, confirmed_fks: list) -> list:
    """
    Infer FK-style relationships from column name matching across tables.

    Rules:
    - Find column names that appear in 2+ tables
    - Skip very generic names (id, name, status, type, date, etc.)
    - If a column is a PK in one table → that table is the reference (one) side
    - Otherwise use cardinality ratio (cardinality / row_count) to determine direction
    - Returns list of {from_table, from_column, to_table, to_column, confidence: "inferred"}
    """
    _GENERIC_SKIP = frozenset({
        "id", "name", "status", "type", "code", "no", "date",
        "createddate", "updateddate", "createdat", "updatedat",
        "created_date", "updated_date", "created_at", "updated_at",
        "description", "notes", "remarks", "active", "enabled",
        "sortorder", "sort_order", "displayorder",
    })

    confirmed_set = {
        (r["from_table"].lower(), r["from_column"].lower(),
         r["to_table"].lower(), r["to_column"].lower())
        for r in confirmed_fks
    }

    # Build per-column occurrence map: col_name → list of {table, row_count, cardinality, is_pk}
    col_map: dict = {}
    pk_map: dict  = {}   # table_name → set of pk column names (lowercased)

    for t in tables_data:
        pk_set = {c.lower() for c in t.get("pk_columns", [])}
        pk_map[t["name"].lower()] = pk_set
        for col in t["columns"]:
            cname = col["name"]
            ckey  = cname.lower()
            col_map.setdefault(ckey, []).append({
                "table":       t["name"],
                "col_exact":   cname,
                "row_count":   t["row_count"],
                "cardinality": col.get("cardinality", -1),
                "is_pk":       ckey in pk_set,
            })

    inferred: list  = []
    seen: set       = set()

    for ckey, occurrences in col_map.items():
        if len(occurrences) < 2:
            continue
        if ckey in _GENERIC_SKIP:
            continue

        for i in range(len(occurrences)):
            for j in range(i + 1, len(occurrences)):
                a, b = occurrences[i], occurrences[j]

                # Both PKs → ambiguous, skip
                if a["is_pk"] and b["is_pk"]:
                    continue

                # Determine ref (one) side vs fk (many) side
                if a["is_pk"] and not b["is_pk"]:
                    ref, fk = a, b
                elif b["is_pk"] and not a["is_pk"]:
                    ref, fk = b, a
                else:
                    # Use cardinality ratio: higher ratio → more unique → reference side
                    def _ratio(x):
                        if x["row_count"] > 0 and x["cardinality"] > 0:
                            return x["cardinality"] / x["row_count"]
                        return -1.0
                    ra, rb = _ratio(a), _ratio(b)
                    if ra > rb and ra > 0.7:
                        ref, fk = a, b
                    elif rb > ra and rb > 0.7:
                        ref, fk = b, a
                    else:
                        continue  # can't determine direction reliably

                key = (fk["table"].lower(), ckey, ref["table"].lower(), ckey)
                if key in confirmed_set or key in seen:
                    continue
                seen.add(key)
                inferred.append({
                    "from_table":  fk["table"],
                    "from_column": fk["col_exact"],
                    "to_table":    ref["table"],
                    "to_column":   ref["col_exact"],
                    "confidence":  "inferred",
                })

    return inferred


def enrich_tables_data(tables_data: list, pk_fk_data: dict) -> list:
    """
    Enrich raw tables_data with semantic metadata:
      - pk_columns: list of PK column names per table
      - per-column cardinality (from categorical dict or explicit cardinality field)
      - per-column role classification
      - confirmed FK references on each column

    pk_fk_data = {
        "pk_map":  {table_name: [col_name, ...]},
        "fk_list": [{from_table, from_column, to_table, to_column}, ...]
    }
    """
    pk_map  = pk_fk_data.get("pk_map", {})
    fk_list = pk_fk_data.get("fk_list", [])

    # Build confirmed FK lookup: (from_table, from_column) → (to_table, to_column)
    fk_lookup: dict = {}
    for fk in fk_list:
        key = (fk["from_table"], fk["from_column"])
        fk_lookup[key] = {"to_table": fk["to_table"], "to_column": fk["to_column"]}

    # First pass: assign PK columns + column roles
    for t in tables_data:
        tname = t["name"]
        t["pk_columns"]   = pk_map.get(tname, [])
        t["confirmed_fks"] = [
            fk for fk in fk_list if fk["from_table"] == tname
        ]

        categorical = t.get("categorical", {})
        row_count   = t.get("row_count", 0)

        for col in t["columns"]:
            cname = col["name"]
            # Cardinality: use categorical count if available, else explicit field
            if cname in categorical:
                col["cardinality"] = len(categorical[cname])
            elif "cardinality" not in col:
                col["cardinality"] = -1

            # FK reference info
            fk_ref = fk_lookup.get((tname, cname))
            if fk_ref:
                col["fk_ref"] = fk_ref
                col["fk_confidence"] = "confirmed"

            # Classify role
            col["role"] = _classify_column_role(
                cname, col["type"], col["cardinality"], row_count
            )

    # Second pass: compute all relationships (confirmed + inferred) — needed for table type
    all_relationships = (
        [{"from_table": f["from_table"], "from_column": f["from_column"],
          "to_table": f["to_table"],    "to_column": f["to_column"],
          "confidence": "confirmed"}
         for f in fk_list]
        + _infer_relationships(tables_data, fk_list)
    )

    # Third pass: classify table type + grain (need all_relationships)
    for t in tables_data:
        t["table_type"] = _classify_table_type(
            t["name"], t["columns"], t["row_count"],
            t["pk_columns"], all_relationships
        )
        t["grain"] = _detect_grain(t["name"], t["pk_columns"], t["table_type"])
        # Attach relevant relationships for this table
        t["relationships"] = [
            r for r in all_relationships
            if r["from_table"] == t["name"] or r["to_table"] == t["name"]
        ]

    return tables_data


# ── Schema file writers ───────────────────────────────────────────────────────

def write_schema_index(
    tables_data: list,
    schema_dir: Path,
    source_name: str = "",
    db_type: str = "",
) -> None:
    """Write schema_index.md — markdown table of all tables."""
    heading  = source_name or schema_dir.name
    db_label = db_type.upper() if db_type else "SQL"
    lines = [
        f"# {heading} ({db_label})",
        "",
        "| Table | Type | Description | Rows |",
        "|-------|------|-------------|------|",
    ]
    for t in tables_data:
        desc      = _derive_table_description(t["name"], t["columns"])
        ttype     = t.get("table_type", "")
        type_label = ttype.capitalize() if ttype else ""
        lines.append(f"| {t['name']} | {type_label} | {desc} | {t['row_count']:,} |")
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "schema_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"Schema index written: {len(tables_data)} tables → {schema_dir}/schema_index.md")


_ROLE_LABELS = {
    "identifier":  "identifier",
    "measure":     "measure",
    "date_column": "date",
    "status":      "status",
    "name_text":   "name/text",
    "dimension":   "dimension",
    "other":       "",
}


def write_table_file(table: dict, tables_dir: Path) -> None:
    """Write an enriched per-table .md file with semantic metadata."""
    tables_dir.mkdir(parents=True, exist_ok=True)
    safe_name    = re.sub(r"[^\w\-]", "_", table["name"])
    path         = tables_dir / f"{safe_name}.md"
    categorical  = table.get("categorical", {})
    pk_columns   = table.get("pk_columns", [])
    table_type   = table.get("table_type", "")
    grain        = table.get("grain", "")
    relationships = table.get("relationships", [])

    lines = [f"# {table['name']}", ""]
    if table_type:
        lines.append(f"**Type**: {table_type.capitalize()} table")
    if grain:
        lines.append(f"**Grain**: {grain}")
    lines.append(f"**Row count**: {table['row_count']:,}")
    if pk_columns:
        lines.append(f"**Primary key**: {', '.join(pk_columns)}")
    lines += ["", "## Columns", "",
              "| Column | Type | Role | Nullable | Sample Values |",
              "|--------|------|------|----------|---------------|"]

    for col in table["columns"]:
        cname    = col["name"]
        null_str = "NULL" if col.get("nullable") else "NOT NULL"
        role     = _ROLE_LABELS.get(col.get("role", "other"), "")

        # Build sample values string
        samples = ""
        if cname in categorical:
            vals = categorical[cname]
            if vals:
                samples = ", ".join(
                    f'"{v}"' if isinstance(v, str) else str(v)
                    for v in vals[:6]
                )

        # Append FK annotation to type if confirmed
        fk_ref = col.get("fk_ref")
        type_str = col["type"]
        if fk_ref:
            type_str += f" → {fk_ref['to_table']}.{fk_ref['to_column']}"

        lines.append(f"| {cname} | {type_str} | {role} | {null_str} | {samples} |")

    # Relationships section
    outgoing = [r for r in relationships if r["from_table"] == table["name"]]
    incoming = [r for r in relationships if r["to_table"] == table["name"]]

    if outgoing or incoming:
        lines += ["", "## Relationships"]
        for r in outgoing:
            tag = "" if r["confidence"] == "confirmed" else " (inferred)"
            lines.append(f"- **{r['from_column']}** → {r['to_table']}.{r['to_column']}{tag}")
        for r in incoming:
            tag = "" if r["confidence"] == "confirmed" else " (inferred)"
            lines.append(f"- **{r['from_column']}** ← {r['from_table']}.{r['from_column']}{tag}")

    # Categorical sample values section
    status_cols = {
        c["name"] for c in table["columns"]
        if c.get("role") in ("status", "dimension") and c["name"] in categorical
    }
    if status_cols:
        lines += ["", "## Categorical values"]
        for cname in sorted(status_cols):
            vals = categorical.get(cname, [])
            if vals:
                formatted = ", ".join(f'"{v}"' for v in vals)
                lines.append(f"- **{cname}**: {formatted}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_relationships_file(
    schema_dir: Path,
    confirmed_fks: list,
    inferred_rels: list,
    tables_data: list,
) -> None:
    """Write relationships.md — source-level map of all table relationships."""
    source_name = schema_dir.name
    lines = [f"# Relationships: {source_name}", ""]

    # Confirmed FK constraints
    lines += ["## Confirmed (from database constraints)", ""]
    if confirmed_fks:
        for r in confirmed_fks:
            lines.append(
                f"- {r['from_table']}.{r['from_column']} → "
                f"{r['to_table']}.{r['to_column']}"
            )
    else:
        lines.append("_(No explicit FK constraints found in this database)_")
    lines.append("")

    # Inferred relationships
    lines += ["## Inferred (from column name matching)", ""]
    if inferred_rels:
        for r in inferred_rels:
            lines.append(
                f"- {r['from_table']}.{r['from_column']} → "
                f"{r['to_table']}.{r['to_column']}"
            )
    else:
        lines.append("_(No inferred relationships detected)_")
    lines.append("")

    # Join paths — derive useful multi-hop paths
    all_rels = confirmed_fks + inferred_rels
    join_paths = _derive_join_paths(all_rels, tables_data)
    if join_paths:
        lines += ["## Common join paths", ""]
        for path_str in join_paths:
            lines.append(f"- {path_str}")

    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "relationships.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"Relationships file written → {schema_dir}/relationships.md")


def _derive_join_paths(all_rels: list, tables_data: list) -> list:
    """Derive readable multi-hop join path descriptions."""
    paths = []
    # Build adjacency: table → [(other_table, via_column)]
    adj: dict = {}
    for r in all_rels:
        adj.setdefault(r["from_table"], []).append(
            (r["to_table"], r["from_column"], r["to_column"])
        )

    # Find transaction tables (likely starting points for analysis)
    tx_tables = {
        t["name"] for t in tables_data
        if t.get("table_type") in ("transaction", "reporting")
    }
    ref_tables = {
        t["name"] for t in tables_data
        if t.get("table_type") == "reference"
    }

    seen_paths: set = set()
    for tx in sorted(tx_tables):
        for to_table, from_col, to_col in adj.get(tx, []):
            path_str = f"{tx}.{from_col} → {to_table}.{to_col}"
            if path_str not in seen_paths:
                seen_paths.add(path_str)
                paths.append(path_str)
            # One more hop
            for to2, fc2, tc2 in adj.get(to_table, []):
                path2 = f"{tx} → {to_table} → {to2} (via {from_col}/{fc2})"
                if path2 not in seen_paths:
                    seen_paths.add(path2)
                    paths.append(path2)

    return paths[:30]  # cap at 30 entries


# ── DatabaseSource base ───────────────────────────────────────────────────────

class DatabaseSource:
    """Base class for all SQL database sources."""

    def __init__(self, name: str, config: dict):
        self._name   = name
        self._config = config
        from app.config import SOURCES_DATA_DIR
        self._schema_dir = SOURCES_DATA_DIR / name   # data/sources/{name}/

        # ── In-memory schema cache ────────────────────────────────────────────
        # Populated by load_cache() at startup and after schema rediscovery.
        # Zero disk reads at query time once warmed.
        self._cache_index:         str            = ""       # schema_index.md
        self._cache_relationships: Optional[str]  = None     # relationships.md
        self._cache_tables:        dict[str, str] = {}       # {stem_lower: content}
        self._cache_loaded:        bool           = False

    # ── DataSource protocol properties ────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    @property
    def source_type(self) -> str:
        return self._config.get("type", "unknown")

    @property
    def description(self) -> str:
        return self._config.get(
            "description",
            f"{self.source_type.upper()} database '{self.get_database_name()}'"
        )

    # ── Schema cache management ───────────────────────────────────────────────

    def load_cache(self) -> None:
        """
        Read all schema files into memory.  Call once at startup and again
        after every schema rediscovery so tools never touch disk at query time.
        """
        # schema_index
        self._cache_index = ""
        for fname in ("schema_index.md", "schema_index.txt"):
            p = self._schema_dir / fname
            try:
                self._cache_index = p.read_text(encoding="utf-8")
                break
            except FileNotFoundError:
                continue
            except Exception as exc:
                logger.warning(f"[{self._name}] cache: read {fname} failed: {exc}")

        # relationships
        self._cache_relationships = None
        rel_path = self._schema_dir / "relationships.md"
        try:
            self._cache_relationships = rel_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning(f"[{self._name}] cache: read relationships.md failed: {exc}")

        # per-table files
        self._cache_tables = {}
        tables_dir = self._schema_dir / "tables"
        if tables_dir.is_dir():
            for p in tables_dir.iterdir():
                if p.suffix in (".md", ".txt"):
                    try:
                        self._cache_tables[p.stem.lower()] = p.read_text(encoding="utf-8")
                    except Exception as exc:
                        logger.warning(f"[{self._name}] cache: read {p.name} failed: {exc}")

        self._cache_loaded = True
        logger.info(
            f"[{self._name}] Schema cache loaded: "
            f"{len(self._cache_tables)} table file(s), "
            f"index={'yes' if self._cache_index else 'no'}, "
            f"relationships={'yes' if self._cache_relationships else 'no'}"
        )

    def invalidate_cache(self) -> None:
        """Clear the in-memory cache (called before rediscovery writes new files)."""
        self._cache_index         = ""
        self._cache_relationships = None
        self._cache_tables        = {}
        self._cache_loaded        = False

    # ── Schema file reads (cache-first) ───────────────────────────────────────

    def get_table_index(self) -> str:
        """Return schema_index content from cache; fall back to disk if not loaded."""
        if self._cache_loaded:
            return self._cache_index
        # Fallback: warm cache on the fly
        self.load_cache()
        return self._cache_index

    def get_compact_index(self) -> str:
        return self.get_table_index()

    def get_table_detail(self, table_name: str) -> Optional[str]:
        """Return per-table schema from cache; fall back to disk if not loaded."""
        if self._cache_loaded:
            return self._cache_tables.get(table_name.lower())

        # Fallback: warm cache, then retry
        self.load_cache()
        return self._cache_tables.get(table_name.lower())

    def get_relationships(self) -> Optional[str]:
        """Return relationships.md from cache; fall back to disk if not loaded."""
        if self._cache_loaded:
            return self._cache_relationships

        self.load_cache()
        return self._cache_relationships

    def get_available_tables(self) -> list[str]:
        if self._cache_loaded:
            return sorted(self._cache_tables.keys())
        tables_dir = self._schema_dir / "tables"
        if not tables_dir.is_dir():
            return []
        stems = {p.stem for p in tables_dir.glob("*.md")}
        stems |= {p.stem for p in tables_dir.glob("*.txt")}
        return sorted(stems)

    def schema_discovered(self) -> bool:
        if self._cache_loaded:
            return bool(self._cache_index)
        return (
            (self._schema_dir / "schema_index.md").exists()
            or (self._schema_dir / "schema_index.txt").exists()
        )

    # ── Subclass must implement ───────────────────────────────────────────────

    def get_database_name(self) -> str:
        return self._config.get("credentials", {}).get("database", self._name)

    def get_db_type(self) -> str:
        return self.source_type

    def get_system_prompt_section(self) -> str:
        raise NotImplementedError

    async def execute_query(self, sql: str) -> list[dict]:
        raise NotImplementedError

    def discover_schema(self, conn, db_name: str, server: str) -> dict:
        raise NotImplementedError

    def validate_credentials(self) -> dict:
        raise NotImplementedError
