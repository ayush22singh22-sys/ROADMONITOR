"""Repair-priority scoring (priority.py).

priority = w1*severity + w2*frequency + w3*poi + w4*road_class + w5*vulnerable
All five terms are normalised to 0..1 and the w* weights to 1.0 in total
(config anomaly.priority_weights). The weights are engineering defaults -
set them formally later via AHP pairwise comparison; the formula needs no
change when the weights change (that is the point of keeping them in
config.yaml).

  severity   - weighted blend of segment sev_p95 and sev_mean
  frequency  - min(1, solid_trip_count / min_confirm_trips); boundary events
               do not add evidence because the privacy trim reduces coverage
  poi        - highest POI weight (hospital 1.0, school 0.9, hostel/college/
               university 0.7, marketplace/bus_stand 0.6) among POIs within
               poi_buffer_m measured ALONG THE ROAD NETWORK (graph distance
               from the segment's endpoint nodes), not straight-line; POIs
               must also snap to the network within poi_snap_max_distance_m
  road_class - class weight (primary 1.0 ... service 0.2) from OSM highway
  vulnerable - independently 1.0 if any vulnerable-users POI (school/
               hospital/college/university/dormitory) is within the buffer

Network distance: Dijkstra over the road graph (nodes = segment endpoints,
edge length = polyline length), multi-source from each POI anchored to its
nearest edge, limited to poi_buffer_m.
"""
import heapq
import json
import math
from datetime import datetime, timezone

from .mapmatch import _bbox_segments

VULNERABLE_TYPES = {"school", "hospital", "college", "university", "dormitory"}


# ---- road graph ----------------------------------------------------------------

def build_graph(net: dict) -> dict:
    """Undirected adjacency: node -> [(node, length_m, segment_id)]."""
    graph = {}
    for s in net["segments"]:
        coords = s["coords"]
        u, v = (round(coords[0][0], 7), round(coords[0][1], 7)), \
               (round(coords[-1][0], 7), round(coords[-1][1], 7))
        length = s.get("length_m") or 0.0
        graph.setdefault(u, []).append((v, length, s["segment_id"]))
        graph.setdefault(v, []).append((u, length, s["segment_id"]))
    return graph


def _dijkstra(graph, sources: dict, limit: float) -> dict:
    """sources: node -> start distance (virtual POI nodes anchor at 0).
    Returns node -> best distance, cutoff at limit."""
    dist = {}
    pq = [(d, n) for n, d in sources.items()]
    heapq.heapify(pq)
    while pq:
        d, n = heapq.heappop(pq)
        if n in dist:
            continue
        dist[n] = d
        if d >= limit:
            continue
        for v, w, _seg in graph.get(n, ()):
            nd = d + w
            if v not in dist and nd <= limit:
                heapq.heappush(pq, (nd, v))
    return dist


def _project_onto_segment(lat: float, lon: float, coords: list) -> tuple:
    """Return the nearest point's distance and along-polyline offset in metres."""
    from .roadnet import polyline_length_m

    clat = sum(c[0] for c in coords) / len(coords)
    kx = 111_320.0 * math.cos(math.radians(clat))
    ky = 110_574.0
    px, py = lon * kx, lat * ky
    best_d, best_along, cumulative = float("inf"), 0.0, 0.0
    for a, b in zip(coords, coords[1:]):
        ax, ay = a[1] * kx, a[0] * ky
        bx, by = b[1] * kx, b[0] * ky
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        fraction = (0.0 if length_sq == 0 else
                    max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy)
                                 / length_sq)))
        d = math.hypot(px - (ax + fraction * dx),
                       py - (ay + fraction * dy))
        edge_length = polyline_length_m([a, b])
        if d < best_d:
            best_d = d
            best_along = cumulative + fraction * edge_length
        cumulative += edge_length
    return best_d, best_along


def snap_poi_to_graph(poi: dict, net: dict, index=None,
                      max_distance_m: float = 100.0) -> dict:
    """Anchors a nearby POI to its nearest edge, returning virtual nodes
    {u: d_from_u, v: length - d_from_u}. Far-off POIs are rejected so an
    unrelated location cannot raise a road's score."""
    best, best_d = None, float("inf")
    best_along = 0.0
    for i, s in enumerate(net["segments"]):
        d, along = _project_onto_segment(poi["lat"], poi["lon"], s["coords"])
        if d < best_d:
            best, best_d, best_along = s, d, along
    if best is None or best_d > max_distance_m:
        return {}
    coords = best["coords"]
    from .roadnet import polyline_length_m
    L = best.get("length_m") or polyline_length_m(coords)
    u = (round(coords[0][0], 7), round(coords[0][1], 7))
    v = (round(coords[-1][0], 7), round(coords[-1][1], 7))
    return {u: best_along, v: max(0.0, L - best_along)}


# ---- priority ------------------------------------------------------------------

