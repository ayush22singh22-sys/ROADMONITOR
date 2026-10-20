"""Tranche-2 tests: roadnet, mapmatch, aggregate, priority, /roads API.
Synthetic mini-networks with controlled geometry (all thresholds from CFG)."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
DB_TMP = Path(tempfile.mkdtemp())
os.environ["ROADMONITOR_DB"] = "sqlite:///" + str(DB_TMP / "anomp_test.db")

from roadmonitor.anomaly import aggregate as agg      # noqa: E402
from roadmonitor.anomaly import mapmatch as mm        # noqa: E402
from roadmonitor.anomaly import priority as prio      # noqa: E402
from roadmonitor.anomaly import roadnet as rn         # noqa: E402
from roadmonitor.config import CFG                    # noqa: E402
from roadmonitor.store import Store, set_store        # noqa: E402
from roadmonitor.anomaly.schema import ensure_tables  # noqa: E402


def mini_net():
    """L-shaped pair: segA north 100 m then segB east 100 m (shared node)."""
    mk = lambda a, b, **kw: {**{"segment_id": kw.pop("id"), "name": kw.pop("name"),
                                "road_class": kw.pop("cls"),
                                "length_m": rn.polyline_length_m([a, b]),
                                "coords": [a, b], "source": "sample"}, **kw}
    return {
        "segments": [
            mk((26.0, 78.0), (26.001, 78.0), id="segA", name="North St",
               cls="secondary"),
            mk((26.001, 78.0), (26.001, 78.001), id="segB", name="East Rd",
               cls="residential"),
        ],
        "pois": [], "source": "sample",
    }


def parallel_net():
    """Two parallel roads joined by a connector at their START only.
    segR1: (26.0,78.0)->(26.0,78.002); segR2: (26.001,78.0)->(26.001,78.002);
    segC connects the two start nodes. A POI at segR2's END is ~111 m
    STRAIGHT-LINE from segR1's end, but ~555 m ALONG the network."""
    seg1 = {"segment_id": "segR1", "name": "Road One", "road_class": "secondary",
            "length_m": rn.polyline_length_m([(26.0, 78.0), (26.0, 78.002)]),
            "coords": [(26.0, 78.0), (26.0, 78.002)], "source": "sample"}
    seg2 = {"segment_id": "segR2", "name": "Road Two",
            "road_class": "residential",
            "length_m": rn.polyline_length_m([(26.001, 78.0), (26.001, 78.002)]),
            "coords": [(26.001, 78.0), (26.001, 78.002)], "source": "sample"}
    conn = {"segment_id": "segC", "name": "Connector", "road_class": "service",
            "length_m": rn.polyline_length_m([(26.0, 78.0), (26.001, 78.0)]),
            "coords": [(26.0, 78.0), (26.001, 78.0)], "source": "sample"}
    return {"segments": [seg1, seg2, conn], "pois": [], "source": "sample"}


CFG_MIN = {
    "anomaly": {
        "sample_hz": 50,
        "match_radius_m": 15.0, "min_confirm_trips": 3,
        "boundary_grace_m": 200.0, "poi_buffer_m": 150.0,
        "poi_weights": {"hospital": 1.0},
        "road_class_weights": {"secondary": 0.8, "residential": 0.4,
                               "unclassified": 0.4},
        "priority_weights": {"severity": 0.45, "frequency": 0.2, "poi": 0.2,
                             "road_class": 0.1, "vulnerable": 0.05},
    },
}


# ---- roadnet --------------------------------------------------------------------

