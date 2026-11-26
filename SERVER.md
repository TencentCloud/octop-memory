# Standalone memory service

`octop-memory serve` exposes the memory engine over HTTP MCP and adds spaces,
tokens and an admin API, so clients (Codex, Cursor, DSH, Octop experts) can share
one memory space without an Octop account or process.

## Layout

```
POST /mcp/          MCP (Streamable HTTP, stateless)
GET  /api/admin/*   spaces, tokens, clients, session exchange
GET  /healthz       liveness + space count
```

## Configuration

`~/.octop-memory/config.json`:

```json
{
  "backend": "sqlite",
  "sqlite": {"db_path": "~/.octop-memory/memory.db"},
  "postgres": {"dsn": "postgresql://localhost/octop_memory"},
  "server": {"host": "127.0.0.1", "port": 8790},
  "control": {"backend": "sqlite", "sqlite": {"db_path": "~/.octop-memory/control.db"}},
  "spaces": {
    "project-alpha": {"backend": "sqlite"},
    "team-beta": {"backend": "postgres", "postgres": {"dsn": "postgresql://…"}}
  }
}
```

- SQLite spaces created through the CLI or admin API default to `spaces/<space>.db`.
  Spaces can share a file when their table prefixes differ. For example, `team-blue`
  and `team_blue` both use the prefix `team_blue` and need separate files. The service
  checks the underlying file at registration and before opening an existing space.
  When a matching prefix points to two different paths that do not exist yet, the
  check can create an empty database file to detect filesystem aliases.
  If two older spaces already share a prefix in one file, resolve the storage mapping
  and review any mixed records before restoring access.
- PostgreSQL reuses the `namespace` column on the shared tables. The backend
  lowercases space IDs and replaces characters outside `a-z`, `0-9`, and `_` with
  `_`. Choose IDs with distinct normalized namespaces across the control store,
  even when using different DSNs. Registration rejects conflicts, and the
  service refuses to open PostgreSQL spaces with conflicts already on record.
- The CLI and admin API use the service defaults, then the settings in `spaces.<id>`,
  then any explicitly supplied backend and connection settings.

## CLI

```bash
octop-memory space create project-alpha --backend sqlite
octop-memory space list
octop-memory token create --space project-alpha --name codex-laptop --scope read,write
octop-memory token create --space project-alpha --name cursor-laptop --scope read,write
octop-memory token list --space project-alpha
octop-memory token rotate <token_id>     # old plaintext stops working immediately
octop-memory token revoke <token_id>
octop-memory auth code --space project-alpha   # one-time code for a management UI
octop-memory serve --host 127.0.0.1 --port 8790
```

Tokens: `omcp_…`, stored as sha256 only, shown once at creation/rotation.
Scopes: `read` (search/get/raws/candidates), `write` (capture/save),
`review` (extract/promote/reject).

## MCP client

Issue a separate MCP token for each client installation in a space. The token ID identifies
the installation even when two installations have the same display name. Clients only need the bearer
token for that space. An optional `X-Octop-Space` must match the binding or the
request returns HTTP 403. Remove `X-Octop-Agent-Id` from older clients before
connecting. Requests with that header return HTTP 400.

```json
{
  "mcpServers": {
    "octop-memory": {
      "type": "http",
      "url": "http://127.0.0.1:8790/mcp/",
      "headers": {"Authorization": "Bearer omcp_…"}
    }
  }
}
```

Tool contract stays compatible with the Octop-hosted server: `memory_search`,
`memory_get`, `memory_save`, `memory_capture` (≤200 events per call, retries are
idempotent per `(space, session, caller, src_key)`), `memory_raws`,
`memory_candidates`, `memory_extract`, `memory_promote`, `memory_reject`.

`memory_search` defaults to `corpus="atom"`, which searches only durable memory.
Select `corpus="raw"` to search original events, or `corpus="all"` for the broader
recall policy with raw fallback. The runtime also accepts `memory`, `wiki`, and
`sessions`. Set `max_results` to limit the hit count. Every hit includes a `path`
for `memory_get`, which returns text in `content` and `excerpt`. Use `start`
(or `from_line`) and `lines` to read a specific range, with line numbers starting
at 1.

For the standalone server, set `recall.min_token_coverage` in its JSON configuration and restart the
service to apply it. The default is `0.0`. For atom searches, values between zero and one require
the corresponding fraction of distinct query tokens to occur in the snippet. Results may be empty.

## Admin API (used by CLI and management UIs)

```
POST /api/admin/session/exchange     {code} -> 7-day space admin session
GET  /api/admin/session
GET  /api/admin/spaces
POST /api/admin/spaces               {space_id, display_name?, backend?, backend_config?}
GET  /api/admin/spaces/{id}/tokens
POST /api/admin/spaces/{id}/tokens   {client_name, scopes}
PATCH /api/admin/tokens/{id}         {client_name?, scopes?, space_id?}
POST /api/admin/tokens/{id}/rotate
DELETE /api/admin/tokens/{id}
GET  /api/admin/spaces/{id}/clients
```

A management UI exchanges a one-time code from the local admin CLI for a
short-lived session and calls this API to issue tokens. A space-scoped session
lists and manages its own space. Creating spaces requires a session from
`octop-memory auth code --all`. A scoped session can confirm its own existing binding.
Registration returns HTTP 200 for the same space and database, including on retries.
A different storage binding returns HTTP 409. Clients must stop before issuing a token on conflict.
A retry keeps the original name, connection settings and tokens.

SQLite compares file paths and recognizes aliases for existing files. With the
PostgreSQL driver installed, registration compares connection parameters locally.
URI and keyword DSNs can match when they specify the same host, database, role
and routing settings. The comparison ignores passwords, client labels and
connection timeouts. Use the registered hostname for databases with DNS aliases.

Moving a token requires access to its current and
destination spaces. On the next request, the same token uses its new space and
its existing scopes. Issue a separate token for each additional space.

Disable an installation with `DELETE /api/admin/tokens/{id}` or
`octop-memory token revoke <token_id>`. Its next MCP request receives HTTP 401.
Other installations in the same space keep their tokens. To reconnect a revoked
installation, issue a fresh token. Rotate an active token to replace its secret.
The clients endpoint lists one row per token, including unused and revoked
installations. Usage counts belong to the token ID across label changes.

When upgrading an existing standalone control store, usage history is folded into
per-token counters. Tokens previously seen under multiple client names or carrying
any disabled-client entries are revoked. Replace them with one credential per
installation. Also replace any token you know was copied into multiple clients.
Other active tokens keep their recorded space and scopes. Clients with Octop-only
credentials need a new space token.

## Current limits

- Control store is SQLite (not yet configurable to PostgreSQL).
- Extraction needs a configured LLM adapter; without one, capture still works and
  extraction fails gracefully.
- Only HTTP (TLS termination is expected in front of the service).
- Octop panel integration is a separate change.
