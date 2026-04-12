import json
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
        raise ValueError("Invalid identifier. Use letters, numbers, and underscore only.")
    return name


def _resolve_output_path(csv_path: str, output_path: str | None) -> Path:
    source = Path(csv_path).expanduser().resolve()
    if output_path:
        return Path(output_path).expanduser().resolve()
    return source.with_name(f"{source.stem}.dedup{source.suffix or '.csv'}")


def _create_or_replace_view(con: duckdb.DuckDBPyConnection, table_name: str, csv_path: str) -> str:
    safe_table = _safe_identifier(table_name)
    con.execute(
        f"""
        CREATE OR REPLACE VIEW "{safe_table}" AS
        SELECT *
        FROM read_csv_auto(?, sample_size=-1, ignore_errors=true)
        """,
        [str(Path(csv_path).expanduser().resolve())],
    )
    return safe_table


@mcp.tool()
def describe_csv(csv_path: str, table_name: str = "tracks") -> dict[str, Any]:
    """Load CSV and return inferred schema + row count."""
    con = duckdb.connect(database=":memory:")
    safe_table = _create_or_replace_view(con, table_name, csv_path)
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
) -> dict[str, Any]:
    """Run SQL against CSV. SQL should reference the given table_name."""
    if max_rows <= 0:
        raise ValueError("max_rows must be > 0.")

    con = duckdb.connect(database=":memory:")
    safe_table = _create_or_replace_view(con, table_name, csv_path)
    limited_sql = f"SELECT * FROM ({sql}) q LIMIT {int(max_rows)}"
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
        "rows_json": json.dumps([dict(zip(cols, r)) for r in rows], ensure_ascii=False, default=str),
    }


@mcp.tool()
def deduplicate_csv(
    csv_path: str,
    key_columns: list[str],
    output_path: str | None = None,
    table_name: str = "tracks",
    order_by: str | None = None,
) -> dict[str, Any]:
    """Deduplicate CSV by key columns and write new CSV with first row per key."""
    if not key_columns:
        raise ValueError("key_columns cannot be empty.")

    con = duckdb.connect(database=":memory:")
    safe_table = _create_or_replace_view(con, table_name, csv_path)
    safe_keys = [_safe_identifier(k) for k in key_columns]
    target = _resolve_output_path(csv_path, output_path)

    if order_by and not re.fullmatch(r"[A-Za-z0-9_,\s\".]+", order_by):
        raise ValueError("Unsafe order_by expression.")

    partition_expr = ", ".join(f'"{k}"' for k in safe_keys)
    if order_by:
        order_expr = order_by
    else:
        order_expr = partition_expr

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
        FROM read_csv_auto(?, sample_size=-1, ignore_errors=true)
        """,
        [str(target)],
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
    mcp.settings.host = os.getenv("HOST", "0.0.0.0")
    mcp.settings.port = int(os.getenv("PORT", "8000"))
    mcp.settings.streamable_http_path = os.getenv("MCP_PATH", "/mcp")
    mcp.settings.transport_security.enable_dns_rebinding_protection = (
        os.getenv("DISABLE_DNS_REBINDING_PROTECTION", "1") != "1"
    )
    mcp.run(transport="streamable-http")
