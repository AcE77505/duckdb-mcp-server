# duckdb-mcp-lan-server

一个可在局域网中访问的 MCP Streamable HTTP 服务，用 DuckDB 对大 CSV 做 SQL 分析和去重。

## 1) 一键启动（推荐）

Windows（PowerShell）：

```powershell
cd tools/duckdb-mcp-lan-server
.\start-server.ps1
```

说明：

- 首次运行会自动创建 `.venv` 并安装依赖
- 后续启动会复用 `.venv`，仅当 `requirements.txt` 变化时才重新安装依赖
- 脚本默认设置 `ENABLE_DNS_REBINDING_PROTECTION=0`
- 可选参数示例：`.\start-server.ps1 -Port 8000 -BindHost 0.0.0.0 -EnableDnsRebindingProtection 0`

## 2) 手动安装依赖（可选）

```bash
cd tools/duckdb-mcp-lan-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3) 手动启动服务（一条命令）

```bash
PORT=8000 python server.py
```

服务会读取同目录下的 `mcp.config.json` 来确定 MCP 工作目录：

```json
{
  "workspaceDir": "workspace"
}
```

- 默认工作目录：`server.py` 同目录下的 `workspace/`
- 若配置相对路径，会以 `server.py` 所在目录为基准解析
- 启动时会自动创建该目录

如果你在同一个终端里频繁切换传输类型（`streamable-http` / `sse`），可先清理当前会话中的相关环境变量：

```bash
source ./clear-mcp-env.sh
```

Windows（PowerShell）：

```powershell
.\clear-mcp-env.ps1
```

- 默认监听：`0.0.0.0`
- 默认端口：`8000`（可用环境变量 `PORT` 覆盖）
- 默认 MCP 传输：`streamable-http`
- Streamable HTTP URL 默认是：`http://<你的电脑IP>:8000/mcp`
- 可选环境变量：
  - `HOST`（默认 `0.0.0.0`）
  - `MCP_TRANSPORT`（默认 `streamable-http`；可选 `sse`）
  - `MCP_PATH`（`streamable-http` 默认 `/mcp`；`sse` 默认 `/sse`）
  - `MCP_MESSAGE_PATH`（仅 `sse` 生效，默认 `/messages`）
  - `ALLOWED_HOSTS`（默认 `*`，逗号分隔）
  - `ENABLE_DNS_REBINDING_PROTECTION`（默认 `1`）
  - `DISABLE_DNS_REBINDING_PROTECTION`（兼容旧变量；当它为 `1` 且未设置 `ENABLE_*` 时会关闭防护）

## 4) rikkahub（Android）配置

手机和电脑连接同一 Wi‑Fi 后，在 rikkahub 的 MCP 配置页填写：

- **URL**: `http://<你的电脑局域网IP>:8000/mcp`
- **Headers**: 可留空（如你后续要加鉴权，再填）

示例（电脑 IP 为 `192.168.1.23`）：

- `http://192.168.1.23:8000/mcp`

如果你看到 `406 Not Acceptable` 且提示 `Client must accept text/event-stream`：

- 这通常表示客户端没有按 Streamable HTTP 方式发起请求（比如直接浏览器访问，或客户端协议不兼容）
- 可切换为 SSE 兼容模式再试：

```bash
MCP_TRANSPORT=sse MCP_PATH=/sse python server.py
```

然后在客户端把 URL 改为：`http://<你的电脑IP>:8000/sse`

> 安全提示：默认监听 `0.0.0.0` 会暴露到你的局域网，请仅在可信网络中使用，并避免把端口直接映射到公网。

## 可用工具

- `describe_csv(csv_path, table_name="tracks", ignore_errors=False)`
  - 返回推断字段和总行数
- `query_csv(csv_path, sql, table_name="tracks", max_rows=1000, ignore_errors=False)`
  - 对 CSV 执行 SQL（SQL 中使用 `table_name` 作为表名，`max_rows` 上限 10000）
- `deduplicate_csv(csv_path, key_columns, output_path=None, table_name="tracks", order_by=None, ignore_errors=False)`
  - 按 key 列去重，输出新的 CSV（默认输出到原文件同目录，文件名为 `<原文件名>.dedup.csv`）
- `duckdb_health()`
  - 返回 `{ ok, duckdbVersion, dbPath, time }`，用于连通性检测（`time` 为 ISO8601）
- `duckdb_list_tables(includeViews=true)`
  - 返回当前数据库中的表和视图：`{ tables, views }`
- `duckdb_describe(table)`
  - 返回指定表结构：`{ table, columns: [{ name, type, nullable }] }`
- `duckdb_preview(table="tracks", limit=50)`
  - 预览前 N 行：`{ table, rows, rowCount }`
- `duckdb_dedup_exact(table="tracks", outTable="tracks_dedup_exact")`
  - 整行去重并写入新表：`CREATE OR REPLACE TABLE outTable AS SELECT DISTINCT * FROM table`
- `duckdb_dedup_consecutive(table="tracks", outTable="tracks_dedup_consecutive", keys?, partitionBy?, orderBy?)`
  - 按顺序去除连续重复记录，支持自定义 `keys`、`partitionBy`、`orderBy`
  - 默认：`keys=["lat","lon","height","speed","angle","vspeed"]`、`partitionBy=["fnum"]`、`orderBy="u_time"`
  - 若 `keys` 中含不存在列，会返回清晰错误并给出缺失列名
- `workspace_list_files(path=".", recursive=true, include_dirs=false, max_entries=1000)`
  - 列出工作区文件/目录（路径限制在 `workspaceDir` 内）
- `workspace_search_files(query, path=".", file_glob="*", case_sensitive=false, max_results=200)`
  - 在工作区中搜索 UTF-8 文本文件内容，返回匹配文件、行号与行内容
- `workspace_read_text_file(path, start_line=1, max_lines=2000)`
  - 以文本方式读取工作区内 UTF-8 文件，支持按行分页读取
- `workspace_write_text_file(content, append=false)`
  - 写入工作区内已存在的 `add.txt`（仅允许该文件；`append=false` 覆盖写入，`append=true` 追加写入）
- `workspace_replace_text_in_line(line_number, old_text, new_text, replace_all=false)`
  - 仅在 `add.txt` 的指定行内替换文本片段；`replace_all=false` 仅替换首个匹配，`true` 替换该行全部匹配
- `pdf_get_structure(pdf_path, max_toc_items=2000)`
  - 读取 PDF 的元数据、总页数与目录结构（TOC）
- `pdf_read_pages(pdf_path, start_page=1, max_pages=10, ocr_fallback=true)`
  - 按页读取 PDF 文本，并返回每页文本长度与图像数量；检测到 FzBookMaker 类乱码时可自动 OCR 回退
- `pdf_search_text(pdf_path, query, case_sensitive=false, max_results=200, start_page=1, max_pages=0, ocr_fallback=true)`
  - 在 PDF 文本中搜索关键词，返回页码、偏移量和上下文片段；乱码页可自动 OCR 回退后再检索
- `pdf_extract_content(pdf_path, start_page=1, max_pages=20, ocr_fallback=true)`
  - 批量提取 PDF 文本内容并按页拼接，适合学术文献阅读与后续处理；乱码页可自动 OCR 回退

## 常见使用建议（针对约 26MB / 12.4 万行数据）

- 先用 `describe_csv` 看字段类型，再写 SQL。
- 查询时先加 `LIMIT`，确认逻辑后再扩大范围。
- 去重时给出稳定的 `order_by`（例如时间列）以保证“保留哪条”可控。
