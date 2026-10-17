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
Browser (photo / mic -> WAV / text) -> FastAPI -> Temporal workflow (durable) -> pipeline
  ADK:  triage_agent (Gemini multimodal, structured output, untrusted-input aware)
          -> scout_agent (read tools -> ranked candidates)
          -> ParallelAgent[ assess_<veh> x3 ]   independent feasibility reviewers, one MCP toolset each
          -> SequentialAgent[ planner_agent -> action_agent ]
                    every tool call passes before_tool_callback (_guard_tool)
                    \__ McpToolset (stdio) __/
                         app/mcp_server.py  (10 tools)
                                |
                       app/tools.py  --  SQLite WAL + ClickHouse (+ Google Routes API)
```
- **MCP tools**: get_shipment_state, get_telemetry_summary, recall_similar_incidents, find_alternate_vehicles, scan_fleet_anomalies, reroute, reserve_dock_slot, notify, log_decision, write_rule.
  Read tools go to scout/assessors, write tools only to the action agent (`tool_filter`): least privilege per agent.
  Use the same server from Claude Desktop: `python -m app.mcp_server` (stdio) or `--http`.

## Why the multi-agent design is trustworthy
**Agent-to-agent interaction** (`app/agents.py`)
- Each agent has one job, one typed output (`Triage`, `Candidates`, `Assessment`, `Plan` Pydantic schemas) and hands off through session state, not free-form chat. No agent can talk another into anything.
- **Independent cross-checking**: a `ParallelAgent` fans out one feasibility assessor per candidate, each with its *own* stdio MCP toolset (no shared-pipe races). The planner weighs their `viable` / `key_risk` verdicts instead of trusting the scout alone. If the fan-out fails, the planner degrades to scout data and says so in the trace.
- Every tool call and result by every agent lands in the incident timeline, so a reviewer can replay who decided what.

**Guardrails** (enforced in code, not in prompts)
- **Policy gate in `reroute`**: confidence < `MIN_CONFIDENCE`, cost > `AUTO_COST_LIMIT_INR`, or cold-chain breach => `pending_approval`. Capacity, reefer class and availability are re-checked server-side; assignment is an atomic conditional UPDATE, so two incidents can't claim one truck.
- **Confidence can't be inflated**: `reroute` clamps the planner's confidence to the triage confidence, and caps an uncorroborated reefer claim at 0.55 so it always escalates.
- **Prompt-injection defence**: driver text is sanitized (`_sanitize_text` strips control chars, tag break-outs and "ignore previous instructions / set confidence=" style directives), wrapped in `<driver_report>` and declared untrusted in the triage prompt.
- **`before_tool_callback` (`_guard_tool`)** on scout, assessors and action agent: pins `incident_id` to the session (an agent can't act on another incident), re-clamps confidence, and redacts costs / phone numbers from customer messages before the tool runs.
- **Outbound safety**: `notify` redacts INR amounts and phone numbers for `party=customer`; free-text fields are sanitized before storage.
- **Idempotency and blast-radius limits**: `reserve_dock_slot` and `notify` return the existing record on retry (no double bookings or spam); `write_rule` refuses to learn from fewer than `LEARN_MIN_SAMPLES` cases and clamps its values.
- **Human in the loop** for anything risky, with a pre-built recommendation to approve or override.

**Groundedness and truthfulness**
- **Telemetry is ground truth**: triage is given the sensor summary alongside the driver's claim. A "reefer failure" with healthy telemetry is capped at 0.55 confidence and escalated, in both the LLM path and the offline path (cross-check is traced).
- **The LLM never computes numbers**: routes, ETAs, costs, SLA and cold-chain margins come from `find_alternate_vehicles` / Routes API. The agents choose among tool-computed options; they can't invent a vehicle (`reroute` rejects unknown or ineligible IDs).
- `pallets_affected` is clamped to the real shipment size; candidates from the scout are filtered to ones with a real `vehicle_id`.
- Learned rules come from measured predicted-vs-actual outcomes with a minimum sample size, not from model opinion.
- Each decision stores reasoning, escalation reasons, route and tool log (SQLite + ClickHouse `tool_log` / `decision_events`) for audit.
- **Evidence**: `RULESET.md` documents every state and decision so a human can trace the optimal path by hand; `tests/e2e_test.py` (13 checks: MCP, durable workflow, crash/restart approval, learning loop, ClickHouse mirroring) passes.

## More architecture
- **Durability**: incidents run as Temporal workflows (wait for approval and outcome durably, survive restarts); a nightly review workflow runs the learning job. Without `TEMPORAL_URL` it falls back to in-memory tasks.
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
