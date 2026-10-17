"""Two interchangeable pipelines over the SAME tool layer:
  - ADK:     Gemini multimodal triage agent -> planner agent -> action agent, tools via the MCP server (stdio)
  - offline: deterministic rules (no API key) calling the same functions, for dev/demo/fallback"""
import asyncio, json, os, re, sys
from typing import Literal
from pydantic import BaseModel
from . import db, tools
from .config import GOOGLE_API_KEY, MODEL, ROOT

APP = "dock_to_door"


class Triage(BaseModel):
    incident_type: Literal["reefer_failure", "damaged_pallet", "breakdown", "road_blockage", "unknown"]
    severity: Literal["low", "medium", "high", "critical"]
    damage_assessment: Literal["none", "salvageable", "partial_loss", "total_loss", "unknown"]
    pallets_affected: int
    vehicle_drivable: bool
    cold_chain_at_risk: bool
    estimated_delay_min: int
    transcript: str
    summary: str
    confidence: float


class Candidates(BaseModel):
    candidates: list[dict]   # vehicle_id, predicted_min, sla_ok, cold_ok, ... straight from the scout tool


class Assessment(BaseModel):
    viable: bool
    key_risk: str
    note: str = ""


class Plan(BaseModel):
    chosen_vehicle_id: str
    confidence: float
    reasoning: str


def _trim(x):
    s = json.dumps(x, default=str)
    return json.loads(s) if len(s) < 3500 else s[:3500] + "...[truncated]"


def trace(inc_id, author, kind, name, data):
    row = db.one("SELECT trace FROM incidents WHERE id=?", (inc_id,)) if inc_id else None
    if row is None:
        return
    tr = json.loads(row["trace"] or "[]")
    tr.append({"ts": tools.iso(tools.now()), "author": author, "kind": kind, "name": name, "data": _trim(data)})
    db.run("UPDATE incidents SET trace=? WHERE id=?", (json.dumps(tr), inc_id))


def save_triage(inc_id, t):
    db.run("UPDATE incidents SET triage=? WHERE id=?", (json.dumps(t), inc_id))


# ------------------------------------------------------------------ offline pipeline
KW = {"reefer_failure": ["reefer", "cooling", "refrigerat", "temperature", "compressor", "chiller", "not cold", "warm"],
      "breakdown": ["breakdown", "broke down", "engine", "tyre", "tire", "puncture", "clutch", "axle", "won't start", "not starting", "accident", "seized"],
      "damaged_pallet": ["damage", "crushed", "fell", "spill", "leak", "torn", "collapsed", "pallet"],
      "road_blockage": ["flood", "blocked", "landslide", "protest", "jam", "closure", "closed road"]}
NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}


def offline_triage(text, has_image, has_audio):
    low = text.lower()
    score = {k: sum(w in low for w in ws) for k, ws in KW.items()}
    typ, top = max(score.items(), key=lambda kv: kv[1])
    if top == 0:
        typ = "unknown"
    m = re.search(r"(\d+|" + "|".join(NUM) + r")\s+pallets?", low)
    pal = int(NUM.get(m.group(1), m.group(1))) if m else 1
    notes = []
    if has_audio:
        notes.append("voice note received but offline mode cannot transcribe")
    if has_image:
        notes.append("photo received but offline mode has no vision")
    return {"incident_type": typ, "severity": "high" if typ in ("reefer_failure", "breakdown") else "medium",
            "damage_assessment": "unknown" if has_image else ("partial_loss" if "crushed" in low or "damage" in low else "none"),
            "pallets_affected": pal, "vehicle_drivable": typ in ("reefer_failure", "damaged_pallet") and "won't" not in low,
            "cold_chain_at_risk": typ == "reefer_failure", "estimated_delay_min": 60,
            "transcript": text, "summary": f"Keyword triage: {typ} (score {top}). " + "; ".join(notes),
            "confidence": 0.85 if top >= 2 else 0.7 if top == 1 else 0.35}


def _call(inc, name, **kw):
    trace(inc, "offline_agent", "tool_call", name, kw)
    res = getattr(tools, name)(**kw)
    trace(inc, "offline_agent", "tool_result", name, res)
    return res


