import os
import re
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import matplotlib
from mcp.server.fastmcp import FastMCP
from pypdf import PdfReader
from scipy import stats as scipy_stats

matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    import fitz  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency
    fitz = None

try:
    from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency
    RapidOCR = None


mcp = FastMCP("duckdb-mcp-lan-server", json_response=True)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SERVER_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _SERVER_DIR / "mcp.config.json"
_DEFAULT_WORKSPACE_DIR = _SERVER_DIR / "workspace"
# FzBookMaker garbled extraction often emits CJK code points in this range.
_FZBOOKMAKER_GARBLED_RE = re.compile(r"[\u7280-\u733f]")
_FZBOOKMAKER_GNAME_RE = re.compile(r"/G[0-9A-F]{2}")
_FZ_GNAME_TOKEN_CHARS = 4
_FZ_GARBLED_MIN_HITS = 8
_FZ_GNAME_MIN_HITS = 5
_FZ_GARBLED_MIN_TEXT_LEN = 120
_FZ_GARBLED_MIN_RATIO = 0.05
_PDF_OCR_RENDER_SCALE = 2
_PDF_SEARCH_SNIPPET_CONTEXT_CHARS = 60


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
_WORKSPACE_WRITABLE_TEXT_FILE = "add.txt"


def _safe_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            "Invalid identifier. Must start with a letter or underscore, "
            "followed by letters, numbers, or underscores."
        )
    return name


def _quote_identifier(name: str) -> str:
    if not name:
        raise ValueError("Identifier cannot be empty.")
    if "\x00" in name:
        raise ValueError("Identifier cannot contain NUL characters.")
    return '"' + name.replace('"', '""') + '"'


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


def _resolve_writable_workspace_text_file() -> Path:
    file_path = _resolve_workspace_path(_WORKSPACE_WRITABLE_TEXT_FILE)
    if not file_path.exists():
        raise ValueError(f"File not found: {file_path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {file_path}")
    return file_path


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _resolve_output_path(csv_path: str, output_path: str | None) -> Path:
    source = _resolve_csv_path(csv_path)
    if output_path:
        path = Path(output_path).expanduser()
        if not path.is_absolute():
            path = WORKSPACE_DIR / path
        return path.resolve()
    return source.with_name(f"{source.stem}.dedup.csv")


def _resolve_output_file_path(output_path: str, default_ext: str = ".png") -> Path:
    path = Path(output_path).expanduser()
    if not path.is_absolute():
        path = WORKSPACE_DIR / path
    resolved = path.resolve()
    if not resolved.suffix:
        resolved = resolved.with_suffix(default_ext)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _resolve_csv_path(csv_path: str) -> Path:
    path = Path(csv_path).expanduser()
    if not path.is_absolute():
        path = WORKSPACE_DIR / path
    path = path.resolve()
    if not path.exists() or not path.is_file():
        raise ValueError(f"CSV file not found: {path}")
    return path


def _resolve_pdf_path(pdf_path: str) -> Path:
    path = _resolve_workspace_path(pdf_path)
    if not path.exists() or not path.is_file():
        raise ValueError(f"PDF file not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF file: {path}")
    return path


def _read_pdf(pdf_path: str) -> tuple[Path, PdfReader]:
    resolved = _resolve_pdf_path(pdf_path)
    try:
        reader = PdfReader(str(resolved))
    except Exception as exc:  # pragma: no cover - parser/library errors
        raise ValueError(f"Failed to read PDF: {resolved}") from exc
    return resolved, reader


def _looks_like_fzbookmaker_garbled(text: str) -> bool:
    if not text:
        return False
    garbled_hits = len(_FZBOOKMAKER_GARBLED_RE.findall(text))
    gname_hits = len(_FZBOOKMAKER_GNAME_RE.findall(text))
    # Conservative thresholds: trigger OCR only on clear signals to reduce false positives.
    if garbled_hits >= _FZ_GARBLED_MIN_HITS:
        return True
    if gname_hits >= _FZ_GNAME_MIN_HITS:
        return True
    garbled_coverage_chars = garbled_hits + (gname_hits * _FZ_GNAME_TOKEN_CHARS)
    if len(text) >= _FZ_GARBLED_MIN_TEXT_LEN and garbled_coverage_chars / len(text) >= _FZ_GARBLED_MIN_RATIO:
        return True
    return False


