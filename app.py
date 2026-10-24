"""CampusBin - Smart Waste Collection & Route Optimization (SOCIOTHON 2026, PS-2, IIT Roorkee)

* 30 bins are picked from data/campus.kml (or campus.kmz) - only placemarks INSIDE the IIT Roorkee campus box.
* Routes follow the real OpenStreetMap road network (data/roads.json cache; downloaded from Overpass if missing).
Run:  python -m pip install -r requirements.txt && python app.py   ->  http://localhost:5000
"""
import json, math, os, random, threading, time, urllib.parse, urllib.request, xml.etree.ElementTree as ET, zipfile
import networkx as nx
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static")
random.seed(7)
LOCK = threading.RLock()

# ---------------- CONFIG ----------------
N_BINS = 30                                   # exactly this many bins are used
CAMPUS_BOX = (29.8590, 77.8890, 29.8720, 77.9030)   # (south, west, north, east) = IIT Roorkee campus
MUST_KEEP_FOLDER = "dustbin"                  # bins in this KML folder are always kept
DEDUPE_M = 30                                 # placemarks closer than this are the same bin
DEPOT_LATLON = None                           # e.g. (29.8650, 77.8960); None -> "Depot" placemark, else centre of bins
SIM_SPEED = 60                                # 1 real second = 1 simulated minute
TRUCK_CAP_L, TRUCK_KMH, STOP_MIN = 1500, 18, 3
FUEL_L_PER_KM, CO2_KG_PER_L, ROAD_FACTOR = 0.40, 2.68, 1.3
# Assumed usage profiles (base fill %/hour) - replace with sensor history when real bins report.
ZONES = {"residential": 4.5, "dining": 7.0, "academic": 3.0, "retail": 6.0, "sports": 2.0}
PEAKS = {"dining": (8, 13, 20), "residential": (7, 22), "retail": (12, 18), "academic": (10, 15), "sports": (6, 17)}
HWY = "motorway|trunk|primary|secondary|tertiary|unclassified|residential|service|living_street|road"
MIRRORS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
           "https://overpass.private.coffee/api/interpreter"]

def hav_km(a, b):
    p1, p2, dl = math.radians(a[0]), math.radians(b[0]), math.radians(b[1] - a[1])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))

# ---------------- KML -> 30 campus bins ----------------
def read_placemarks():
    if os.path.exists("data/campus.kmz"):
        z = zipfile.ZipFile("data/campus.kmz"); root = ET.fromstring(z.read(next(n for n in z.namelist() if n.endswith(".kml"))))
    else:
        root = ET.parse("data/campus.kml").getroot()
    folder_of = {id(pm): (next((c.text for c in f if c.tag.endswith("name")), "") or "").strip()
                 for f in root.iter() if f.tag.endswith("Folder") for pm in f if pm.tag.endswith("Placemark")}
    out = []
    for pm in root.iter():
        if not pm.tag.endswith("Placemark"): continue
        pt = next((e for e in pm.iter() if e.tag.endswith("Point")), None)
        if pt is None: continue
        nm = (next((c.text for c in pm if c.tag.endswith("name")), "") or "?").strip()
        lon, lat = (float(v) for v in next(e.text for e in pt.iter() if e.tag.endswith("coordinates")).strip().split(",")[:2])
        out.append(dict(folder=folder_of.get(id(pm), ""), name=nm, lat=lat, lon=lon))
    return out

def in_campus(p):
    s, w, n, e = CAMPUS_BOX; return s <= p["lat"] <= n and w <= p["lon"] <= e

def select_bins(places):
    depot = next((p for p in places if "depot" in p["name"].lower()), None)
    cand = []
    for p in places:                                           # inside campus, de-duplicated
        if "depot" in p["name"].lower() or not in_campus(p): continue
        if all(hav_km((p["lat"], p["lon"]), (q["lat"], q["lon"])) * 1000 > DEDUPE_M for q in cand): cand.append(p)
    keep = [p for p in cand if MUST_KEEP_FOLDER in p["folder"].lower()][:N_BINS]
    rest = [p for p in cand if p not in keep]
    while len(keep) < N_BINS and rest:                         # farthest-point sampling -> even campus coverage
        nxt = max(rest, key=lambda r: min(hav_km((r["lat"], r["lon"]), (k["lat"], k["lon"])) for k in keep) if keep else 0)
        keep.append(nxt); rest.remove(nxt)
    return keep, depot, len(cand)

