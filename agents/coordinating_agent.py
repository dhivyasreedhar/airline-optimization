import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from models import Flight, AllocationOption, DisruptionEvent
from agents.crew_agent import CrewAgent, CREW_ACTIVATION_COST
from agents.aircraft_agent import AircraftAgent
from agents.passenger_agent import PassengerAgent

# Uncovered flights face a much worse outcome than a managed delay.
UNCOVERED_DELAY_MULTIPLIER = 3

# Probability that an unmanaged inbound flight is diverted to an alternate
# airport (vs. held at origin). Diverted aircraft must ferry back to hub.
DIVERSION_PROBABILITY = 0.30

# DOT tarmac delay rule fines (per departure, not per passenger in this model).
# Domestic: 3h limit; International: 4h limit. Fine = $27,500/departure.
DOT_DOMESTIC_LIMIT_MIN    = 180
DOT_INTERNATIONAL_LIMIT_MIN = 240
DOT_TARMAC_FINE           = 27_500.0
_INTERNATIONAL_DESTS = {"NRT", "LHR", "GRU", "CDG", "FRA", "HND", "PEK"}

# CBA seniority soft constraint: out-of-order activation costs the airline a
# union grievance penalty. Proxy: $5 per seniority rank point per crew member.
CBA_SENIORITY_RATE = 5.0


def _make_uncovered(
    flight: Flight,
    aircraft_agent: AircraftAgent,
    passenger_agent: PassengerAgent,
    delay_minutes: int,
    hub: str = "",
) -> AllocationOption:
    effective_delay = delay_minutes * UNCOVERED_DELAY_MULTIPLIER
    aircraft_cost = aircraft_agent.compute_aircraft_cost(flight, effective_delay)
    passenger_cost = passenger_agent.compute_passenger_cost(flight, effective_delay)

    # Inbound (destination == hub): aircraft is at remote station during ground stop.
    # There is a DIVERSION_PROBABILITY chance it gets diverted to an alternate airport
    # instead of holding, requiring a ferry flight back. Outbound aircraft simply
    # ground-delay at the hub — no repositioning cost.
    ferry_cost = 0.0
    violated = ["NO_LEGAL_CREW_AVAILABLE"]
    if hub and flight.destination == hub:
        ferry_cost = aircraft_agent.compute_ferry_cost(flight) * DIVERSION_PROBABILITY
        violated.append("INBOUND_DIVERSION_RISK")

    # DOT tarmac delay fine: applies when uncovered effective delay exceeds the limit.
    is_intl = flight.destination in _INTERNATIONAL_DESTS or flight.origin in _INTERNATIONAL_DESTS
    dot_limit = DOT_INTERNATIONAL_LIMIT_MIN if is_intl else DOT_DOMESTIC_LIMIT_MIN
    dot_fine = 0.0
    if effective_delay > dot_limit:
        dot_fine = DOT_TARMAC_FINE
        violated.append("DOT_TARMAC")

    return AllocationOption(
        flight_id=flight.flight_id,
        option_id=f"{flight.flight_id}_UNCOVERED",
        reserve_ca=None,
        reserve_fo=None,
        estimated_delay_minutes=effective_delay,
        aircraft_cost=aircraft_cost,
        passenger_cost=passenger_cost,
        crew_activation_cost=0.0,
        soft_constraint_penalties={},
        total_cost=aircraft_cost + passenger_cost + ferry_cost + dot_fine,
        hard_constraints_checked=[],
        hard_constraints_violated=violated,
        feasible=False,
        ferry_cost=ferry_cost,
        dot_fine=dot_fine,
    )


