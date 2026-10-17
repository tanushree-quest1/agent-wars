"""Simulated fleet: 6 trucks en route Chennai -> Bengaluru, 5 spare vehicles parked at depots."""
import random
from datetime import timedelta
from . import db, geo, tools

WP = [("Chennai Hub", 13.0827, 80.2707), ("Sriperumbudur", 12.9675, 79.9416), ("Vellore", 12.9165, 79.1325),
      ("Krishnagiri", 12.5186, 78.2137), ("Hosur", 12.7409, 77.8253), ("Bengaluru Hub", 12.9716, 77.5946)]
DEPOTS = [("DEP-CHN", 0, 4), ("DEP-SPR", 1, 2), ("DEP-VEL", 2, 2), ("DEP-KRI", 3, 2), ("DEP-HSR", 4, 3), ("DEP-BLR", 5, 4)]


def point_along(f):
    legs = [geo.haversine(WP[i][1:], WP[i + 1][1:]) for i in range(len(WP) - 1)]
    target, acc = f * sum(legs), 0
    for i, L in enumerate(legs):
        if acc + L >= target:
            t = (target - acc) / L
            a, b = WP[i], WP[i + 1]
            return a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t
        acc += L
    return WP[-1][1], WP[-1][2]


TRUCKS = [  # id, driver, frac, class, customer, cargo, pallets, dest, lat, lon, slack_min, priority
    ("TRK-1", "Murugan", .15, "ambient", "Flipkart Hub", "Consumer electronics", 8, "Devanahalli", 13.2468, 77.7138, 240, "standard"),
    ("TRK-2", "Selvam", .30, "ambient", "Reliance Retail", "Appliances", 7, "Electronic City", 12.8399, 77.6770, 200, "standard"),
    ("TRK-3", "Karthik", .43, "reefer", "Apollo Pharmacy DC", "Insulin & vaccines (2-8C)", 6, "Whitefield", 12.9698, 77.7500, 150, "critical"),
    ("TRK-4", "Ravi", .60, "reefer", "BigBasket Hub", "Dairy & frozen", 8, "Yeshwanthpur", 13.0285, 77.5409, 220, "priority"),
    ("TRK-5", "Anbu", .75, "ambient", "Narayana Health", "Medical consumables", 5, "Bommasandra", 12.8163, 77.6948, 150, "priority"),
    ("TRK-6", "Suresh", .85, "ambient", "Bosch Peenya", "Auto parts", 9, "Peenya", 13.0285, 77.5197, 120, "standard"),
]
SPARES = [("SPR-1", "Prakash", 2, "reefer", 12), ("SPR-2", "Bala", 3, "ambient", 14), ("SPR-3", "Dinesh", 1, "reefer", 10),
          ("SPR-4", "Gopal", 4, "reefer", 12), ("SPR-5", "Hari", 0, "ambient", 14)]


def seed():
    db.init()
    db.reset()
    now = tools.now()
    for did, i, slots in DEPOTS:
        db.insert("depots", {"id": did, "name": WP[i][0] + (" Depot" if "Hub" not in WP[i][0] else ""), "lat": WP[i][1], "lon": WP[i][2], "dock_slots": slots})
    n = 0
    for vid, drv, f, cls, cust, cargo, pal, dname, dlat, dlon, slack, prio in TRUCKS:
        n += 1
        lat, lon = point_along(f)
        db.insert("vehicles", {"id": vid, "driver": drv, "phone": f"+91 90000 000{n:02d}", "lat": lat, "lon": lon, "capacity": 12, "temp_class": cls, "status": "in_transit", "depot_id": None})
        eta = geo.route((lat, lon), (dlat, dlon))["minutes"]
        db.insert("shipments", {"id": f"S-{100 + n}", "vehicle_id": vid, "customer": cust, "cargo": cargo, "pallets": pal, "temp_class": cls,
                                "max_temp_c": 8.0 if cls == "reefer" else None, "priority": prio, "dest_name": f"{cust} - {dname}",
                                "dest_lat": dlat, "dest_lon": dlon, "sla_ts": tools.iso(now + timedelta(minutes=eta + slack)), "status": "in_transit"})
        for m in range(40, -1, -1):  # 41 min of 1-min telemetry
            ts = tools.iso(now - timedelta(minutes=m))
            ff = max(0, f - m * 0.0004)
            la, lo = point_along(ff)
            if cls == "reefer":
                temp = 2 + max(0, 30 - m) * 0.28 + random.uniform(-.1, .1) if vid == "TRK-3" else 2.5 + random.uniform(-.3, .3)
            else:
                temp = 28 + random.uniform(-1, 1)
            db.insert("telemetry", {"ts": ts, "vehicle_id": vid, "lat": la, "lon": lo, "temp_c": round(temp, 2), "speed": 55 + random.uniform(-6, 6)})
    for k, (vid, drv, i, cls, cap) in enumerate(SPARES, 1):
        db.insert("vehicles", {"id": vid, "driver": drv, "phone": f"+91 90000 001{k:02d}", "lat": WP[i][1] + .002, "lon": WP[i][2] + .002,
                               "capacity": cap, "temp_class": cls, "status": "available", "depot_id": DEPOTS[i][0]})


if __name__ == "__main__":
    seed()
    print("seeded")