def offline(inc_id):
    inc = db.one("SELECT * FROM incidents WHERE id=?", (inc_id,))
    vid = inc["vehicle_id"]
    t = offline_triage(inc["text"] or "", bool(inc["image_path"]), bool(inc["audio_path"]))
    trace(inc_id, "offline_agent", "text", "triage", t)
    _call(inc_id, "get_shipment_state", vehicle_id=vid)
    tel = _call(inc_id, "get_telemetry_summary", vehicle_id=vid)
    if t["incident_type"] == "reefer_failure":  # cross-modal check: does telemetry corroborate the report?
        if tel.get("anomaly"):
            t["confidence"] = min(0.95, t["confidence"] + 0.08)
            trace(inc_id, "offline_agent", "text", "cross-check", "Telemetry confirms temperature anomaly -> confidence up")
        else:
            t["confidence"] = max(0.2, t["confidence"] - 0.3)
            trace(inc_id, "offline_agent", "text", "cross-check", "Telemetry does NOT show a temperature anomaly -> confidence down")
    save_triage(inc_id, t)
    _call(inc_id, "recall_similar_incidents", incident_type=t["incident_type"])
    res = _call(inc_id, "find_alternate_vehicles", incident_id=inc_id)
    cands = res.get("candidates", [])
    if not cands:
        trace(inc_id, "offline_agent", "text", "escalate", "No feasible replacement. Human dispatcher required.")
        _call(inc_id, "notify", incident_id=inc_id, party="dispatcher", message=f"No feasible vehicle for incident #{inc_id} ({t['incident_type']}). Manual action needed.", channel="dashboard")
        _call(inc_id, "log_decision", incident_id=inc_id, summary="No feasible replacement found; escalated.")
        return
    best = next((c for c in cands if c["cold_ok"]), cands[0])
    conf = t["confidence"] * (1.0 if best["sla_ok"] else 0.85)
    r = _call(inc_id, "reroute", incident_id=inc_id, replacement_vehicle_id=best["vehicle_id"], confidence=conf,
              reasoning=f"Lowest predicted recovery ({best['predicted_min']} min) among options meeting cold-chain constraints; {t['summary']}")
    if r["status"] == "executed":
        if r.get("meet_depot"):
            _call(inc_id, "reserve_dock_slot", depot_id=r["meet_depot"], incident_id=inc_id, start_in_min=30)
        for party, msg in tools.messages_for(r["decision_id"]).items():
            _call(inc_id, "notify", incident_id=inc_id, party=party, message=msg, channel="whatsapp")
        summ = f"Auto-executed: {r['replacement_vehicle_id']} meets at {r['meet_point']}, ETA {r['eta_local']}."
    elif r["status"] == "pending_approval":
        _call(inc_id, "notify", incident_id=inc_id, party="dispatcher", channel="dashboard",
              message=f"Approval needed: recommend {r['replacement_vehicle_id']} at {r['meet_point']}, ETA {r['eta_local']}. Reasons: {'; '.join(r['escalation_reasons'])}")
        summ = f"Escalated to dispatcher: {'; '.join(r['escalation_reasons'])}"
    else:
        summ = f"Plan rejected: {r.get('reason')}"
    _call(inc_id, "log_decision", incident_id=inc_id, summary=summ)


# ------------------------------------------------------------------ ADK pipeline
TRIAGE_INSTR = """You are the TRIAGE agent of a freight control tower. The driver of vehicle {vehicle_id} reported an incident (text, photo and/or voice note).
Inspect EVERY attachment. Voice notes may mix Tamil and English: transcribe into English in `transcript`.
For photos: judge pallet damage (none/salvageable/partial_loss/total_loss), count affected pallets, read visible labels.
Decide incident_type, severity, whether the truck is still drivable, whether cold chain is at risk, and the likely delay if it is a road blockage.
Confidence must be honest: lower it when evidence is missing, vague or contradictory between modalities.
Learned rules and past cases from the feedback loop (use them):
{similar_context}"""

SCOUT_INSTR = """You are the SCOUT agent. Incident #{incident_id} on vehicle {vehicle_id}. Triage result: {triage}
Do this in order: call get_shipment_state(vehicle_id), get_telemetry_summary(vehicle_id), recall_similar_incidents(triage.incident_type),
then find_alternate_vehicles(incident_id). Also call scan_fleet_anomalies if a cooling fault is in play.
Telemetry is ground truth: if the driver reports a cooling fault but telemetry shows none (or vice versa), note it.
List every candidate (vehicle_id, predicted_min, sla_ok, cold_ok, cost_inr) best first, as your structured output."""

ASSESS_TMPL = """You are a FEASIBILITY assessor. Incident #{inc_id}: vehicle {vid} ({cargo}, {temp_class}) needs a swap.
Your candidate is {cid} ({pred} min predicted, cold_ok={cold}, sla_ok={sla}, cost INR {cost}).
Cross-check it: call get_shipment_state({cid}) and get_telemetry_summary({vid}). Verify status, position and capacity make the transfer realistic
and that the cold chain survives until the handover. Reply with ONLY JSON: viable, key_risk (one line), note."""