PLACES = read_placemarks()
RAW_BINS, DEPOT_PM, N_CANDIDATES = select_bins(PLACES)
cx = sum(p["lat"] for p in RAW_BINS) / len(RAW_BINS); cy = sum(p["lon"] for p in RAW_BINS) / len(RAW_BINS)
RAW_BINS.sort(key=lambda p: math.atan2(p["lat"] - cx, p["lon"] - cy))   # neighbours get the same usage profile
for i, p in enumerate(RAW_BINS): p["zone"] = list(ZONES)[i * len(ZONES) // len(RAW_BINS)]

# ---------------- ROAD NETWORK ----------------
G = None; NODE_XY = {}; ROUTING = "straight-line fallback (road data not loaded)"

def fetch_roads(s, w, n, e):
    q = f'[out:json][timeout:90];way["highway"~"^({HWY})$"]({s},{w},{n},{e});(._;>;);out body;'
    for url in MIRRORS:
        try:
            print("Downloading campus roads from", url)
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(), headers={"User-Agent": "CampusBin/1.0"})
            return json.load(urllib.request.urlopen(req, timeout=120))
        except Exception as ex: print("  failed:", ex)

def build_graph(data):
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in data["elements"] if e["type"] == "node"}
    g = nx.DiGraph()
    for e in data["elements"]:
        if e["type"] != "way": continue
        t = e.get("tags", {}); one = t.get("oneway") in ("yes", "true", "1") or t.get("junction") == "roundabout"; rev = t.get("oneway") == "-1"
        for a, b in zip(e["nodes"], e["nodes"][1:]):
            if a in nodes and b in nodes:
                d = hav_km(nodes[a], nodes[b])
                if rev: g.add_edge(b, a, length=d)
                else:
                    g.add_edge(a, b, length=d)
                    if not one: g.add_edge(b, a, length=d)
    g = g.subgraph(max(nx.strongly_connected_components(g), key=len)).copy()   # drivable core only
    return g, {n: nodes[n] for n in g.nodes}

def init_roads(points, tol=0.002):
    global G, NODE_XY, ROUTING
    s, n = min(p[0] for p in points) - .008, max(p[0] for p in points) + .008
    w, e = min(p[1] for p in points) - .008, max(p[1] for p in points) + .008
    data, cache = None, "data/roads.json"
    if os.path.exists(cache):                                  # offline-first: reuse the cache if it covers the bins
        c = json.load(open(cache)); b = c["bbox"]
        if b[0] <= min(p[0] for p in points) + tol and b[1] <= min(p[1] for p in points) + tol and \
           b[2] >= max(p[0] for p in points) - tol and b[3] >= max(p[1] for p in points) - tol: data = c["data"]
    if data is None:
        data = fetch_roads(s, w, n, e)
        if data: json.dump({"bbox": [s, w, n, e], "data": data}, open(cache, "w"))
    if data:
        G, NODE_XY = build_graph(data); ROUTING = f"OpenStreetMap road network ({G.number_of_nodes()} junctions, {G.number_of_edges()} road segments)"
    else: print("!! No road data - using straight-line x", ROAD_FACTOR)

def snap(lat, lon):
    return min(NODE_XY, key=lambda k: hav_km((lat, lon), NODE_XY[k])) if G is not None else None

SP = {}
def sp(src):
    if src not in SP: SP[src] = nx.single_source_dijkstra(G, src, weight="length")
    return SP[src]

def dist(a, b):
    if G is None: return hav_km((a["lat"], a["lon"]), (b["lat"], b["lon"])) * ROAD_FACTOR
    return 0.0 if a["node"] == b["node"] else sp(a["node"])[0].get(b["node"], 1e9)

def leg(a, b):
    if G is None: return [[a["lat"], a["lon"]], [b["lat"], b["lon"]]]
    if a["node"] == b["node"]: return [list(NODE_XY[a["node"]])]
    return [list(NODE_XY[n]) for n in sp(a["node"])[1][b["node"]]]

# ---------------- STATE ----------------
def load_bins():
    return [dict(id=f"B{i+1:02d}", name=f"Bin {i+1:02d}", src=f'{p["folder"] or "Campus"} / {p["name"]}', lat=p["lat"], lon=p["lon"], zone=p["zone"],
                 node=snap(p["lat"], p["lon"]), cap=random.choice([120, 240, 240, 360]), fill=random.uniform(10, 90),
                 rate=ZONES[p["zone"]] * random.uniform(.8, 1.2), rate_est=ZONES[p["zone"]], flagged=False, last_collected=None,
                 battery=random.randint(60, 100)) for i, p in enumerate(RAW_BINS)]

