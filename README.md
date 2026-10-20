# Road Monitor 🛣️📱

A modular end-to-end platform for mobile road surface monitoring, sensor telemetry logging, and drive condition analysis.

---

## 📁 Monorepo Structure

`
Road-Monitor/
├── .github/
│   └── workflows/
│       └── build-android.yml    # CI: builds Android APK on push & PR
├── android/                     # Native Android Sensor Logger app (Kotlin)
│   ├── app/
│   │   ├── src/main/java/com/example/apptest22/
│   │   │   ├── MainActivity.kt  # UI controls & permission management
│   │   │   ├── LoggerService.kt # Foreground service logging IMU & GPS to CSV
│   │   │   └── Uploader.kt      # HTTP multipart uploader to backend
│   │   └── AndroidManifest.xml
│   ├── build.gradle
│   ├── settings.gradle
│   └── gradlew / gradlew.bat    # Gradle 8.7 wrapper
├── backend/                     # FastAPI ingestion & drive analysis server
│   ├── app.py                   # REST endpoints: /upload, /drives, /ping
│   └── requirements.txt
├── phone/                       # Headless data collection scripts
│   ├── logger.py                # Termux sensor logger via Termux API
│   ├── start.sh                 # Background execution script
│   └── stop.sh                  # Graceful shutdown script
├── web/                         # React 18 + Vite 5 frontend
│   ├── src/                     # Web application source
│   ├── vite.config.js           # Includes dev proxy to :8000 backend
│   └── package.json
├── data/                        # Drive data directory
│   └── raw/                     # Drive summary JSONs and marker annotations
└── docs/                        # Project documentation & assets
    ├── project-report/          # Academic and technical reports
    ├── diagrams/                # System architecture and flow charts
    ├── presentation/            # Slides and review materials
    ├── screenshots/             # Application screenshots & captures
    └── android-app.zip          # Standalone Android application archive
`

---

## 🚀 Implemented Features

### 1. Android Sensor Logger (`android/`)
- **100% Kotlin codebase** targeting Android 14 (API 34, minSdk 26).
- **Zero external dependencies**: Built purely on core Android platform APIs.
- **Continuous Foreground Service**: `LoggerService` records telemetry uninterrupted while screen is off.
- **Sensor Telemetry**:
  - Linear Acceleration & Gravity (`Sensor.TYPE_ACCELEROMETER`)
  - Angular Velocity (`Sensor.TYPE_GYROSCOPE`)
  - Magnetic Field (`Sensor.TYPE_MAGNETIC_FIELD`)
  - Geospatial Coordinates (`LocationManager.GPS_PROVIDER` & `NETWORK_PROVIDER`)
- **Local Storage**: Streams real-time readings into CSV files on device storage.
- **HTTP Multipart Upload**: `Uploader.kt` securely transfers completed drive recordings to the backend.

### 2. FastAPI Ingestion Backend (`backend/`)
- High-performance asynchronous REST API powered by FastAPI & Uvicorn.
- **`POST /upload`**: Receives multi-part sensor archives, extracts files into `data/raw/`, and generates telemetry summaries.
- **`GET /drives`**: Lists all recorded sessions with calculated metadata (sample count, total duration, geographic bounding box).
- **`GET /drives/{drive_id}` & `/csv`**: Retrieves detailed drive metrics or streams raw CSV telemetry.
- **`GET/POST /drives/{drive_id}/markers`**: Query or submit road anomaly tags (potholes, speed bumps, road defects).
- **`GET /ping`**: Server health-check probe.

### 3. Termux Phone Scripts (`phone/`)
- Headless Python logging scripts utilizing the Termux API for automated or background test fixtures without UI overhead.

### 4. Web Application (`web/`)
- Single-page application built on React 18 and Vite 5.
- Development proxy configured to seamlessly route `/api`, `/upload`, and `/ping` to the FastAPI backend on port 8000.

### 5. Automated CI/CD (`.github/workflows/`)
- GitHub Actions pipeline runs on every push and pull request to `main`.
- Automates Gradle 8.7 / JDK 17 setup, compiles the native Kotlin app, and publishes `app-debug.apk` directly to GitHub workflow artifacts.

---

## 🛠️ Getting Started

### Prerequisites
- **Android**: Android Studio Jellyfish or newer / JDK 17
- **Backend**: Python 3.10+
- **Web**: Node.js 18+ and npm

