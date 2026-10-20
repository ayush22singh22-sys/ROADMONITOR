"""API v1 routes (Phase B).

Auth: optional X-Device-Key (sha256-checked in the store). Lenient until the
Phase A app learns to register keys; flip config api.auth.require_device_key
to enforce.
"""
import gzip
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import cfg_get
from .store import get_store

router = APIRouter(prefix="/api/v1")


# ---- auth -------------------------------------------------------------------

def check_device_key(x_device_key: str | None = Header(default=None)) -> None:
    required = cfg_get("api.auth.require_device_key", False)
    if x_device_key is None:
        if required:
            raise HTTPException(401, "missing X-Device-Key")
        return
    if not get_store().device_key_is_known(x_device_key):
        raise HTTPException(401, "unknown device key")


# ---- devices ------------------------------------------------------------------

class DeviceRegisterIn(BaseModel):
    name: str = ""


@router.post("/devices/register")
def device_register(body: DeviceRegisterIn):
    """Returns a per-device secret once. Only its sha256 is stored."""
    return get_store().register_device(body.name)


# ---- trips ---------------------------------------------------------------------

class EventIn(BaseModel):
    ts: int
    lat: float | None = None
    lon: float | None = None
    dist_m: float | None = None
    peak: float | None = None
    p2p: float | None = None
    width_ms: float | None = None
    jerk: float | None = None
    energy: float | None = None
    speed: float | None = None
    gyro_peak: float | None = None
    speed_drop: float | None = None
    type_hint: str = "rough"


class SegmentIn(BaseModel):
    ts_start: int
    ts_end: int
    lat: float | None = None
    lon: float | None = None
    dist_m: float | None = None
    rms: float | None = None
    std: float | None = None
    max: float | None = None
    p95: float | None = None
    kurtosis: float | None = None
    band_0_5: float | None = None
    band_5_15: float | None = None
    band_15p: float | None = None
    speed_mean: float | None = None
    n: int | None = Field(default=None, ge=0)


class TripIn(BaseModel):
    """Body = the app's upload JSON (plan 4.4). metadata is kept raw."""
    metadata: dict
    events: list[EventIn] = []
    segments: list[SegmentIn] = []

    @field_validator("metadata")
    @classmethod
    def metadata_has_trip_id(cls, v: dict) -> dict:
        if not str(v.get("trip_id") or "").strip():
            raise ValueError("metadata.trip_id is required")
        return v