def _extract_page_text_with_fallback(
    page_index: int,
    page_obj: Any,
    enable_ocr_fallback: bool,
    ocr_doc: Any | None = None,
    ocr_engine: Any | None = None,
) -> tuple[str, str]:
    text = page_obj.extract_text() or ""
    if not enable_ocr_fallback or not _looks_like_fzbookmaker_garbled(text):
        return text, "text-layer"
    if ocr_doc is None or ocr_engine is None:
        return text, "text-layer"

    try:
        # Render at 2x to improve OCR accuracy while keeping runtime acceptable.
        pix = ocr_doc[page_index].get_pixmap(
            matrix=fitz.Matrix(_PDF_OCR_RENDER_SCALE, _PDF_OCR_RENDER_SCALE), alpha=False
        )
        image_bytes = pix.tobytes("png")
        ocr_result, _ = ocr_engine(image_bytes)
        if not ocr_result:
            return text, "text-layer"

        lines: list[str] = []
        for item in ocr_result:
            if len(item) < 2:
                continue
            line_text = str(item[1]).strip()
            if line_text:
                lines.append(line_text)
        if not lines:
            return text, "text-layer"
        return "\n".join(lines), "ocr-fallback"
    except Exception:
        return text, "text-layer"


def _build_ocr_fallback_resources(pdf_file: Path, enable_ocr_fallback: bool) -> tuple[Any | None, Any | None]:
    if not enable_ocr_fallback:
        return None, None
    if fitz is None or RapidOCR is None:
        return None, None
    try:
        doc = fitz.open(str(pdf_file))
        ocr = RapidOCR()
        return doc, ocr
    except Exception:
        return None, None


def _create_or_replace_view(
    con: duckdb.DuckDBPyConnection, table_name: str, csv_path: str, ignore_errors: bool
) -> str:
    safe_table = _safe_identifier(table_name)
    source = _resolve_csv_path(csv_path)
    source_literal = _sql_string_literal(str(source))
    ignore_errors_literal = "true" if ignore_errors else "false"
    con.execute(
        f"""
        CREATE OR REPLACE VIEW "{safe_table}" AS
        SELECT *
        FROM read_csv_auto({source_literal}, sample_size=-1, ignore_errors={ignore_errors_literal})
        """
    )
    return safe_table


def _list_columns(con: duckdb.DuckDBPyConnection, table_name: str) -> set[str]:
    rows = con.execute(f'DESCRIBE SELECT * FROM "{table_name}"').fetchall()
    return {r[0] for r in rows}