---

### Running the Android App

#### Option A: Local Build with Gradle Wrapper
`ash
cd android
./gradlew assembleDebug      # Linux / macOS
gradlew.bat assembleDebug   # Windows
`
The compiled APK will be generated at:
`android/app/build/outputs/apk/debug/app-debug.apk`

#### Option B: GitHub Actions (No local Android SDK needed)
Push changes to GitHub and download the pre-compiled `app-debug.apk` from the **Actions** tab.

---

### Running the Backend Server

`ash
# 1. Install dependencies
pip install -r backend/requirements.txt

# 2. Start FastAPI development server
python backend/app.py
`
The server will start listening at `http://localhost:8000`. Interactive OpenAPI documentation will be accessible at `http://localhost:8000/docs`.

---

### Running the Web Frontend

`ash
# 1. Navigate to web directory
cd web

# 2. Install dependencies
npm install

# 3. Start Vite dev server
npm run dev
`
Open `http://localhost:5173` in your browser. All API calls to `/api` or `/upload` will be proxied to `http://localhost:8000`.

---

## 📊 Data Format

Drive recordings are stored as CSV files with synchronized timestamps:
- `timestamp`: Unix millisecond timestamp
- `acc_x, acc_y, acc_z`: Linear acceleration in m/s²
- `gyro_x, gyro_y, gyro_z`: Angular rate in rad/s
- `mag_x, mag_y, mag_z`: Geomagnetic field in µT
- `latitude, longitude, altitude, speed`: GPS telemetry

Large raw CSV files are excluded from Git version control; sample drive summaries (`*.summary.json`) and anomaly markers (`*_markers.csv`) are tracked in `data/raw/`.

---

## 🗺️ Planned Roadmap

The following modules are planned for subsequent iterations:
- [x] **Interactive GIS Map**: now `/dashboard` on the unified server (Leaflet, classes A–D, priority table, event clusters).
- [ ] **Automated Anomaly Detection**: rule-based pothole/bump/rough classification shipped (v1); ML model upgrade deferred.
- [x] **Persistent Database**: embedded SQLite by default; Postgres/PostGIS DDL + docker-compose included.
- [x] **Real-time Live Streaming**: retained as-is (legacy `POST /api/live` WebSocket).

---

## 🚀 Road Monitor 2.0 (phases A–E, implemented)

On top of the legacy system above, the platform now has a full sensor→map pipeline:

### Phase A — Android pipeline (`android/`)
Pure-Kotlin `com.roadmonitor.pipeline` package: 50 Hz resample, gravity/orientation fix,
0.5–20 Hz band-pass, pothole/bump/rough event detection (gyro-gated, refractory),
10 m roughness bins (rms/std/p95/kurtosis + 3 frequency bands), 30 s calibration,
trip metadata, `*_events.jsonl` / `*_segments.jsonl` outputs. WorkManager uploader
sends `POST /api/v1/trips` (gzip + idempotent `X-Trip-Id`) with automatic fallback to the
legacy multipart `/upload`, applying the 150 m start/end privacy trim and WiFi-only default.
11 JVM unit tests on synthetic signals.

### Phase B — Backend v1 API (`backend/roadmonitor/`)
FastAPI `application` mounts the untouched legacy `app` *and* the `/api/v1` router:
`POST /devices/register`, idempotent `POST /trips`, `GET /trips/{id}/status`, `GET /trips`.
Optional `X-Device-Key` auth (enforced when configured). SQLite store with the exact
schema of `backend/sql/schema_postgres.sql` (swap to Postgres with docker-compose).

### Phase C — Processing (`backend/roadmonitor/processing/`, `process.py`)
Full recompute pipeline: DBSCAN (dependency-free) or OSM link grouping via an
OSRM `/match` adapter (`mapmatch.enabled` in config), speed-normalized +
recency-decayed aggregation, percentile classes A–D, trip/device confidence with
candidate→confirmed gating, event clustering with median peak/width/type.
Endpoints: `/api/v1/analysis/{links,events,priority,recompute}`.

### Phase D — Dashboard
`GET /dashboard` (unified server only): Leaflet map with class-coloured links,
cluster popups, class/status filters, priority table (click-to-zoom), one-click recompute.

