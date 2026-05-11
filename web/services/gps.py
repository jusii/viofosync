"""GPS aggregation — read .gpx sidecars for a day and split
them into journeys.

A "journey" is a continuous movement segment. We detect stops
by looking for speed ≈ 0 sustained for more than ``stop_gap``
minutes, then split the track there. A time gap in the points
themselves (e.g. the camera was off for 20 minutes) also
ends the current journey even if speed never dropped.

Inputs are standard GPX 1.0 files written by viofosync's own
:func:`generate_gpx`. We parse just the elements we need with
``xml.etree`` — no external dep on gpxpy.
"""

from __future__ import annotations

import datetime as _dt
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

_GPX_NS = "{http://www.topografix.com/GPX/1/0}"

# Tunables — could be surfaced as settings later.
MAX_REASONABLE_SPEED_MPS = 85.0   # ≈ 306 km/h; well above road speeds
OUTLIER_MIN_JUMP_M = 2000.0       # ignore normal GPS wobble
OUTLIER_RETURN_RADIUS_M = 150.0   # "jump away then come back"

# Stop / journey detection (points → stops → journeys pipeline).
STOP_RADIUS_M = 50.0              # cluster tightness
MIN_STOP_DURATION_S = 300         # 5 min — long enough to "count"
MIN_DEPARTURE_DURATION_S = 60     # motion must last this to end a stop
MIN_JOURNEY_DISTANCE_M = 200.0    # drop trivial driveway shuffles
# Time-gap splitter: if two consecutive GPS fixes are more than
# this far apart in time, the camera was probably off. Treat
# each side as a separate "session" so a big teleport (home →
# parked elsewhere) can't be drawn as a single journey.
SESSION_GAP_SECONDS = 1800        # 30 minutes


@dataclass
class Point:
    t: _dt.datetime
    lat: float
    lon: float
    speed: float
    bearing: float


@dataclass
class Stop:
    start_time: _dt.datetime
    end_time: _dt.datetime
    center_lat: float
    center_lon: float
    point_count: int

    @property
    def duration_s(self) -> float:
        return (self.end_time - self.start_time).total_seconds()


@dataclass
class Journey:
    start_time: _dt.datetime
    end_time: _dt.datetime
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    distance_m: float
    points: List[Point] = field(default_factory=list)


def _parse_gpx(path: str) -> List[Point]:
    points: List[Point] = []
    try:
        tree = ET.parse(path)
    except (ET.ParseError, FileNotFoundError):
        return points
    root = tree.getroot()
    for trkpt in root.iter(f"{_GPX_NS}trkpt"):
        try:
            lat = float(trkpt.attrib["lat"])
            lon = float(trkpt.attrib["lon"])
        except (KeyError, ValueError):
            continue

        t_elem = trkpt.find(f"{_GPX_NS}time")
        if t_elem is None or not t_elem.text:
            continue
        try:
            # Format written by generate_gpx: "YYYY-MM-DDTHH:MM:SSZ".
            # The dashcam's GPS chipset always emits UTC, so we
            # tag the parsed datetime as UTC-aware. This matters
            # in DST: filenames track the camera's local clock
            # (BST = UTC+1 in summer) while the GPX times are
            # UTC. Without tzinfo, .timestamp() would re-interpret
            # them as local time and shift every fix by an hour,
            # so clip↔journey matching would be off by 1h.
            t = _dt.datetime.strptime(
                t_elem.text, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue

        sp = 0.0
        sp_elem = trkpt.find(f"{_GPX_NS}speed")
        if sp_elem is not None and sp_elem.text:
            try:
                sp = float(sp_elem.text)
            except ValueError:
                pass

        br = 0.0
        br_elem = trkpt.find(f"{_GPX_NS}course")
        if br_elem is not None and br_elem.text:
            try:
                br = float(br_elem.text)
            except ValueError:
                pass

        points.append(Point(t, lat, lon, sp, br))
    return points


def _haversine(a: Point, b: Point) -> float:
    """Great-circle distance in metres between two points."""
    import math
    r = 6371000.0
    lat1 = math.radians(a.lat)
    lat2 = math.radians(b.lat)
    dlat = math.radians(b.lat - a.lat)
    dlon = math.radians(b.lon - a.lon)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(h))