class CoordinatingAgent:
    def __init__(
        self,
        crew_agent: CrewAgent,
        aircraft_agent: AircraftAgent,
        passenger_agent: PassengerAgent,
        anthropic_client,
        hub: str = "",
        hub_gates: dict = None,
    ):
        self.crew = crew_agent
        self.aircraft = aircraft_agent
        self.passenger = passenger_agent
        self.client = anthropic_client
        self.hub = hub
        # Gate pool: counts of each gate type at the hub
        self.hub_gates = hub_gates or {"widebody": 8, "narrowbody": 15, "regional": 20}
        self._used_gate_counts: dict = {}

    def _gate_available(self, flight: Flight) -> tuple:
        """Check if the flight's required gate type is still available."""
        gt = flight.gate_type
        used = self._used_gate_counts.get(gt, 0)
        total = self.hub_gates.get(gt, 0)
        if used >= total:
            return False, f"GATE: all {gt} gates in use ({used}/{total})"
        return True, ""

    def _use_gate(self, flight: Flight) -> None:
        gt = flight.gate_type
        self._used_gate_counts[gt] = self._used_gate_counts.get(gt, 0) + 1

    def _available_gate_list(self) -> list:
        """Build a flat list of remaining gate type strings for compatibility check."""
        gates = []
        for gtype, total in self.hub_gates.items():
            used = self._used_gate_counts.get(gtype, 0)
            gates.extend([gtype] * max(0, total - used))
        return gates

    def build_options(
        self,
        flight: Flight,
        current_time: datetime,
        delay_minutes: int,
    ) -> list:
        """
        Enumerate all legal (CA, FO) reserve pairs for this flight.
        Gate availability is checked first for hub-departing flights.
        Only feasible options are returned.
        """
        # Gate check only applies to outbound flights (departing from hub).
        # Inbound aircraft are at a remote station — their gate need is for the
        # return leg, which is handled separately.
        if flight.origin == self.hub:
            gate_ok, _ = self.aircraft.check_gate_compatibility(
                flight, self._available_gate_list()
            )
            if not gate_ok:
                return []

        options = []
        ca_results = self.crew.get_legal_ca_options(flight, current_time, delay_minutes)
        legal_cas = [r["crew"] for r in ca_results if r["feasible"]]

        for ca in legal_cas:
            legal_fos = self.crew.get_legal_fo_for_ca(ca, flight, current_time, delay_minutes)
            for fo in legal_fos:
                aircraft_cost = self.aircraft.compute_aircraft_cost(flight, delay_minutes)
                passenger_cost = self.passenger.compute_passenger_cost(flight, delay_minutes)
                activation_cost = CREW_ACTIVATION_COST * 2   # CA + FO

                # CBA seniority soft constraint: penalise activation of senior reserves
                # when junior ones exist. Proxy: $5 × seniority_rank per crew member.
                seniority_penalty = (ca.seniority_rank + fo.seniority_rank) * CBA_SENIORITY_RATE
                soft_penalties = {}
                if seniority_penalty > 0:
                    soft_penalties["cba_seniority"] = round(seniority_penalty, 2)

                option = AllocationOption(
                    flight_id=flight.flight_id,
                    option_id=f"{flight.flight_id}_{ca.crew_id}_{fo.crew_id}",
                    reserve_ca=ca.crew_id,
                    reserve_fo=fo.crew_id,
                    estimated_delay_minutes=delay_minutes,
                    aircraft_cost=aircraft_cost,
                    passenger_cost=passenger_cost,
                    crew_activation_cost=activation_cost,
                    soft_constraint_penalties=soft_penalties,
                    total_cost=aircraft_cost + passenger_cost + activation_cost + seniority_penalty,
                    hard_constraints_checked=[
                        "type_rating", "role", "domicile",
                        "callout_window", "fdp_part117", "committed_pool",
                        "gate_availability",
                    ],
                    hard_constraints_violated=[],
                    feasible=True,
                    ferry_cost=0.0,
                    dot_fine=0.0,
                )
                options.append(option)

        return sorted(options, key=lambda o: o.total_cost)

    def _marginal_cost_estimate(self, flight: Flight, delay_minutes: int) -> float:
        """Cost saved by covering this flight vs. leaving it uncovered — used for pool-aware ranking."""
        eff = delay_minutes * UNCOVERED_DELAY_MULTIPLIER
        uncov = (
            self.aircraft.compute_aircraft_cost(flight, eff)
            + self.passenger.compute_passenger_cost(flight, eff)
            + (
                self.aircraft.compute_ferry_cost(flight) * DIVERSION_PROBABILITY
                if self.hub and flight.destination == self.hub else 0.0
            )
        )
        covered = (
            self.aircraft.compute_aircraft_cost(flight, delay_minutes)
            + self.passenger.compute_passenger_cost(flight, delay_minutes)
        )
        return uncov - covered

    def allocate_pool_aware(
        self, affected_flights: list, current_time: datetime, delay_minutes: int
    ) -> list:
        """
        Rank flights by marginal cost saved (uncovered cost minus covered cost) so the
        flights with the most to lose are served first. Gate and crew pools are both tracked.
        """
        self.crew.reset()
        self._used_gate_counts = {}
        ranked = sorted(
            affected_flights,
            key=lambda f: self._marginal_cost_estimate(f, delay_minutes),
            reverse=True,
        )
        selected = []

        for flight in ranked:
            options = self.build_options(flight, current_time, delay_minutes)
            if options:
                best = options[0]
                self.crew.commit(best.reserve_ca)
                self.crew.commit(best.reserve_fo)
                self._use_gate(flight)
                selected.append(best)
            else:
                selected.append(
                    _make_uncovered(flight, self.aircraft, self.passenger,
                                    delay_minutes, self.hub)
                )

        return selected

    def allocate_greedy(
        self, affected_flights: list, current_time: datetime, delay_minutes: int
    ) -> list:
        """
        Process flights in scheduled departure order.
        Same reserve pool and gate state as pool-aware — only ordering differs.
        """
        self.crew.reset()
        self._used_gate_counts = {}
        ordered = sorted(affected_flights, key=lambda f: f.scheduled_departure)
        selected = []

        for flight in ordered:
            options = self.build_options(flight, current_time, delay_minutes)
            if options:
                best = options[0]
                self.crew.commit(best.reserve_ca)
                self.crew.commit(best.reserve_fo)
                self._use_gate(flight)
                selected.append(best)
            else:
                selected.append(
                    _make_uncovered(flight, self.aircraft, self.passenger,
                                    delay_minutes, self.hub)
                )

        return selected

    def narrate(
        self,
        pool_aware: list,
        greedy: list,
        disruption: DisruptionEvent,
    ) -> str:
        pool_cost = sum(o.total_cost for o in pool_aware)
        greedy_cost = sum(o.total_cost for o in greedy)
        uncovered_pool = [o for o in pool_aware if not o.feasible]
        uncovered_greedy = [o for o in greedy if not o.feasible]
        delta = greedy_cost - pool_cost

        breakdown_lines = "\n".join(
            f"  {o.flight_id}: ${o.total_cost:,.0f} "
            f"(aircraft ${o.aircraft_cost:,.0f} + pax ${o.passenger_cost:,.0f} "
            f"+ crew ${o.crew_activation_cost:,.0f}"
            + (f" + ferry ${o.ferry_cost:,.0f}" if o.ferry_cost else "")
            + (f" + DOT_fine ${o.dot_fine:,.0f}" if o.dot_fine else "")
            + (f" + CBA ${o.soft_constraint_penalties['cba_seniority']:,.0f}"
               if o.soft_constraint_penalties.get("cba_seniority") else "")
            + f") {'COVERED' if o.feasible else 'UNCOVERED'}"
            + (" [INBOUND DIVERSION RISK]" if "INBOUND_DIVERSION_RISK" in o.hard_constraints_violated else "")
            + (" [DOT TARMAC FINE]" if "DOT_TARMAC" in o.hard_constraints_violated else "")
            for o in pool_aware
        )

        summary = f"""
DISRUPTION: {disruption.hub} weather ground stop
Duration: {disruption.duration_minutes} min actual | {disruption.effective_window_minutes} min effective window
Scenario: {disruption.scenario}

POOL-AWARE ALLOCATION (highest-value flights first):
  Total cost: ${pool_cost:,.2f}
  Flights covered: {len(pool_aware) - len(uncovered_pool)}/{len(pool_aware)}
  Uncovered: {[o.flight_id for o in uncovered_pool] or 'none'}

GREEDY ALLOCATION (departure order):
  Total cost: ${greedy_cost:,.2f}
  Flights covered: {len(greedy) - len(uncovered_greedy)}/{len(greedy)}
  Uncovered: {[o.flight_id for o in uncovered_greedy] or 'none'}

COST DELTA: ${abs(delta):,.2f} ({'pool-aware saves' if delta > 0 else 'greedy saves'} this amount)

PER-FLIGHT BREAKDOWN (pool-aware):
{breakdown_lines}

NOTE: Uncovered inbound flights carry a 30% diversion probability — ferry cost is included.
Uncovered outbound flights incur only ground-delay cost (aircraft stays at hub).
"""

        response = self.client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1200,
            messages=[{
                "role": "user",
                "content": (
                    "You are the reasoning layer of an airline disruption management system.\n"
                    "A dispatcher is reviewing the following allocation recommendation. "
                    "Explain it in 4–5 sentences. Name specific flight IDs (e.g. UA901, UA204) "
                    "and specific dollar amounts. State what was prioritized and why. "
                    "Flag any uncovered flights and what the dispatcher should know before accepting. "
                    "If any uncovered flights are inbound, mention the diversion risk and ferry cost.\n\n"
                    f"{summary}\n\n"
                    "Be precise. Reference the cost delta between strategies."
                ),
            }],
        )
        return response.content[0].text