def test_sample_network_stable_ids_and_store_roundtrip():
    n1 = rn.load_sample_network()
    n2 = rn.load_sample_network()
    ids1 = sorted(s["segment_id"] for s in n1["segments"])
    ids2 = sorted(s["segment_id"] for s in n2["segments"])
    assert ids1 == ids2 and len(ids1) >= 40       # stable across loads
    assert all(s["length_m"] > 100 for s in n1["segments"])
    assert {p["poi_type"] for p in n1["pois"]} == set(rn.POI_TYPES)

    s = Store("sqlite:///" + str(Path(tempfile.mkdtemp()) / "net.db"))
    ensure_tables(s)
    rn.save_to_store(s, n1)
    back = rn.load_from_store(s)
    assert len(back["segments"]) == len(n1["segments"])
    first = n1["segments"][0]
    restored = [x for x in back["segments"]
                if x["segment_id"] == first["segment_id"]][0]
    assert abs(restored["coords"][0][0] - first["coords"][0][0]) < 1e-7
    # WKT survives: version load replaces same ids, no dupes
    rn.save_to_store(s, n1)
    assert len(rn.load_from_store(s)["segments"]) == len(n1["segments"])


def test_user_geojson_upload(tmp_path):
    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "LineString",
                      "coordinates": [[78.0, 26.0], [78.0, 26.001]]},
         "properties": {"name": "Uploaded Rd", "highway": "primary"}},
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [78.0, 26.0015]},
         "properties": {"poi_type": "hospital", "name": "H1"}},
    ]}
    f = tmp_path / "net.geojson"
    f.write_text(__import__("json").dumps(doc), encoding="utf-8")
    net = rn.load_geojson_file(f)
    assert len(net["segments"]) == 1 and net["segments"][0]["road_class"] == "primary"
    assert len(net["pois"]) == 1 and net["pois"][0]["poi_type"] == "hospital"


# ---- mapmatch --------------------------------------------------------------------

def test_point_to_polyline_metric():
    d = mm.point_to_polyline_m(26.0, 78.0, [(26.0, 78.0), (26.001, 78.0)])
    assert d < 0.5
    d_off = mm.point_to_polyline_m(26.0, 78.0005, [(26.0, 78.0), (26.001, 78.0)])
    assert 40 < d_off < 60                        # ~55 m east of the line


def test_nearest_edge_snaps_and_rejects():
    net = mini_net()
    seg, d = mm.nearest_edge(26.0005, 78.0, net)   # on segA
    assert seg == "segA" and d < 0.5
    seg, d = mm.nearest_edge(26.001, 78.0005, net)  # on segB past the corner
    assert seg == "segB" and d < 0.5
    seg, d = mm.nearest_edge(26.0, 78.0005, net)    # 55 m off segA: > radius
    assert seg is None


# ---- aggregate --------------------------------------------------------------------

def _plant_events(s, seg_id, trips, lat, lon, boundary_lat=None):
    for i, t in enumerate(trips):
        # windows for the trip: 0..500 m north (boundary usable > 2*grace)
        wrows = [{"ts_start": i, "ts_end": i, "lat": 26.0 + j * 0.00225,
                  "lon": lon, "dist_m": 0, "rms": 1.0, "std": 0, "max": 1,
                  "p95": 1, "kurtosis": 0, "band_0_5": 0, "band_5_15": 0,
                  "band_15p": 0, "speed_mean": 8.0, "n": 200}
                 for j in range(6)]                # 0..~500 m
        s.insert_trip({"trip_id": t, "device_id": f"d{i}"}, events=[],
                      segments=wrows)
        s.conn.execute(
            "INSERT INTO anomaly_events (trip_id, vehicle_type, event_type,"
            " ts, lat, lon, severity, method, segment_id, features)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t, "bike", "pothole", i, lat, lon, 0.5,
             "threshold-based, unvalidated", seg_id, "{}"))
    s.conn.commit()


def test_confirmation_needs_solid_interior_trips():
    s = Store("sqlite:///" + str(Path(tempfile.mkdtemp()) / "agg.db"))
    ensure_tables(s)
    # segment ~300 m from both trip ends -> NOT boundary
    # (windows span 0..~1250 m north at ~250 m spacing)
    _plant_events(s, "segX", ["t1", "t2", "t3"], 26.0 + 0.0027, 78.02)
    rep = agg.compute(s, CFG_MIN)
    st = s.conn.execute("SELECT * FROM segment_stats WHERE segment_id=?",
                        ("segX",)).fetchone()
    assert dict(st)["confirmed"] == 1
    assert rep["confirmed"] == 1
    # boundary-adjacent event (~100 m from trip end) cannot confirm
    _plant_events(s, "segY", ["t4", "t5", "t6"], 26.0 + 0.0009, 78.03)
    agg.compute(s, CFG_MIN)
    st = s.conn.execute("SELECT * FROM segment_stats WHERE segment_id=?",
                        ("segY",)).fetchone()
    d = dict(st)
    comps = __import__("json").loads(d["components"])
    assert d["confirmed"] == 0 and comps["low_coverage"] == 1