### Phase E — Validation (`backend/roadmonitor/validation.py`)
```
cd backend && .venv\\Scripts\\python.exe -m roadmonitor.validation   # or: python -m roadmonitor.validation
```
Three controlled protocols on synthetic drives (thresholds in `config.yaml → validation`):
- **repeatability** — same device twice → class concordance 1.0, score r 1.0
- **cross_device** — different devices → same result
- **speed** — shake ∝ (v/v_ref)^alpha planted physically; normalisation collapses 2.72× raw spread to 1.0×

### Quick start (backend, venv)
```bash
cd backend
pip install -r requirements.txt
python -m roadmonitor.main        # http://localhost:8000  (legacy + /api/v1 + /dashboard)
pytest tests -q                   # 36 tests
```
Docker (db + redis; api service staged for Postgres): `docker compose up`.


## Road Anomaly Priority Pipeline (rule-based, Gwalior-focused)

Additive to everything above: segments roads, flags per-segment defects from
phone sensors, and produces a repair-priority ranking. Everything is
**rule-based and explicitly labelled `threshold-based, unvalidated`** in every
API response - no accuracy claims until labelled data exists (the same I/O
schema then doubles as the ML plug-in point).

Chain: `ingest -> detect -> severity -> mapmatch -> aggregate -> priority`

- **roadnet** (`anomaly/roadnet.py`): OSMnx drive network + POIs (school,
  hospital, college, university, dormitory, marketplace, bus_station),
  cached to GeoJSON; stable `seg_<hash>` ids; user-uploaded GeoJSON
  networks supported; offline **sample grid** fallback (Gwalior centre,
  source=`sample`) so demos/tests run without the geo stack.
- **mapmatch** (`anomaly/mapmatch.py`): default = nearest-edge snap against
  a 15 m radius (config `anomaly.match_radius_m`); swap-in matchers keep the
  same event contract.
- **detect** (`anomaly/detect.py`): pothole / speed-bump / rough-roads rules,
  gyro + speed + refractory gates, per-vehicle magnitude scaling and
  normalization anchors from `config.yaml`.
- **aggregate** (`anomaly/aggregate.py`): per-segment counts, mean/p95
  severity, unique trips + vehicle types; **confirmed needs >= 3 independent
  trips** (`min_confirm_trips`); events within `boundary_grace_m` (200 m) of
  a trip's recorded start/end are flagged and excluded from confirmation and
  priority evidence. The app's 150 m privacy trim makes that region sparse.
- **priority** (`anomaly/priority.py`, configured in
  `config.yaml → anomaly.priority_weights`): `0.45*severity +
  0.20*frequency + 0.20*POI + 0.10*road_class + 0.05*vulnerable_users`.
  Severity blends event-severity p95 (70%) and mean (30%); frequency is the
  number of distinct non-boundary trips, capped at the three-trip confirmation
  threshold. POI impact uses the highest-weight POI within `poi_buffer_m`
  (250 m along the road graph); POIs more than `poi_snap_max_distance_m`
  (100 m) from the network are ignored. Vulnerable-user proximity is scored
  independently from the highest-weight POI. All components are stored and
  returned for inspection. Values remain provisional until calibrated against
  field inspections; the score is a ranking index, not a probability.

### API (additive group, no legacy endpoint touched)
```
POST /api/v1/roads/load?mode=sample|osmnx|upload:<geojson>   # (re)load network
POST /api/v1/roads/network                                   # upload GeoJSON network
POST /api/v1/roads/recompute                                 # aggregate + priority run
GET  /api/v1/roads/segments?min_priority=&poi_type=&within_m=&vehicle_type=&confirmed_only=
GET  /api/v1/roads/ranking                                   # ranked repair list
GET  /api/v1/roads/geojson                                   # map features, priority-coloured + score terms
GET  /priority-map                                           # responsive map, score legend, filters + breakdown
```

### Demo without any real data
```
cd backend
.venv\Scripts\python.exe scripts\simulate_trips.py          # fake trips on sample grid
.venv\Scripts\python.exe scripts\simulate_trips.py --source osmnx   # real net (needs osmnx)
# then: python -m roadmonitor.main and open http://localhost:8000/priority-map
pytest tests -q                   # 58 tests (13 detect + 9 pipeline + 36 phases A-E)
```
Simulated trips carry planted potholes/bumps/rough zones on interior road
positions (demo data only - never presented as real road conditions).
