# Workspace

## Overview

pnpm workspace monorepo (TypeScript node services) + Python/FastAPI algorithmic trading bot.

## AlgoSoft Bot (Primary Project)

- **Location**: `bot/` directory
- **Runtime**: Python 3.12, FastAPI + Uvicorn on port 5000
- **Database**: SQLite at `bot/config/algosoft.db`
- **Config**: `bot/config/credentials.ini` — source of truth for broker credentials
- **Default admin**: `admin` / `Admin@123`
- **Workflow**: "AlgoSoft Bot" — `cd bot && python -m uvicorn web.server:app --host 0.0.0.0 --port 5000`

### Bot Architecture

- `bot/web/server.py` — FastAPI app, startup seeder, route registration
- `bot/web/admin_api.py` — All admin REST API endpoints (clients, plans, data providers)
- `bot/web/db.py` — SQLite schema: users, data_providers, subscription_plans, brokers, strategies
- `bot/hub/dual_feed_manager.py` — Dual WebSocket feed (Upstox + Dhan simultaneous)
- `bot/hub/provider_factory.py` — Creates provider instances from DB credentials
- `bot/utils/auth_manager_upstox.py` — Automated TOTP login for Upstox
- `bot/utils/auth_manager_dhan.py` — Token validation for Dhan (30-day tokens)

### Key API Endpoints

- `GET /api/admin/clients` — list all clients
- `GET/POST/PUT/DELETE /api/admin/plans` — subscription plans CRUD
- `GET /api/admin/data-providers/health` — feeder token health (days_remaining, expires_in, warn_expiry)
- `POST /api/admin/data-providers/{provider}/connect` — trigger background automated login
- `GET /api/admin/clients/{id}/trades/export?from_date=&to_date=` — download client trade history as CSV

### Static IP Enforcement (Task #55)

Each client can be assigned an Elastic IP (AWS) in the admin client detail page. The bot enforces source-IP binding at the socket level using:
- `SourceIPHTTPAdapter` in `bot/brokers/base_broker.py` — patches `init_poolmanager()` with `source_address=(ip,0)`
- `urllib3.util.connection.create_connection` monkey-patch as fallback for SDKs that bypass requests
- `_set_source_ip()` / `_clear_source_ip()` wrappers called around every broker auth call
- Upstox async path uses `aiohttp.TCPConnector(local_addr=(source_ip, 0))`

### Client Dashboard UX (Task #60)

- Broker tabs show coloured status dots: green=running+fresh, yellow=configured+stale, red=stale/error
- Stale token banner auto-shows with "Refresh Token →" button when any broker token expires
- `upstox-totp` warning removed from startup logs (package does not exist on PyPI)

### Subscription Plans

Three default plans seeded in DB: FREE (1 broker), PREMIUM (3 brokers), PRO (5 brokers). Full CRUD at `/admin/subscription-plans`. Tier changes validated against `subscription_plans` table in DB.

### Global Data Feeders

- **Upstox**: Daily access token via TOTP automation. Auto-renews at 09:01 AM IST via scheduler. Status: `not_configured` until first automated login.
- **Dhan**: 30-day access token from credentials.ini. Status: `configured` when token is present and valid.

---

## TypeScript Monorepo (Secondary)

- **Monorepo tool**: pnpm workspaces
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM

### Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure details.

---

## GitHub Auto-Push

Every commit on `main` is automatically pushed to GitHub after it is made.

- **Python script**: `scripts/github_push.py` — uses dulwich to push `main` to GitHub; token is never exposed in process list; push output is suppressed
- **Shell wrapper**: `scripts/github-push.sh` — discovers the correct Python with dulwich and invokes the Python script
- **Hook**: `.git/hooks/post-commit` — triggers the wrapper in the background after each commit (non-blocking, always exits 0)
- **Log**: `logs/github-push.log` — all push results (success and failure) are logged here
- **Env vars required**:
  - `GITHUB_TOKEN` — personal access token with repo write access (already configured as a secret)
  - `GITHUB_REPO` — repository in `owner/repo` format (e.g. `ssrajpal2001/Option_Selling_May_2026`, stored as shared env var)
- **Hook reinstall**: `scripts/post-merge.sh` reinstalls the hook automatically after each task merge so it is never lost
