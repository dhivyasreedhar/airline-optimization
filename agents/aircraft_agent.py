import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Flight

# Cost per minute of delay by aircraft tier
# Source: Airlines for America 2024; Eurocontrol Standard Inputs
COST_PER_MIN = {
    "widebody":          175.0,   # $150–200+/min, midpoint
    "widebody_domestic": 135.0,   # $120–150/min
    "narrowbody":         87.0,   # $74–100/min, midpoint
    "regional":           40.0,   # $30–50/min, midpoint
}

# Ferry/repositioning cost: rate × typical hours to reposition from alternate airport
FERRY_COST_PER_HOUR = {
    "widebody":          4_500.0,
    "widebody_domestic": 3_500.0,
    "narrowbody":        2_000.0,
    "regional":            800.0,
}
FERRY_HOURS_ESTIMATE = {
    "widebody": 3.0, "widebody_domestic": 2.5, "narrowbody": 1.5, "regional": 1.0,
}


class AircraftAgent:
    def __init__(self, cost_overrides: dict = None):
        self.rates = {**COST_PER_MIN, **(cost_overrides or {})}

    def compute_aircraft_cost(self, flight: Flight, delay_minutes: int) -> float:
        rate = self.rates.get(flight.aircraft_type, 87.0)
        return rate * delay_minutes

    def compute_ferry_cost(self, flight: Flight) -> float:
        """Expected repositioning cost if diverted aircraft must ferry back to hub."""
        rate = FERRY_COST_PER_HOUR.get(flight.aircraft_type, 2_000.0)
        hours = FERRY_HOURS_ESTIMATE.get(flight.aircraft_type, 1.5)
        return rate * hours

    def check_gate_compatibility(
        self, flight: Flight, available_gates: list
    ) -> tuple:
        """
        available_gates: list of gate-type strings still available at the hub.
        Gate hierarchy: widebody gates serve all types; narrowbody gates serve
        narrowbody + regional; regional gates serve only regional.
        """
        if not available_gates:
            return False, "GATE: no gates available at hub"
        gt = flight.gate_type
        if gt == "regional":
            return True, ""
        if gt == "narrowbody":
            if any(g in ("narrowbody", "widebody") for g in available_gates):
                return True, ""
            return False, "GATE: no narrowbody/widebody gate available"
        # widebody or widebody_domestic
        if any(g == "widebody" for g in available_gates):
            return True, ""
        return False, f"GATE: no widebody gate available ({len(available_gates)} gates remain, wrong type)"

    def rank_by_cost(
        self, flights: list, delay_minutes: int
    ) -> list:
        """Sort flights highest-cost-first so pool-aware allocates most valuable first."""
        return sorted(
            flights,
            key=lambda f: self.compute_aircraft_cost(f, delay_minutes),
            reverse=True,
        )

    def cost_summary(self, flights: list, delay_minutes: int) -> None:
        print(f"\n  Aircraft cost ranking (delay={delay_minutes} min):")
        ranked = self.rank_by_cost(flights, delay_minutes)
        for f in ranked:
            cost = self.compute_aircraft_cost(f, delay_minutes)
            rate = COST_PER_MIN.get(f.aircraft_type, 87.0)
            print(f"    {f.flight_id:8s} {f.aircraft_type:20s} "
                  f"${rate:.0f}/min × {delay_minutes}min = ${cost:,.0f}")
