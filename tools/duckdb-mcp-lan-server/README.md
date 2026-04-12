# duckdb-mcp-lan-server

一个可在局域网中访问的 MCP Streamable HTTP 服务，用 DuckDB 对大 CSV 做 SQL 分析和去重。

## 1) 安装依赖

```bash
cd /home/runner/work/duckdb-mcp-server/duckdb-mcp-server/tools/duckdb-mcp-lan-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) 启动服务（一条命令）

```bash
PORT=8000 python server.py
```

- 默认监听：`0.0.0.0`
- 默认端口：`8000`（可用环境变量 `PORT` 覆盖）
- MCP Streamable HTTP URL 默认是：`http://<你的电脑IP>:8000/mcp`
- 可选环境变量：
  - `HOST`（默认 `0.0.0.0`）
  - `MCP_PATH`（默认 `/mcp`）
  - `DISABLE_DNS_REBINDING_PROTECTION`（默认 `1`，便于局域网访问）

## 3) rikkahub（Android）配置

手机和电脑连接同一 Wi‑Fi 后，在 rikkahub 的 MCP 配置页填写：

- **URL**: `http://<你的电脑局域网IP>:8000/mcp`
- **Headers**: 可留空（如你后续要加鉴权，再填）

示例（电脑 IP 为 `192.168.1.23`）：

- `http://192.168.1.23:8000/mcp`

## 可用工具

- `describe_csv(csv_path, table_name="tracks")`
  - 返回推断字段和总行数
- `query_csv(csv_path, sql, table_name="tracks", max_rows=1000)`
  - 对 CSV 执行 SQL（SQL 中使用 `table_name` 作为表名）
- `deduplicate_csv(csv_path, key_columns, output_path=None, table_name="tracks", order_by=None)`
  - 按 key 列去重，输出新的 CSV（默认输出到原文件同目录，文件名追加 `.dedup`）

## 常见使用建议（针对约 26MB / 12.4 万行数据）

- 先用 `describe_csv` 看字段类型，再写 SQL。
- 查询时先加 `LIMIT`，确认逻辑后再扩大范围。
- 去重时给出稳定的 `order_by`（例如时间列）以保证“保留哪条”可控。