JUDGE_INSTR = """You are the PLANNING agent. Incident #{incident_id} on vehicle {vehicle_id}. Triage: {triage}
Ranked candidates: {candidates}
Independent feasibility assessments: {assessments}
Choose the candidate that keeps the cold chain intact, then meets the SLA, then is fastest. Weigh the assessors' risks; telemetry is ground truth.
Reply with ONLY a JSON object with keys chosen_vehicle_id, confidence (0-1, never above triage confidence), reasoning (2-3 sentences)."""

ACT_INSTR = """You are the ACTION agent. Incident #{incident_id}. Triage: {triage}. Plan: {plan}
1. Call reroute(incident_id, replacement_vehicle_id=chosen_vehicle_id, confidence, reasoning). The tool enforces the safety policy.
2. If status is executed: if meet_depot is set call reserve_dock_slot(depot_id, incident_id); then call notify for driver, replacement_driver, customer and dispatcher with concise messages including meet point and ETA (never internal costs to the customer).
3. If status is pending_approval: do NOT retry or choose another vehicle. Call notify to dispatcher only, with your recommendation and the escalation reasons.
4. If rejected: notify the dispatcher with the reason.
5. Always finish with log_decision(incident_id, summary)."""


def _mcp(names):
    from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
    from mcp import StdioServerParameters
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], env=dict(os.environ), cwd=str(ROOT))
    return McpToolset(connection_params=StdioConnectionParams(server_params=params, timeout=30), tool_filter=names)


def _as_dict(v):
    if isinstance(v, dict):
        return v
    s = re.sub(r"^```(?:json)?|```$", "", str(v).strip(), flags=re.M).strip()
    return json.loads(s)


async def _consume(runner, uid, sid, content, inc_id):
    async for ev in runner.run_async(user_id=uid, session_id=sid, new_message=content):
        for fc in ev.get_function_calls():
            trace(inc_id, ev.author, "tool_call", fc.name, dict(fc.args or {}))
        for fr in ev.get_function_responses():
            trace(inc_id, ev.author, "tool_result", fr.name, fr.response)
        if ev.is_final_response() and ev.content and ev.content.parts:
            txt = "".join(p.text or "" for p in ev.content.parts if getattr(p, "text", None))
            if txt.strip():
                trace(inc_id, ev.author, "text", "final", txt)


