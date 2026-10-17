"""Core logistics logic — single source of truth.
Exposed as MCP tools (mcp_server.py), called directly by the offline pipeline, and by REST (main.py).
Safety policy (confidence / cost / cold-chain gate) lives INSIDE `reroute`, so no prompt can bypass it."""
import functools, json, random, statistics
from datetime import datetime, timezone, timedelta
from . import ch, db, geo
from .config import AUTO_COST_LIMIT_INR, COST_PER_KM, HANDLING_MIN, LEARN_MIN_SAMPLES, MIN_CONFIDENCE

IST = timezone(timedelta(hours=5, minutes=30))
COLD_TOLERANCE_MIN = 60   # cumulative minutes above limit before pharma/dairy is written off
PARTIES = ("driver", "replacement_driver", "customer", "dispatcher")


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def clock(dt):
    return dt.astimezone(IST).strftime("%H:%M IST")


def audited(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        try:
            res = fn(*a, **k)
        except Exception as e:  # tools never raise into the agent
            res = {"error": f"{type(e).__name__}: {e}"}
        s_args, s_res = json.dumps([a, k], default=str)[:2000], json.dumps(res, default=str)[:4000]
        db.run("INSERT INTO tool_log VALUES(?,?,?,?)", (iso(now()), fn.__name__, s_args, s_res))
        ch.write("tool_log", {"ts": iso(now()), "tool": fn.__name__, "args": s_args, "result": s_res})
        return res
    return w


def _mirror(did, status=None):
    """Append a decision lifecycle event to ClickHouse (source of truth stays SQLite)."""
    d = db.one("SELECT * FROM decisions WHERE id=?", (did,))
    if d:
        ch.write("decision_events", {"ts": iso(now()), "decision_id": d["id"], "incident_id": d["incident_id"], "incident_type": d["incident_type"],
                                    "status": status or d["status"], "predicted_min": d["predicted_min"], "actual_min": d["actual_min"],
                                    "cost_inr": d["cost_inr"], "confidence": d["confidence"], "sla_ok": d["sla_ok"], "cold_ok": d["cold_ok"], "rating": d["rating"]})


# ---------------------------------------------------------------- helpers
def _shipment_for(vehicle_id):
    return db.one("SELECT * FROM shipments WHERE vehicle_id=? AND status IN ('in_transit','recovering') ORDER BY sla_ts LIMIT 1", (vehicle_id,))


def _sla_remaining(s):
    return (datetime.fromisoformat(s["sla_ts"]) - now()).total_seconds() / 60


def _rule(t):
    return db.one("SELECT * FROM rules WHERE incident_type=?", (t,)) or {}


def _ctx(incident_id):
    inc = db.one("SELECT * FROM incidents WHERE id=?", (incident_id,))
    if not inc:
        return None
    t = json.loads(inc["triage"]) if inc.get("triage") else {}
    raw = t.get("incident_type") or "unknown"
    return {"incident": inc, "triage": t, "vehicle_id": inc["vehicle_id"], "raw_type": raw,
            "type": "breakdown" if raw == "unknown" else raw,
            "pallets_affected": int(t.get("pallets_affected") or 0),
            "drivable": bool(t.get("vehicle_drivable")) and raw != "unknown",
            "delay_min": int(t.get("estimated_delay_min") or 60)}


def _telemetry(vehicle_id, minutes=30):
    rows = db.q("SELECT temp_c FROM telemetry WHERE vehicle_id=? AND ts>=? ORDER BY ts", (vehicle_id, iso(now() - timedelta(minutes=minutes))))
    s = _shipment_for(vehicle_id)
    if not rows or not s or s["temp_class"] != "reefer":
        return {"vehicle_id": vehicle_id, "cold_chain": False, "readings": len(rows)}
    lim, temps = s["max_temp_c"], [r["temp_c"] for r in rows]
    trend = (temps[-1] - temps[0]) / max(1, len(temps) - 1)
    above, latest = sum(1 for t in temps if t > lim), temps[-1]
    if latest > lim:
        spoil = max(0, COLD_TOLERANCE_MIN - above)
    elif trend > 0.02:
        spoil = (lim - latest) / trend + COLD_TOLERANCE_MIN
    else:
        spoil = None
    return {"vehicle_id": vehicle_id, "cold_chain": True, "readings": len(temps), "latest_temp_c": round(latest, 1), "limit_c": lim,
            "trend_c_per_min": round(trend, 2), "minutes_above_limit": above,
            "spoilage_in_min": None if spoil is None else round(spoil), "anomaly": bool(trend > 0.1 or latest > lim)}


def _depot_ahead(truck, dest):
    best = None
    for d in db.q("SELECT * FROM depots"):
        p = (d["lat"], d["lon"])
        if geo.haversine(p, dest) < geo.haversine(truck, dest) - 3 and geo.haversine(truck, p) <= 90:
            if not best or geo.haversine(truck, p) < geo.haversine(truck, (best["lat"], best["lon"])):
                best = d
    return best


def _plan_for(ctx, repl_id):
    vid, typ = ctx["vehicle_id"], ctx["type"]
    tv, ship = db.one("SELECT * FROM vehicles WHERE id=?", (vid,)), _shipment_for(vid)
    if not tv or not ship:
        return {"error": f"no active shipment for {vid}"}
    dest, truck = (ship["dest_lat"], ship["dest_lon"]), (tv["lat"], tv["lon"])
    buf = _rule(ctx["raw_type"]).get("eta_buffer_pct") or 0
    sla_rem = _sla_remaining(ship)
    direct = geo.route(truck, dest)
    p = {"vehicle_id": repl_id, "shipment_id": ship["id"], "customer": ship["customer"], "buffer_pct": buf, "sla_remaining_min": round(sla_rem)}

    if repl_id == vid:  # detour with the same truck
        if typ != "road_blockage":
            return {"error": "detour is only valid for road_blockage"}
        raw, cost, cold_ok, spoil = direct["minutes"] + ctx["delay_min"], ctx["delay_min"] * 20, True, None
        p.update(mode="detour", meet_label="continue on alternate route", meet_depot=None, meet_lat=None, meet_lon=None, t_transfer_min=0, pallets_moved=0)
        legs, src = [direct["points"]], direct["source"]
    else:
        rv = db.one("SELECT * FROM vehicles WHERE id=?", (repl_id,))
        if not rv:
            return {"error": f"unknown vehicle {repl_id}"}
        if rv["status"] != "available":
            return {"error": f"{repl_id} is {rv['status']}, not available"}
        move = ctx["pallets_affected"] if typ == "damaged_pallet" and ctx["pallets_affected"] > 0 else ship["pallets"]
        if rv["capacity"] < move:
            return {"error": f"{repl_id} capacity {rv['capacity']} < {move} pallets"}
        if ship["temp_class"] == "reefer" and rv["temp_class"] != "reefer":
            return {"error": f"{repl_id} is not a reefer; cargo needs 2-8C"}
        repl = (rv["lat"], rv["lon"])
        a = geo.route(repl, truck)
        opts = [{"label": "roadside at breakdown point", "depot": None, "meet": truck, "t_meet": a["minutes"], "l1": a, "l2": direct}]
        dep = _depot_ahead(truck, dest) if ctx["drivable"] else None
        if dep:
            dp = (dep["lat"], dep["lon"])
            tt, tr, l2 = geo.route(truck, dp), geo.route(repl, dp), geo.route(dp, dest)
            opts.append({"label": dep["name"], "depot": dep["id"], "meet": dp, "t_meet": max(tt["minutes"], tr["minutes"]), "l1": tr, "l2": l2})
        spoil = None
        if ship["temp_class"] == "reefer" and typ in ("reefer_failure", "breakdown"):
            spoil = _telemetry(vid).get("spoilage_in_min")
            spoil = COLD_TOLERANCE_MIN if spoil is None else spoil
        for o in opts:
            o["t_transfer"] = o["t_meet"] + HANDLING_MIN
            o["raw"] = o["t_transfer"] + o["l2"]["minutes"]
            o["cold_ok"] = spoil is None or o["t_transfer"] <= spoil
        best = min(opts, key=lambda o: (not o["cold_ok"], o["raw"]))
        raw, cold_ok = best["raw"], best["cold_ok"]
        cost = (best["l1"]["km"] + best["l2"]["km"]) * COST_PER_KM + 1500
        p.update(mode="swap", meet_label=best["label"], meet_depot=best["depot"], meet_lat=best["meet"][0], meet_lon=best["meet"][1],
                 t_transfer_min=round(best["t_transfer"]), pallets_moved=move, replacement_driver=rv["driver"])
        legs, src = [best["l1"]["points"], best["l2"]["points"]], best["l2"]["source"]
    pred = raw * (1 + buf / 100)
    p.update(predicted_raw_min=round(raw), predicted_min=round(pred), sla_ok=bool(pred <= sla_rem), cold_ok=bool(cold_ok),
             spoilage_in_min=spoil, cost_inr=round(cost), eta_local=clock(now() + timedelta(minutes=pred)), route=legs, route_source=src)
    return p


def _compact(p):
    return {k: v for k, v in p.items() if k != "route"}


def _execute(did):
    d = db.one("SELECT * FROM decisions WHERE id=?", (did,))
    with db.tx() as c:
        if d["mode"] == "swap":
            if c.execute("UPDATE vehicles SET status='dispatched' WHERE id=? AND status='available'", (d["vehicle_id"],)).rowcount == 0:
                return False  # lost the race: another incident grabbed this vehicle
        if d["incident_type"] in ("reefer_failure", "breakdown", "unknown"):
            c.execute("UPDATE vehicles SET status='out_of_service' WHERE id=?", (d["broken_vehicle"],))
        c.execute("UPDATE shipments SET status='recovering' WHERE id=?", (d["shipment_id"],))
        c.execute("UPDATE decisions SET status='executed' WHERE id=?", (did,))
        c.execute("UPDATE incidents SET status='actioned' WHERE id=?", (d["incident_id"],))
    _mirror(did, "executed")
    return True


def messages_for(did):
    d = db.one("SELECT * FROM decisions WHERE id=?", (did,))
    s = db.one("SELECT * FROM shipments WHERE id=?", (d["shipment_id"],))
    bv = db.one("SELECT * FROM vehicles WHERE id=?", (d["broken_vehicle"],))
    rv = db.one("SELECT * FROM vehicles WHERE id=?", (d["vehicle_id"],))
    late = "" if d["sla_ok"] else " This may miss the committed window; we are working on it."
    if d["mode"] == "detour":
        return {"driver": f"Take the alternate route avoiding the blockage. Updated ETA {d['eta_local']}.",
                "customer": f"Shipment {s['id']} is rerouted around a road blockage. New ETA {d['eta_local']}.{late}",
                "dispatcher": f"Auto-detour for {bv['id']} ({d['incident_type']}). ETA {d['eta_local']}, extra cost INR {d['cost_inr']:.0f}."}
    return {"driver": f"{rv['id']} ({rv['driver']}) will meet you at {d['meet_label']}. Keep cargo secured; transfer takes ~{HANDLING_MIN} min.",
            "replacement_driver": f"Proceed to {d['meet_label']}, take over shipment {s['id']} ({s['cargo']}), then deliver to {s['dest_name']}. Target ETA {d['eta_local']}.",
            "customer": f"Shipment {s['id']} ({s['cargo']}) hit a transit issue and is being moved to a replacement vehicle. New ETA {d['eta_local']}.{late}",
            "dispatcher": f"{d['incident_type']} on {bv['id']}: {rv['id']} dispatched, meet at {d['meet_label']}, ETA {d['eta_local']}, cost INR {d['cost_inr']:.0f}, confidence {d['confidence']:.2f}."}


def _post_actions(did):
    d = db.one("SELECT * FROM decisions WHERE id=?", (did,))
    if d["meet_depot"]:
        reserve_dock_slot(d["meet_depot"], d["incident_id"], 30)
    for party, msg in messages_for(did).items():
        notify(d["incident_id"], party, msg, "dashboard")


# ---------------------------------------------------------------- MCP tools (agent-facing)
@audited
def scan_fleet_anomalies(minutes: int = 10) -> dict:
    """ClickHouse fleet scan: reefer trucks with readings above their temperature limit or a rising drift in the last N minutes. Falls back to SQLite per-truck summaries when ClickHouse is off."""
    rows = ch.fleet_drift(minutes)
    if rows is not None:
        return {"window_min": minutes, "source": "clickhouse", "anomalies": rows}
    out = []
    for s in db.q("SELECT vehicle_id FROM shipments WHERE temp_class='reefer' AND status IN ('in_transit','recovering')"):
        t = _telemetry(s["vehicle_id"], minutes)
        if t.get("anomaly"):
            out.append(t)
    return {"window_min": minutes, "source": "sqlite", "anomalies": out}


@audited
def get_shipment_state(vehicle_id: str) -> dict:
    """Get a vehicle's position/status, its active shipment, SLA minutes remaining and baseline ETA to destination."""
    v = db.one("SELECT * FROM vehicles WHERE id=?", (vehicle_id,))
    if not v:
        return {"error": f"unknown vehicle {vehicle_id}"}
    s = _shipment_for(vehicle_id)
    out = {"vehicle": v, "shipment": s}
    if s:
        r = geo.route((v["lat"], v["lon"]), (s["dest_lat"], s["dest_lon"]))
        out.update(baseline_eta_min=round(r["minutes"]), sla_remaining_min=round(_sla_remaining(s)), route_source=r["source"])
    return out


@audited
def get_telemetry_summary(vehicle_id: str, minutes: int = 30) -> dict:
    """Summarise recent reefer temperature telemetry: latest temp, trend, minutes above limit and estimated minutes until spoilage."""
    return _telemetry(vehicle_id, minutes)


@audited
def recall_similar_incidents(incident_type: str) -> dict:
    """Recall resolved past incidents of this type (predicted vs actual recovery time) plus learned rules from the feedback loop."""
    past = db.q("SELECT incident_type, vehicle_id, predicted_raw_min, actual_min, sla_held, status, rating FROM decisions "
                "WHERE incident_type=? AND actual_min IS NOT NULL ORDER BY id DESC LIMIT 5", (incident_type,))
    r = _rule(incident_type)
    return {"learned_rule": r.get("text"), "eta_buffer_pct": r.get("eta_buffer_pct", 0), "past_incidents": past}


@audited
def find_alternate_vehicles(incident_id: int) -> dict:
    """Rank replacement options for an incident using Google Routes ETAs. Checks capacity, temperature class, cold-chain deadline and SLA. Best option first."""
    ctx = _ctx(incident_id)
    if not ctx:
        return {"error": "unknown incident"}
    ids = [ctx["vehicle_id"]] if ctx["raw_type"] == "road_blockage" else [v["id"] for v in db.q("SELECT id FROM vehicles WHERE status='available'")]
    plans, excluded = [], []
    for vid in ids:
        p = _plan_for(ctx, vid)
        (excluded.append({"vehicle_id": vid, "reason": p["error"]}) if "error" in p else plans.append(p))
    plans.sort(key=lambda p: (not p["cold_ok"], not p["sla_ok"], p["predicted_min"]))
    return {"incident_type": ctx["raw_type"], "candidates": [_compact(p) for p in plans], "excluded": excluded}


@audited
def reroute(incident_id: int, replacement_vehicle_id: str, confidence: float, reasoning: str) -> dict:
    """Commit a recovery plan. Server-side policy decides: executes automatically, or returns pending_approval (low confidence, high cost, or cold-chain breach). Pass the incident's own vehicle id to detour a road blockage."""
    ctx = _ctx(incident_id)
    if not ctx:
        return {"status": "rejected", "reason": "unknown incident"}
    if db.one("SELECT id FROM decisions WHERE incident_id=? AND status IN ('executed','pending_approval')", (incident_id,)):
        return {"status": "rejected", "reason": "incident already has an active decision"}
    p = _plan_for(ctx, replacement_vehicle_id)
    if "error" in p:
        return {"status": "rejected", "reason": p["error"]}
    conf = max(0.0, min(1.0, float(confidence)) - (_rule(ctx["raw_type"]).get("conf_penalty") or 0))
    why = []
    if conf < MIN_CONFIDENCE:
        why.append(f"confidence {conf:.2f} below threshold {MIN_CONFIDENCE}")
    if p["cost_inr"] > AUTO_COST_LIMIT_INR:
        why.append(f"cost INR {p['cost_inr']} exceeds auto limit {AUTO_COST_LIMIT_INR:.0f}")
    if not p["cold_ok"]:
        why.append("cold chain breaches before transfer completes; human should consider salvage or nearest cold store")
    status = "pending_approval" if why else "executed"
    did = db.insert("decisions", {"incident_id": incident_id, "ts": iso(now()), "incident_type": ctx["raw_type"], "mode": p["mode"], "shipment_id": p["shipment_id"],
                                  "broken_vehicle": ctx["vehicle_id"], "vehicle_id": replacement_vehicle_id, "meet_lat": p["meet_lat"], "meet_lon": p["meet_lon"],
                                  "meet_label": p["meet_label"], "meet_depot": p["meet_depot"], "predicted_raw_min": p["predicted_raw_min"] / (1 + p["buffer_pct"] / 100),
                                  "predicted_min": p["predicted_min"], "sla_remaining_min": p["sla_remaining_min"], "sla_ok": int(p["sla_ok"]), "cold_ok": int(p["cold_ok"]),
                                  "cost_inr": p["cost_inr"], "confidence": conf, "status": "pending_approval", "reasoning": reasoning[:1500],
                                  "escalation": "; ".join(why), "eta_local": p["eta_local"], "route": json.dumps(p["route"])})
    if status == "executed":
        if not _execute(did):
            db.run("UPDATE decisions SET status='failed' WHERE id=?", (did,))
            return {"status": "rejected", "reason": f"{replacement_vehicle_id} was just taken by another incident; pick another"}
    else:
        db.run("UPDATE incidents SET status='awaiting_approval' WHERE id=?", (incident_id,))
    _mirror(did)
    return {"status": status, "decision_id": did, "shipment_id": p["shipment_id"], "customer": p["customer"], "replacement_vehicle_id": replacement_vehicle_id,
            "replacement_driver": p.get("replacement_driver"), "meet_point": p["meet_label"], "meet_depot": p["meet_depot"], "predicted_total_min": p["predicted_min"],
            "eta_local": p["eta_local"], "sla_ok": p["sla_ok"], "cold_ok": p["cold_ok"], "cost_inr": p["cost_inr"], "effective_confidence": round(conf, 2), "escalation_reasons": why}


@audited
def reserve_dock_slot(depot_id: str, incident_id: int, start_in_min: int = 30) -> dict:
    """Reserve a dock slot at a depot for cargo transfer. Rolls to the next hour if the dock is full."""
    dep = db.one("SELECT * FROM depots WHERE id=?", (depot_id,))
    if not dep:
        return {"reserved": False, "error": f"unknown depot {depot_id}"}
    for h in range(4):
        slot = (now() + timedelta(minutes=start_in_min + 60 * h)).astimezone(IST).strftime("%d %b %H:00 IST")
        if db.one("SELECT COUNT(*) n FROM reservations WHERE depot_id=? AND slot=?", (depot_id, slot))["n"] < dep["dock_slots"]:
            db.insert("reservations", {"depot_id": depot_id, "slot": slot, "incident_id": incident_id})
            return {"reserved": True, "depot": dep["name"], "slot": slot}
    return {"reserved": False, "error": "no dock capacity in next 4 hours"}


@audited
def notify(incident_id: int, party: str, message: str, channel: str = "whatsapp") -> dict:
    """Send a message. party is one of driver, replacement_driver, customer, dispatcher. (Channel delivery is simulated; wire WhatsApp/Twilio here.)"""
    if party not in PARTIES:
        return {"sent": False, "error": f"party must be one of {PARTIES}"}
    db.insert("notifications", {"ts": iso(now()), "incident_id": incident_id, "party": party, "channel": channel, "message": message[:600]})
    return {"sent": True, "simulated": True, "party": party}


@audited
def log_decision(incident_id: int, summary: str) -> dict:
    """Write the final audit summary for the incident (what happened, what was done, why)."""
    db.run("UPDATE incidents SET summary=? WHERE id=?", (summary[:1500], incident_id))
    return {"logged": True}


@audited
def write_rule(incident_type: str, eta_buffer_pct: float, conf_penalty: float, rationale: str) -> dict:
    """Write or update a learned rule for an incident type (called by the REVIEW agent in the feedback loop).
    eta_buffer_pct pads predicted recovery time; conf_penalty lowers auto-act confidence. Values are clamped server-side."""
    eta_buffer_pct = max(0.0, min(60.0, float(eta_buffer_pct)))
    conf_penalty = max(0.0, min(0.30, float(conf_penalty)))
    if incident_type not in ("reefer_failure", "damaged_pallet", "breakdown", "road_blockage", "unknown"):
        return {"error": f"unknown incident_type {incident_type}"}
    support = db.one("SELECT COUNT(*) n FROM decisions WHERE incident_type=? AND actual_min IS NOT NULL", (incident_type,))["n"]
    text = f"{incident_type}: {rationale[:300]} (+{eta_buffer_pct:.0f}% ETA buffer, -{conf_penalty:.2f} confidence)"
    db.run("INSERT OR REPLACE INTO rules VALUES(?,?,?,?,?,?)", (incident_type, iso(now()), eta_buffer_pct, conf_penalty, text, support))
    return {"written": True, "incident_type": incident_type, "eta_buffer_pct": eta_buffer_pct, "conf_penalty": conf_penalty, "support": support}


# ---------------------------------------------------------------- dispatcher + feedback loop (REST-facing)
def approve_decision(did):
    d = db.one("SELECT * FROM decisions WHERE id=?", (did,))
    if not d or d["status"] != "pending_approval":
        return {"error": "decision is not pending approval"}
    if not _execute(did):
        db.run("UPDATE decisions SET status='failed' WHERE id=?", (did,))
        return {"error": "vehicle no longer available"}
    _post_actions(did)
    return {"ok": True}


def override_decision(did, vehicle_id, reason):
    old = db.one("SELECT * FROM decisions WHERE id=?", (did,))
    ctx = _ctx(old["incident_id"])
    if old["status"] == "executed" and old["mode"] == "swap":
        db.run("UPDATE vehicles SET status='available' WHERE id=?", (old["vehicle_id"],))  # release the agent's pick
    p = _plan_for(ctx, vehicle_id)
    if "error" in p:
        if old["status"] == "executed" and old["mode"] == "swap":
            db.run("UPDATE vehicles SET status='dispatched' WHERE id=?", (old["vehicle_id"],))
        return {"error": p["error"]}
    db.run("UPDATE decisions SET status='overridden' WHERE id=?", (did,))
    _mirror(did, "overridden")
    nid = db.insert("decisions", {"incident_id": old["incident_id"], "ts": iso(now()), "incident_type": old["incident_type"], "mode": p["mode"], "shipment_id": p["shipment_id"],
                                  "broken_vehicle": old["broken_vehicle"], "vehicle_id": vehicle_id, "meet_lat": p["meet_lat"], "meet_lon": p["meet_lon"], "meet_label": p["meet_label"],
                                  "meet_depot": p["meet_depot"], "predicted_raw_min": p["predicted_raw_min"] / (1 + p["buffer_pct"] / 100), "predicted_min": p["predicted_min"],
                                  "sla_remaining_min": p["sla_remaining_min"], "sla_ok": int(p["sla_ok"]), "cold_ok": int(p["cold_ok"]), "cost_inr": p["cost_inr"], "confidence": 1.0,
                                  "status": "pending_approval", "reasoning": f"Dispatcher override: {reason}", "eta_local": p["eta_local"], "override_of": old["vehicle_id"], "route": json.dumps(p["route"])})
    _mirror(nid)
    if not _execute(nid):
        return {"error": "vehicle no longer available"}
    _post_actions(nid)
    return {"ok": True, "decision_id": nid}


def rate_decision(did, rating):
    db.run("UPDATE decisions SET rating=? WHERE id=?", (int(rating), did))
    return {"ok": True}


FACTOR = {"reefer_failure": 1.30, "breakdown": 1.18, "damaged_pallet": 1.10, "road_blockage": 1.05, "unknown": 1.15}


def simulate_outcome(did, actual_min=None):
    """Stands in for real delivery confirmation. The simulated world is systematically slower than the raw model (esp. reefer repairs),
    which is exactly the bias the feedback loop should discover."""
    d = db.one("SELECT * FROM decisions WHERE id=?", (did,))
    if not d or d["status"] != "executed":
        return {"error": "only executed decisions have outcomes"}
    act = float(actual_min) if actual_min else d["predicted_raw_min"] * FACTOR.get(d["incident_type"], 1.15) * random.uniform(.96, 1.04)
    db.run("UPDATE decisions SET actual_min=?, sla_held=? WHERE id=?", (round(act), int(act <= d["sla_remaining_min"]), did))
    _mirror(did, "outcome")
    db.run("UPDATE shipments SET status='delivered' WHERE id=?", (d["shipment_id"],))
    if d["mode"] == "swap":
        s = db.one("SELECT * FROM shipments WHERE id=?", (d["shipment_id"],))
        db.run("UPDATE vehicles SET status='available', lat=?, lon=? WHERE id=?", (s["dest_lat"], s["dest_lon"], d["vehicle_id"]))
    return {"ok": True, "actual_min": round(act)}


def learn():
    """Nightly learner: compares predicted vs actual, clusters by incident type, writes rules that the planner tools then apply."""
    out = []
    for (t,) in [(r["incident_type"],) for r in db.q("SELECT DISTINCT incident_type FROM decisions WHERE actual_min IS NOT NULL")]:
        rows = db.q("SELECT predicted_raw_min p, actual_min a FROM decisions WHERE incident_type=? AND actual_min IS NOT NULL", (t,))
        ov = db.one("SELECT COUNT(*) n FROM decisions WHERE incident_type=? AND status='overridden'", (t,))["n"]
        ratio = statistics.mean(r["a"] / r["p"] for r in rows if r["p"])
        buf = max(0, min(60, round((ratio - 1) * 100))) if len(rows) >= LEARN_MIN_SAMPLES and abs(ratio - 1) >= .08 else 0
        pen = 0.1 if ov >= 2 else 0.0
        parts = []
        if buf:
            parts.append(f"{t}: actual recovery ran {ratio:.2f}x the raw estimate over {len(rows)} cases; add {buf}% ETA buffer")
        if pen:
            parts.append(f"dispatchers overrode the agent {ov} times; lower confidence by {pen}")
        text = "; ".join(parts) or f"{t}: predictions within tolerance ({len(rows)} cases)"
        db.run("INSERT OR REPLACE INTO rules VALUES(?,?,?,?,?,?)", (t, iso(now()), buf, pen, text, len(rows)))
        out.append(text)
    return {"rules": out}


def snapshot():
    decs = db.q("SELECT * FROM decisions ORDER BY id DESC LIMIT 30")
    for d in decs[6:]:
        d.pop("route", None)
    done = db.q("SELECT predicted_min, actual_min, sla_held FROM decisions WHERE actual_min IS NOT NULL")
    stats = {"resolved": len(done),
             "mape_pct": round(100 * statistics.mean(abs(r["actual_min"] - r["predicted_min"]) / r["actual_min"] for r in done)) if done else None,
             "sla_held_pct": round(100 * statistics.mean(r["sla_held"] for r in done)) if done else None,
             "overrides": db.one("SELECT COUNT(*) n FROM decisions WHERE status='overridden'")["n"]}
    return {"depots": db.q("SELECT * FROM depots"), "vehicles": db.q("SELECT * FROM vehicles"), "shipments": db.q("SELECT * FROM shipments"),
            "incidents": db.q("SELECT id, ts, vehicle_id, status, mode, substr(text,1,70) text FROM incidents ORDER BY id DESC LIMIT 15"),
            "decisions": decs, "rules": db.q("SELECT * FROM rules"), "notifications": db.q("SELECT * FROM notifications ORDER BY id DESC LIMIT 25"), "stats": stats}
