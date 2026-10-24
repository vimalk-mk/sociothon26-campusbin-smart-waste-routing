# CampusBin: Smart Waste Collection & Route Optimization
SOCIOTHON 2026 · IIT Roorkee · Problem Statement 2

## Run
    python -m pip install -r requirements.txt
    python app.py        # http://localhost:5000
`data/roads.json` (OpenStreetMap roads) is bundled, so it starts offline. If it is missing, roads are downloaded from Overpass once.

## Which 30 bins?
`data/campus.kml` has 91 placemarks, including survey points outside the campus (Sati Mohalla, the "8Aug" folder, interviews). `app.py` keeps only points inside `CAMPUS_BOX`, merges duplicates within 30 m (37 distinct points remain), keeps the 10 "Dustbin" folder points, and adds 20 more by farthest-point sampling so the bins cover the whole campus. Change `N_BINS`, `CAMPUS_BOX` or `DEPOT_LATLON` at the top of `app.py`. A placemark named "Depot" is used as the depot if you add one.

## Demo flow
Watch bins fill → Collection plan → move sliders → Plan routes (compare with visit-all baseline) → report an overflow (flagged bin gets priority) → re-plan → Mark as collected.

## Method
1. Fill model: zone base rate × hourly peak pattern; an EWMA learns each bin's rate → time-to-full. Zones, capacities and rates are **simulated assumptions** until sensors report.
2. Selection: visit if fill ≥ threshold, predicted full within horizon, or citizen-reported.
3. Routing: capacity-limited sweep clusters → nearest neighbour → 2-opt, all on road-network distance; trip ETA = 18 km/h + 3 min/stop.
4. Impact vs baseline (fixed route visiting every bin): km, diesel (0.4 L/km), CO₂ (2.68 kg/L).
5. Upgrade path: OR-Tools VRP, live OSRM, Prophet/LSTM forecasts.

## IoT hook
    curl -X POST localhost:5000/api/telemetry -H "Content-Type: application/json" -d '{"bin_id":"B05","fill":83.5}'

## API
GET /api/bins · GET /api/plan?threshold=70&horizon=3&cap=1500 · GET /api/forecast · POST /api/report · /api/collect · /api/telemetry · /api/reset
