"""
engine.py — dCortex Disruption Engine

Pure computation layer. No UI, no global state.
Call run(airport, start_hour, start_min, duration_min) -> DisruptionResult.

Architecture:
  1. Network   — directed graph: airports (nodes), flights (edges), aircraft/crew (resources)
  2. Impact    — BFS traversal from the disrupted node, bidirectional
  3. Options   — every feasible option enumerated + constraint-checked per flight
  4. Plan      — joint allocation across all flights (greedy priority order, shared pool)
  5. Trace     — full reasoning record: what was considered, checked, chosen, predicted
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Optional

TODAY = date.today()

def _dt(h: int, m: int = 0) -> datetime:
    return datetime(TODAY.year, TODAY.month, TODAY.day, h, m)


# ─── FAA Part 117 Table B ─────────────────────────────────────────────────────
# (duty_start_hour, segment_count) -> max FDP hours
# Unaugmented, domestic operations. Simplified from full table.

_FDP_TABLE: dict[tuple[int, int], float] = {}
for _hr_range, _by_seg in [
    (range(0, 4),   [9.0,  9.0,  9.0,  9.0,  9.0, 9.0]),
    (range(4, 5),   [10.0, 9.0,  9.0,  9.0,  9.0, 9.0]),
    (range(5, 6),   [12.0, 12.0, 11.5, 10.5, 9.0, 9.0]),
    (range(6, 10),  [13.0, 13.0, 12.0, 11.0, 9.0, 9.0]),
    (range(10, 24), [12.0, 12.0, 11.0, 10.0, 9.0, 9.0]),
]:
    for _h in _hr_range:
        for _s, _v in enumerate(_by_seg, 1):
            _FDP_TABLE[(_h, _s)] = _v

def max_fdp_hours(duty_start_hour: int, segments: int) -> float:
    return _FDP_TABLE.get((duty_start_hour, min(max(segments, 1), 6)), 9.0)


# ─── Cost model ───────────────────────────────────────────────────────────────
# Source: Eurocontrol Standard Inputs for Cost-Benefit Analyses
# Three tiers: widebody international / narrowbody mainline / regional

_COST_PER_MIN    = {
    "B777": 180.0,   # widebody international
    "B737":  80.0,   # narrowbody mainline
    "B757":  95.0,
    "A320":  85.0,
    "E175":  40.0,   # regional jet
}
_RESERVE_COST    = 800.0   # per activation (CBA average)
_AC_SWAP_FEE     = 500.0   # admin + repositioning fee
_CANCEL_PER_PAX  = 450.0   # full reaccommodation cost per passenger (cancel)
_CANCEL_OPS_FEE  = 5_000.0 # fixed ops fee per cancellation
_MIN_TURN_MIN    = 45      # minimum ground turn, narrow/medium-body
_RESERVE_CALLOUT = 90      # reserve callout to flight-ready (minutes)

# Passenger misconnection — delay-dependent (fix: cost is per option, not per flight)
_MCT_MIN         = 45      # domestic minimum connection time; connections break beyond this
_CONNECT_RATE    = 0.30    # fraction of pax on connecting itineraries (conservative hub estimate)
_MISCONNECT_COST = 350.0   # reaccommodation cost per misconnecting passenger


def _misconnect_cost(flight: Flight, delay_min: int) -> float:
    """
    Connecting passengers who miss their onward flight because of this delay.
    Zero below MCT. Scales linearly to full connecting fraction at 2×MCT.
    A B777-NRT with 280 pax at 90-min delay costs far more in misconnections
    than an E175-DTW with 68 pax — this is the function that captures that.
    """
    if delay_min <= _MCT_MIN:
        return 0.0
    fraction_broken = min(1.0, (delay_min - _MCT_MIN) / _MCT_MIN)
    return flight.passengers * _CONNECT_RATE * fraction_broken * _MISCONNECT_COST


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Aircraft:
    id: str
    type: str         # B737 | B757 | A320
    subtype: str
    location: str     # home station (airport code)
    spare: bool = False

@dataclass
class CrewMember:
    id: str
    role: str              # CA | FO
    location: str
    certifications: list[str]
    duty_start_hour: int   # hour duty period began (local)
    duty_segments: int     # flight segments completed today
    is_reserve: bool

@dataclass
class Flight:
    id: str
    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    aircraft_id: str
    crew_ids: list[str]
    passengers: int

    @property
    def duration_hours(self) -> float:
        return (self.arrival - self.departure).total_seconds() / 3600

@dataclass
class Network:
    airports: dict[str, dict]
    flights: dict[str, Flight]
    aircraft: dict[str, Aircraft]
    crew: dict[str, CrewMember]


# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class ConstraintResult:
    name: str
    passed: bool
    detail: str

@dataclass
class ResolutionOption:
    type: str           # DELAY | CREW_SWAP | AIRCRAFT_SWAP | CANCEL
    description: str
    cost: float
    delay_min: int
    feasible: bool
    recommended: bool = False
    constraints: list[ConstraintResult] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)  # IDs committed

@dataclass
class FlightImpact:
    flight: Flight
    impact_type: str    # DEPARTURE_BLOCKED | ARRIVAL_BLOCKED | CASCADE
    expected_delay_min: int
    cause: str

@dataclass
class FlightResolution:
    impact: FlightImpact
    options: list[ResolutionOption]
    recommended: Optional[ResolutionOption]

@dataclass
class DisruptionResult:
    airport: str
    cause: str
    start: datetime
    duration_min: int
    effective_end: datetime
    resolutions: list[FlightResolution]
    reserved_resources: dict[str, str]
    plan_cost: float
    donoth_cost: float
    trace: dict
    generated_at: datetime = field(default_factory=datetime.now)

    @property
    def total_pax(self) -> int:
        return sum(r.impact.flight.passengers for r in self.resolutions)

    @property
    def direct_count(self) -> int:
        return sum(1 for r in self.resolutions
                   if r.impact.impact_type != "CASCADE")

    @property
    def cascade_count(self) -> int:
        return sum(1 for r in self.resolutions
                   if r.impact.impact_type == "CASCADE")


# ─── Network builder ──────────────────────────────────────────────────────────

def build_network() -> Network:
    airports = {
        "ORD": {"name": "Chicago O'Hare",    "hub": True},
        "LAX": {"name": "Los Angeles",        "hub": False},
        "JFK": {"name": "New York JFK",       "hub": False},
        "DEN": {"name": "Denver",             "hub": False},
        "MIA": {"name": "Miami",              "hub": False},
        "SEA": {"name": "Seattle",            "hub": False},
        "SFO": {"name": "San Francisco",      "hub": False},
        "DTW": {"name": "Detroit",             "hub": False},
    }

    # ── Aircraft ──────────────────────────────────────────────────────────────
    ac_data = [
        # ORD hub fleet
        ("N101UA", "B737", "B737-900ER", "ORD", False),
        ("N102UA", "B737", "B737-800",   "ORD", False),
        ("N103UA", "B757", "B757-200",   "ORD", False),
        ("N107UA", "B737", "B737-900ER", "ORD", True),   # SPARE at ORD
        ("N112UA", "B737", "B737-900ER", "ORD", False),
        ("N113UA", "B757", "B757-200",   "ORD", False),
        # Spokes
        ("N104UA", "B737", "B737-900ER", "MIA", False),
        ("N105UA", "A320", "A320-200",   "JFK", False),
        ("N106UA", "B737", "B737-800",   "DEN", False),
        ("N110UA", "B757", "B757-200",   "SFO", False),
        ("N111UA", "B737", "B737-800",   "LAX", False),
        ("N114UA", "B757", "B757-200",   "MIA", False),
        ("N115UA", "B737", "B737-900ER", "SFO", False),
        ("N116UA", "B737", "B737-800",   "SEA", False),
        ("N117UA", "B737", "B737-900ER", "LAX", False),
        ("N118UA", "B737", "B737-800",   "DEN", False),
        ("N119UA", "B737", "B737-800",   "SFO", False),  # SFO 14:00 departure
        ("N121UA", "B737", "B737-800",   "DEN", False),  # DEN->SFO inbound
        ("N120UA", "B737", "B737-800",   "LAX", True),   # SPARE at LAX
        # Widebody international tier
        ("N201UA", "B777", "B777-200",   "ORD", False),  # ORD→NRT departure
        ("N202UA", "B777", "B777-200",   "LAX", False),  # LAX→ORD inbound, then ORD→LAX cascade
        # Regional jet tier
        ("N301UA", "E175", "E175",       "ORD", False),  # ORD→DTW departure
        ("N302UA", "E175", "E175",       "DTW", False),  # DTW→ORD inbound, then ORD→DTW cascade
    ]
    aircraft = {
        a[0]: Aircraft(id=a[0], type=a[1], subtype=a[2], location=a[3], spare=a[4])
        for a in ac_data
    }

    # ── Crew ─────────────────────────────────────────────────────────────────
    # (id, role, location, certs, duty_start_hour, segments_today, is_reserve)
    # Key: CA02 at ORD has 2 segments done starting at 06:00 → near FDP limit
    # location = current position after inbound leg (not home base)
    crew_data = [
        # ORD line crew
        ("CA01", "CA", "ORD", ["B737"],       8, 1, False),
        ("FO01", "FO", "ORD", ["B737"],       8, 1, False),
        ("CA02", "CA", "ORD", ["B737"],       6, 2, False),  # ← near FDP limit
        ("CA06", "CA", "ORD", ["B757"],       9, 1, False),
        ("FO06", "FO", "ORD", ["B757"],       9, 1, False),
        # Inbound/spoke line crew — location = where they ARE after first leg
        ("CA05", "CA", "ORD", ["B737"],       7, 1, False),  # flew DEN->ORD (UA201)
        ("FO05", "FO", "ORD", ["B737"],       7, 1, False),
        ("CA03", "CA", "ORD", ["B737"],       7, 1, False),  # flew MIA->ORD (UA202)
        ("FO03", "FO", "ORD", ["B737"],       7, 1, False),
        ("CA10", "CA", "MIA", ["B757"],       9, 0, False),
        ("FO09", "FO", "MIA", ["B757"],       9, 0, False),
        ("CA11", "CA", "LAX", ["B757"],       8, 1, False),  # flew SFO->LAX (UA510)
        ("FO10", "FO", "LAX", ["B757"],       8, 1, False),
        ("CA07", "CA", "LAX", ["B737"],       9, 0, False),
        ("FO11", "FO", "LAX", ["B737"],       9, 0, False),
        ("CA08", "CA", "JFK", ["B737"],       8, 1, False),  # flew ORD->JFK (UA520)
        ("FO07", "FO", "JFK", ["B737"],       8, 1, False),
        ("CA09", "CA", "MIA", ["B757"],       8, 1, False),  # flew ORD->MIA (UA530, in-air)
        ("FO08", "FO", "MIA", ["B757"],       8, 1, False),
        ("CA12", "CA", "SEA", ["B737"],       8, 1, False),  # flew SFO->SEA (UA540)
        ("FO12", "FO", "SEA", ["B737"],       8, 1, False),
        ("CA13", "CA", "SEA", ["B737"],       9, 0, False),
        ("FO13", "FO", "SEA", ["B737"],       9, 0, False),
        ("CA14", "CA", "DEN", ["B737"],       8, 1, False),  # flew LAX->DEN (UA550)
        ("FO14", "FO", "DEN", ["B737"],       8, 1, False),
        ("CA04", "CA", "JFK", ["A320"],       9, 1, False),
        ("FO04", "FO", "JFK", ["A320"],       9, 1, False),
        ("CA17", "CA", "DEN", ["B737"],       7, 1, False),  # for UA552 DEN->LAX
        ("FO17", "FO", "DEN", ["B737"],       7, 1, False),
        ("CA15", "CA", "SFO", ["B737"],       8, 0, False),  # for UA560 SFO->DEN
        ("FO15", "FO", "SFO", ["B737"],       8, 0, False),
        ("CA16", "CA", "DEN", ["B737"],       7, 1, False),  # for UA561 DEN->SFO
        ("FO16", "FO", "DEN", ["B737"],       7, 1, False),
        # ORD reserves
        ("RCA01", "CA", "ORD", ["B737","B757"], 0, 0, True),
        ("RFO01", "FO", "ORD", ["B737","B757"], 0, 0, True),
        ("RCA02", "CA", "ORD", ["B737"],        0, 0, True),
        ("RFO02", "FO", "ORD", ["B737"],        0, 0, True),
        ("RCA03", "CA", "ORD", ["B757"],        0, 0, True),
        ("RFO03", "FO", "ORD", ["B757"],        0, 0, True),
        # Spoke reserves
        ("RCA04", "CA", "LAX", ["B737"],            0, 0, True),
        ("RFO04", "FO", "LAX", ["B737"],            0, 0, True),
        ("RCA10", "CA", "LAX", ["B757","B737"],     0, 0, True),  # B757 reserve (fix)
        ("RFO10", "FO", "LAX", ["B757","B737"],     0, 0, True),
        ("RCA05", "CA", "JFK", ["A320","B737"],     0, 0, True),
        ("RFO05", "FO", "JFK", ["A320","B737"],     0, 0, True),
        ("RCA06", "CA", "MIA", ["B757","B737"],     0, 0, True),
        ("RFO06", "FO", "MIA", ["B757","B737"],     0, 0, True),
        ("RCA07", "CA", "SEA", ["B737"],            0, 0, True),
        ("RFO07", "FO", "SEA", ["B737"],            0, 0, True),
        ("RCA08", "CA", "DEN", ["B737"],            0, 0, True),
        ("RFO08", "FO", "DEN", ["B737"],            0, 0, True),
        ("RCA09", "CA", "SFO", ["B737","B757"],     0, 0, True),
        ("RFO09", "FO", "SFO", ["B737","B757"],     0, 0, True),
        # B777 line crew (ORD-based, segs_today=0 = start of duty)
        ("CA20", "CA", "ORD", ["B777"],             7, 0, False),  # UA800 ORD→NRT
        ("FO20", "FO", "ORD", ["B777"],             7, 0, False),
        # B777 crew at LAX after inbound leg; operate cascade flight from ORD
        ("CA21", "CA", "LAX", ["B777"],             8, 1, False),  # flew LAX→ORD (UA802)
        ("FO21", "FO", "LAX", ["B777"],             8, 1, False),
        # E175 line crew (ORD-based)
        ("CA22", "CA", "ORD", ["E175"],             8, 0, False),  # UA900 ORD→DTW
        ("FO22", "FO", "ORD", ["E175"],             8, 0, False),
        # E175 crew at DTW after inbound leg; operate cascade from ORD
        ("CA23", "CA", "DTW", ["E175"],             8, 1, False),  # flew DTW→ORD (UA901)
        ("FO23", "FO", "DTW", ["E175"],             8, 1, False),
        # B777 reserves at ORD
        ("RCA11", "CA", "ORD", ["B777"],            0, 0, True),
        ("RFO11", "FO", "ORD", ["B777"],            0, 0, True),
        # E175 reserves at ORD
        ("RCA12", "CA", "ORD", ["E175"],            0, 0, True),
        ("RFO12", "FO", "ORD", ["E175"],            0, 0, True),
    ]
    crew = {
        c[0]: CrewMember(id=c[0], role=c[1], location=c[2], certifications=c[3],
                         duty_start_hour=c[4], duty_segments=c[5], is_reserve=c[6])
        for c in crew_data
    }

    # ── Flights ───────────────────────────────────────────────────────────────
    # Designed so every airport has:
    #   - 1 departure (~14:00) → DEPARTURE_BLOCKED when that airport is disrupted
    #   - 1 inbound arrival (~14:10-14:25) → ARRIVAL_BLOCKED (upstream)
    #   - 1 downstream flight using the inbound aircraft → CASCADE
    raw_flights = [
        # ── ORD hub ──
        ("UA101", "ORD","LAX", 14, 0,  18, 0,  "N101UA", ["CA01","FO01"], 156),
        ("UA102", "ORD","JFK", 14,15,  17,45,  "N103UA", ["CA06","FO06"], 180),
        ("UA103", "ORD","SEA", 14,25,  17,55,  "N102UA", ["CA02","FO01"], 145),  # CA02 near FDP
        ("UA201", "DEN","ORD", 12, 0,  14,10,  "N106UA", ["CA05","FO05"], 120),  # inbound to ORD
        ("UA202", "MIA","ORD", 11, 0,  14,20,  "N104UA", ["CA03","FO03"], 165),  # inbound to ORD
        ("UA301", "ORD","DEN", 15, 0,  17, 0,  "N106UA", ["CA05","FO05"], 130),  # cascade via N106UA
        ("UA302", "ORD","MIA", 15,30,  19, 0,  "N104UA", ["CA03","FO03"], 158),  # cascade via N104UA
        # ── LAX spoke ──
        ("UA510", "SFO","LAX", 12,10,  14,15,  "N110UA", ["CA11","FO10"], 162),  # inbound to LAX
        ("UA511", "LAX","ORD", 15,45,  20,15,  "N110UA", ["CA11","FO10"], 168),  # cascade via N110UA
        ("UA512", "LAX","SFO", 14,20,  15,35,  "N111UA", ["CA07","FO11"], 134),  # departs LAX
        # ── JFK spoke ──
        ("UA520", "ORD","JFK", 11,10,  14,15,  "N112UA", ["CA08","FO07"], 170),  # inbound to JFK
        ("UA521", "JFK","LAX", 16, 0,  19,30,  "N112UA", ["CA08","FO07"], 155),  # cascade via N112UA
        ("UA522", "JFK","ORD", 14,20,  16,20,  "N105UA", ["CA04","FO04"], 140),  # departs JFK
        # ── MIA spoke ──
        ("UA530", "ORD","MIA", 11, 0,  14,20,  "N113UA", ["CA09","FO08"], 168),  # inbound to MIA
        ("UA531", "MIA","ORD", 15,45,  18,15,  "N113UA", ["CA09","FO08"], 142),  # cascade via N113UA
        ("UA532", "MIA","JFK", 14,45,  17,30,  "N114UA", ["CA10","FO09"], 155),  # departs MIA
        # ── SEA spoke ──
        ("UA540", "SFO","SEA", 12, 5,  14,15,  "N115UA", ["CA12","FO12"], 148),  # inbound to SEA
        ("UA541", "SEA","ORD", 15,45,  20,15,  "N115UA", ["CA12","FO12"], 152),  # cascade via N115UA
        ("UA542", "SEA","LAX", 14,15,  16,45,  "N116UA", ["CA13","FO13"], 138),  # departs SEA
        # ── DEN spoke ──
        ("UA550", "LAX","DEN", 12, 0,  14,10,  "N117UA", ["CA14","FO14"], 125),  # inbound to DEN
        ("UA551", "DEN","SFO", 15,30,  17,15,  "N117UA", ["CA14","FO14"], 118),  # cascade via N117UA
        ("UA552", "DEN","LAX", 14, 8,  15,30,  "N118UA", ["CA17","FO17"], 110),  # departs DEN
        # ── SFO spoke ──
        ("UA560", "SFO","DEN", 14, 5,  17, 5,  "N119UA", ["CA15","FO15"], 132),  # departs SFO
        ("UA561", "DEN","SFO", 12,10,  14,25,  "N121UA", ["CA16","FO16"], 119),  # inbound to SFO
        ("UA562", "SFO","LAX", 15,45,  17, 0,  "N121UA", ["CA16","FO16"], 128),  # cascade via N121UA
        # ── B777 widebody international tier ──
        ("UA800", "ORD","NRT", 14,30,  18,30,  "N201UA", ["CA20","FO20"], 280),  # departs ORD ($180/min)
        ("UA802", "LAX","ORD", 11,30,  14,25,  "N202UA", ["CA21","FO21"], 265),  # inbound to ORD
        ("UA803", "ORD","LAX", 16, 0,  19,30,  "N202UA", ["CA21","FO21"], 250),  # cascade via N202UA
        # ── E175 regional jet tier ──
        ("UA900", "ORD","DTW", 14, 5,  15,30,  "N301UA", ["CA22","FO22"],  68),  # departs ORD ($40/min)
        ("UA901", "DTW","ORD", 12,15,  14,10,  "N302UA", ["CA23","FO23"],  72),  # inbound to ORD
        ("UA902", "ORD","DTW", 16, 0,  17,30,  "N302UA", ["CA23","FO23"],  65),  # cascade via N302UA
    ]
    flights = {}
    for f in raw_flights:
        fid, orig, dest = f[0], f[1], f[2]
        dep = _dt(f[3], f[4])
        arr = _dt(f[5], f[6])
        ac_id, crew_ids, pax = f[7], f[8], f[9]
        flights[fid] = Flight(fid, orig, dest, dep, arr, ac_id, crew_ids, pax)

    return Network(airports=airports, flights=flights, aircraft=aircraft, crew=crew)


# ─── Disruption window ────────────────────────────────────────────────────────

def _effective_end(start: datetime, duration_min: int) -> datetime:
    """
    Effective end of disruption including ATC queue recovery.
    Queue recovery ≈ 1.5× shutdown duration (minimum 30 min).
    An 18-min ground stop is operationally a 60–90 min disruption.
    """
    recovery = max(30, int(duration_min * 1.5))
    return start + timedelta(minutes=duration_min + recovery)


# ─── Impact detection (BFS) ───────────────────────────────────────────────────

def find_impacts(network: Network, airport: str,
                 start: datetime, duration_min: int) -> list[FlightImpact]:
    """
    Bidirectional BFS from the disrupted node.
    Returns impacts sorted by passenger count (priority order for allocation).
    """
    win_end = _effective_end(start, duration_min)

    impacts: dict[str, FlightImpact] = {}
    # aircraft_id -> earliest the aircraft is available after the disruption
    delayed_ac: dict[str, datetime] = {}

    # ── Pass 1: direct impacts ────────────────────────────────────────────────
    for f in network.flights.values():
        if f.origin == airport and start <= f.departure <= win_end:
            delay = max(15, int((win_end - f.departure).total_seconds() / 60) + 15)
            impacts[f.id] = FlightImpact(f, "DEPARTURE_BLOCKED", delay,
                                          "Departure blocked by ground stop")
            avail = win_end + timedelta(minutes=_MIN_TURN_MIN)
            delayed_ac[f.aircraft_id] = max(delayed_ac.get(f.aircraft_id, avail), avail)

        elif f.destination == airport and start <= f.arrival <= win_end:
            delay = max(15, int((win_end - f.arrival).total_seconds() / 60) + 15)
            impacts[f.id] = FlightImpact(f, "ARRIVAL_BLOCKED", delay,
                                          "Arrival blocked — aircraft held at altitude")
            avail = win_end + timedelta(minutes=_MIN_TURN_MIN)
            delayed_ac[f.aircraft_id] = max(delayed_ac.get(f.aircraft_id, avail), avail)

    # ── Pass 2+: cascade (BFS until stable) ──────────────────────────────────
    changed = True
    while changed:
        changed = False
        for f in network.flights.values():
            if f.id in impacts:
                continue
            if f.aircraft_id not in delayed_ac:
                continue
            ac_avail = delayed_ac[f.aircraft_id]
            earliest_dep = ac_avail + timedelta(minutes=_MIN_TURN_MIN)
            if earliest_dep > f.departure:
                delay = max(1, int((earliest_dep - f.departure).total_seconds() / 60))
                impacts[f.id] = FlightImpact(
                    f, "CASCADE", delay,
                    f"Aircraft {f.aircraft_id} arriving late from upstream disruption"
                )
                new_avail = f.arrival + timedelta(minutes=delay + _MIN_TURN_MIN)
                delayed_ac[f.aircraft_id] = max(delayed_ac.get(f.aircraft_id, new_avail), new_avail)
                changed = True

    return sorted(impacts.values(), key=lambda i: -i.flight.passengers)


# ─── Constraint checking ──────────────────────────────────────────────────────

def _fdp_check(crew_member: CrewMember, ac_type: str,
               disruption_time: datetime, flight_h: float,
               delay_h: float = 0.0) -> ConstraintResult:
    if ac_type not in crew_member.certifications:
        return ConstraintResult(
            f"{crew_member.id} certification", False,
            f"Not certified for {ac_type} (holds: {', '.join(crew_member.certifications)})"
        )
    duty_start = _dt(crew_member.duty_start_hour)
    duty_elapsed_h = (disruption_time - duty_start).total_seconds() / 3600
    segments = crew_member.duty_segments + 1
    limit = max_fdp_hours(crew_member.duty_start_hour, segments)
    needed = duty_elapsed_h + delay_h + flight_h
    remaining = limit - duty_elapsed_h
    if needed > limit:
        return ConstraintResult(
            f"{crew_member.id} FDP (Part 117)", False,
            f"Needs {needed:.1f}h total, limit {limit:.1f}h — only {remaining:.1f}h remaining"
        )
    return ConstraintResult(
        f"{crew_member.id} FDP (Part 117)", True,
        f"{remaining:.1f}h remaining (limit {limit:.1f}h, {segments} segs)"
    )


# ─── Option generators ────────────────────────────────────────────────────────

def _delay_option(flight: Flight, delay_min: int, network: Network,
                  disruption_time: datetime) -> ResolutionOption:
    ac = network.aircraft.get(flight.aircraft_id)
    ac_type = ac.type if ac else "B737"
    crew = [network.crew[c] for c in flight.crew_ids if c in network.crew]

    constraints = [
        _fdp_check(c, ac_type, disruption_time, flight.duration_hours, delay_min / 60)
        for c in crew
    ]
    if ac:
        constraints.append(ConstraintResult(
            f"Aircraft {ac.id}", True, f"{ac.subtype} on stand at {flight.origin}"
        ))

    dep = (flight.departure + timedelta(minutes=delay_min)).strftime("%H:%M")
    return ResolutionOption(
        type="DELAY",
        description=f"Delay {delay_min} min — departs {dep}",
        cost=_COST_PER_MIN.get(ac_type, 80.0) * delay_min + _misconnect_cost(flight, delay_min),
        delay_min=delay_min,
        feasible=all(c.passed for c in constraints),
        constraints=constraints,
    )


def _crew_swap_option(flight: Flight, delay_min: int, network: Network,
                      reserved: dict) -> Optional[ResolutionOption]:
    ac = network.aircraft.get(flight.aircraft_id)
    if not ac:
        return None

    eff_delay = max(delay_min, _RESERVE_CALLOUT)

    avail_ca = [c for c in network.crew.values()
                if c.is_reserve and c.location == flight.origin
                and ac.type in c.certifications and c.role == "CA"
                and c.id not in reserved]
    avail_fo = [c for c in network.crew.values()
                if c.is_reserve and c.location == flight.origin
                and ac.type in c.certifications and c.role == "FO"
                and c.id not in reserved]

    best_ca = avail_ca[0] if avail_ca else None
    best_fo = avail_fo[0] if avail_fo else None

    constraints = [
        ConstraintResult("Reserve callout window", True,
                         f"Min {_RESERVE_CALLOUT} min → effective delay {eff_delay} min"),
        ConstraintResult(f"Reserve CA ({ac.type}) at {flight.origin}",
                         bool(best_ca),
                         f"{best_ca.id} available" if best_ca
                         else f"No certified reserve CA at {flight.origin}"),
        ConstraintResult(f"Reserve FO ({ac.type}) at {flight.origin}",
                         bool(best_fo),
                         f"{best_fo.id} available" if best_fo
                         else f"No certified reserve FO at {flight.origin}"),
    ]

    dep = (flight.departure + timedelta(minutes=eff_delay)).strftime("%H:%M")
    resources = [c.id for c in [best_ca, best_fo] if c]

    return ResolutionOption(
        type="CREW_SWAP",
        description=(f"Reserve crew "
                     f"({best_ca.id if best_ca else '—'} / {best_fo.id if best_fo else '—'}) "
                     f"— departs {dep}"),
        cost=_RESERVE_COST * 2 + _COST_PER_MIN.get(ac.type, 80.0) * eff_delay + _misconnect_cost(flight, eff_delay),
        delay_min=eff_delay,
        feasible=bool(best_ca and best_fo),
        constraints=constraints,
        resources=resources,
    )


def _ac_swap_option(flight: Flight, delay_min: int, network: Network,
                    disruption_time: datetime, reserved: dict) -> Optional[ResolutionOption]:
    ac = network.aircraft.get(flight.aircraft_id)
    if not ac:
        return None

    spares = [
        a for a in network.aircraft.values()
        if a.spare and a.type == ac.type
        and a.location == flight.origin
        and a.id != flight.aircraft_id
        and a.id not in reserved
    ]
    if not spares:
        return None

    spare = spares[0]
    crew = [network.crew[c] for c in flight.crew_ids if c in network.crew]
    constraints = [
        _fdp_check(c, ac.type, disruption_time, flight.duration_hours, delay_min / 60)
        for c in crew
    ]
    constraints.append(ConstraintResult(
        f"Spare {ac.type} at {flight.origin}", True,
        f"{spare.id} ({spare.subtype}) unscheduled — available now"
    ))

    dep = (flight.departure + timedelta(minutes=delay_min)).strftime("%H:%M")
    return ResolutionOption(
        type="AIRCRAFT_SWAP",
        description=f"Swap to spare {spare.id} — departs {dep}",
        cost=_COST_PER_MIN.get(ac.type, 80.0) * delay_min + _AC_SWAP_FEE + _misconnect_cost(flight, delay_min),
        delay_min=delay_min,
        feasible=all(c.passed for c in constraints),
        constraints=constraints,
        resources=[spare.id],
    )


def _cancel_option(flight: Flight) -> ResolutionOption:
    return ResolutionOption(
        type="CANCEL",
        description=f"Cancel — reaccommodate {flight.passengers} passengers",
        cost=flight.passengers * _CANCEL_PER_PAX + _CANCEL_OPS_FEE,
        delay_min=0,
        feasible=True,
        constraints=[ConstraintResult("Feasibility", True, "Always available — last resort")],
    )


def _gen_options(flight: Flight, impact: FlightImpact, network: Network,
                 disruption_time: datetime, reserved: dict) -> list[ResolutionOption]:
    delay = max(impact.expected_delay_min, 30)
    options: list[ResolutionOption] = []

    options.append(_delay_option(flight, delay, network, disruption_time))

    cs = _crew_swap_option(flight, delay, network, reserved)
    if cs:
        options.append(cs)

    # Aircraft swap uses the actual cascade delay (not padded), making it cheaper when spare exists
    ac_delay = max(impact.expected_delay_min, 5)
    acs = _ac_swap_option(flight, ac_delay, network, disruption_time, reserved)
    if acs:
        options.append(acs)

    options.append(_cancel_option(flight))
    return options


# ─── Plan generator (joint allocation) ───────────────────────────────────────

def gen_plan(network: Network, impacts: list[FlightImpact],
             disruption_time: datetime) -> tuple[list[FlightResolution], float, float, dict]:
    """
    Greedy joint allocation: process flights by passenger count (highest first).
    Resources committed to Flight A are unavailable for Flights B, C, ...
    Returns (resolutions, plan_cost, donoth_cost, reserved_resources).
    """
    reserved: dict[str, str] = {}   # resource_id -> flight_id
    resolutions: list[FlightResolution] = []
    plan_cost = 0.0
    donoth_cost = 0.0

    for impact in impacts:
        f = impact.flight
        ac = network.aircraft.get(f.aircraft_id)
        ac_type = ac.type if ac else "B737"

        # Do-nothing cost: full delay + passenger cascade costs
        donoth_delay = impact.expected_delay_min + 60  # cascade compounds
        donoth_cost += _COST_PER_MIN.get(ac_type, 80.0) * donoth_delay

        options = _gen_options(f, impact, network, disruption_time, reserved)
        feasible = sorted([o for o in options if o.feasible], key=lambda o: o.cost)

        if feasible:
            rec = feasible[0]
            rec.recommended = True
            for rid in rec.resources:
                reserved[rid] = f.id
            plan_cost += rec.cost
        else:
            # No feasible option — force cancel
            cancel = next(o for o in options if o.type == "CANCEL")
            cancel.recommended = True
            plan_cost += cancel.cost

        recommended = next((o for o in options if o.recommended), None)
        resolutions.append(FlightResolution(impact, options, recommended))

    return resolutions, plan_cost, donoth_cost, reserved


# ─── Trace builder ────────────────────────────────────────────────────────────

def _build_trace(airport: str, start: datetime, duration_min: int,
                 eff_end: datetime, resolutions: list[FlightResolution],
                 reserved: dict, plan_cost: float) -> dict:
    total_opts = sum(len(r.options) for r in resolutions)
    total_checks = sum(sum(len(o.constraints) for o in r.options) for r in resolutions)
    violations = [
        f"{r.impact.flight.id} {o.type}: {c.detail}"
        for r in resolutions
        for o in r.options
        for c in o.constraints
        if not c.passed
    ]
    return {
        "id": f"TRACE_{airport}_{start.strftime('%Y%m%d_%H%M')}_{duration_min}min",
        "generated_at": datetime.now().isoformat(),
        "airport": airport,
        "disruption_start": start.strftime("%H:%M"),
        "disruption_duration_min": duration_min,
        "effective_window_end": eff_end.strftime("%H:%M"),
        "flights_affected": len(resolutions),
        "passengers": sum(r.impact.flight.passengers for r in resolutions),
        "options_generated": total_opts,
        "constraints_checked": total_checks,
        "constraint_violations": violations[:8],
        "recommendations": {
            r.impact.flight.id: f"{r.recommended.type} — ${r.recommended.cost:,.0f}"
            for r in resolutions if r.recommended
        },
        "resources_committed": [f"{k} → {v}" for k, v in reserved.items()],
        "predicted_cost": plan_cost,
        "actual_cost": None,
        "chosen_by": "SYSTEM",
    }


# ─── Main entry point ─────────────────────────────────────────────────────────

def run(airport: str, start_hour: int = 14, start_min: int = 0,
        duration_min: int = 30, cause: str = "Weather") -> DisruptionResult:
    """Run the full disruption pipeline for the given airport and timing."""
    network = build_network()
    start = _dt(start_hour, start_min)
    eff_end = _effective_end(start, duration_min)

    impacts = find_impacts(network, airport, start, duration_min)

    if not impacts:
        return DisruptionResult(
            airport=airport, cause=cause, start=start,
            duration_min=duration_min, effective_end=eff_end,
            resolutions=[], reserved_resources={},
            plan_cost=0.0, donoth_cost=0.0,
            trace=_build_trace(airport, start, duration_min, eff_end, [], {}, 0.0),
        )

    resolutions, plan_cost, donoth_cost, reserved = gen_plan(
        network, impacts, start
    )
    trace = _build_trace(airport, start, duration_min, eff_end,
                         resolutions, reserved, plan_cost)

    return DisruptionResult(
        airport=airport, cause=cause, start=start,
        duration_min=duration_min, effective_end=eff_end,
        resolutions=resolutions, reserved_resources=reserved,
        plan_cost=plan_cost, donoth_cost=donoth_cost,
        trace=trace,
    )