def poi_impact(poi: dict, net: dict, index, cfg) -> dict:
    """Dijkstra-limited buffer for one POI: returns
    {segment_id: (weight, poi_name, poi_type, network_dist_m)} over the
    segments within poi_buffer_m measured along the road network."""
    anchor = snap_poi_to_graph(
        poi, net, index,
        max_distance_m=cfg["anomaly"].get("poi_snap_max_distance_m", 100.0))
    if not anchor:
        return {}
    graph = build_graph(net)
    dist = _dijkstra(_graph_with_virtual(graph, anchor), {"__poi__": 0.0},
                     cfg["anomaly"]["poi_buffer_m"])
    # a segment is touched when either endpoint node is within the buffer
    w = cfg["anomaly"]["poi_weights"].get(poi["poi_type"], 0.3)
    out = {}
    for s in net["segments"]:
        u = (round(s["coords"][0][0], 7), round(s["coords"][0][1], 7))
        v = (round(s["coords"][-1][0], 7), round(s["coords"][-1][1], 7))
        d = min(dist.get(u, math.inf), dist.get(v, math.inf))
        if d <= cfg["anomaly"]["poi_buffer_m"]:
            out[s["segment_id"]] = (w, p_name(poi), poi["poi_type"], d)
    return out


def p_name(poi: dict) -> str:
    return poi.get("name") or poi["poi_type"]


def _graph_with_virtual(graph, anchor: dict):
    g = {n: list(nbrs) for n, nbrs in graph.items()}
    g["__poi__"] = [(n, d, "__poi__") for n, d in anchor.items()]
    for n, d in anchor.items():
        g.setdefault(n, []).append(("__poi__", d, "__poi__"))
    return g


def compute(store, net: dict, cfg) -> dict:
    """Full priority recompute: reads segment_stats, adds priority +
    components, writes back. Returns summary counts."""
    import json as _json
    w = cfg["anomaly"]["priority_weights"]
    min_trips = cfg["anomaly"].get("min_confirm_trips", 3)
    class_w = cfg["anomaly"]["road_class_weights"]

    pois = [dict(r) for r in store.conn.execute(
        "SELECT * FROM net_pois").fetchall()]
    index, _ = _bbox_segments(net["segments"])
    poi_by_seg = {}
    vulnerable_by_seg = set()
    for p in pois:
        for seg_id, (pw, pname, ptype, pd) in poi_impact(p, net, index, cfg).items():
            if ptype in VULNERABLE_TYPES:
                vulnerable_by_seg.add(seg_id)
            cur = poi_by_seg.get(seg_id)
            if cur is None or pw > cur[0]:
                poi_by_seg[seg_id] = (pw, pname, ptype, pd)

    road_class = {}
    for s in net["segments"]:
        road_class[s["segment_id"]] = s.get("road_class") or "unclassified"

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updated = 0
    with store.lock:
        rows = [dict(r) for r in store.conn.execute(
            "SELECT * FROM segment_stats").fetchall()]
        for st in rows:
            comps = _json.loads(st.get("components") or "{}")
            p95_weight = cfg["anomaly"].get("severity_p95_weight", 0.7)
            severity = (p95_weight * (st.get("sev_p95") or 0.0)
                        + (1.0 - p95_weight) * (st.get("sev_mean") or 0.0))
            solid_trip_count = comps.get("solid_trip_count")
            if solid_trip_count is None:
                solid_trip_count = st.get("n_trips") or 0
            freq = min(1.0, solid_trip_count / max(min_trips, 1))
            cur = poi_by_seg.get(st["segment_id"], (0.0, None, "", None))
            poi_c = cur[0]
            poi_name, poi_type, poi_dist = cur[1], cur[2], cur[3]
            vuln = 1.0 if st["segment_id"] in vulnerable_by_seg else 0.0
            rc = class_w.get(road_class.get(st["segment_id"], "unclassified"), 0.4)
            priority = (w["severity"] * severity + w["frequency"] * freq
                        + w["poi"] * poi_c + w["road_class"] * rc
                        + w["vulnerable"] * vuln)
            components = {
                "priority_terms": {
                    "severity": round(severity, 4),
                    "severity_p95": round(st.get("sev_p95") or 0.0, 4),
                    "severity_mean": round(st.get("sev_mean") or 0.0, 4),
                    "frequency": round(freq, 4),
                    "solid_trip_count": solid_trip_count,
                    "reported_trip_count": st.get("n_trips") or 0,
                    "boundary_events": comps.get("boundary_events", 0),
                    "poi": poi_c,
                    "poi_name": poi_name,
                    "poi_type": poi_type,
                    "poi_dist_m": round(poi_dist, 1) if poi_dist is not None else None,
                    "poi_snap_max_distance_m": cfg["anomaly"].get(
                        "poi_snap_max_distance_m", 100.0),
                    "poi_buffer_m": cfg["anomaly"]["poi_buffer_m"],
                    "road_class": rc,
                    "vulnerable": vuln,
                    "weights": w,
                    "min_confirm_trips": min_trips,
                    "severity_p95_weight": p95_weight,
                    "method": "threshold-based, unvalidated",
                },
                **comps,
            }
            store.conn.execute(
                "UPDATE segment_stats SET priority=?, components=?, updated_at=?"
                " WHERE segment_id=?",
                (round(priority, 4), _json.dumps(components), now,
                 st["segment_id"]))
            updated += 1
        store.conn.commit()
    return {"scored": updated,
            "confirmed": sum(1 for s in rows if s.get("confirmed"))}