_pts = [(p["lat"], p["lon"]) for p in RAW_BINS]
init_roads(_pts)
_dl = DEPOT_LATLON or ((DEPOT_PM["lat"], DEPOT_PM["lon"]) if DEPOT_PM else (sum(p[0] for p in _pts) / len(_pts), sum(p[1] for p in _pts) / len(_pts)))
DEPOT = {"name": "Waste Depot", "lat": _dl[0], "lon": _dl[1], "node": snap(*_dl)}
if G is not None: DEPOT["lat"], DEPOT["lon"] = NODE_XY[DEPOT["node"]]
BINS, REPORTS, STATE = load_bins(), [], {"t": time.time(), "sim_hour": 8.0}

def hour_mult(zone, h): return 0.6 + 1.6 * max(math.exp(-((h % 24 - p) ** 2) / 4) for p in PEAKS[zone])

def tick():
    with LOCK:
        now = time.time(); dt = (now - STATE["t"]) * SIM_SPEED / 3600; STATE["t"] = now
        STATE["sim_hour"] = (STATE["sim_hour"] + dt) % 24
        for b in BINS:
            inst = b["rate"] * hour_mult(b["zone"], STATE["sim_hour"])
            b["fill"] = min(100, b["fill"] + inst * dt)
            b["rate_est"] = 0.9 * b["rate_est"] + 0.1 * inst        # EWMA fill-rate predictor
            b["battery"] = max(5, b["battery"] - dt * 0.002)

# ---------------- ROUTING (capacity VRP: sweep -> nearest neighbour -> 2-opt on road distance) ----------------
def tour_len(seq, D): return sum(D(seq[i], seq[i + 1]) for i in range(len(seq) - 1))

def two_opt(seq, D):
    best, cur, improved = seq[:], tour_len(seq, D), True
    while improved:
        improved = False
        for i in range(1, len(best) - 2):
            for j in range(i + 1, len(best) - 1):
                new = best[:i] + best[i:j + 1][::-1] + best[j + 1:]; l = tour_len(new, D)
                if l + 1e-9 < cur: best, cur, improved = new, l, True
    return best

def vrp(selected, cap=TRUCK_CAP_L):
    sel = sorted(selected, key=lambda b: math.atan2(b["lat"] - DEPOT["lat"], b["lon"] - DEPOT["lon"]))
    trips, cur, load = [], [], 0
    for b in sel:
        l = b["cap"] * b["fill"] / 100
        if cur and load + l > cap: trips.append(cur); cur, load = [], 0
        cur.append(b); load += l
    if cur: trips.append(cur)
    out = []
    for tr in trips:
        P = [DEPOT] + tr; D = lambda i, j: dist(P[i], P[j]); left, seq = list(range(1, len(P))), [0]
        while left:
            n = min(left, key=lambda j: D(seq[-1], j)); seq.append(n); left.remove(n)
        seq = two_opt(seq + [0], D); path, stops, t = [], [], 0.0
        for i in range(len(seq) - 1):
            seg = leg(P[seq[i]], P[seq[i + 1]]); path += seg if not path else seg[1:]
            d = D(seq[i], seq[i + 1]); t += d / TRUCK_KMH * 60
            if i + 1 < len(seq) - 1:
                b = P[seq[i + 1]]; t += STOP_MIN
                stops.append(dict(id=b["id"], name=b["name"], zone=b["zone"], fill=round(b["fill"]), leg_km=round(d, 2), eta_min=round(t), flagged=b["flagged"]))
        km = tour_len(seq, D)
        out.append(dict(stops=stops, ids=[s["id"] for s in stops], path=path, km=round(km, 2), duration_min=round(t), load_l=round(sum(b["cap"] * b["fill"] / 100 for b in tr))))
    return out

def metrics(trips):
    km = sum(t["km"] for t in trips); fuel = km * FUEL_L_PER_KM
    return dict(km=round(km, 2), fuel_l=round(fuel, 2), co2_kg=round(fuel * CO2_KG_PER_L, 2), trips=len(trips),
                stops=sum(len(t["stops"]) for t in trips), duration_min=sum(t["duration_min"] for t in trips))