# ---- priority --------------------------------------------------------------------

def test_poi_buffer_is_network_distance_not_straight_line():
    net = parallel_net()
    cfg = {k: v for k, v in CFG_MIN.items()}
    cfg["anomaly"] = {**cfg["anomaly"], "poi_buffer_m": 150.0}
    poi = {"poi_id": "h", "poi_type": "hospital", "lat": 26.001, "lon": 78.002,
           "name": "City Hospital"}
    idx, _ = mm._bbox_segments(net["segments"])
    hit = prio.poi_impact(poi, net, idx, cfg)
    # segR1's end node is ~111 m STRAIGHT-LINE from the POI (euclid buffer
    # would include it), but ~555 m ALONG the road graph -> excluded
    assert "segR1" not in hit
    # the POI's own road is within 150 m along-network; the connector is not
    # (it is 222 m away down the full length of road 2)
    assert "segR2" in hit and "segC" not in hit
    # widen the buffer: segR1 joins, and network distances order correctly
    cfg["anomaly"]["poi_buffer_m"] = 600.0
    hit = prio.poi_impact(poi, net, idx, cfg)
    assert hit["segR2"][3] < hit["segC"][3] < hit["segR1"][3]
    assert hit["segR2"][0] == 1.0            # hospital weight
    assert hit["segR2"][2] == "hospital"


def test_poi_too_far_from_network_is_ignored():
    net = mini_net()
    cfg = {**CFG_MIN, "anomaly": {**CFG_MIN["anomaly"],
                                  "poi_snap_max_distance_m": 100.0}}
    poi = {"poi_id": "far", "poi_type": "hospital", "lat": 26.01,
           "lon": 78.0, "name": "Far hospital"}
    idx, _ = mm._bbox_segments(net["segments"])
    assert prio.poi_impact(poi, net, idx, cfg) == {}


def test_priority_formula_components_and_order():
    s = Store("sqlite:///" + str(Path(tempfile.mkdtemp()) / "prio.db"))
    ensure_tables(s)
    net = parallel_net()
    net["pois"] = [{"poi_id": "h", "poi_type": "hospital", "lat": 26.001,
                    "lon": 78.002, "name": "City Hospital", "source": "sample"}]
    rn.save_to_store(s, net)
    # equal events on both roads, same severity -> only POI / road_class /
    # vulnerable terms may differ (POI buffer 150 m excludes segR1)
    _plant_events(s, "segR1", ["a1", "a2", "a3"], 26.0, 78.001)
    _plant_events(s, "segR2", ["b1", "b2", "b3"], 26.001, 78.001)
    agg.compute(s, CFG_MIN)
    prio.compute(s, net, CFG_MIN)
    rows = {r["segment_id"]: r for r in s.conn.execute(
        "SELECT * FROM segment_stats").fetchall()}
    ra, rb = dict(rows["segR1"]), dict(rows["segR2"])
    ca = __import__("json").loads(ra["components"])["priority_terms"]
    cb = __import__("json").loads(rb["components"])["priority_terms"]
    assert ca["poi"] == 0.0 and ca["vulnerable"] == 0.0   # far along-network
    assert cb["poi"] == 1.0 and cb["vulnerable"] == 1.0   # hospital on segR2
    assert ca["road_class"] == 0.8 and cb["road_class"] == 0.4
    assert ra["priority"] < rb["priority"]


