"""ClickHouse sink for append-heavy tables (telemetry, tool_log, decision_events).
SQLite stays the source of truth for mutable state; ClickHouse is the log/analytics store.
Empty CLICKHOUSE_URL or a down server => writes no-op, reads return None (caller falls back to SQLite)."""
import logging
import threading
from datetime import datetime, timedelta, timezone
from .config import CLICKHOUSE_DB, CLICKHOUSE_PASSWORD, CLICKHOUSE_URL, CLICKHOUSE_USER

log = logging.getLogger("d2d.ch")
_c, _ready, _down = None, False, False
_lock = threading.Lock()

COLS = {  # insert column order must match the table DDL below
    "telemetry": ("ts", "vehicle_id", "temp_class", "max_temp_c", "temp_c", "lat", "lon", "speed"),
    "tool_log": ("ts", "tool", "args", "result"),
    "decision_events": ("ts", "decision_id", "incident_id", "incident_type", "status", "predicted_min",
                        "actual_min", "cost_inr", "confidence", "sla_ok", "cold_ok", "rating"),
}
DDL = [
    f"CREATE TABLE IF NOT EXISTS {CLICKHOUSE_DB}.telemetry (ts DateTime('UTC'), vehicle_id String, temp_class String, max_temp_c Nullable(Float64), temp_c Float64, lat Float64, lon Float64, speed Float64) ENGINE MergeTree ORDER BY (vehicle_id, ts)",
    f"CREATE TABLE IF NOT EXISTS {CLICKHOUSE_DB}.tool_log (ts DateTime('UTC'), tool String, args String, result String) ENGINE MergeTree ORDER BY ts",
    f"CREATE TABLE IF NOT EXISTS {CLICKHOUSE_DB}.decision_events (ts DateTime('UTC'), decision_id Int64, incident_id Int64, incident_type String, status String, predicted_min Float64, actual_min Nullable(Float64), cost_inr Float64, confidence Float64, sla_ok Int8, cold_ok Int8, rating Nullable(Int8)) ENGINE MergeTree ORDER BY ts",
]


def enabled():
    return bool(CLICKHOUSE_URL)


def _client():
    global _c, _ready, _down
    if _c is None and not _down and CLICKHOUSE_URL:
        try:
            import clickhouse_connect
            _c = clickhouse_connect.get_client(dsn=CLICKHOUSE_URL, username=CLICKHOUSE_USER, password=CLICKHOUSE_PASSWORD)
        except Exception as e:
            _down = True   # one warning, then silence: every write is a no-op until restart
            log.warning("ClickHouse connect failed (%s); analytics degrade to SQLite", e)
    if _c is not None and not _ready:
        try:
            _c.command(f"CREATE DATABASE IF NOT EXISTS {CLICKHOUSE_DB}")
            for ddl in DDL:
                _c.command(ddl)
            _ready = True
        except Exception as e:
            log.warning("ClickHouse init failed (%s); analytics degrade to SQLite", e)
            _c, _down = None, True
    return _c


def _dt(v):
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)


def reset():
    global _ready
    with _lock:
        c = _client()
        if not c:
            return
        try:
            for t in COLS:
                c.command(f"DROP TABLE IF EXISTS {CLICKHOUSE_DB}.{t}")
            _ready = False
            _client()
        except Exception as e:
            log.warning("ClickHouse reset failed (%s)", e)


def write(table, row):
    """Fire-and-forget mirror of one row; never raises into callers."""
    with _lock:
        c = _client()
        if not c:
            return
        try:
            row = dict(row, ts=_dt(row["ts"]))
            c.insert(f"{CLICKHOUSE_DB}.{table}", [tuple(row.get(k) for k in COLS[table])])
        except Exception as e:
            log.warning("ClickHouse write %s failed (%s)", table, e)


def fleet_drift(minutes):
    """Fleet scan: reefer trucks with readings above their limit or temp drift, last N minutes.
    Returns list, or None when ClickHouse is off/unavailable (caller falls back to SQLite)."""
    with _lock:
        c = _client()
        if not c:
            return None
        w = _dt(datetime.now(timezone.utc) - timedelta(minutes=max(1, int(minutes))))
        q = f"""SELECT vehicle_id, round(max(temp_c) - min(temp_c), 2) drift_c, round(avg(temp_c), 2) avg_temp,
                       round(max(temp_c), 2) max_temp, countIf(temp_c > max_temp_c) readings_above,
                       round(min(max_temp_c), 1) limit_c, count() readings
                FROM {CLICKHOUSE_DB}.telemetry WHERE ts >= %(w)s AND temp_class = 'reefer'
                GROUP BY vehicle_id HAVING readings_above > 0 OR drift_c > 1.0 ORDER BY drift_c DESC"""
        try:
            return list(c.query(q, parameters={"w": w}).named_results())
        except Exception as e:
            log.warning("ClickHouse fleet scan failed (%s); using SQLite", e)
            return None
