import os
import re
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("duckdb-mcp-lan-server", json_response=True)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SERVER_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _SERVER_DIR / "mcp.config.json"
_DEFAULT_WORKSPACE_DIR = _SERVER_DIR / "workspace"


def _load_mcp_config() -> dict[str, Any]:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid config JSON: {_CONFIG_PATH}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Config root must be an object: {_CONFIG_PATH}")
    return raw


def _resolve_workspace_dir() -> Path:
    config = _load_mcp_config()
    raw = config.get("workspaceDir")
    if raw is None:
        return _DEFAULT_WORKSPACE_DIR.resolve()
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("workspaceDir in mcp.config.json must be a non-empty string.")
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        path = (_SERVER_DIR / path).resolve()
    return path.resolve()


WORKSPACE_DIR = _resolve_workspace_dir()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


def _safe_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            "Invalid identifier. Must start with a letter or underscore, "
            "followed by letters, numbers, or underscores."
        )
    return name


def _duckdb_database_path() -> str:
    raw = os.getenv("DUCKDB_PATH")
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = WORKSPACE_DIR / path
        return str(path.resolve())
    return str((WORKSPACE_DIR / "duckdb_mcp.db").resolve())


def _connect_database() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(database=_duckdb_database_path())


def _resolve_workspace_path(path: str | None = None) -> Path:
    if path is None or not path.strip() or path.strip() == ".":
        candidate = WORKSPACE_DIR
    else:
        candidate = Path(path.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = WORKSPACE_DIR / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(WORKSPACE_DIR)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace: {resolved}") from exc
    return resolved


def _read_utf8_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"File is not valid UTF-8 text: {path}") from exc


def _resolve_output_path(csv_path: str, output_path: str | None) -> Path:
    source = _resolve_csv_path(csv_path)
    if output_path:
        path = Path(output_path).expanduser()
        if not path.is_absolute():
            path = WORKSPACE_DIR / path
        return path.resolve()
    return source.with_name(f"{source.stem}.dedup.csv")


def _resolve_csv_path(csv_path: str) -> Path:
    path = Path(csv_path).expanduser()
    if not path.is_absolute():
        path = WORKSPACE_DIR / path
    path = path.resolve()
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


