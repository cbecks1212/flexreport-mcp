# flexreport-mcp

A standalone **MCP microservice** that exposes the [FlexReport](https://flexreportfinapi.com)
equity backend's **live earnings events** and **research-report artifacts** as on-demand
tools for Claude (and any MCP client).

It is a thin, stateless proxy over the backend's public HTTP API. It holds **no
credentials** and never touches AWS/Redis/DB — it forwards the caller's bearer JWT
so the backend handles auth, plan quota, and rate limits. No code is shared with the
API repo; the only contract is the HTTP API + a JWT.

## Tools

| Tool | Backend endpoint | What it does |
|---|---|---|
| `list_realtime_events(event_type, tickers, sector, industry, market_cap)` | `POST /get-realtime-events` | Pull live events (EPS updates, transcripts, ratings, …) from the 12h cache |
| `generate_report(ticker, overrides)` | `POST /create-full-report` | Start a structured report job for a ticker → `{ticker: {task_id, status}}` |
| `generate_research_report(query, delivery)` | `POST /generate-research-report` | Start a report job from a plain-English query → `{task_id, status}` |
| `get_task_status(task_id)` | `GET /task-status` | Poll an async job to `SUCCESS` and read its `result` |
| `get_report_artifact(symbols)` | `POST /get-cached-reports` | Bulk-fetch cached PDFs (base64) + a `missing` list |

Typical agent loop: **discover** events → **generate** a report → **poll** status → **fetch** artifact.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set API_BASE_URL, MCP_HOST, MCP_PORT
set -a && source .env && set +a
python server.py              # serves streamable-http on http://MCP_HOST:MCP_PORT/mcp
```

## Auth

1. Get a JWT once from the backend: `POST /token` (OAuth2 password flow).
2. Configure it in your MCP client as the `Authorization: Bearer <JWT>` header.
3. The agent's calls carry that header → this server forwards it → the backend
   authenticates and meters the request as that user.

## Wire into an MCP client

`.mcp.json` (Claude Code):

```json
{
  "mcpServers": {
    "flexreport": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <YOUR_JWT>" }
    }
  }
}
```

## Verify with MCP Inspector

```bash
npx @modelcontextprotocol/inspector
# Connect to http://localhost:8000/mcp with header Authorization: Bearer <JWT>
# Confirm 5 tools list, then exercise:
#   list_realtime_events("eps_update")        -> events (or [])
#   generate_report("AAPL")                   -> read ["AAPL"]["task_id"]
#   get_task_status(task_id)                  -> eventually SUCCESS
#   get_report_artifact(["AAPL"])             -> base64 (or missing)
# Negative: call any JWT tool with no token   -> clean {"error": ...}, no crash
```

## Deploy

Build the image and run it as its own container (e.g. a separate ECS service with
its own task definition), independent of the API and Celery workers. Set
`API_BASE_URL` to the deployed backend URL.

```bash
docker build -t flexreport-mcp .
docker run -p 8000:8000 -e API_BASE_URL=https://flexreportfinapi.com flexreport-mcp
```