async def _read_json_gzip(request: Request) -> dict:
    raw = await request.body()
    limit = int(cfg_get("api.request.max_body_mb", 25) * 1024 * 1024)
    if len(raw) > limit:
        raise HTTPException(413, "body too large")
    if request.headers.get("content-encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            raise HTTPException(400, "invalid gzip body")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "invalid JSON body")


@router.post("/trips", dependencies=[Depends(check_device_key)])
async def post_trip(request: Request):
    """Receives one finished trip. Idempotent on trip_id:
    re-uploads return 200 with duplicate=true and no double inserts."""
    body = await _read_json_gzip(request)
    try:
        trip = TripIn.model_validate_json(json.dumps(body))
    except ValidationError as ve:
        raise HTTPException(422, str(ve))
    events = [e.model_dump() for e in trip.events]
    segments = [s.model_dump() for s in trip.segments]
    result = get_store().insert_trip(trip.metadata, events, segments)
    return result


@router.get("/trips/{trip_id}/status", dependencies=[Depends(check_device_key)])
def trip_status(trip_id: str):
    status = get_store().trip_status(trip_id)
    if status is None:
        raise HTTPException(404, "unknown trip_id")
    return status


@router.get("/trips", dependencies=[Depends(check_device_key)])
def trips_list(limit: int = 50):
    """Ops listing (dashboard / debugging). Newest first."""
    limit = max(1, min(limit, 500))
    return {"trips": get_store().list_trips(limit)}


# ---- Phase C: processing results ------------------------------------------------

@router.get("/analysis/links", dependencies=[Depends(check_device_key)])
def analysis_links(min_status: str = "candidate", limit: int = 1000):
    """Aggregated road links (roughness classes + confidence)."""
    limit = max(1, min(limit, 5000))
    return get_store().links(min_status, limit)


@router.get("/analysis/events", dependencies=[Depends(check_device_key)])
def analysis_events(min_status: str = "candidate", limit: int = 1000):
    """Clustered events (suspected potholes / breakers)."""
    limit = max(1, min(limit, 5000))
    return get_store().event_clusters(min_status, limit)


@router.get("/analysis/priority", dependencies=[Depends(check_device_key)])
def analysis_priority(min_status: str = "candidate", limit: int = 1000):
    """Links ranked by fix-first priority (class x confidence x recency)."""
    limit = max(1, min(limit, 5000))
    from .processing.priority import rank_links
    from .config import CFG
    from .processing.aggregate import now_ms_utc
    return rank_links(get_store().links(min_status, limit), CFG, now_ms_utc())


@router.post("/analysis/recompute", dependencies=[Depends(check_device_key)])
def analysis_recompute():
    """Inline recompute (dev). In the docker deployment Celery runs this."""
    from .process import recompute_all
    return recompute_all()


# ---- Road Anomaly Priority Pipeline (additive, no legacy endpoint touched) ------

COLOR_STOPS = [  # priority band -> colour (green -> red)
    (0.75, "#ef4d4d"), (0.55, "#f2b23e"), (0.35, "#a8d841"), (0.0, "#3fdc74"),
]


def _color(priority):
    for stop, c in COLOR_STOPS:
        if priority >= stop:
            return c
    return "#3fdc74"


@router.post("/roads/load")
def roads_load(mode: str = "auto"):
    """(Re)load the road network + POIs into the store.
    mode: 'sample' (offline demo grid) | 'osmnx' (needs osmnx installed)
          | 'upload:<path/to/file.geojson>' | 'auto'."""
    from .anomaly import roadnet as rn
    net = rn.load_network(get_store(), mode)
    return {"source": net.get("source"), "segments": len(net["segments"]),
            "pois": len(net["pois"])}


@router.post("/roads/network")
async def roads_upload_network(request):
    """User-uploaded road network (GeoJSON FeatureCollection: LineStrings =
    segments, Points with poi_type = POIs)."""
    from .anomaly import roadnet as rn
    import json
    body = await request.body()
    if body[:2] == b"\x1f\x8b":
        import gzip
        body = gzip.decompress(body)
    import tempfile, os
    f = tempfile.NamedTemporaryFile(suffix=".geojson", delete=False)
    f.write(body)
    f.close()
    net = rn.load_geojson_file(f.name, source="upload")
    rn.save_to_store(get_store(), net, source="upload")
    os.unlink(f.name)
    return {"source": "upload", "segments": len(net["segments"]),
            "pois": len(net["pois"])}


@router.post("/roads/recompute")
def roads_recompute(mode: str = "auto"):
    """detect→...→priority full run trigger (dev convenience; simulate_trips.py
    calls the same functions directly)."""
    from .anomaly.pipeline import recompute as rec
    from .anomaly.schema import ensure_tables
    from .config import CFG
    ensure_tables(get_store())
    return rec(get_store(), CFG, net_mode=mode)


@router.get("/roads/segments")
def roads_segments(min_priority: float = 0.0, poi_type: str = "",
                   within_m: int = 250, vehicle_type: str = "",
                   confirmed_only: bool = False, limit: int = 2000):
    """Segment stats filtered by priority / nearby POI type (network dist)
    / vehicle type / confirmation."""
    store = get_store()
    from .anomaly.schema import ensure_tables
    ensure_tables(store)
    sql = ("SELECT st.*, ns.name, ns.road_class FROM segment_stats st JOIN"
           " net_segments ns ON ns.segment_id = st.segment_id"
           " WHERE st.priority >= ?")
    args = [min_priority]
    if confirmed_only:
        sql += " AND st.confirmed = 1"
    sql += " ORDER BY st.priority DESC LIMIT ?"
    args.append(max(1, min(limit, 5000)))
    rows = [dict(r) for r in store.conn.execute(sql, args)]
    if poi_type:
        rows = [r for r in rows if _nearest_poi(store, r["segment_id"],
                                                poi_type, within_m) is not None]
    if vehicle_type:
        rows = [r for r in rows if store.conn.execute(
            "SELECT COUNT(*) FROM anomaly_events WHERE segment_id=?"
            " AND vehicle_type=?", (r["segment_id"], vehicle_type)).fetchone()[0] > 0]
    out = []
    for r in rows:
        comps = json.loads(r.pop("components") or "{}") or {}
        r["priority_terms"] = comps.get("priority_terms") or {}
        r["color"] = _color(r["priority"] or 0.0)
        out.append(r)
    return out


def _nearest_poi(store, segment_id: str, poi_type: str, within_m: int):
    """Re-queries the network-distance POI match for one segment using the
    stored priority-term metadata first, then graph recompute as fallback."""
    comps = store.conn.execute(
        "SELECT components FROM segment_stats WHERE segment_id=?",
        (segment_id,)).fetchone()
    terms = (json.loads(comps[0]) or {}).get("priority_terms", {}) if comps else {}
    if terms.get("poi_type") == poi_type:
        d = terms.get("poi_dist_m")
        if d is None or d <= within_m:
            return terms.get("poi_name")
    from .anomaly.priority import poi_impact, _bbox_segments  # recomputed live
    from .anomaly import roadnet as rn
    from .config import CFG
    net = rn.load_from_store(store)
    if not net["segments"]:
        return None
    poi = store.conn.execute(
        "SELECT poi_id, poi_type, lat, lon, name FROM net_pois"
        " WHERE poi_type=? LIMIT 100", (poi_type,)).fetchall()
    idx, _ = _bbox_segments(net["segments"])
    for p in [dict(zip(("poi_id", "poi_type", "lat", "lon", "name"), r))
              for r in poi]:
        if segment_id in poi_impact(p, net, idx, CFG):
            return p.get("name") or p["poi_type"]
    return None


@router.get("/roads/ranking")
def roads_ranking(min_priority: float = 0.0, confirmed_only: bool = False,
                  vehicle_type: str = "", limit: int = 100):
    """Ranked repair list: rank, road name, severity, nearest POI + its
    network distance, priority, trip count."""
    store = get_store()
    from .anomaly.schema import ensure_tables
    ensure_tables(store)
    sql = ("SELECT st.*, ns.name, ns.road_class FROM segment_stats st"
           " JOIN net_segments ns ON ns.segment_id = st.segment_id"
           " WHERE st.priority >= ?")
    args = [min_priority]
    if confirmed_only:
        sql += " AND st.confirmed = 1"
    if vehicle_type:
        sql += (" AND EXISTS (SELECT 1 FROM anomaly_events ae WHERE"
                " ae.segment_id = st.segment_id AND ae.vehicle_type = ?)")
        args.append(vehicle_type)
    sql += " ORDER BY st.priority DESC LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    out = []
    rows = [dict(r) for r in store.conn.execute(sql, args)]
    rows.sort(key=lambda r: (-(r["priority"] or 0.0), -(r["sev_p95"] or 0.0)))
    vehicle_types = {}
    for vehicle_type, segment_id in store.conn.execute(
            "SELECT DISTINCT vehicle_type, segment_id FROM anomaly_events"
            " WHERE vehicle_type IS NOT NULL AND segment_id IS NOT NULL"):
        vehicle_types.setdefault(segment_id, []).append(vehicle_type)
    for i, r in enumerate(rows, 1):
        comps = json.loads(r.pop("components") or "{}") or {}
        terms = comps.get("priority_terms") or {}
        out.append({
            "rank": i,
            "road_name": r.get("name"),
            "segment_id": r["segment_id"],
            "severity": terms.get("severity", r.get("sev_p95")),
            "severity_p95": r.get("sev_p95"),
            "severity_mean": r.get("sev_mean"),
            "nearest_poi": terms.get("poi_name"),
            "poi_type": terms.get("poi_type"),
            "poi_dist_m": terms.get("poi_dist_m"),
            "priority": r.get("priority"),
            "trip_count": r.get("n_trips"),
            "solid_trip_count": terms.get("solid_trip_count", r.get("n_trips")),
            "n_events": r.get("n_events"),
            "road_class": r.get("road_class"),
            "vehicle_types": vehicle_types.get(r["segment_id"], []),
            "confirmed": bool(r.get("confirmed")),
            "priority_terms": terms,
            "method": "threshold-based, unvalidated",
        })
    return out


@router.get("/roads/geojson")
def roads_geojson(min_priority: float = 0.0, confirmed_only: bool = False):
    """Map-ready FeatureCollection; segments coloured by priority."""
    store = get_store()
    from .anomaly.schema import ensure_tables
    ensure_tables(store)
    sql = ("SELECT st.priority, st.sev_mean, st.sev_p95, st.n_events, st.n_trips,"
           " st.confirmed, st.components, ns.segment_id, ns.geom, ns.name, ns.road_class"
           " FROM segment_stats st JOIN net_segments ns ON"
           " ns.segment_id = st.segment_id WHERE st.priority >= ?")
    args = [min_priority]
    if confirmed_only:
        sql += " AND st.confirmed = 1"
    feats = []
    for r in store.conn.execute(sql, args):
        d = dict(r)
        components = json.loads(d.pop("components") or "{}") or {}
        terms = components.get("priority_terms") or {}
        props = {"segment_id": d["segment_id"], "name": d["name"],
                 "road_class": d["road_class"], "priority": d["priority"],
                 "sev_p95": d["sev_p95"], "n_trips": d["n_trips"],
                 "solid_trip_count": terms.get("solid_trip_count", d["n_trips"]),
                 "n_events": d["n_events"], "confirmed": bool(d["confirmed"]),
                 "priority_terms": terms,
                 "color": _color(d["priority"] or 0.0),
                 "method": "threshold-based, unvalidated"}
        feats.append({"type": "Feature",
                      "geometry": _wkt_to_geojson(d["geom"]),
                      "properties": props})
    return {"type": "FeatureCollection", "features": feats}


def _wkt_to_geojson(wkt: str) -> dict:
    body = wkt[wkt.index("(") + 1:wkt.rindex(")")]
    pts = [[float(lo), float(la)] for lo, la in
           (p.split() for p in body.split(", "))]
    return {"type": "LineString", "coordinates": pts}
