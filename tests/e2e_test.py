"""End-to-end test: Temporal workflows + ClickHouse + offline agents (no keys needed).
Run:  .venv/bin/python tests/e2e_test.py
Covers: auto-swap, feedback loop (rules written by the REVIEW step after outcomes),
durable approve across an app crash, MCP tool surface, ClickHouse mirrors + fleet scan."""
import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update(TEMPORAL_URL="localhost:7233", CLICKHOUSE_URL="http://localhost:8123",
                  CLICKHOUSE_USER="d2d", CLICKHOUSE_PASSWORD="d2d",
                  D2D_DB=str(ROOT / "data" / "e2e.sqlite"), PORT="8666",
                  TEMPORAL_WF_PREFIX=f"e2e-{uuid.uuid4().hex[:6]}-")   # old open workflows keep their ids
os.environ.pop("GOOGLE_API_KEY", None)
os.environ.pop("GOOGLE_MAPS_API_KEY", None)
BASE = "http://127.0.0.1:8666"
FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def api(path, method="GET", **kw):
    import httpx
    r = httpx.request(method, BASE + path, timeout=30, **kw)
    return r.json()


def wait_for(pred, timeout=40):
    for _ in range(timeout * 2):
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def latest_dec(iid):
    return (api(f"/api/incidents/{iid}")["decisions"] or [{}])[-1]


async def wf(inc_id, what="describe"):
    from app.temporal import client, wfid
    h = (await client()).get_workflow_handle(wfid(inc_id))
    return await (h.query("status") if what == "query" else h.describe())


async def mcp_check():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async with stdio_client(StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(ROOT))) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()   # mcp 2.x: the handshake is explicit, not part of __aenter__
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            check("mcp exposes 10 tools", len(tools) == 10, str(tools))
            res = await s.call_tool("scan_fleet_anomalies", {"minutes": 60})
            txt = res.content[0].text
            check("mcp scan_fleet_anomalies answers", "anomalies" in txt and ("TRK-3" in txt or "TRK-4" in txt), txt[:200])


def main():
    asyncio.run(mcp_check())
    from app import db, seed
    db.init(); seed.seed()   # fresh fleet + telemetry before the app starts
    log = open(ROOT / "data" / "e2e.log", "w")
    srv = subprocess.Popen([sys.executable, "run.py"], cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
    try:
        assert wait_for(lambda: api("/api/health")["temporal"]), "server did not start"

        # 1) reefer failure -> auto swap, executed
        i1 = api("/api/incidents", "POST", data={"vehicle_id": "TRK-3", "mode": "offline", "text":
                "Reefer unit has stopped cooling and the temperature display keeps climbing. Two pallets also got crushed when I braked hard. Truck is still drivable."})["id"]
        assert wait_for(lambda: latest_dec(i1).get("status") == "executed"), "no executed decision"
        d1 = latest_dec(i1)
        check("auto-swap executed (durable)", d1["vehicle_id"] != "TRK-3", str(d1.get("vehicle_id")))

        # 2) outcome -> REVIEW step in the workflow writes a rule
        assert api(f"/api/decisions/{d1['id']}/simulate-outcome", "POST").get("ok")
        check("review runs after outcome", wait_for(lambda: any(r["incident_type"] == "reefer_failure" for r in api("/api/state")["rules"])))

        # 3) second reefer case (healthy telemetry contradicts the driver -> escalates, dispatcher approves) -> ETA buffer rule
        i2 = api("/api/incidents", "POST", data={"vehicle_id": "TRK-4", "mode": "offline", "text":
                "Cooling compressor is making a loud noise, box temperature is rising, reefer failing, cargo dairy needs 2-8C."})["id"]
        assert wait_for(lambda: latest_dec(i2).get("status") == "pending_approval"), "reefer report with healthy telemetry should escalate"
        assert api(f"/api/decisions/{latest_dec(i2)['id']}/approve", "POST").get("ok")
        assert wait_for(lambda: latest_dec(i2).get("status") == "executed"), "second swap"
        assert api(f"/api/decisions/{latest_dec(i2)['id']}/simulate-outcome", "POST").get("ok")
        check("learned ETA buffer after 2 misses", wait_for(lambda: any(
            r["eta_buffer_pct"] > 0 for r in api("/api/state")["rules"])))

        # 4) vague report -> escalates; durable wait; approve ACROSS A CRASH
        i3 = api("/api/incidents", "POST", data={"vehicle_id": "TRK-1", "mode": "offline", "text": "something is wrong with the truck, not sure what"})["id"]
        assert wait_for(lambda: latest_dec(i3).get("status") == "pending_approval"), "no escalation"
        check("workflow waits durably", asyncio.run(wf(i3, "query")) == "awaiting_dispatcher")

        srv.kill(); srv.wait()   # simulate the 2 a.m. crash
        srv = subprocess.Popen([sys.executable, "run.py"], cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
        assert wait_for(lambda: api("/api/health")["temporal"]), "restart failed"

        res = api(f"/api/decisions/{latest_dec(i3)['id']}/approve", "POST")
        check("approve signalled after crash+restart", res.get("ok") and res.get("via") == "temporal", str(res))
        assert wait_for(lambda: latest_dec(i3).get("status") == "executed"), "approve did not execute"
        assert api(f"/api/decisions/{latest_dec(i3)['id']}/simulate-outcome", "POST").get("ok")
        check("workflow completes after outcome+review", wait_for(lambda: getattr(asyncio.run(wf(i3, "describe")).status, "name", "") == "COMPLETED"))

        # 5) ClickHouse: fleet scan via tool + mirrored tables
        from app import tools as t
        scan = t.scan_fleet_anomalies(90)
        check("fleet scan reads clickhouse", scan["source"] == "clickhouse" and scan["anomalies"], str(scan)[:200])
        from app import ch
        c = ch._client()
        for tbl, n in (("telemetry", 246), ("tool_log", 1), ("decision_events", 1)):
            got = c.query(f"SELECT count() FROM {ch.CLICKHOUSE_DB}.{tbl}").result_rows[0][0]
            check(f"clickhouse {tbl} mirrored", got >= n, str(got))

        # 6) nightly review workflow is running (durable timer)
        async def nightly():
            from app.temporal import client
            ids = [w.id async for w in (await client()).list_workflows()]
            return any("dock-to-door-nightly-review" == i.split("/")[-1] for i in ids)
        check("nightly review workflow running", asyncio.run(nightly()))
    finally:
        srv.terminate()
    print("\n" + ("ALL PASS" if not FAILURES else f"FAILURES: {FAILURES}"))
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
