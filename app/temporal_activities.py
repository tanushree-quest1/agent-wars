"""Temporal activities: every side effect (agents, MCP tools, DB) runs here —
retried, timed out, recorded in history, replayable after a crash."""
import mimetypes
from temporalio import activity
from . import agents, db, feedback, tools
from .config import DATA


@activity.defn
async def run_pipeline(args: dict) -> dict:
    """Triage -> plan -> act (ADK or offline). Safe to retry: tools reject a second active decision."""
    inc = db.one("SELECT * FROM incidents WHERE id=?", (args["inc_id"],))
    if not inc:
        return {"error": "unknown incident"}
    img = aud = None
    for col, kind in (("image_path", "image"), ("audio_path", "audio")):
        p = DATA / "uploads" / inc[col] if inc[col] else None
        if p and p.exists():
            data = p.read_bytes()
            img = (data, mimetypes.guess_type(p.name)[0] or "image/jpeg") if kind == "image" else data
    await agents.run(inc["id"], inc["mode"], img, aud)
    return {"incident_status": db.one("SELECT status x FROM incidents WHERE id=?", (inc["id"],))["x"]}


def _did(inc_id):
    return db.one("SELECT id FROM decisions WHERE incident_id=? ORDER BY id DESC LIMIT 1", (inc_id,))


@activity.defn
def approve(inc_id: int, did: int) -> dict:
    return tools.approve_decision(did) if db.one("SELECT 1 x FROM decisions WHERE id=? AND incident_id=?", (did, inc_id)) else {"error": "unknown decision"}


@activity.defn
def override(inc_id: int, did: int, vehicle_id: str, reason: str = "") -> dict:
    return tools.override_decision(did, vehicle_id, reason or "no reason given") if db.one("SELECT 1 x FROM decisions WHERE id=? AND incident_id=?", (did, inc_id)) else {"error": "unknown decision"}


@activity.defn
def outcome(inc_id: int, did: int) -> dict:
    return tools.simulate_outcome(did) if db.one("SELECT 1 x FROM decisions WHERE id=? AND incident_id=?", (did, inc_id)) else {"error": "unknown decision"}


@activity.defn
def review(inc_id: int) -> dict:
    """Feedback loop: learn from this incident's outcome right away."""
    return feedback.review(inc_id)


@activity.defn
def review_all() -> dict:
    """Nightly feedback loop: cluster all failure modes and rewrite rules."""
    return feedback.review(None)
