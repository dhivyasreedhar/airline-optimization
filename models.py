from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Flight:
    flight_id: str
    origin: str
    destination: str
    aircraft_type: str          # "widebody", "narrowbody", "regional", "widebody_domestic"
    scheduled_departure: datetime
    scheduled_arrival: datetime
    passenger_count: int
    tail_number: str
    crew_required: dict         # {"CA": id, "FO": id}
    gate_type: str              # "widebody", "narrowbody", "regional"


@dataclass
class CrewMember:
    crew_id: str
    role: str                   # "CA" or "FO"
    type_rating: str            # "B737", "B777", "B787", "B767", "E175"
    domicile: str
    current_station: str
    duty_start: datetime
    segments_flown: int
    is_reserve: bool
    callout_status: str         # "available", "short_call", "long_call", "unavailable"
    seniority_rank: int = 0     # 0 = most junior (activate first per CBA), higher = more senior


@dataclass
class DisruptionEvent:
    event_id: str
    hub: str
    start_time: datetime
    duration_minutes: int
    effective_window_minutes: int
    scenario: str               # "P50", "P75", "P90"


@dataclass
class AllocationOption:
    flight_id: str
    option_id: str
    reserve_ca: Optional[str]
    reserve_fo: Optional[str]
    estimated_delay_minutes: int
    aircraft_cost: float
    passenger_cost: float
    crew_activation_cost: float
    soft_constraint_penalties: dict
    total_cost: float
    hard_constraints_checked: list
    hard_constraints_violated: list    # must be empty — options with violations discarded
    feasible: bool
    ferry_cost: float = 0.0            # repositioning cost if inbound aircraft is diverted
    dot_fine: float = 0.0              # DOT tarmac rule fine if uncovered delay > 3h/4h limit


@dataclass
class ReasoningTrace:
    trace_id: str
    disruption_event: DisruptionEvent
    cascade_graph: dict
    affected_flights: list
    options_generated: list
    allocation_method: str
    option_selected: Optional[AllocationOption]
    predicted_cost: float
    actual_cost: Optional[float]
    override_reason: Optional[str]
    timestamp: datetime
    llm_narration: str