def _require_columns(available_columns: set[str], required_columns: list[str]) -> None:
    missing = [name for name in required_columns if name not in available_columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


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

        column = parts[0]
        if column not in allowed_columns:
            raise ValueError(f"Unknown column in order_by: {column}")

        direction = "ASC"
        if len(parts) == 2:
            direction = parts[1].upper()
            if direction not in ("ASC", "DESC"):
                raise ValueError("Invalid order direction. Use ASC or DESC.")

        clauses.append(f"{_quote_identifier(column)} {direction}")

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


def _validate_where_sql(where_sql: str) -> str:
    normalized = where_sql.strip()
    if not normalized:
        raise ValueError("where_sql cannot be empty.")
    if ";" in normalized:
        raise ValueError("where_sql must be a single expression without semicolons.")
    if "--" in normalized or "/*" in normalized or "*/" in normalized:
        raise ValueError("where_sql cannot contain SQL comments.")
    if re.search(
        r"\b(select|with|copy|create|insert|update|delete|drop|alter|attach|detach|pragma)\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        raise ValueError("where_sql must be a filter expression only.")
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
        safe_keys = [k.strip() for k in key_columns]
        if any(not k for k in safe_keys):
            raise ValueError("key_columns cannot contain empty names.")
        missing = [k for k in safe_keys if k not in allowed_columns]
        if missing:
            raise ValueError(f"Unknown key columns: {missing}")
        target = _resolve_output_path(csv_path, output_path)

        partition_expr = ", ".join(_quote_identifier(k) for k in safe_keys)
        order_expr = _safe_order_by(order_by, allowed_columns, partition_expr)

        before = con.execute(f'SELECT COUNT(*) FROM "{safe_table}"').fetchone()[0]
        target_literal = _sql_string_literal(str(target))
        ignore_errors_literal = "true" if ignore_errors else "false"
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
            TO {target_literal}
            WITH (FORMAT CSV, HEADER true)
        """
        con.execute(dedup_sql)
        after = con.execute(
            f"""
            SELECT COUNT(*)
            FROM read_csv_auto({target_literal}, sample_size=-1, ignore_errors={ignore_errors_literal})
            """
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
def filter_csv(
    csv_path: str,
    where_sql: str,
    output_path: str | None = None,
    table_name: str = "tracks",
    ignore_errors: bool = False,
) -> dict[str, Any]:
    """Filter CSV rows by condition and export a new CSV file."""
    source = _resolve_csv_path(csv_path)
    target = (
        _resolve_output_path(str(source), output_path)
        if output_path
        else source.with_name(f"{source.stem}.filtered.csv")
    )
    safe_where = _validate_where_sql(where_sql)

    with duckdb.connect(database=":memory:") as con:
        safe_table = _create_or_replace_view(con, table_name, str(source), ignore_errors)
        before = con.execute(f'SELECT COUNT(*) FROM "{safe_table}"').fetchone()[0]

        target_literal = _sql_string_literal(str(target))
        ignore_errors_literal = "true" if ignore_errors else "false"
        con.execute(
            f"""
            COPY (
                SELECT *
                FROM "{safe_table}"
                WHERE {safe_where}
            )
            TO {target_literal}
            WITH (FORMAT CSV, HEADER true)
            """
        )
        after = con.execute(
            f"""
            SELECT COUNT(*)
            FROM read_csv_auto({target_literal}, sample_size=-1, ignore_errors={ignore_errors_literal})
            """
        ).fetchone()[0]

    return {
        "table_name": safe_table,
        "source_csv": str(source),
        "output_csv": str(target),
        "where_sql": safe_where,
        "rows_before": int(before),
        "rows_after": int(after),
        "removed_rows": int(before - after),
    }


@mcp.tool()
def plot_basic(
    csv_path: str,
    chart_type: str,
    x_field: str | None = None,
    y_field: str | None = None,
    color_field: str | None = None,
    output_path: str = "plot_basic.png",
    bins: int = 20,
    table_name: str = "tracks",
    ignore_errors: bool = False,
) -> dict[str, Any]:
    """Generate a basic chart from CSV data and save it to an image file."""
    chart = chart_type.strip().lower()
    if chart not in {"scatter", "line", "histogram", "box"}:
        raise ValueError("chart_type must be one of: scatter, line, histogram, box.")
    if bins <= 0:
        raise ValueError("bins must be > 0.")

    csv_file = _resolve_csv_path(csv_path)
    target = _resolve_output_file_path(output_path, default_ext=".png")
    with duckdb.connect(database=":memory:") as con:
        safe_table = _create_or_replace_view(con, table_name, str(csv_file), ignore_errors)
        columns = _list_columns(con, safe_table)
        required: list[str] = []
        if chart in {"scatter", "line"}:
            if not x_field or not y_field:
                raise ValueError(f"{chart} requires x_field and y_field.")
            required.extend([x_field, y_field])
        elif chart in {"histogram", "box"}:
            value_field = (y_field or x_field or "").strip()
            if not value_field:
                raise ValueError(f"{chart} requires x_field or y_field as value field.")
            required.append(value_field)
        if color_field:
            required.append(color_field)
        _require_columns(columns, required)

        fig, ax = plt.subplots(figsize=(10, 6))
        try:
            if chart in {"scatter", "line"}:
                x_id = _quote_identifier(x_field or "")
                y_id = _quote_identifier(y_field or "")
                if color_field:
                    c_id = _quote_identifier(color_field)
                    rows = con.execute(
                        f"""
                        SELECT {x_id}, {y_id}, {c_id}
                        FROM "{safe_table}"
                        WHERE {x_id} IS NOT NULL AND {y_id} IS NOT NULL
                        ORDER BY {c_id}
                        """
                    ).fetchall()
                    if not rows:
                        raise ValueError("No rows available for plotting after NULL filtering.")
                    grouped: dict[str, list[tuple[float, float]]] = {}
                    for x_val, y_val, c_val in rows:
                        x_num = _to_float(x_val)
                        y_num = _to_float(y_val)
                        if x_num is None or y_num is None:
                            continue
                        key = str(c_val)
                        grouped.setdefault(key, []).append((x_num, y_num))
                    if not grouped:
                        raise ValueError("No numeric rows available for plotting.")
                    for group, points in grouped.items():
                        xs = [p[0] for p in points]
                        ys = [p[1] for p in points]
                        if chart == "scatter":
                            ax.scatter(xs, ys, s=14, alpha=0.75, label=group)
                        else:
                            ax.plot(xs, ys, linewidth=1.2, label=group)
                    ax.legend(loc="best", fontsize=8)
                else:
                    rows = con.execute(
                        f"""
                        SELECT {x_id}, {y_id}
                        FROM "{safe_table}"
                        WHERE {x_id} IS NOT NULL AND {y_id} IS NOT NULL
                        """
                    ).fetchall()
                    xs: list[float] = []
                    ys: list[float] = []
                    for x_val, y_val in rows:
                        x_num = _to_float(x_val)
                        y_num = _to_float(y_val)
                        if x_num is None or y_num is None:
                            continue
                        xs.append(x_num)
                        ys.append(y_num)
                    if not xs:
                        raise ValueError("No numeric rows available for plotting.")
                    if chart == "scatter":
                        ax.scatter(xs, ys, s=14, alpha=0.75)
                    else:
                        ax.plot(xs, ys, linewidth=1.2)
                ax.set_xlabel(x_field or "")
                ax.set_ylabel(y_field or "")
                ax.set_title(f"{chart.capitalize()} Plot")
            elif chart == "histogram":
                value_field = (y_field or x_field or "").strip()
                field_id = _quote_identifier(value_field)
                rows = con.execute(
                    f"""
                    SELECT TRY_CAST({field_id} AS DOUBLE) AS __v
                    FROM "{safe_table}"
                    WHERE TRY_CAST({field_id} AS DOUBLE) IS NOT NULL
                    """
                ).fetchall()
                values = [float(r[0]) for r in rows]
                if not values:
                    raise ValueError("No numeric rows available for histogram.")
                ax.hist(values, bins=int(bins), edgecolor="white")
                ax.set_xlabel(value_field)
                ax.set_ylabel("Count")
                ax.set_title("Histogram")
            else:
                value_field = (y_field or x_field or "").strip()
                field_id = _quote_identifier(value_field)
                rows = con.execute(
                    f"""
                    SELECT TRY_CAST({field_id} AS DOUBLE) AS __v
                    FROM "{safe_table}"
                    WHERE TRY_CAST({field_id} AS DOUBLE) IS NOT NULL
                    """
                ).fetchall()
                values = [float(r[0]) for r in rows]
                if not values:
                    raise ValueError("No numeric rows available for box plot.")
                ax.boxplot(values, vert=True)
                ax.set_ylabel(value_field)
                ax.set_title("Box Plot")
            fig.tight_layout()
            fig.savefig(str(target), dpi=150)
        finally:
            plt.close(fig)

    return {
        "csv_path": str(csv_file),
        "chart_type": chart,
        "output_path": str(target),
        "x_field": x_field,
        "y_field": y_field,
        "color_field": color_field,
        "bins": int(bins),
    }


@mcp.tool()
def plot_time_series(
    csv_path: str,
    time_field: str,
    value_fields: list[str],
    output_path: str,
    table_name: str = "tracks",
    ignore_errors: bool = False,
) -> dict[str, Any]:
    """Plot one or more value fields against a timestamp field."""
    safe_values = [f.strip() for f in value_fields if f and f.strip()]
    if not safe_values:
        raise ValueError("value_fields cannot be empty.")

    csv_file = _resolve_csv_path(csv_path)
    target = _resolve_output_file_path(output_path, default_ext=".png")
    with duckdb.connect(database=":memory:") as con:
        safe_table = _create_or_replace_view(con, table_name, str(csv_file), ignore_errors)
        columns = _list_columns(con, safe_table)
        _require_columns(columns, [time_field] + safe_values)
        time_id = _quote_identifier(time_field)
        value_ids = [_quote_identifier(f) for f in safe_values]
        select_values = ", ".join(value_ids)
        rows = con.execute(
            f"""
            SELECT TRY_CAST({time_id} AS TIMESTAMP) AS __ts, {select_values}
            FROM "{safe_table}"
            WHERE TRY_CAST({time_id} AS TIMESTAMP) IS NOT NULL
            ORDER BY __ts
            """
        ).fetchall()
        if not rows:
            raise ValueError("No valid timestamp rows available for plotting.")

        fig, ax = plt.subplots(figsize=(12, 6))
        try:
            for idx, field in enumerate(safe_values, start=1):
                xs: list[Any] = []
                ys: list[float] = []
                for row in rows:
                    value = _to_float(row[idx])
                    if value is None:
                        continue
                    xs.append(row[0])
                    ys.append(value)
                if xs:
                    ax.plot(xs, ys, linewidth=1.2, label=field)
            if not ax.lines:
                raise ValueError("No numeric values available for value_fields.")
            ax.set_xlabel(time_field)
            ax.set_ylabel("Value")
            ax.set_title("Time Series")
            ax.legend(loc="best", fontsize=8)
            fig.autofmt_xdate()
            fig.tight_layout()
            fig.savefig(str(target), dpi=150)
        finally:
            plt.close(fig)

    return {
        "csv_path": str(csv_file),
        "time_field": time_field,
        "value_fields": safe_values,
        "output_path": str(target),
        "row_count": len(rows),
    }


@mcp.tool()
def plot_geo(
    csv_path: str,
    x_field: str,
    y_field: str,
    output_path: str,
    color_field: str | None = None,
    size_field: str | None = None,
    table_name: str = "tracks",
    ignore_errors: bool = False,
) -> dict[str, Any]:
    """Plot geographic points using x/y coordinate fields."""
    csv_file = _resolve_csv_path(csv_path)
    target = _resolve_output_file_path(output_path, default_ext=".png")
    with duckdb.connect(database=":memory:") as con:
        safe_table = _create_or_replace_view(con, table_name, str(csv_file), ignore_errors)
        columns = _list_columns(con, safe_table)
        required = [x_field, y_field]
        if color_field:
            required.append(color_field)
        if size_field:
            required.append(size_field)
        _require_columns(columns, required)

        x_id = _quote_identifier(x_field)
        y_id = _quote_identifier(y_field)
        select_parts = [x_id, y_id]
        if color_field:
            select_parts.append(_quote_identifier(color_field))
        if size_field:
            select_parts.append(_quote_identifier(size_field))
        select_expr = ", ".join(select_parts)
        rows = con.execute(
            f"""
            SELECT {select_expr}
            FROM "{safe_table}"
            WHERE {x_id} IS NOT NULL AND {y_id} IS NOT NULL
            """
        ).fetchall()
        if not rows:
            raise ValueError("No rows available for geo plotting.")

        xs: list[float] = []
        ys: list[float] = []
        colors: list[str] = []
        sizes: list[float] = []
        for row in rows:
            x_num = _to_float(row[0])
            y_num = _to_float(row[1])
            if x_num is None or y_num is None:
                continue
            xs.append(x_num)
            ys.append(y_num)
            cursor = 2
            if color_field:
                colors.append(str(row[cursor]))
                cursor += 1
            if size_field:
                size_num = _to_float(row[cursor])
                sizes.append(size_num if size_num is not None else 20.0)
        if not xs:
            raise ValueError("No numeric coordinate rows available for geo plotting.")

        fig, ax = plt.subplots(figsize=(10, 8))
        try:
            if color_field:
                unique = sorted(set(colors))
                palette: dict[str, int] = {label: idx for idx, label in enumerate(unique)}
                c_values = [palette[c] for c in colors]
                marker_sizes = sizes if size_field and len(sizes) == len(xs) else 20
                scatter = ax.scatter(xs, ys, c=c_values, s=marker_sizes, alpha=0.75, cmap="viridis")
                cbar = fig.colorbar(scatter, ax=ax)
                cbar.set_label(color_field)
            else:
                marker_sizes = sizes if size_field and len(sizes) == len(xs) else 20
                ax.scatter(xs, ys, s=marker_sizes, alpha=0.75)
            ax.set_xlabel(x_field)
            ax.set_ylabel(y_field)
            ax.set_title("Geo Scatter Plot")
            fig.tight_layout()
            fig.savefig(str(target), dpi=150)
        finally:
            plt.close(fig)

    return {
        "csv_path": str(csv_file),
        "x_field": x_field,
        "y_field": y_field,
        "color_field": color_field,
        "size_field": size_field,
        "output_path": str(target),
        "row_count": len(xs),
    }


@mcp.tool()
def analyze_correlation(
    csv_path: str,
    field_x: str,
    field_y: str,
    method: str = "pearson",
    table_name: str = "tracks",
    ignore_errors: bool = False,
) -> dict[str, Any]:
    """Compute correlation coefficient, p-value, and sample size for two fields."""
    method_normalized = method.strip().lower()
    if method_normalized not in {"pearson", "spearman"}:
        raise ValueError("method must be pearson or spearman.")

    csv_file = _resolve_csv_path(csv_path)
    with duckdb.connect(database=":memory:") as con:
        safe_table = _create_or_replace_view(con, table_name, str(csv_file), ignore_errors)
        columns = _list_columns(con, safe_table)
        _require_columns(columns, [field_x, field_y])
        x_id = _quote_identifier(field_x)
        y_id = _quote_identifier(field_y)
        rows = con.execute(
            f"""
            SELECT TRY_CAST({x_id} AS DOUBLE) AS x, TRY_CAST({y_id} AS DOUBLE) AS y
            FROM "{safe_table}"
            WHERE TRY_CAST({x_id} AS DOUBLE) IS NOT NULL
              AND TRY_CAST({y_id} AS DOUBLE) IS NOT NULL
            """
        ).fetchall()

    if len(rows) < 2:
        raise ValueError("At least 2 valid numeric samples are required.")
    xs = [float(r[0]) for r in rows]
    ys = [float(r[1]) for r in rows]
    if method_normalized == "pearson":
        result = scipy_stats.pearsonr(xs, ys)
    else:
        result = scipy_stats.spearmanr(xs, ys)
    return {
        "csv_path": str(csv_file),
        "field_x": field_x,
        "field_y": field_y,
        "method": method_normalized,
        "correlation": float(result.statistic),
        "p_value": float(result.pvalue),
        "sample_count": len(rows),
    }


@mcp.tool()
def analyze_distribution(
    csv_path: str,
    field: str,
    table_name: str = "tracks",
    ignore_errors: bool = False,
) -> dict[str, Any]:
    """Compute descriptive distribution statistics for a numeric field."""
    csv_file = _resolve_csv_path(csv_path)
    with duckdb.connect(database=":memory:") as con:
        safe_table = _create_or_replace_view(con, table_name, str(csv_file), ignore_errors)
        columns = _list_columns(con, safe_table)
        _require_columns(columns, [field])
        field_id = _quote_identifier(field)
        row = con.execute(
            f"""
            WITH base AS (
                SELECT TRY_CAST({field_id} AS DOUBLE) AS __v
                FROM "{safe_table}"
                WHERE TRY_CAST({field_id} AS DOUBLE) IS NOT NULL
            )
            SELECT
                COUNT(*) AS sample_count,
                MIN(__v) AS min_value,
                MAX(__v) AS max_value,
                AVG(__v) AS mean_value,
                MEDIAN(__v) AS median_value,
                STDDEV_SAMP(__v) AS stddev,
                quantile_cont(__v, 0.25) AS q1,
                quantile_cont(__v, 0.75) AS q3
            FROM base
            """
        ).fetchone()
    sample_count = int(row[0]) if row else 0
    if sample_count == 0:
        raise ValueError("No valid numeric samples found for field.")
    return {
        "csv_path": str(csv_file),
        "field": field,
        "sample_count": sample_count,
        "min": float(row[1]),
        "max": float(row[2]),
        "mean": float(row[3]),
        "median": float(row[4]),
        "std": float(row[5]) if row[5] is not None else None,
        "q1": float(row[6]),
        "q3": float(row[7]),
    }


@mcp.tool()
def analyze_group_stats(
    csv_path: str,
    group_field: str,
    value_fields: list[str],
    stats: list[str] | None = None,
    table_name: str = "tracks",
    ignore_errors: bool = False,
) -> dict[str, Any]:
    """Compute grouped statistics for one or more numeric fields."""
    safe_values = [f.strip() for f in value_fields if f and f.strip()]
    if not safe_values:
        raise ValueError("value_fields cannot be empty.")
    safe_stats = [s.strip().lower() for s in (stats or ["mean", "std", "count"]) if s and s.strip()]
    if not safe_stats:
        raise ValueError("stats cannot be empty.")

    supported = {"mean", "std", "count", "min", "max", "sum", "median"}
    unsupported = [s for s in safe_stats if s not in supported]
    if unsupported:
        raise ValueError(f"Unsupported stats: {unsupported}")

    csv_file = _resolve_csv_path(csv_path)
    with duckdb.connect(database=":memory:") as con:
        safe_table = _create_or_replace_view(con, table_name, str(csv_file), ignore_errors)
        columns = _list_columns(con, safe_table)
        _require_columns(columns, [group_field] + safe_values)

        exprs: list[str] = [_quote_identifier(group_field)]
        for field in safe_values:
            field_id = _quote_identifier(field)
            numeric_expr = f"TRY_CAST({field_id} AS DOUBLE)"
            for stat_name in safe_stats:
                alias = _quote_identifier(f"{field}__{stat_name}")
                if stat_name == "count":
                    exprs.append(f"COUNT({numeric_expr}) AS {alias}")
                elif stat_name == "mean":
                    exprs.append(f"AVG({numeric_expr}) AS {alias}")
                elif stat_name == "std":
                    exprs.append(f"STDDEV_SAMP({numeric_expr}) AS {alias}")
                elif stat_name == "min":
                    exprs.append(f"MIN({numeric_expr}) AS {alias}")
                elif stat_name == "max":
                    exprs.append(f"MAX({numeric_expr}) AS {alias}")
                elif stat_name == "sum":
                    exprs.append(f"SUM({numeric_expr}) AS {alias}")
                else:
                    exprs.append(f"MEDIAN({numeric_expr}) AS {alias}")

        group_id = _quote_identifier(group_field)
        sql = f"""
            SELECT {", ".join(exprs)}
            FROM "{safe_table}"
            GROUP BY {group_id}
            ORDER BY {group_id}
        """
        result = con.execute(sql)
        result_columns = [d[0] for d in result.description]
        rows = result.fetchall()

    return {
        "csv_path": str(csv_file),
        "group_field": group_field,
        "value_fields": safe_values,
        "stats": safe_stats,
        "columns": result_columns,
        "rows": [list(r) for r in rows],
        "group_count": len(rows),
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


@mcp.tool()
def workspace_write_text_file(content: str, append: bool = False) -> dict[str, Any]:
    """写入工作区内已存在的 add.txt（仅允许该文件）。"""
    file_path = _resolve_writable_workspace_text_file()

    mode = "a" if append else "w"
    with file_path.open(mode, encoding="utf-8") as f:
        f.write(content)

    return {
        "workspace": str(WORKSPACE_DIR),
        "path": file_path.relative_to(WORKSPACE_DIR).as_posix(),
        "append": append,
        "bytes_written": len(content.encode("utf-8")),
        "file_size": int(file_path.stat().st_size),
    }


@mcp.tool()
def workspace_replace_text_in_line(
    line_number: int,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> dict[str, Any]:
    """在 add.txt 指定行内替换文本片段。"""
    if line_number <= 0:
        raise ValueError("line_number must be a positive integer.")
    if old_text == "":
        raise ValueError("old_text cannot be empty.")

    file_path = _resolve_writable_workspace_text_file()
    text = _read_utf8_text(file_path)
    lines = text.splitlines(keepends=True)
    if not lines:
        raise ValueError("Target file is empty.")
    if line_number > len(lines):
        raise ValueError(f"line_number out of range: {line_number} > {len(lines)}")

    idx = line_number - 1
    original_line = lines[idx]
    line_break = ""
    line_body = original_line
    for candidate in ("\r\n", "\n", "\r"):
        if original_line.endswith(candidate):
            line_break = candidate
            line_body = original_line[: -len(candidate)]
            break

    match_count = line_body.count(old_text)
    if match_count == 0:
        raise ValueError("old_text not found in target line.")

    replaced_count = match_count if replace_all else 1
    updated_body = line_body.replace(old_text, new_text, replaced_count)
    lines[idx] = updated_body + line_break
    file_path.write_text("".join(lines), encoding="utf-8")

    return {
        "workspace": str(WORKSPACE_DIR),
        "path": file_path.relative_to(WORKSPACE_DIR).as_posix(),
        "line_number": line_number,
        "replace_all": replace_all,
        "replaced_count": replaced_count,
        "line_before": line_body,
        "line_after": updated_body,
        "file_size": int(file_path.stat().st_size),
    }


@mcp.tool()
def pdf_get_structure(pdf_path: str, max_toc_items: int = 2000) -> dict[str, Any]:
    """读取 PDF 元数据与目录结构（TOC）。"""
    if max_toc_items <= 0:
        raise ValueError("max_toc_items must be > 0.")
    if max_toc_items > 20000:
        raise ValueError("max_toc_items must be <= 20000.")

    resolved, reader = _read_pdf(pdf_path)
    metadata_raw = reader.metadata or {}
    metadata: dict[str, Any] = {}
    if isinstance(metadata_raw, dict):
        for k, v in metadata_raw.items():
            key = str(k).lstrip("/")
            metadata[key] = str(v) if v is not None else None

    items: list[dict[str, Any]] = []

    def walk_outline(nodes: list[Any], depth: int) -> None:
        for node in nodes:
            if len(items) >= max_toc_items:
                return
            if isinstance(node, list):
                walk_outline(node, depth + 1)
                continue

            title = str(getattr(node, "title", "")).strip()
            page_number: int | None = None
            try:
                page_number = int(reader.get_destination_page_number(node) + 1)
            except Exception:
                page_number = None
            items.append({"title": title, "page": page_number, "depth": depth})

    try:
        outline = reader.outline
        if isinstance(outline, list):
            walk_outline(outline, 1)
    except Exception:
        items = []

    return {
        "workspace": str(WORKSPACE_DIR),
        "path": resolved.relative_to(WORKSPACE_DIR).as_posix(),
        "page_count": len(reader.pages),
        "metadata": metadata,
        "toc_count": len(items),
        "toc_truncated": len(items) >= max_toc_items,
        "toc": items,
    }


@mcp.tool()
def pdf_read_pages(
    pdf_path: str,
    start_page: int = 1,
    max_pages: int = 10,
    ocr_fallback: bool = True,
) -> dict[str, Any]:
    """按页读取 PDF 文本内容，并返回每页文本与图像数量统计。"""
    if start_page <= 0:
        raise ValueError("start_page must be > 0.")
    if max_pages <= 0:
        raise ValueError("max_pages must be > 0.")
    if max_pages > 200:
        raise ValueError("max_pages must be <= 200.")

    resolved, reader = _read_pdf(pdf_path)
    total_pages = len(reader.pages)
    start_idx = start_page - 1
    end_idx = min(start_idx + max_pages, total_pages)

    pages: list[dict[str, Any]] = []
    ocr_doc, ocr_engine = _build_ocr_fallback_resources(resolved, ocr_fallback)
    try:
        for page_idx in range(start_idx, end_idx):
            page = reader.pages[page_idx]
            text, text_source = _extract_page_text_with_fallback(
                page_idx, page, ocr_fallback, ocr_doc, ocr_engine
            )
            image_count = 0
            try:
                image_count = len(list(page.images))
            except Exception:
                image_count = 0
            pages.append(
                {
                    "page": page_idx + 1,
                    "char_count": len(text),
                    "image_count": int(image_count),
                    "text_source": text_source,
                    "text": text,
                }
            )
    finally:
        if ocr_doc is not None:
            ocr_doc.close()

    return {
        "workspace": str(WORKSPACE_DIR),
        "path": resolved.relative_to(WORKSPACE_DIR).as_posix(),
        "start_page": start_page,
        "end_page": end_idx,
        "page_count": total_pages,
        "returned_pages": len(pages),
        "truncated": end_idx < total_pages,
        "ocr_fallback": ocr_fallback,
        "pages": pages,
    }


@mcp.tool()
def pdf_search_text(
    pdf_path: str,
    query: str,
    case_sensitive: bool = False,
    max_results: int = 200,
    start_page: int = 1,
    max_pages: int = 0,
    ocr_fallback: bool = True,
) -> dict[str, Any]:
    """在 PDF 文本中搜索关键词，返回页码与片段。"""
    if not query or not query.strip():
        raise ValueError("query cannot be empty.")
    if max_results <= 0:
        raise ValueError("max_results must be > 0.")
    if max_results > 5000:
        raise ValueError("max_results must be <= 5000.")
    if start_page <= 0:
        raise ValueError("start_page must be > 0.")
    if max_pages < 0:
        raise ValueError("max_pages must be >= 0.")

    resolved, reader = _read_pdf(pdf_path)
    total_pages = len(reader.pages)
    start_idx = start_page - 1
    if max_pages == 0:
        end_idx = total_pages
    else:
        end_idx = min(start_idx + max_pages, total_pages)

    needle = query if case_sensitive else query.lower()
    matches: list[dict[str, Any]] = []
    truncated = False
    ocr_doc, ocr_engine = _build_ocr_fallback_resources(resolved, ocr_fallback)
    try:
        for page_idx in range(start_idx, end_idx):
            page = reader.pages[page_idx]
            text, text_source = _extract_page_text_with_fallback(
                page_idx, page, ocr_fallback, ocr_doc, ocr_engine
            )
            haystack = text if case_sensitive else text.lower()
            from_idx = 0
            while True:
                at = haystack.find(needle, from_idx)
                if at < 0:
                    break
                snippet_start = max(0, at - _PDF_SEARCH_SNIPPET_CONTEXT_CHARS)
                snippet_end = min(
                    len(text), at + len(query) + _PDF_SEARCH_SNIPPET_CONTEXT_CHARS
                )
                snippet = text[snippet_start:snippet_end]
                matches.append(
                    {
                        "page": page_idx + 1,
                        "offset": at,
                        "text_source": text_source,
                        "snippet": snippet,
                    }
                )
                if len(matches) >= max_results:
                    truncated = True
                    break
                from_idx = at + max(1, len(needle))
            if truncated:
                break
    finally:
        if ocr_doc is not None:
            ocr_doc.close()

    return {
        "workspace": str(WORKSPACE_DIR),
        "path": resolved.relative_to(WORKSPACE_DIR).as_posix(),
        "query": query,
        "case_sensitive": case_sensitive,
        "ocr_fallback": ocr_fallback,
        "start_page": start_page,
        "end_page": end_idx,
        "searched_pages": max(0, end_idx - start_idx),
        "returned": len(matches),
        "truncated": truncated,
        "matches": matches,
    }


@mcp.tool()
def pdf_extract_content(
    pdf_path: str,
    start_page: int = 1,
    max_pages: int = 20,
    ocr_fallback: bool = True,
) -> dict[str, Any]:
    """提取 PDF 内容并输出分页拼接文本。"""
    if start_page <= 0:
        raise ValueError("start_page must be > 0.")
    if max_pages <= 0:
        raise ValueError("max_pages must be > 0.")
    if max_pages > 500:
        raise ValueError("max_pages must be <= 500.")

    resolved, reader = _read_pdf(pdf_path)
    total_pages = len(reader.pages)
    start_idx = start_page - 1
    end_idx = min(start_idx + max_pages, total_pages)

    chunks: list[str] = []
    page_sources: list[dict[str, Any]] = []
    ocr_doc, ocr_engine = _build_ocr_fallback_resources(resolved, ocr_fallback)
    try:
        for page_idx in range(start_idx, end_idx):
            text, text_source = _extract_page_text_with_fallback(
                page_idx, reader.pages[page_idx], ocr_fallback, ocr_doc, ocr_engine
            )
            chunks.append(f"## Page {page_idx + 1}\n\n{text}".rstrip())
            page_sources.append({"page": page_idx + 1, "text_source": text_source})
    finally:
        if ocr_doc is not None:
            ocr_doc.close()
    content = "\n\n".join(chunks).strip()

    return {
        "workspace": str(WORKSPACE_DIR),
        "path": resolved.relative_to(WORKSPACE_DIR).as_posix(),
        "start_page": start_page,
        "end_page": end_idx,
        "page_count": total_pages,
        "truncated": end_idx < total_pages,
        "ocr_fallback": ocr_fallback,
        "page_sources": page_sources,
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