def _filter_outliers(points: List[Point]) -> List[Point]:
    """Drop GPS fixes that lie far from the median of their
    5-point neighbourhood.

    Median-of-5 tolerates *pairs* of consecutive bad fixes
    where median-of-3 would fail (two bad neighbours outvote
    one good one)."""
    import math

    n = len(points)
    if n < 3:
        return points
    half = 2 if n >= 5 else 1

    threshold_m = OUTLIER_RETURN_RADIUS_M
    r = 6371000.0

    filtered: List[Point] = []
    for idx, curr in enumerate(points):
        center = min(max(idx, half), n - 1 - half)
        window = points[center - half : center + half + 1]

        lats = sorted(p.lat for p in window)
        lons = sorted(p.lon for p in window)
        median_lat = lats[len(lats) // 2]
        median_lon = lons[len(lons) // 2]

        phi1 = math.radians(curr.lat)
        phi2 = math.radians(median_lat)
        dphi = math.radians(median_lat - curr.lat)
        dlam = math.radians(median_lon - curr.lon)
        h = (math.sin(dphi / 2) ** 2
             + math.cos(phi1) * math.cos(phi2)
             * math.sin(dlam / 2) ** 2)
        dist = 2 * r * math.asin(math.sqrt(h))

        if dist > threshold_m:
            continue
        filtered.append(curr)

    return filtered


def _haversine_ll(
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> float:
    import math
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    h = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(h))


def detect_stops(points: List[Point]) -> List[Stop]:
    """State-machine stop detector.

    Two states: MOVING (possibly accumulating a stationary
    candidate) and STATIONARY (in a confirmed stop).

    - In MOVING, every new point checks distance from the
      current anchor. Inside the radius → append to candidate.
      Outside → reset the anchor to the new point and restart
      the candidate around it. If the candidate lives for at
      least ``MIN_STOP_DURATION_S``, the state flips to
      STATIONARY.
    - In STATIONARY, new points outside the radius start a
      departure timer; we only close the stop once that timer
      hits ``MIN_DEPARTURE_DURATION_S`` of sustained motion,
      so single stray fixes don't end the stop prematurely."""
    stops: List[Stop] = []
    if len(points) < 2:
        return stops

    MOVING, STATIONARY = 0, 1
    state = MOVING
    anchor: Optional[Point] = None
    candidate_start_t: Optional[_dt.datetime] = None
    candidate_points: List[Point] = []
    confirmed_start_t: Optional[_dt.datetime] = None
    confirmed_end_t: Optional[_dt.datetime] = None
    departure_start_t: Optional[_dt.datetime] = None

    def _emit() -> None:
        if confirmed_start_t is None or confirmed_end_t is None:
            return
        n = len(candidate_points) or 1
        cx = sum(p.lat for p in candidate_points) / n
        cy = sum(p.lon for p in candidate_points) / n
        stops.append(Stop(
            start_time=confirmed_start_t,
            end_time=confirmed_end_t,
            center_lat=cx,
            center_lon=cy,
            point_count=n,
        ))

    for p in points:
        if state == MOVING:
            inside = (
                anchor is not None
                and _haversine_ll(
                    anchor.lat, anchor.lon, p.lat, p.lon
                ) <= STOP_RADIUS_M
            )
            if not inside:
                # Reset candidate around this new anchor.
                anchor = p
                candidate_start_t = p.t
                candidate_points = [p]
            else:
                candidate_points.append(p)
                if (
                    candidate_start_t is not None
                    and (p.t - candidate_start_t).total_seconds()
                    >= MIN_STOP_DURATION_S
                ):
                    state = STATIONARY
                    confirmed_start_t = candidate_start_t
                    confirmed_end_t = p.t
                    departure_start_t = None
        else:  # STATIONARY
            if _haversine_ll(
                anchor.lat, anchor.lon, p.lat, p.lon
            ) <= STOP_RADIUS_M:
                candidate_points.append(p)
                confirmed_end_t = p.t
                departure_start_t = None
            else:
                if departure_start_t is None:
                    departure_start_t = p.t
                if (
                    (p.t - departure_start_t).total_seconds()
                    >= MIN_DEPARTURE_DURATION_S
                ):
                    _emit()
                    # Next candidate begins around this point.
                    state = MOVING
                    anchor = p
                    candidate_start_t = p.t
                    candidate_points = [p]
                    confirmed_start_t = None
                    confirmed_end_t = None
                    departure_start_t = None

    if state == STATIONARY:
        _emit()

    return stops


def build_journeys(
    points: List[Point], stops: List[Stop],
) -> List[Journey]:
    """Carve journeys out of the point stream using confirmed
    stop boundaries. Drops journeys shorter than
    ``MIN_JOURNEY_DISTANCE_M`` so driveway shuffles don't
    clutter the view."""
    if len(points) < 2:
        return []

    spans: List[tuple[_dt.datetime, _dt.datetime]] = []
    if not stops:
        spans.append((points[0].t, points[-1].t))
    else:
        if stops[0].start_time > points[0].t:
            spans.append((points[0].t, stops[0].start_time))
        for i in range(len(stops) - 1):
            spans.append((stops[i].end_time, stops[i + 1].start_time))
        if stops[-1].end_time < points[-1].t:
            spans.append((stops[-1].end_time, points[-1].t))

    journeys: List[Journey] = []
    for start_t, end_t in spans:
        span_pts = [p for p in points if start_t <= p.t <= end_t]
        if len(span_pts) < 2:
            continue
        dist = 0.0
        for i in range(1, len(span_pts)):
            dist += _haversine(span_pts[i - 1], span_pts[i])
        if dist < MIN_JOURNEY_DISTANCE_M:
            continue
        journeys.append(Journey(
            start_time=span_pts[0].t,
            end_time=span_pts[-1].t,
            start_lat=span_pts[0].lat,
            start_lon=span_pts[0].lon,
            end_lat=span_pts[-1].lat,
            end_lon=span_pts[-1].lon,
            distance_m=dist,
            points=span_pts,
        ))
    return journeys


def _split_on_time_gaps(
    points: List[Point], max_gap_s: float,
) -> List[List[Point]]:
    """Break the point stream wherever consecutive fixes are
    more than ``max_gap_s`` apart — the camera was off, so
    anything that follows is a new session."""
    if not points:
        return []
    chunks: List[List[Point]] = [[points[0]]]
    for p in points[1:]:
        if (p.t - chunks[-1][-1].t).total_seconds() > max_gap_s:
            chunks.append([p])
        else:
            chunks[-1].append(p)
    return chunks


def _is_stationary_chunk(chunk: List[Point]) -> bool:
    """True if every fix lies within ``STOP_RADIUS_M`` of the
    first. Used to recognise short stationary sessions (e.g. a
    single at-home clip) that wouldn't clear the normal
    ``MIN_STOP_DURATION_S`` threshold but still deserve a card
    so their clips don't disappear."""
    if len(chunk) < 2:
        return True
    anchor = chunk[0]
    for p in chunk[1:]:
        if _haversine_ll(
            anchor.lat, anchor.lon, p.lat, p.lon,
        ) > STOP_RADIUS_M:
            return False
    return True


def _chunk_as_stop(chunk: List[Point]) -> Stop:
    n = len(chunk)
    cx = sum(p.lat for p in chunk) / n
    cy = sum(p.lon for p in chunk) / n
    return Stop(
        start_time=chunk[0].t,
        end_time=chunk[-1].t,
        center_lat=cx,
        center_lon=cy,
        point_count=n,
    )


def aggregate_day(
    gpx_paths: Iterable[str],
) -> tuple[List[Point], List[Stop], List[Journey]]:
    """Merge sidecars, filter outliers, split on camera-off
    gaps, then run the stop-and-journey pipeline on each
    session. Returns ``(points, stops, journeys)``."""
    merged: List[Point] = []
    for p in gpx_paths:
        if os.path.exists(p):
            merged.extend(_parse_gpx(p))
    merged.sort(key=lambda p: p.t)
    merged = _filter_outliers(merged)

    if not merged:
        return [], [], []

    sessions = _split_on_time_gaps(merged, SESSION_GAP_SECONDS)
    all_stops: List[Stop] = []
    all_journeys: List[Journey] = []
    for session in sessions:
        if _is_stationary_chunk(session):
            # Whole session was at one spot — materialise it
            # as a stop so its clips have a card to attach to,
            # even if it's shorter than MIN_STOP_DURATION_S.
            all_stops.append(_chunk_as_stop(session))
            continue
        stops = detect_stops(session)
        journeys = build_journeys(session, stops)
        all_stops.extend(stops)
        all_journeys.extend(journeys)

    return merged, all_stops, all_journeys