def test_priority_uses_boundary_excluded_trips_and_blended_severity():
    s = Store("sqlite:///" + str(Path(tempfile.mkdtemp()) / "terms.db"))
    ensure_tables(s)
    net = mini_net()
    rn.save_to_store(s, net)
    _plant_events(s, "segA", ["e1", "e2", "e3"], 26.0005, 78.0)
    agg.compute(s, CFG_MIN)
    s.conn.execute(
        "UPDATE segment_stats SET sev_mean=0.2, sev_p95=0.8, components=?"
        " WHERE segment_id='segA'",
        ('{"solid_trip_count":1,"boundary_events":2}',))
    s.conn.commit()
    cfg = {**CFG_MIN, "anomaly": {**CFG_MIN["anomaly"],
                                  "severity_p95_weight": 0.7}}
    prio.compute(s, net, cfg)
    row = dict(s.conn.execute(
        "SELECT * FROM segment_stats WHERE segment_id='segA'").fetchone())
    terms = __import__("json").loads(row["components"])["priority_terms"]
    assert terms["severity"] == 0.62
    assert terms["frequency"] == pytest.approx(1 / 3, abs=1e-4)
    assert terms["solid_trip_count"] == 1


def test_priority_weights_respect_formula():
    s = Store("sqlite:///" + str(Path(tempfile.mkdtemp()) / "wf.db"))
    ensure_tables(s)
    net = mini_net()
    rn.save_to_store(s, net)
    _plant_events(s, "segA", ["w1", "w2", "w3"], 26.0005, 78.0)
    agg.compute(s, CFG_MIN)
    prio.compute(s, net, CFG_MIN)
    r = dict(s.conn.execute(
        "SELECT * FROM segment_stats WHERE segment_id='segA'").fetchone())
    t = __import__("json").loads(r["components"])["priority_terms"]
    w = CFG_MIN["anomaly"]["priority_weights"]
    manual = (w["severity"] * t["severity"] + w["frequency"] * t["frequency"]
              + w["poi"] * t["poi"] + w["road_class"] * t["road_class"]
              + w["vulnerable"] * t["vulnerable"])
    assert abs(manual - r["priority"]) < 1e-3


# ---- API + dashboard --------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from roadmonitor.main import application
    s = Store(os.environ["ROADMONITOR_DB"])
    set_store(s)
    return TestClient(application), s


def test_roads_end_to_end(client):
    tc, s = client
    ensure_tables(s)
    r = tc.post("/api/v1/roads/load?mode=sample")
    assert r.status_code == 200 and r.json()["source"] == "sample"
    # simulate a couple of confirmed hits on two segments
    net = rn.load_from_store(s)
    segs = net["segments"][:3]
    for k, seg in enumerate(segs):
        _plant_events(s, seg["segment_id"],
                      [f"tr{k}a", f"tr{k}b", f"tr{k}c"],
                      seg["coords"][0][0] + 0.001, seg["coords"][0][1])
    rep = tc.post("/api/v1/roads/recompute")
    assert rep.status_code == 200
    body = rep.json()
    assert body["n_segments"] > 20 and body["method"] == "threshold-based, unvalidated"

    segs_json = tc.get("/api/v1/roads/segments", params={"min_priority": 0.0}).json()
    assert isinstance(segs_json, list) and len(segs_json) > 0
    assert all(set(("priority", "color", "priority_terms")) <= set(x) for x in segs_json[:5])

    ranking = tc.get("/api/v1/roads/ranking").json()
    assert ranking and ranking[0]["rank"] == 1
    assert ranking[0]["priority"] >= ranking[-1]["priority"]

    geo = tc.get("/api/v1/roads/geojson").json()
    assert geo["type"] == "FeatureCollection" and geo["features"]
    props = geo["features"][0]["properties"]
    assert props["color"] in ("#ef4d4d", "#f2b23e", "#a8d841", "#3fdc74")
    assert props["method"] == "threshold-based, unvalidated"
    assert "priority_terms" in props

    confirmed = tc.get("/api/v1/roads/ranking",
                       params={"confirmed_only": "true"}).json()
    assert all(r["confirmed"] for r in confirmed)
    assert "priority_terms" in ranking[0]
    assert "vehicle_types" in ranking[0]

    dash = tc.get("/priority-map")
    assert dash.status_code == 200
    assert "repair-priority" in dash.text.lower()
