"""Feedback loop: a REVIEW agent (ADK, falls back to offline statistics) that learns
from outcomes and dispatcher overrides and writes rules the planner applies next time.
Runs (1) after every incident outcome (Temporal workflow) and (2) nightly (Temporal timer)."""
import asyncio
import json
import statistics
from . import db
from .config import GOOGLE_API_KEY, MODEL
from .tools import learn  # offline fallback: pure statistics

REVIEW_INSTR = """You are the REVIEW agent of a freight feedback loop. You get resolved decisions
(predicted vs actual recovery minutes), human overrides, thumbs ratings, and the current learned rules.
Cluster the failure modes by incident_type and decide which rules to change.
Be conservative: only call write_rule for an incident_type when the evidence clearly supports it
(a repeated prediction miss or repeated dispatcher overrides). Keep unchanged rules as-is; small samples (<2) prove nothing.
Output: one short line of analysis per incident_type, then your write_rule calls."""


def _evidence():
    """Deterministic evidence pack: per-type stats the review agent reasons over."""
    out = {}
    for r in db.q("SELECT DISTINCT incident_type t FROM decisions WHERE actual_min IS NOT NULL OR status='overridden'"):
        t = r["t"]
        rows = db.q("SELECT predicted_min, predicted_raw_min, actual_min, sla_held, sla_remaining_min, status, rating, reasoning, vehicle_id, mode FROM decisions "
                    "WHERE incident_type=? AND (actual_min IS NOT NULL OR status='overridden') ORDER BY id DESC LIMIT 8", (t,))
        for d in rows:
            for k in ("predicted_min", "predicted_raw_min", "actual_min", "sla_remaining_min", "rating"):
                if d[k] is not None:
                    d[k] = round(d[k], 1)
        ov = db.one("SELECT COUNT(*) n FROM decisions WHERE incident_type=? AND status='overridden'", (t,))["n"]
        mean_ratio = round(statistics.mean(d["actual_min"] / d["predicted_raw_min"] for d in rows if d["actual_min"] and d["predicted_raw_min"]), 2) if any(d["actual_min"] and d["predicted_raw_min"] for d in rows) else None
        out[t] = {"resolved_recent": rows, "overrides": ov, "mean_actual_over_predicted": mean_ratio,
                  "current_rule": db.one("SELECT text, eta_buffer_pct, conf_penalty FROM rules WHERE incident_type=?", (t,))}
    return out


def review(inc_id=None):
    """Run the review agent now. ADK review agent when a Gemini key is set, else the offline statistics learner."""
    ev = _evidence()
    if not ev:
        return {"mode": "offline", "rules": ["no resolved decisions or overrides to learn from yet"], "evidence": {}}
    if GOOGLE_API_KEY:
        try:
            asyncio.run(_adk_review(ev, inc_id))
            mode = "adk"
        except Exception as e:
            if inc_id:
                from .agents import trace
                trace(inc_id, "system", "text", "review_error", f"{type(e).__name__}: {e}; offline learner used instead")
            learn()
            mode = "offline(fallback)"
    else:
        learn()
        mode = "offline"
    return {"mode": mode, "rules": [r["text"] for r in db.q("SELECT text FROM rules")], "evidence": ev}


async def _adk_review(ev, inc_id):
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from .agents import APP, _consume, _mcp
    ts = _mcp(["write_rule"])
    try:
        agent = LlmAgent(name="review_agent", model=MODEL, instruction=REVIEW_INSTR, tools=[ts])
        svc = InMemorySessionService()
        await svc.create_session(app_name=APP, user_id="reviewer", session_id="review")
        content = types.Content(role="user", parts=[types.Part(text="Evidence pack (JSON):\n" + json.dumps(ev, default=str)[:6000] + "\nReview it now.")])
        await _consume(Runner(agent=agent, app_name=APP, session_service=svc), "reviewer", "review", content, inc_id)
    finally:
        try:
            await ts.close()
        except Exception:
            pass
