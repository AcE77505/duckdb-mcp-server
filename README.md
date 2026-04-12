# duckdb-mcp-server

## Projects

- `tools/duckdb-mcp-lan-server`: 局域网可访问的 DuckDB MCP Streamable HTTP 服务（用于大 CSV SQL 分析与去重）

## 部署教程（duckdb-mcp-lan-server）

### 1) 安装依赖

```bash
cd /home/runner/work/duckdb-mcp-server/duckdb-mcp-server/tools/duckdb-mcp-lan-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows（PowerShell）：

```powershell
cd .\tools\duckdb-mcp-lan-server
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2) 启动服务

```bash
PORT=8000 python server.py
```

Windows（PowerShell）：

```powershell
$env:PORT=8000
python server.py
```

- 默认监听：`0.0.0.0`
- 默认端口：`8000`（可通过 `PORT` 覆盖）
- 默认 MCP 传输：`streamable-http`
- 默认 MCP 地址：`http://<你的电脑局域网IP>:8000/mcp`

### 3) 手机同 Wi‑Fi 连接（rikkahub）

- URL 填：`http://<你的电脑局域网IP>:8000/mcp`
- Headers 可留空

如果遇到 `406 Not Acceptable`（`Client must accept text/event-stream`），说明客户端与 Streamable HTTP 不兼容，可改用 SSE：

```powershell
$env:MCP_TRANSPORT="sse"
$env:MCP_PATH="/sse"
python server.py
```

客户端 URL 改为：`http://<你的电脑局域网IP>:8000/sse`

更多配置与工具说明见：
`/home/runner/work/duckdb-mcp-server/duckdb-mcp-server/tools/duckdb-mcp-lan-server/README.md`