async def adk(inc_id, image, audio):
    from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    inc = db.one("SELECT * FROM incidents WHERE id=?", (inc_id,))
    vid, uid, sid = inc["vehicle_id"], "dispatcher", f"inc-{inc_id}"
    ship = db.one("SELECT * FROM shipments WHERE vehicle_id=? AND status IN ('in_transit','recovering')", (vid,)) or {}
    rules = db.q("SELECT text FROM rules")
    ctx = "\n".join(r["text"] for r in rules) or "(none yet)"
    svc = InMemorySessionService()
    await svc.create_session(app_name=APP, user_id=uid, session_id=sid, state={"incident_id": inc_id, "vehicle_id": vid, "similar_context": ctx})

    parts = [types.Part(text=f"Vehicle {vid}, cargo: {ship.get('cargo')} ({ship.get('temp_class')}, {ship.get('pallets')} pallets). Driver message: {inc['text'] or '(none)'}")]
    if image:
        parts.append(types.Part.from_bytes(data=image[0], mime_type=image[1]))
    if audio:
        parts.append(types.Part.from_bytes(data=audio, mime_type="audio/wav"))
    triage_agent = LlmAgent(name="triage_agent", model=MODEL, instruction=TRIAGE_INSTR, output_schema=Triage, output_key="triage")
    await _consume(Runner(agent=triage_agent, app_name=APP, session_service=svc), uid, sid, types.Content(role="user", parts=parts), inc_id)
    sess = await svc.get_session(app_name=APP, user_id=uid, session_id=sid)
    save_triage(inc_id, _as_dict(sess.state["triage"]))

    read_ts, act_ts = _mcp(["get_shipment_state", "get_telemetry_summary", "recall_similar_incidents", "find_alternate_vehicles", "scan_fleet_anomalies"]), \
        _mcp(["reroute", "reserve_dock_slot", "notify", "log_decision"])
    extra_ts = []
    async def _state():
        return (await svc.get_session(app_name=APP, user_id=uid, session_id=sid)).state
    try:
        # 1) scout: read tools -> ranked candidates
        scout = LlmAgent(name="scout_agent", model=MODEL, instruction=SCOUT_INSTR, tools=[read_ts], output_schema=Candidates, output_key="candidates")
        await _consume(Runner(agent=scout, app_name=APP, session_service=svc), uid, sid,
                       types.Content(role="user", parts=[types.Part(text=f"Scout recovery options for incident {inc_id}.")]), inc_id)
        cands = [c for c in (_as_dict((await _state()).get("candidates") or {}).get("candidates") or []) if isinstance(c, dict) and c.get("vehicle_id")][:3]
        if not cands:
            trace(inc_id, "adk_agent", "text", "escalate", "No feasible replacement. Human dispatcher required.")
            tools.notify(incident_id=inc_id, party="dispatcher", channel="dashboard",
                         message=f"No feasible vehicle for incident #{inc_id}. Manual action needed.")
            tools.log_decision(incident_id=inc_id, summary="No feasible replacement found; escalated.")
            return
        # 2) fan-out: one feasibility assessor per candidate, in parallel (each with its own stdio MCP toolset)
        assessors = []
        for c in cands:
            ats = _mcp(["get_shipment_state", "get_telemetry_summary"])
            extra_ts.append(ats)
            assessors.append(LlmAgent(name=f"assess_{c['vehicle_id']}".replace("-", "_"), model=MODEL,
                                      instruction=ASSESS_TMPL.format(inc_id=inc_id, vid=vid, cargo=ship.get("cargo"), temp_class=ship.get("temp_class"),
                                                                     cid=c["vehicle_id"], pred=c.get("predicted_min"), cold=c.get("cold_ok"), sla=c.get("sla_ok"), cost=c.get("cost_inr")),
                                      tools=[ats], output_schema=Assessment, output_key=f"assess_{c['vehicle_id']}"))
        assessments = {}
        try:
            await _consume(Runner(agent=ParallelAgent(name="feasibility_fanout", sub_agents=assessors), app_name=APP, session_service=svc), uid, sid,
                           types.Content(role="user", parts=[types.Part(text=f"Assess every candidate for incident {inc_id} now, in parallel.")]), inc_id)
            st = await _state()
            assessments = {c["vehicle_id"]: st.get(f"assess_{c['vehicle_id']}") for c in cands if st.get(f"assess_{c['vehicle_id']}")}
        except Exception as e:
            trace(inc_id, "system", "text", "fanout_error", f"{type(e).__name__}: {e}; judge proceeds on scout data alone")
        # 3+4) judge, then act
        t = _as_dict((await _state()).get("triage") or {})
        judge = LlmAgent(name="planner_agent", model=MODEL, output_schema=Plan, output_key="plan",
                         instruction=JUDGE_INSTR.format(incident_id=inc_id, vehicle_id=vid, triage=json.dumps(t, default=str)[:900],
                                                       candidates=json.dumps(cands, default=str)[:900], assessments=json.dumps(assessments, default=str)[:900]))
        actor = LlmAgent(name="action_agent", model=MODEL, instruction=ACT_INSTR, tools=[act_ts])
        await _consume(Runner(agent=SequentialAgent(name="plan_and_act", sub_agents=[judge, actor]), app_name=APP, session_service=svc), uid, sid,
                       types.Content(role="user", parts=[types.Part(text=f"Plan and act on incident {inc_id}.")]), inc_id)
    finally:
        for ts in (read_ts, act_ts, *extra_ts):
            try:
                await ts.close()
            except Exception:
                pass


# ------------------------------------------------------------------ entry point
async def run(inc_id, mode, image, audio):
    try:
        use_adk = mode == "adk" or (mode == "auto" and bool(GOOGLE_API_KEY))
        db.run("UPDATE incidents SET mode=? WHERE id=?", ("adk" if use_adk else "offline", inc_id))
        if use_adk:
            try:
                await adk(inc_id, image, audio)
            except Exception as e:
                trace(inc_id, "system", "text", "adk_error", f"{type(e).__name__}: {e}")
                if db.one("SELECT 1 x FROM decisions WHERE incident_id=?", (inc_id,)):
                    raise
                trace(inc_id, "system", "text", "fallback", "Falling back to the offline rules pipeline")
                db.run("UPDATE incidents SET mode='offline(fallback)' WHERE id=?", (inc_id,))
                await asyncio.to_thread(offline, inc_id)
        else:
            await asyncio.to_thread(offline, inc_id)
    except Exception as e:
        db.run("UPDATE incidents SET error=? WHERE id=?", (f"{type(e).__name__}: {e}", inc_id))
        trace(inc_id, "system", "text", "error", str(e))
    finally:
        db.run("UPDATE incidents SET status='needs_human' WHERE id=? AND status='running'", (inc_id,))
