# Dock-to-Door — autonomous exception handling for last-mile freight

A driver sends a photo, a voice note or text. Agents triage it, re-plan the fleet, act, and learn from the outcome.
Humans only get pulled in when confidence is low, cost is high, or the cold chain would breach.

## Run (2 minutes)
```bash
pip install -r requirements.txt
cp .env.example .env        # optional: add GOOGLE_API_KEY (Gemini) and GOOGLE_MAPS_API_KEY (Routes API)
python run.py               # http://127.0.0.1:8000
```
The UI is an Angular app in `web/`; the built bundle is committed under `app/static/` and served by the same FastAPI process.
To change it: `cd web && npm install && npm run watch` (rebuilds into `app/static/` on save; `npm run build` for a one-off).
No keys? It still runs end to end in **offline mode** (rule-based triage, haversine ETAs) over the *same* tools and DB.
The header badges show which mode you're in.

## Architecture
```
Browser (photo / mic -> WAV / text) -> FastAPI -> pipeline
  ADK:  triage_agent (Gemini multimodal, structured output)
          -> SequentialAgent[ planner_agent , action_agent ]
                    \__ McpToolset (stdio) __/
                         app/mcp_server.py  (8 tools)
                                |
                       app/tools.py  --  SQLite WAL  (+ Google Routes API)
```
- **MCP tools**: get_shipment_state, get_telemetry_summary, recall_similar_incidents, find_alternate_vehicles, reroute, reserve_dock_slot, notify, log_decision.
  Planner gets the read tools, action agent the write tools (`tool_filter`).
  Use the same server from Claude Desktop: `python -m app.mcp_server` (stdio) or `--http`.
- **Safety policy lives in `reroute`**, not in prompts: confidence < `MIN_CONFIDENCE`, cost > `AUTO_COST_LIMIT_INR`, or cold-chain breach before transfer => `pending_approval`.
  Constraints (capacity, reefer class, availability) are re-checked server-side; vehicle assignment is an atomic conditional UPDATE, so two incidents can't claim one truck.
- **Shortest path**: Google **Routes API** `computeRoutes` (traffic-aware) for every leg; the planner compares roadside vs depot-ahead transfer per candidate and picks the lowest total. The LLM never computes routes.
- **Multi-modal**: photo + WAV voice note go to Gemini as inline parts; telemetry cross-checks the driver's claim (healthy reefer telemetry lowers confidence).
- **Feedback loop**: dispatcher approve / override (+reason) / thumbs; "Simulate outcome" stands in for delivery confirmation; **Run learning job** compares predicted vs actual per incident type and writes rules
  (ETA buffer %, confidence penalty after repeated overrides). `reroute` and `recall_similar_incidents` apply them on the next incident.

## 5-minute demo script
1. Preset **Reefer failing + crushed pallets** (TRK-3) -> Send. Watch the timeline; the map shows the replacement route (dashed) and the transfer point. Auto-executed, 4 messages sent.
2. Preset **Vague report** (TRK-1) -> confidence 0.35 -> escalated with a pre-built recommendation. Click **Approve** or **Override**.
3. Preset **Engine seized** and **Road flooded** -> swap and detour paths.
4. On executed decisions click **Simulate outcome**; once two reefer cases are resolved click **Run learning job** -> rule "+29% ETA buffer". Next reefer plan is more conservative.
5. Upload a damage photo and record a voice note (Gemini mode) to see multimodal triage.

## Honest notes
- Verified here: DB, tools, MCP server <-> ADK `McpToolset` over stdio, all four scenarios, approve/override/learn, ADK-failure fallback, JS syntax.
  **Not exercised here: live Gemini calls** (no key in my sandbox). If the first ADK run errors, the app falls back to offline and logs the error in the timeline. Check `GEMINI_MODEL` first.
- DB is **SQLite (WAL)** for zero-setup and multi-process safety (API + MCP subprocess). The access layer is in `db.py`; for production move `telemetry` and `decisions` to ClickHouse and hot vehicle state to Redis.
- ETAs use Routes API per candidate. Fleet-wide VRP via the **Route Optimization API** is the next step (needs a GCP project + OAuth), not wired.
- Notifications are simulated (stored + shown). Telemetry, fleet and delivery outcomes are simulated by `seed.py` / `simulate_outcome`.
