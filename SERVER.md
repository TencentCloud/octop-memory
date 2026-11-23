# Standalone memory service

`octop-memory serve` exposes the memory engine over HTTP MCP and adds spaces,
tokens and an admin API, so clients (Codex, Cursor, DSH, Octop experts) can share
one memory space without an Octop account or process.

## Layout

```
POST /mcp/          MCP (Streamable HTTP, stateless) — alias POST /mcp/memory/
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

- SQLite: one database file per space (`spaces/<space>.db`); the control store is separate.
- PostgreSQL reuses the `namespace` column on the shared tables. The backend
  lowercases space IDs and replaces characters outside `a-z`, `0-9`, and `_` with
  `_`. Choose IDs with distinct normalized namespaces across the control store,
  even when using different DSNs. Registration rejects conflicts, and the
  service refuses to open PostgreSQL spaces with conflicts already on record.
- `backend` selects the default for new spaces; `spaces.<id>` overrides per space.

## CLI

```bash
octop-memory space create project-alpha --backend sqlite
octop-memory space list
octop-memory token create --space project-alpha --name codex-laptop --scope read,write
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

MCP tokens can only access spaces registered in the control store.

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

## Admin API (used by CLI and management UIs)

```
POST /api/admin/session/exchange     {code} -> 7-day space admin session
GET  /api/admin/session
GET  /api/admin/spaces
POST /api/admin/spaces               {space_id, display_name, backend?}
GET  /api/admin/spaces/{id}/tokens
POST /api/admin/spaces/{id}/tokens   {client_name, scopes}
PATCH /api/admin/tokens/{id}         {client_name?, scopes?, disabled_clients?, space_id?}
POST /api/admin/tokens/{id}/rotate
DELETE /api/admin/tokens/{id}
GET  /api/admin/spaces/{id}/clients
```

A management UI exchanges a one-time code from the local admin CLI for a
short-lived session and calls this API to issue tokens. A space-scoped session
lists and manages its own space. Creating spaces requires a session from
`octop-memory auth code --all`. Re-registering a space the caller already
manages returns HTTP 409. Token moves and imports require permission for every
affected space. Managing a legacy token with multiple grants also requires
permission for all of its spaces.

## Legacy Octop token migration

```bash
python tools/migrate_octop_tokens.py --octop-db ~/.octop/octop.db --dry-run
python tools/migrate_octop_tokens.py --octop-db ~/.octop/octop.db
```

- One space per expert: `octop-expert-<agent_id>`
- sha256 digest is copied, so the **same plaintext keeps working**
- Single-expert tokens → primary space; multi-expert tokens → primary space +
  `memory_space_grants`; unrestricted tokens → snapshot of the user's experts at
  migration time
- `memory_token_map` records the old → new id mapping

## Current limits

- Control store is SQLite (not yet configurable to PostgreSQL).
- Extraction needs a configured LLM adapter; without one, capture still works and
  extraction fails gracefully.
- Only HTTP (TLS termination is expected in front of the service).
- Octop panel integration is a separate change.
