"""SQLite (WAL) store. Multi-process safe, so the MCP server subprocess and the API share it.
Swap point for ClickHouse: telemetry + decisions are the append-heavy tables."""
import sqlite3
from contextlib import contextmanager
from .config import DB_PATH

TABLES = ["depots", "vehicles", "shipments", "telemetry", "incidents", "decisions",
          "reservations", "notifications", "rules", "tool_log"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS depots(id TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL, dock_slots INTEGER);
CREATE TABLE IF NOT EXISTS vehicles(id TEXT PRIMARY KEY, driver TEXT, phone TEXT, lat REAL, lon REAL, capacity INTEGER, temp_class TEXT, status TEXT, depot_id TEXT);
CREATE TABLE IF NOT EXISTS shipments(id TEXT PRIMARY KEY, vehicle_id TEXT, customer TEXT, cargo TEXT, pallets INTEGER, temp_class TEXT, max_temp_c REAL, priority TEXT, dest_name TEXT, dest_lat REAL, dest_lon REAL, sla_ts TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS telemetry(ts TEXT, vehicle_id TEXT, lat REAL, lon REAL, temp_c REAL, speed REAL);
CREATE INDEX IF NOT EXISTS ix_tel ON telemetry(vehicle_id, ts);
CREATE TABLE IF NOT EXISTS incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, vehicle_id TEXT, text TEXT, image_path TEXT, audio_path TEXT, mode TEXT, status TEXT, triage TEXT, trace TEXT DEFAULT '[]', summary TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER, ts TEXT, incident_type TEXT, mode TEXT, shipment_id TEXT, broken_vehicle TEXT, vehicle_id TEXT, meet_lat REAL, meet_lon REAL, meet_label TEXT, meet_depot TEXT, predicted_raw_min REAL, predicted_min REAL, sla_remaining_min REAL, sla_ok INTEGER, cold_ok INTEGER, cost_inr REAL, confidence REAL, status TEXT, reasoning TEXT, escalation TEXT, eta_local TEXT, override_of TEXT, rating INTEGER, actual_min REAL, sla_held INTEGER, route TEXT);
CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY AUTOINCREMENT, depot_id TEXT, slot TEXT, incident_id INTEGER);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, incident_id INTEGER, party TEXT, channel TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS rules(incident_type TEXT PRIMARY KEY, ts TEXT, eta_buffer_pct REAL, conf_penalty REAL, text TEXT, support INTEGER);
CREATE TABLE IF NOT EXISTS tool_log(ts TEXT, tool TEXT, args TEXT, result TEXT);
"""


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init():
    c = conn()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.close()


def q(sql, a=()):
    c = conn()
    try:
        return [dict(r) for r in c.execute(sql, a).fetchall()]
    finally:
        c.close()


def one(sql, a=()):
    r = q(sql, a)
    return r[0] if r else None


def run(sql, a=()):
    c = conn()
    try:
        return c.execute(sql, a).lastrowid
    finally:
        c.close()


def insert(table, d):
    ph = ",".join("?" * len(d))
    return run(f"INSERT INTO {table}({','.join(d)}) VALUES({ph})", tuple(d.values()))


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def reset():
    for t in TABLES:
        run(f"DELETE FROM {t}")
