import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Flight

# Probability of connection break by delay bucket (minutes) and route type
# Based on MCT (Minimum Connection Time) distributions
CONNECTION_BREAK_RATES = {
    "domestic_short":  {30: 0.05, 60: 0.20,  90: 0.45, 120: 0.70},
    "domestic_medium": {30: 0.02, 60: 0.10,  90: 0.30, 120: 0.55},
    "international":   {30: 0.01, 60: 0.05,  90: 0.15, 120: 0.35},
}

REACCOMMODATION_COST_PER_PAX = 1_000.0   # midpoint of $800–$1,200

# Fraction of passengers who are connecting (vs. O&D)
CONNECT_RATE = {
    "widebody":          0.65,   # international feed is mostly connecting
    "widebody_domestic": 0.50,
    "narrowbody":        0.40,
    "regional":          0.80,   # regional flights are predominantly connecting traffic
}

INTERNATIONAL_DESTS = {"NRT", "LHR", "GRU", "CDG", "FRA", "HND", "PEK"}


def _route_type(flight: Flight) -> str:
    if flight.destination in INTERNATIONAL_DESTS or flight.origin in INTERNATIONAL_DESTS:
        return "international"
    if flight.aircraft_type == "regional":
        return "domestic_short"
    return "domestic_medium"


def _interpolate_break_rate(delay_minutes: int, rates: dict) -> float:
    buckets = sorted(rates.keys())
    if delay_minutes <= buckets[0]:
        return rates[buckets[0]]
    if delay_minutes >= buckets[-1]:
        return rates[buckets[-1]]
    for i in range(len(buckets) - 1):
        lo, hi = buckets[i], buckets[i + 1]
        if lo <= delay_minutes < hi:
            t = (delay_minutes - lo) / (hi - lo)
            return rates[lo] + t * (rates[hi] - rates[lo])
    return rates[buckets[-1]]


class PassengerAgent:
    def __init__(self, reaccommodation_cost: float = REACCOMMODATION_COST_PER_PAX):
        self.reaccommodation_cost = reaccommodation_cost

    def compute_passenger_cost(self, flight: Flight, delay_minutes: int) -> float:
        route_type = _route_type(flight)
        rates = CONNECTION_BREAK_RATES[route_type]
        break_rate = _interpolate_break_rate(delay_minutes, rates)
        connecting_pax = flight.passenger_count * CONNECT_RATE.get(flight.aircraft_type, 0.40)
        missed = connecting_pax * break_rate
        return missed * self.reaccommodation_cost

    def connection_risk_summary(self, flight: Flight, delay_minutes: int) -> str:
        route_type = _route_type(flight)
        rates = CONNECTION_BREAK_RATES[route_type]
        break_rate = _interpolate_break_rate(delay_minutes, rates)
        connecting_pax = flight.passenger_count * CONNECT_RATE.get(flight.aircraft_type, 0.40)
        missed = connecting_pax * break_rate
        cost = missed * REACCOMMODATION_COST_PER_PAX
        return (
            f"{flight.flight_id}: {flight.passenger_count} pax, "
            f"{connecting_pax:.0f} connecting, "
            f"{break_rate:.0%} break rate → {missed:.0f} misconnects → ${cost:,.0f}"
        )
