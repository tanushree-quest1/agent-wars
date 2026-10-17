import asyncio, json, uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import agents, db, seed, tools
from .config import DATA, GOOGLE_API_KEY, MAPS_KEY, MODEL

TASKS = set()


@asynccontextmanager
async def lifespan(_):
    db.init()
    if not db.one("SELECT 1 x FROM vehicles"):
        seed.seed()
    yield

app = FastAPI(title="Dock-to-Door", lifespan=lifespan)
app.mount("/uploads", StaticFiles(directory=DATA / "uploads"), name="uploads")
STATIC = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"adk_enabled": bool(GOOGLE_API_KEY), "model": MODEL, "google_routes": bool(MAPS_KEY)}


@app.get("/api/state")
def state():
    return tools.snapshot()


@app.post("/api/incidents")
async def create_incident(vehicle_id: str = Form(...), text: str = Form(""), mode: str = Form("auto"),
                          image: UploadFile | None = File(None), audio: UploadFile | None = File(None)):
    if not db.one("SELECT 1 x FROM vehicles WHERE id=?", (vehicle_id,)):
        raise HTTPException(404, "unknown vehicle")
    img = aud = None
    img_name = aud_name = None
    if image and image.filename:
        data = await image.read()
        img_name = uuid.uuid4().hex + Path(image.filename).suffix.lower()
        (DATA / "uploads" / img_name).write_bytes(data)
        img = (data, image.content_type or "image/jpeg")
    if audio and audio.filename:
        aud = await audio.read()
        aud_name = uuid.uuid4().hex + ".wav"
        (DATA / "uploads" / aud_name).write_bytes(aud)
    iid = db.insert("incidents", {"ts": tools.iso(tools.now()), "vehicle_id": vehicle_id, "text": text, "image_path": img_name,
                                  "audio_path": aud_name, "mode": mode, "status": "running", "trace": "[]"})
    t = asyncio.create_task(agents.run(iid, mode, img, aud))
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return {"id": iid}


@app.get("/api/incidents/{iid}")
def incident(iid: int):
    inc = db.one("SELECT * FROM incidents WHERE id=?", (iid,))
    if not inc:
        raise HTTPException(404)
    inc["triage"] = json.loads(inc["triage"]) if inc["triage"] else None
    inc["trace"] = json.loads(inc["trace"] or "[]")
    inc["decisions"] = db.q("SELECT * FROM decisions WHERE incident_id=? ORDER BY id", (iid,))
    for d in inc["decisions"]:
        d.pop("route", None)
    return inc


@app.get("/api/telemetry/{vid}")
def telemetry(vid: str):
    return db.q("SELECT ts, temp_c FROM telemetry WHERE vehicle_id=? ORDER BY ts", (vid,))


@app.get("/api/decisions/{did}/candidates")
def candidates(did: int):
    d = db.one("SELECT incident_id FROM decisions WHERE id=?", (did,))
    return tools.find_alternate_vehicles(d["incident_id"])


class Override(BaseModel):
    vehicle_id: str
    reason: str = ""


class Rating(BaseModel):
    rating: int


@app.post("/api/decisions/{did}/approve")
def approve(did: int):
    return tools.approve_decision(did)


@app.post("/api/decisions/{did}/override")
def override(did: int, body: Override):
    return tools.override_decision(did, body.vehicle_id, body.reason or "no reason given")


@app.post("/api/decisions/{did}/rate")
def rate(did: int, body: Rating):
    return tools.rate_decision(did, body.rating)


@app.post("/api/decisions/{did}/simulate-outcome")
def outcome(did: int):
    return tools.simulate_outcome(did)


@app.post("/api/learn")
def learn():
    return tools.learn()


@app.post("/api/reset")
def reset():
    seed.seed()
    return {"ok": True}