def _table_columns(con: duckdb.DuckDBPyConnection, table_name: str) -> set[str]:
    safe_table = _safe_identifier(table_name)
    rows = con.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = ?
        """,
        [safe_table],
    ).fetchall()
    return {r[0] for r in rows}


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
    """读取 CSV 并返回自动推断的字段信息与总行数。"""
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
    """对 CSV 执行 SQL 查询（SQL 中请使用 table_name 作为表名）。"""
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
    """按 key 列对 CSV 去重，并输出每组保留一条记录的新 CSV。"""
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


@mcp.tool()
def duckdb_health() -> dict[str, Any]:
    """快速检查 DuckDB 连通性并返回版本、数据库路径与当前时间。"""
    with _connect_database() as con:
        version = con.execute("SELECT version()").fetchone()[0]
    return {
        "ok": True,
        "duckdbVersion": str(version),
        "dbPath": _duckdb_database_path(),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@mcp.tool()
def duckdb_list_tables(includeViews: bool = True) -> dict[str, Any]:
    """列出当前数据库中的表与视图名称。"""
    with _connect_database() as con:
        base_sql = """
            SELECT table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY table_name
        """
        rows = con.execute(base_sql).fetchall()

    tables = [r[0] for r in rows if r[1] == "BASE TABLE"]
    views = [r[0] for r in rows if r[1] == "VIEW"]
    if not includeViews:
        views = []
    return {"tables": tables, "views": views}


@mcp.tool()
def duckdb_describe(table: str) -> dict[str, Any]:
    """返回指定表的字段结构信息。"""
    safe_table = _safe_identifier(table)
    with _connect_database() as con:
        columns = con.execute(
            f"""
            SELECT name, type, "notnull"
            FROM pragma_table_info('{safe_table}')
            ORDER BY cid
            """
        ).fetchall()
    if not columns:
        raise ValueError(f"Table not found or has no columns: {safe_table}")

    return {
        "table": safe_table,
        "columns": [
            {"name": col[0], "type": col[1], "nullable": (col[2] == 0)} for col in columns
        ],
    }


@mcp.tool()
def duckdb_preview(table: str = "tracks", limit: int = 50) -> dict[str, Any]:
    """返回指定表前 N 行数据，便于快速预览。"""
    if limit <= 0:
        raise ValueError("limit must be > 0.")
    if limit > 5000:
        raise ValueError("limit must be <= 5000.")

    safe_table = _safe_identifier(table)
    with _connect_database() as con:
        rows = con.execute(f'SELECT * FROM "{safe_table}" LIMIT {int(limit)}').fetchall()
    return {"table": safe_table, "rows": [list(r) for r in rows], "rowCount": len(rows)}


@mcp.tool()
def duckdb_dedup_exact(table: str = "tracks", outTable: str = "tracks_dedup_exact") -> dict[str, Any]:
    """对整行完全相同的数据做去重并写入新表。"""
    safe_table = _safe_identifier(table)
    safe_out = _safe_identifier(outTable)
    with _connect_database() as con:
        con.execute(
            f'CREATE OR REPLACE TABLE "{safe_out}" AS SELECT DISTINCT * FROM "{safe_table}"'
        )
        row_count = con.execute(f'SELECT COUNT(*) FROM "{safe_out}"').fetchone()[0]
    return {"outTable": safe_out, "rowCount": int(row_count)}


@mcp.tool()
def duckdb_dedup_consecutive(
    table: str = "tracks",
    outTable: str = "tracks_dedup_consecutive",
    keys: list[str] | None = None,
    partitionBy: list[str] | None = None,
    orderBy: str | None = None,
) -> dict[str, Any]:
    """按分组和顺序去除连续重复行（保留每段连续记录的第一条）。"""
    safe_table = _safe_identifier(table)
    safe_out = _safe_identifier(outTable)
    safe_keys = keys or ["lat", "lon", "height", "speed", "angle", "vspeed"]
    safe_partition = partitionBy or ["fnum"]
    safe_keys = [_safe_identifier(k) for k in safe_keys]
    safe_partition = [_safe_identifier(k) for k in safe_partition]

    with _connect_database() as con:
        existing_columns = _table_columns(con, safe_table)
        if not existing_columns:
            raise ValueError(f"Table not found: {safe_table}")

        missing_keys = [k for k in safe_keys if k not in existing_columns]
        if missing_keys:
            raise ValueError(f"Missing key columns: {missing_keys}")

        missing_partition = [k for k in safe_partition if k not in existing_columns]
        if missing_partition:
            raise ValueError(f"Missing partitionBy columns: {missing_partition}")

        order_expr = _safe_order_by(orderBy, existing_columns, '"u_time" ASC')
        partition_expr = ", ".join(f'"{k}"' for k in safe_partition)
        equals_expr = " AND ".join(
            [f'("{k}" IS NOT DISTINCT FROM LAG("{k}") OVER w)' for k in safe_keys]
        )

        con.execute(
            f"""
            CREATE OR REPLACE TABLE "{safe_out}" AS
            WITH marked AS (
                SELECT *,
                       CASE WHEN {equals_expr} THEN 0 ELSE 1 END AS __keep
                FROM "{safe_table}"
                WINDOW w AS (PARTITION BY {partition_expr} ORDER BY {order_expr})
            )
            SELECT * EXCLUDE (__keep)
            FROM marked
            WHERE __keep = 1
            """
        )
        row_count = con.execute(f'SELECT COUNT(*) FROM "{safe_out}"').fetchone()[0]

    return {"outTable": safe_out, "rowCount": int(row_count)}


@mcp.tool()
def workspace_list_files(
    path: str = ".",
    recursive: bool = True,
    include_dirs: bool = False,
    max_entries: int = 1000,
) -> dict[str, Any]:
    """列出工作区中的文件或目录。"""
    if max_entries <= 0:
        raise ValueError("max_entries must be > 0.")
    if max_entries > 10000:
        raise ValueError("max_entries must be <= 10000.")

    root = _resolve_workspace_path(path)
    if not root.exists():
        raise ValueError(f"Path not found: {root}")
    if not root.is_dir():
        raise ValueError(f"Path is not a directory: {root}")

    iterator = root.rglob("*") if recursive else root.glob("*")
    entries: list[dict[str, Any]] = []
    truncated = False
    for item in iterator:
        if item.is_dir() and not include_dirs:
            continue
        record: dict[str, Any] = {
            "path": item.relative_to(WORKSPACE_DIR).as_posix(),
            "type": "dir" if item.is_dir() else "file",
        }
        if item.is_file():
            record["size"] = int(item.stat().st_size)
        entries.append(record)
        if len(entries) >= max_entries:
            truncated = True
            break

    return {
        "workspace": str(WORKSPACE_DIR),
        "base_path": root.relative_to(WORKSPACE_DIR).as_posix() if root != WORKSPACE_DIR else ".",
        "recursive": recursive,
        "include_dirs": include_dirs,
        "returned": len(entries),
        "truncated": truncated,
        "entries": entries,
    }


@mcp.tool()
def workspace_search_files(
    query: str,
    path: str = ".",
    file_glob: str = "*",
    case_sensitive: bool = False,
    max_results: int = 200,
) -> dict[str, Any]:
    """在工作区中按文件名过滤后搜索文本内容（UTF-8）。"""
    if not query or not query.strip():
        raise ValueError("query cannot be empty.")
    if max_results <= 0:
        raise ValueError("max_results must be > 0.")
    if max_results > 5000:
        raise ValueError("max_results must be <= 5000.")

    root = _resolve_workspace_path(path)
    if not root.exists():
        raise ValueError(f"Path not found: {root}")
    if not root.is_dir():
        raise ValueError(f"Path is not a directory: {root}")

    needle = query if case_sensitive else query.lower()
    matches: list[dict[str, Any]] = []
    truncated = False

    for file_path in root.rglob(file_glob):
        if not file_path.is_file():
            continue
        try:
            text = _read_utf8_text(file_path)
        except ValueError:
            continue

        lines = text.splitlines()
        for idx, line in enumerate(lines, start=1):
            target = line if case_sensitive else line.lower()
            if needle in target:
                matches.append(
                    {
                        "path": file_path.relative_to(WORKSPACE_DIR).as_posix(),
                        "line": idx,
                        "text": line,
                    }
                )
                if len(matches) >= max_results:
                    truncated = True
                    break
        if truncated:
            break

    return {
        "workspace": str(WORKSPACE_DIR),
        "base_path": root.relative_to(WORKSPACE_DIR).as_posix() if root != WORKSPACE_DIR else ".",
        "query": query,
        "file_glob": file_glob,
        "case_sensitive": case_sensitive,
        "returned": len(matches),
        "truncated": truncated,
        "matches": matches,
    }


@mcp.tool()
def workspace_read_text_file(path: str, start_line: int = 1, max_lines: int = 2000) -> dict[str, Any]:
    """以文本方式读取工作区内的 UTF-8 文件。"""
    if start_line <= 0:
        raise ValueError("start_line must be > 0.")
    if max_lines <= 0:
        raise ValueError("max_lines must be > 0.")
    if max_lines > 10000:
        raise ValueError("max_lines must be <= 10000.")

    file_path = _resolve_workspace_path(path)
    if not file_path.exists():
        raise ValueError(f"File not found: {file_path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {file_path}")

    text = _read_utf8_text(file_path)
    lines = text.splitlines()

    start_idx = start_line - 1
    end_idx = min(start_idx + max_lines, len(lines))
    content = "\n".join(lines[start_idx:end_idx]) if start_idx < len(lines) else ""

    return {
        "workspace": str(WORKSPACE_DIR),
        "path": file_path.relative_to(WORKSPACE_DIR).as_posix(),
        "start_line": start_line,
        "end_line": end_idx,
        "total_lines": len(lines),
        "truncated": end_idx < len(lines),
        "content": content,
    }


if __name__ == "__main__":
    os.chdir(WORKSPACE_DIR)
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
