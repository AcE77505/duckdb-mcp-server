import os
import re
from pathlib import Path
from typing import Any

import duckdb
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("duckdb-mcp-lan-server", json_response=True)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            "Invalid identifier. Must start with a letter or underscore, "
            "followed by letters, numbers, or underscores."
        )
    return name


def _resolve_output_path(csv_path: str, output_path: str | None) -> Path:
    source = _resolve_csv_path(csv_path)
    if output_path:
        return Path(output_path).expanduser().resolve()
    return source.with_name(f"{source.stem}.dedup.csv")


def _resolve_csv_path(csv_path: str) -> Path:
    path = Path(csv_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ValueError(f"CSV file not found: {path}")
    return path


def _create_or_replace_view(
    con: duckdb.DuckDBPyConnection, table_name: str, csv_path: str, ignore_errors: bool
) -> str:
    safe_table = _safe_identifier(table_name)
    source = _resolve_csv_path(csv_path)
    con.execute(
        f"""
        CREATE OR REPLACE VIEW "{safe_table}" AS
        SELECT *
        FROM read_csv_auto(?, sample_size=-1, ignore_errors=?)
        """,
        [str(source), ignore_errors],
    )
    return safe_table


def _list_columns(con: duckdb.DuckDBPyConnection, table_name: str) -> set[str]:
    rows = con.execute(f'DESCRIBE SELECT * FROM "{table_name}"').fetchall()
    return {r[0] for r in rows}


def _safe_order_by(order_by: str | None, allowed_columns: set[str], fallback_expr: str) -> str:
    if not order_by:
        return fallback_expr

    clauses: list[str] = []
    for raw_clause in order_by.split(","):
        raw_clause = raw_clause.strip()
        if not raw_clause:
            continue

        parts = raw_clause.split()
        if len(parts) not in (1, 2):
            raise ValueError(
                "Invalid order_by clause format. "
                "Each clause must be: column_name [ASC|DESC]."
            )

        column = _safe_identifier(parts[0])
        if column not in allowed_columns:
            raise ValueError(f"Unknown column in order_by: {column}")

        direction = "ASC"
        if len(parts) == 2:
            direction = parts[1].upper()
            if direction not in ("ASC", "DESC"):
                raise ValueError("Invalid order direction. Use ASC or DESC.")

        clauses.append(f'"{column}" {direction}')

    if not clauses:
        raise ValueError("order_by must contain at least one valid column clause.")
    return ", ".join(clauses)


def _validate_query_sql(sql: str) -> str:
    normalized = sql.strip()
    if ";" in normalized:
        if normalized.count(";") != 1 or not normalized.endswith(";"):
            raise ValueError("Only a single query statement is allowed.")
        normalized = normalized[:-1].strip()

    lowered = normalized.lower()
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise ValueError("Only SELECT / WITH queries are allowed.")
    if "with recursive" in lowered:
        raise ValueError("WITH RECURSIVE is not allowed.")
    return normalized


@mcp.tool()
def describe_csv(csv_path: str, table_name: str = "tracks", ignore_errors: bool = False) -> dict[str, Any]:
    """Load CSV and return inferred schema + row count."""
    with duckdb.connect(database=":memory:") as con:
        safe_table = _create_or_replace_view(con, table_name, csv_path, ignore_errors)
        columns = con.execute(f'DESCRIBE SELECT * FROM "{safe_table}"').fetchall()
        row_count = con.execute(f'SELECT COUNT(*) FROM "{safe_table}"').fetchone()[0]
        return {
            "table_name": safe_table,
            "csv_path": str(Path(csv_path).expanduser().resolve()),
            "row_count": int(row_count),
            "columns": [
                {
                    "name": col[0],
                    "type": col[1],
                    "null": col[2],
                    "key": col[3],
                    "default": col[4],
                    "extra": col[5],
                }
                for col in columns
            ],
        }


@mcp.tool()
def query_csv(
    csv_path: str,
    sql: str,
    table_name: str = "tracks",
    max_rows: int = 1000,
    ignore_errors: bool = False,
) -> dict[str, Any]:
    """Run SQL against CSV. SQL should reference the given table_name."""
    if max_rows <= 0:
        raise ValueError("max_rows must be > 0.")
    if max_rows > 10000:
        raise ValueError("max_rows must be <= 10000.")

    with duckdb.connect(database=":memory:") as con:
        safe_table = _create_or_replace_view(con, table_name, csv_path, ignore_errors)
        safe_sql = _validate_query_sql(sql)
        limited_sql = f"SELECT * FROM ({safe_sql}) q LIMIT {int(max_rows)}"
        result = con.execute(limited_sql)
        cols = [d[0] for d in result.description]
        rows = result.fetchall()

        return {
            "table_name": safe_table,
            "csv_path": str(Path(csv_path).expanduser().resolve()),
            "max_rows": int(max_rows),
            "returned_rows": len(rows),
            "columns": cols,
            "rows": [list(r) for r in rows],
        }


@mcp.tool()
def deduplicate_csv(
    csv_path: str,
    key_columns: list[str],
    output_path: str | None = None,
    table_name: str = "tracks",
    order_by: str | None = None,
    ignore_errors: bool = False,
) -> dict[str, Any]:
    """Deduplicate CSV by key columns and write new CSV with first row per key."""
    if not key_columns:
        raise ValueError("key_columns cannot be empty.")

    with duckdb.connect(database=":memory:") as con:
        safe_table = _create_or_replace_view(con, table_name, csv_path, ignore_errors)
        allowed_columns = _list_columns(con, safe_table)
        safe_keys = [_safe_identifier(k) for k in key_columns]
        missing = [k for k in safe_keys if k not in allowed_columns]
        if missing:
            raise ValueError(f"Unknown key columns: {missing}")
        target = _resolve_output_path(csv_path, output_path)

        partition_expr = ", ".join(f'"{k}"' for k in safe_keys)
        order_expr = _safe_order_by(order_by, allowed_columns, partition_expr)

        before = con.execute(f'SELECT COUNT(*) FROM "{safe_table}"').fetchone()[0]
        dedup_sql = f"""
            COPY (
                SELECT * EXCLUDE (__rn)
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (PARTITION BY {partition_expr} ORDER BY {order_expr}) AS __rn
                    FROM "{safe_table}"
                )
                WHERE __rn = 1
            )
            TO ?
            WITH (FORMAT CSV, HEADER true)
        """
        con.execute(dedup_sql, [str(target)])
        after = con.execute(
            f"""
            SELECT COUNT(*)
            FROM read_csv_auto(?, sample_size=-1, ignore_errors=?)
            """,
            [str(target), ignore_errors],
        ).fetchone()[0]

        return {
            "table_name": safe_table,
            "source_csv": str(Path(csv_path).expanduser().resolve()),
            "output_csv": str(target),
            "key_columns": safe_keys,
            "rows_before": int(before),
            "rows_after": int(after),
            "removed_rows": int(before - after),
        }


if __name__ == "__main__":
    raw_transport = os.getenv("MCP_TRANSPORT", "streamable-http").strip().lower()
    if raw_transport in {"streamable-http", "streamable_http", "streamable", "mcp"}:
        transport = "streamable-http"
    elif raw_transport == "sse":
        transport = "sse"
    else:
        raise ValueError(
            "Invalid MCP_TRANSPORT. Supported values: "
            "streamable-http (or aliases: streamable_http, streamable, mcp), "
            "or sse (only 'sse')."
        )

    mcp.settings.host = os.getenv("HOST", "0.0.0.0")
    mcp.settings.port = int(os.getenv("PORT", "8000"))
    default_mcp_path = "/mcp" if transport == "streamable-http" else "/sse"
    mcp_path = os.getenv("MCP_PATH", default_mcp_path)
    # Keep runtime compatibility when transport-specific settings are not
    # available on the installed MCP SDK.
    if transport == "streamable-http" and hasattr(mcp.settings, "streamable_http_path"):
        mcp.settings.streamable_http_path = mcp_path
    if transport == "sse":
        if hasattr(mcp.settings, "sse_path"):
            mcp.settings.sse_path = mcp_path
        if hasattr(mcp.settings, "message_path"):
            mcp.settings.message_path = os.getenv("MCP_MESSAGE_PATH", "/messages")
    # Backward compatibility: prefer ENABLE_DNS_REBINDING_PROTECTION, but
    # still support legacy DISABLE_DNS_REBINDING_PROTECTION.
    enable_dns_rebinding_protection = os.getenv("ENABLE_DNS_REBINDING_PROTECTION")
    if enable_dns_rebinding_protection is None:
        enable_dns_rebinding_protection = (
            "0" if os.getenv("DISABLE_DNS_REBINDING_PROTECTION", "0") == "1" else "1"
        )
    mcp.settings.transport_security.enable_dns_rebinding_protection = (
        enable_dns_rebinding_protection == "1"
    )
    mcp.settings.transport_security.allowed_hosts = [
        item.strip()
        for item in os.getenv("ALLOWED_HOSTS", "*").split(",")
        if item.strip()
    ]
    mcp.run(transport=transport)
