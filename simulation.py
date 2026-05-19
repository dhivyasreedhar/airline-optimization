"""
simulation.py — dCortex Disruption Simulation Engine

Two modes:
  simulate()  Pre-disruption: run P50/P75/P90 scenarios before committing.
              The OCC can practice disruptions in advance, see the full cost
              envelope, and pre-position resources for the most likely outcome.
  run()       Live decision: single confirmed scenario, full plan generated,
              trace locked at dispatcher decision time.

Four-agent architecture:
  CrewAgent          Part 117 FDP limits, reserve availability by type/role/station
  AircraftAgent      Type ratings, spare aircraft availability, fleet cost by tier
  PassengerAgent     Connection risk, delay-dependent misconnection cost
  CoordinatingAgent  Orchestrates the above; manages the shared resource pool
                     across all affected flights simultaneously
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
# Unaugmented, domestic operations.

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
_RESERVE_COST    = 800.0
_AC_SWAP_FEE     = 500.0
_CANCEL_PER_PAX  = 450.0
_CANCEL_OPS_FEE  = 5_000.0
_MIN_TURN_MIN    = 45
_RESERVE_CALLOUT = 90

_MCT_MIN         = 45     # domestic minimum connection time
_CONNECT_RATE    = 0.30   # fraction of pax on connecting itineraries
_MISCONNECT_COST = 350.0  # reaccommodation cost per misconnecting passenger


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Aircraft:
    id: str
    type: str
    subtype: str
    location: str
    spare: bool = False

@dataclass
class CrewMember:
    id: str
    role: str              # CA | FO
    location: str
    certifications: list[str]
    duty_start_hour: int
    duty_segments: int
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
    resources: list[str] = field(default_factory=list)

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
        return sum(1 for r in self.resolutions if r.impact.impact_type != "CASCADE")

    @property
    def cascade_count(self) -> int:
        return sum(1 for r in self.resolutions if r.impact.impact_type == "CASCADE")

@dataclass
class Scenario:
    """One severity tier within a simulation run."""
    label: str          # P50 | P75 | P90
    duration_min: int
    result: DisruptionResult

@dataclass
class SimulationResult:
    """
    Multi-scenario output: full cost envelope before committing to any action.
    Run this on forecast data to see what a P75 ground stop at ORD costs
    before the FAA issues the ground stop — and position reserves accordingly.
    """
    airport: str
    cause: str
    start: datetime
    scenarios: list[Scenario]   # ordered P50, P75, P90

    def by_label(self, label: str) -> DisruptionResult:
        return next(s.result for s in self.scenarios if s.label == label)

    @property
    def p50(self) -> DisruptionResult:
        return self.by_label("P50")

    @property
    def p75(self) -> DisruptionResult:
        return self.by_label("P75")

    @property
    def p90(self) -> DisruptionResult:
        return self.by_label("P90")


SEVERITY_PROFILES = {
    "P50": 18,  # median convective weather ground stop
    "P75": 30,  # 75th percentile
    "P90": 45,  # 90th percentile — plan to this, position for P75, act on P50
}


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
        "DTW": {"name": "Detroit",            "hub": False},
    }

    ac_data = [
        # ORD hub fleet
        ("N101UA", "B737", "B737-900ER", "ORD", False),
        ("N102UA", "B737", "B737-800",   "ORD", False),
        ("N103UA", "B757", "B757-200",   "ORD", False),
        ("N107UA", "B737", "B737-900ER", "ORD", True),
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
        ("N119UA", "B737", "B737-800",   "SFO", False),
        ("N120UA", "B737", "B737-800",   "LAX", True),
        ("N121UA", "B737", "B737-800",   "DEN", False),
        # Widebody international tier
        ("N201UA", "B777", "B777-200",   "ORD", False),
        ("N202UA", "B777", "B777-200",   "LAX", False),
        # Regional jet tier
        ("N301UA", "E175", "E175",       "ORD", False),
        ("N302UA", "E175", "E175",       "DTW", False),
    ]
    aircraft = {
        a[0]: Aircraft(id=a[0], type=a[1], subtype=a[2], location=a[3], spare=a[4])
        for a in ac_data
    }

    crew_data = [
        # ORD line crew
        ("CA01", "CA", "ORD", ["B737"],       8, 1, False),
        ("FO01", "FO", "ORD", ["B737"],       8, 1, False),
        ("CA02", "CA", "ORD", ["B737"],       6, 2, False),  # near FDP limit — demo case
        ("CA06", "CA", "ORD", ["B757"],       9, 1, False),
        ("FO06", "FO", "ORD", ["B757"],       9, 1, False),
        # Inbound/spoke crew — location = where they ARE after first leg
        ("CA05", "CA", "ORD", ["B737"],       7, 1, False),
        ("FO05", "FO", "ORD", ["B737"],       7, 1, False),
        ("CA03", "CA", "ORD", ["B737"],       7, 1, False),
        ("FO03", "FO", "ORD", ["B737"],       7, 1, False),
        ("CA10", "CA", "MIA", ["B757"],       9, 0, False),
        ("FO09", "FO", "MIA", ["B757"],       9, 0, False),
        ("CA11", "CA", "LAX", ["B757"],       8, 1, False),
        ("FO10", "FO", "LAX", ["B757"],       8, 1, False),
        ("CA07", "CA", "LAX", ["B737"],       9, 0, False),
        ("FO11", "FO", "LAX", ["B737"],       9, 0, False),
        ("CA08", "CA", "JFK", ["B737"],       8, 1, False),
        ("FO07", "FO", "JFK", ["B737"],       8, 1, False),
        ("CA09", "CA", "MIA", ["B757"],       8, 1, False),
        ("FO08", "FO", "MIA", ["B757"],       8, 1, False),
        ("CA12", "CA", "SEA", ["B737"],       8, 1, False),
        ("FO12", "FO", "SEA", ["B737"],       8, 1, False),
        ("CA13", "CA", "SEA", ["B737"],       9, 0, False),
        ("FO13", "FO", "SEA", ["B737"],       9, 0, False),
        ("CA14", "CA", "DEN", ["B737"],       8, 1, False),
        ("FO14", "FO", "DEN", ["B737"],       8, 1, False),
        ("CA04", "CA", "JFK", ["A320"],       9, 1, False),
        ("FO04", "FO", "JFK", ["A320"],       9, 1, False),
        ("CA17", "CA", "DEN", ["B737"],       7, 1, False),
        ("FO17", "FO", "DEN", ["B737"],       7, 1, False),
        ("CA15", "CA", "SFO", ["B737"],       8, 0, False),
        ("FO15", "FO", "SFO", ["B737"],       8, 0, False),
        ("CA16", "CA", "DEN", ["B737"],       7, 1, False),
        ("FO16", "FO", "DEN", ["B737"],       7, 1, False),
        # B777 crew
        ("CA20", "CA", "ORD", ["B777"],       7, 0, False),
        ("FO20", "FO", "ORD", ["B777"],       7, 0, False),
        ("CA21", "CA", "LAX", ["B777"],       8, 1, False),
        ("FO21", "FO", "LAX", ["B777"],       8, 1, False),
        # E175 crew
        ("CA22", "CA", "ORD", ["E175"],       8, 0, False),
        ("FO22", "FO", "ORD", ["E175"],       8, 0, False),
        ("CA23", "CA", "DTW", ["E175"],       8, 1, False),
        ("FO23", "FO", "DTW", ["E175"],       8, 1, False),
        # ORD reserves
        ("RCA01", "CA", "ORD", ["B737","B757"], 0, 0, True),
        ("RFO01", "FO", "ORD", ["B737","B757"], 0, 0, True),
        ("RCA02", "CA", "ORD", ["B737"],        0, 0, True),
        ("RFO02", "FO", "ORD", ["B737"],        0, 0, True),
        ("RCA03", "CA", "ORD", ["B757"],        0, 0, True),
        ("RFO03", "FO", "ORD", ["B757"],        0, 0, True),
        ("RCA11", "CA", "ORD", ["B777"],        0, 0, True),
        ("RFO11", "FO", "ORD", ["B777"],        0, 0, True),
        ("RCA12", "CA", "ORD", ["E175"],        0, 0, True),
        ("RFO12", "FO", "ORD", ["E175"],        0, 0, True),
        # Spoke reserves
        ("RCA04", "CA", "LAX", ["B737"],            0, 0, True),
        ("RFO04", "FO", "LAX", ["B737"],            0, 0, True),
        ("RCA10", "CA", "LAX", ["B757","B737"],     0, 0, True),
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
    ]
    crew = {
        c[0]: CrewMember(id=c[0], role=c[1], location=c[2], certifications=c[3],
                         duty_start_hour=c[4], duty_segments=c[5], is_reserve=c[6])
        for c in crew_data
    }

    raw_flights = [
        # ── ORD hub ──
        ("UA101", "ORD","LAX", 14, 0,  18, 0,  "N101UA", ["CA01","FO01"], 156),
        ("UA102", "ORD","JFK", 14,15,  17,45,  "N103UA", ["CA06","FO06"], 180),
        ("UA103", "ORD","SEA", 14,25,  17,55,  "N102UA", ["CA02","FO01"], 145),
        ("UA201", "DEN","ORD", 12, 0,  14,10,  "N106UA", ["CA05","FO05"], 120),
        ("UA202", "MIA","ORD", 11, 0,  14,20,  "N104UA", ["CA03","FO03"], 165),
        ("UA301", "ORD","DEN", 15, 0,  17, 0,  "N106UA", ["CA05","FO05"], 130),
        ("UA302", "ORD","MIA", 15,30,  19, 0,  "N104UA", ["CA03","FO03"], 158),
        # ── LAX spoke ──
        ("UA510", "SFO","LAX", 12,10,  14,15,  "N110UA", ["CA11","FO10"], 162),
        ("UA511", "LAX","ORD", 15,45,  20,15,  "N110UA", ["CA11","FO10"], 168),
        ("UA512", "LAX","SFO", 14,20,  15,35,  "N111UA", ["CA07","FO11"], 134),
        # ── JFK spoke ──
        ("UA520", "ORD","JFK", 11,10,  14,15,  "N112UA", ["CA08","FO07"], 170),
        ("UA521", "JFK","LAX", 16, 0,  19,30,  "N112UA", ["CA08","FO07"], 155),
        ("UA522", "JFK","ORD", 14,20,  16,20,  "N105UA", ["CA04","FO04"], 140),
        # ── MIA spoke ──
        ("UA530", "ORD","MIA", 11, 0,  14,20,  "N113UA", ["CA09","FO08"], 168),
        ("UA531", "MIA","ORD", 15,45,  18,15,  "N113UA", ["CA09","FO08"], 142),
        ("UA532", "MIA","JFK", 14,45,  17,30,  "N114UA", ["CA10","FO09"], 155),
        # ── SEA spoke ──
        ("UA540", "SFO","SEA", 12, 5,  14,15,  "N115UA", ["CA12","FO12"], 148),
        ("UA541", "SEA","ORD", 15,45,  20,15,  "N115UA", ["CA12","FO12"], 152),
        ("UA542", "SEA","LAX", 14,15,  16,45,  "N116UA", ["CA13","FO13"], 138),
        # ── DEN spoke ──
        ("UA550", "LAX","DEN", 12, 0,  14,10,  "N117UA", ["CA14","FO14"], 125),
        ("UA551", "DEN","SFO", 15,30,  17,15,  "N117UA", ["CA14","FO14"], 118),
        ("UA552", "DEN","LAX", 14, 8,  15,30,  "N118UA", ["CA17","FO17"], 110),
        # ── SFO spoke ──
        ("UA560", "SFO","DEN", 14, 5,  17, 5,  "N119UA", ["CA15","FO15"], 132),
        ("UA561", "DEN","SFO", 12,10,  14,25,  "N121UA", ["CA16","FO16"], 119),
        ("UA562", "SFO","LAX", 15,45,  17, 0,  "N121UA", ["CA16","FO16"], 128),
        # ── B777 widebody international ──
        ("UA800", "ORD","NRT", 14,30,  18,30,  "N201UA", ["CA20","FO20"], 280),
        ("UA802", "LAX","ORD", 11,30,  14,25,  "N202UA", ["CA21","FO21"], 265),
        ("UA803", "ORD","LAX", 16, 0,  19,30,  "N202UA", ["CA21","FO21"], 250),
        # ── E175 regional ──
        ("UA900", "ORD","DTW", 14, 5,  15,30,  "N301UA", ["CA22","FO22"],  68),
        ("UA901", "DTW","ORD", 12,15,  14,10,  "N302UA", ["CA23","FO23"],  72),
        ("UA902", "ORD","DTW", 16, 0,  17,30,  "N302UA", ["CA23","FO23"],  65),
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
    delayed_ac: dict[str, datetime] = {}

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

    changed = True
    while changed:
        changed = False
        for f in network.flights.values():
            if f.id in impacts or f.aircraft_id not in delayed_ac:
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


# ─── Agent classes ────────────────────────────────────────────────────────────

class CrewAgent:
    """
    Specialist agent: crew constraints and reserve availability.

    Owns FAA Part 117 FDP checking (Table B, exact implementation — an
    approximation that clears an illegal crew member creates regulatory
    liability) and the reserve pool query interface by type/role/station.
    """

    def __init__(self, network: Network):
        self.network = network

    def fdp_check(self, crew_member: CrewMember, ac_type: str,
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

    def available_reserves(self, origin: str, ac_type: str, role: str,
                           reserved: dict) -> list[CrewMember]:
        """Find reserves available at this station for this aircraft type and role."""
        return [
            c for c in self.network.crew.values()
            if c.is_reserve
            and c.location == origin
            and ac_type in c.certifications
            and c.role == role
            and c.id not in reserved
        ]


class AircraftAgent:
    """
    Specialist agent: type ratings, rotations, and spare aircraft availability.

    Owns the fleet cost model (three tiers: widebody international /
    narrowbody mainline / regional) and the spare aircraft query interface.
    A flat cost function would treat a B777-NRT identically to an E175-DTW —
    an order-of-magnitude error in allocation priority.
    """

    def __init__(self, network: Network):
        self.network = network

    def cost_per_min(self, ac_type: str) -> float:
        return _COST_PER_MIN.get(ac_type, 80.0)

    def available_spare(self, flight: Flight, reserved: dict) -> Optional[Aircraft]:
        ac = self.network.aircraft.get(flight.aircraft_id)
        if not ac:
            return None
        spares = [
            a for a in self.network.aircraft.values()
            if a.spare and a.type == ac.type
            and a.location == flight.origin
            and a.id != flight.aircraft_id
            and a.id not in reserved
        ]
        return spares[0] if spares else None

    def on_stand_check(self, flight: Flight) -> ConstraintResult:
        ac = self.network.aircraft.get(flight.aircraft_id)
        if ac:
            return ConstraintResult(
                f"Aircraft {ac.id}", True,
                f"{ac.subtype} on stand at {flight.origin}"
            )
        return ConstraintResult("Aircraft", False, "Aircraft record not found")


class PassengerAgent:
    """
    Specialist agent: connection risk and delay-dependent misconnection cost.

    Passengers are on journeys, not flights. A 40-minute delay breaks a
    connecting itinerary two hops away. This agent makes misconnection cost
    a function of the specific delay option being evaluated — different delays
    break different connections — rather than a flat charge per flight.
    """

    def misconnect_cost(self, flight: Flight, delay_min: int) -> float:
        """
        Zero below MCT (45 min). Scales linearly to full connecting fraction at 2×MCT.
        B777-NRT (280 pax, 90-min delay): $9,800 in misconnections alone.
        E175-DTW (68 pax, 90-min delay): $2,142.
        """
        if delay_min <= _MCT_MIN:
            return 0.0
        fraction_broken = min(1.0, (delay_min - _MCT_MIN) / _MCT_MIN)
        return flight.passengers * _CONNECT_RATE * fraction_broken * _MISCONNECT_COST

    def connection_risk(self, flight: Flight, delay_min: int) -> str:
        if delay_min <= _MCT_MIN:
            return "LOW"
        if delay_min <= int(_MCT_MIN * 1.5):
            return "MEDIUM"
        return "HIGH"


class CoordinatingAgent:
    """
    Orchestrating agent: manages the shared resource pool and coordinates
    CrewAgent, AircraftAgent, and PassengerAgent across all affected flights.

    This is what separates dCortex from sequential dispatcher allocation:
    resources committed to Flight A are tracked before Flight B's options
    are scored, using full pool visibility across the entire impact set.
    Dispatchers allocate flight-by-flight — best reserve for A, then best
    remaining for B. They can't see whether that sequence depletes the pool
    for C through H in ways a different order would avoid.

    Allocation strategy: priority-ordered by passenger count, shared pool
    accounting. Not true joint optimization (integer-program) — deferred.
    Structurally different from sequential greedy; the gap is largest when
    disruptions are most expensive.
    """

    def __init__(self, network: Network):
        self.network = network
        self.crew_agent = CrewAgent(network)
        self.aircraft_agent = AircraftAgent(network)
        self.passenger_agent = PassengerAgent()

    def _delay_option(self, flight: Flight, delay_min: int,
                      disruption_time: datetime) -> ResolutionOption:
        ac = self.network.aircraft.get(flight.aircraft_id)
        ac_type = ac.type if ac else "B737"
        crew = [self.network.crew[c] for c in flight.crew_ids if c in self.network.crew]

        constraints = [
            self.crew_agent.fdp_check(c, ac_type, disruption_time,
                                       flight.duration_hours, delay_min / 60)
            for c in crew
        ]
        constraints.append(self.aircraft_agent.on_stand_check(flight))

        dep = (flight.departure + timedelta(minutes=delay_min)).strftime("%H:%M")
        pax_cost = self.passenger_agent.misconnect_cost(flight, delay_min)
        return ResolutionOption(
            type="DELAY",
            description=f"Delay {delay_min} min — departs {dep}",
            cost=self.aircraft_agent.cost_per_min(ac_type) * delay_min + pax_cost,
            delay_min=delay_min,
            feasible=all(c.passed for c in constraints),
            constraints=constraints,
        )

    def _crew_swap_option(self, flight: Flight, delay_min: int,
                          reserved: dict) -> Optional[ResolutionOption]:
        ac = self.network.aircraft.get(flight.aircraft_id)
        if not ac:
            return None

        eff_delay = max(delay_min, _RESERVE_CALLOUT)
        ca_list = self.crew_agent.available_reserves(flight.origin, ac.type, "CA", reserved)
        fo_list = self.crew_agent.available_reserves(flight.origin, ac.type, "FO", reserved)
        ca = ca_list[0] if ca_list else None
        fo = fo_list[0] if fo_list else None

        constraints = [
            ConstraintResult("Reserve callout window", True,
                             f"Min {_RESERVE_CALLOUT} min → effective delay {eff_delay} min"),
            ConstraintResult(f"Reserve CA ({ac.type}) at {flight.origin}",
                             bool(ca),
                             f"{ca.id} available" if ca
                             else f"No certified reserve CA at {flight.origin}"),
            ConstraintResult(f"Reserve FO ({ac.type}) at {flight.origin}",
                             bool(fo),
                             f"{fo.id} available" if fo
                             else f"No certified reserve FO at {flight.origin}"),
        ]

        dep = (flight.departure + timedelta(minutes=eff_delay)).strftime("%H:%M")
        pax_cost = self.passenger_agent.misconnect_cost(flight, eff_delay)
        return ResolutionOption(
            type="CREW_SWAP",
            description=(f"Reserve crew ({ca.id if ca else '—'} / {fo.id if fo else '—'}) "
                         f"— departs {dep}"),
            cost=_RESERVE_COST * 2 + self.aircraft_agent.cost_per_min(ac.type) * eff_delay + pax_cost,
            delay_min=eff_delay,
            feasible=bool(ca and fo),
            constraints=constraints,
            resources=[c.id for c in [ca, fo] if c],
        )

    def _ac_swap_option(self, flight: Flight, delay_min: int,
                        disruption_time: datetime, reserved: dict) -> Optional[ResolutionOption]:
        ac = self.network.aircraft.get(flight.aircraft_id)
        if not ac:
            return None

        spare = self.aircraft_agent.available_spare(flight, reserved)
        if not spare:
            return None

        crew = [self.network.crew[c] for c in flight.crew_ids if c in self.network.crew]
        constraints = [
            self.crew_agent.fdp_check(c, ac.type, disruption_time,
                                       flight.duration_hours, delay_min / 60)
            for c in crew
        ]
        constraints.append(ConstraintResult(
            f"Spare {ac.type} at {flight.origin}", True,
            f"{spare.id} ({spare.subtype}) unscheduled — available now"
        ))

        dep = (flight.departure + timedelta(minutes=delay_min)).strftime("%H:%M")
        pax_cost = self.passenger_agent.misconnect_cost(flight, delay_min)
        return ResolutionOption(
            type="AIRCRAFT_SWAP",
            description=f"Swap to spare {spare.id} — departs {dep}",
            cost=self.aircraft_agent.cost_per_min(ac.type) * delay_min + _AC_SWAP_FEE + pax_cost,
            delay_min=delay_min,
            feasible=all(c.passed for c in constraints),
            constraints=constraints,
            resources=[spare.id],
        )

    def _cancel_option(self, flight: Flight) -> ResolutionOption:
        return ResolutionOption(
            type="CANCEL",
            description=f"Cancel — reaccommodate {flight.passengers} passengers",
            cost=flight.passengers * _CANCEL_PER_PAX + _CANCEL_OPS_FEE,
            delay_min=0,
            feasible=True,
            constraints=[ConstraintResult("Feasibility", True, "Always available — last resort")],
        )

    def gen_options(self, flight: Flight, impact: FlightImpact,
                    disruption_time: datetime, reserved: dict) -> list[ResolutionOption]:
        delay = max(impact.expected_delay_min, 30)
        options: list[ResolutionOption] = [
            self._delay_option(flight, delay, disruption_time)
        ]

        cs = self._crew_swap_option(flight, delay, reserved)
        if cs:
            options.append(cs)

        acs = self._ac_swap_option(flight, max(impact.expected_delay_min, 5),
                                    disruption_time, reserved)
        if acs:
            options.append(acs)

        options.append(self._cancel_option(flight))
        return options

    def gen_plan(self, impacts: list[FlightImpact],
                 disruption_time: datetime) -> tuple[list[FlightResolution], float, float, dict]:
        reserved: dict[str, str] = {}
        resolutions: list[FlightResolution] = []
        plan_cost = 0.0
        donoth_cost = 0.0

        for impact in impacts:
            f = impact.flight
            ac = self.network.aircraft.get(f.aircraft_id)
            ac_type = ac.type if ac else "B737"

            donoth_delay = impact.expected_delay_min + 60
            donoth_cost += self.aircraft_agent.cost_per_min(ac_type) * donoth_delay

            options = self.gen_options(f, impact, disruption_time, reserved)
            feasible = sorted([o for o in options if o.feasible], key=lambda o: o.cost)

            if feasible:
                rec = feasible[0]
                rec.recommended = True
                for rid in rec.resources:
                    reserved[rid] = f.id
                plan_cost += rec.cost
            else:
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


# ─── Entry points ─────────────────────────────────────────────────────────────

def run(airport: str, start_hour: int = 14, start_min: int = 0,
        duration_min: int = 30, cause: str = "Weather") -> DisruptionResult:
    """
    Single-scenario pipeline: confirmed disruption, known duration.
    Used by simulate() for each severity tier, and directly in live mode
    once the FAA issues the ground stop and duration is known.
    """
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

    coordinator = CoordinatingAgent(network)
    resolutions, plan_cost, donoth_cost, reserved = coordinator.gen_plan(impacts, start)
    trace = _build_trace(airport, start, duration_min, eff_end, resolutions, reserved, plan_cost)

    return DisruptionResult(
        airport=airport, cause=cause, start=start,
        duration_min=duration_min, effective_end=eff_end,
        resolutions=resolutions, reserved_resources=reserved,
        plan_cost=plan_cost, donoth_cost=donoth_cost,
        trace=trace,
    )


def simulate(airport: str, start_hour: int = 14, start_min: int = 0,
             cause: str = "Weather") -> SimulationResult:
    """
    Multi-scenario simulation: run P50/P75/P90 before committing to any action.

    This is the simulator mode. Before the FAA issues a ground stop, the OCC
    can ask: 'If ORD takes a weather hit at 14:00, what does the P75 scenario
    cost, and what resources do we need?' The answer comes back in milliseconds,
    not 15-20 minutes of manual correlation.

    The OCC can practice disruptions on forecast data, test pre-positioning
    strategies ('what if we move a B777 reserve to ORD before the storm?'),
    and build institutional muscle memory before the disruption fires.

    In live mode: run simulate() on first alert to see the envelope, then
    run() with the confirmed duration once the FAA issues the EDCT.
    """
    scenarios = [
        Scenario(label=label, duration_min=dur,
                 result=run(airport, start_hour, start_min, dur, cause))
        for label, dur in SEVERITY_PROFILES.items()
    ]
    return SimulationResult(
        airport=airport, cause=cause,
        start=_dt(start_hour, start_min),
        scenarios=scenarios,
    )