# ---------------- API ----------------
@app.get("/api/bins")
def bins():
    tick()
    with LOCK:
        for b in BINS: b["tte_h"] = round((100 - b["fill"]) / b["rate_est"], 1) if b["rate_est"] > 0 else None
        zones = {z: round(sum(b["fill"] for b in BINS if b["zone"] == z) / max(1, sum(1 for b in BINS if b["zone"] == z))) for z in ZONES}
        return jsonify(bins=BINS, depot=DEPOT, routing=ROUTING, sim_hour=round(STATE["sim_hour"], 2), reports=REPORTS[-20:], zones=zones,
                       source=dict(candidates=N_CANDIDATES, used=len(BINS), box=CAMPUS_BOX))

@app.get("/api/plan")
def plan():
    tick()
    th = float(request.args.get("threshold", 70)); hor = float(request.args.get("horizon", 3)); cap = float(request.args.get("cap", TRUCK_CAP_L))
    with LOCK:
        sel = [b for b in BINS if b["fill"] >= th or b["flagged"] or b["fill"] + b["rate_est"] * hor >= 100]
        opt, base = vrp(sel, cap), vrp(BINS, cap)                 # baseline = fixed route visiting every bin
    mo, mb = metrics(opt), metrics(base)
    sav = dict(km=round(mb["km"] - mo["km"], 2), fuel_l=round(mb["fuel_l"] - mo["fuel_l"], 2), co2_kg=round(mb["co2_kg"] - mo["co2_kg"], 2),
               min=mb["duration_min"] - mo["duration_min"], pct=round(100 * (1 - mo["km"] / mb["km"]), 1) if mb["km"] else 0)
    return jsonify(routing=ROUTING, trips=opt, optimized=mo, baseline=mb, savings=sav, selected=[b["id"] for b in sel])

@app.get("/api/forecast")
def forecast():
    """Next 24 h without collection: fill-rate by zone and number of bins that would cross 70 % / overflow."""
    tick()
    with LOCK:
        h0 = STATE["sim_hour"]; f = {b["id"]: b["fill"] for b in BINS}; hrs, zone_rate, over70, full = [], {z: [] for z in ZONES}, [], []
        for k in range(1, 25):
            h = h0 + k
            for z in ZONES:
                bs = [b for b in BINS if b["zone"] == z]
                zone_rate[z].append(round(sum(b["rate"] * hour_mult(z, h) for b in bs) / max(1, len(bs)), 2))
            for b in BINS: f[b["id"]] = min(100, f[b["id"]] + b["rate"] * hour_mult(b["zone"], h))
            hrs.append(f"{int(h % 24):02d}:00"); over70.append(sum(v >= 70 for v in f.values())); full.append(sum(v >= 100 for v in f.values()))
    return jsonify(hours=hrs, zone_rate=zone_rate, over70=over70, full=full)

@app.post("/api/collect")
def collect():
    ids = set((request.get_json(silent=True) or {}).get("ids", []))
    with LOCK:
        for b in BINS:
            if b["id"] in ids: b.update(fill=random.uniform(0, 5), flagged=False, last_collected=time.strftime("%H:%M:%S"))
    return jsonify(ok=True, collected=len(ids))

@app.post("/api/report")
def report():
    j = request.get_json(silent=True) or {}
    with LOCK:
        b = next((x for x in BINS if x["id"] == j.get("bin_id")), None)
        if not b: return jsonify(error="unknown bin"), 404
        b["flagged"] = True
        REPORTS.append(dict(bin_id=b["id"], name=b["name"], type=j.get("type", "overflow"), note=j.get("note", ""), at=time.strftime("%H:%M:%S")))
    return jsonify(ok=True)

@app.post("/api/telemetry")   # IoT hook: ESP32 + ultrasonic sensor POSTs {"bin_id":"B05","fill":83.5}
def telemetry():
    j = request.get_json(silent=True) or {}
    with LOCK:
        b = next((x for x in BINS if x["id"] == j.get("bin_id")), None)
        if b: b["fill"] = max(0.0, min(100.0, float(j["fill"])))
    return jsonify(ok=bool(b))

@app.post("/api/reset")
def reset():
    global BINS, REPORTS
    with LOCK: BINS, REPORTS = load_bins(), []
    return jsonify(ok=True)

@app.get("/")
def index(): return send_from_directory("static", "index.html")

if __name__ == "__main__": app.run(debug=False, port=5000)
