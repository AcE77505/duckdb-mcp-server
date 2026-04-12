# duckdb-mcp-lan-server

一个可在局域网中访问的 MCP Streamable HTTP 服务，用 DuckDB 对大 CSV 做 SQL 分析和去重。

## 1) 安装依赖

```bash
cd tools/duckdb-mcp-lan-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) 启动服务（一条命令）

```bash
PORT=8000 python server.py
```

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

## 3) rikkahub（Android）配置

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

## 常见使用建议（针对约 26MB / 12.4 万行数据）

- 先用 `describe_csv` 看字段类型，再写 SQL。
- 查询时先加 `LIMIT`，确认逻辑后再扩大范围。
- 去重时给出稳定的 `order_by`（例如时间列）以保证“保留哪条”可控。
