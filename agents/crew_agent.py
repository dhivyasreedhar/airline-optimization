import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from models import Flight, CrewMember, AllocationOption

# FAA Part 117 Table B — Maximum Flight Duty Period by scheduled start hour (local)
# Single augmented operations, standard domestic
PART_117_TABLE_B = {
    (0,  4):  9.0,   # 0000–0359
    (4,  5): 10.0,   # 0400–0459
    (5,  6): 11.5,   # 0500–0559
    (6,  7): 12.0,   # 0600–0659
    (7,  8): 12.0,   # 0700–0759
    (8,  9): 12.0,   # 0800–0859
    (9, 10): 12.0,   # 0900–0959
    (10, 11): 12.0,  # 1000–1059
    (11, 12): 11.5,  # 1100–1159
    (12, 13): 11.0,  # 1200–1259
    (13, 14): 10.5,  # 1300–1359
    (14, 15): 10.0,  # 1400–1459
    (15, 16):  9.5,  # 1500–1559
    (16, 17):  9.0,  # 1600–1659
    (17, 18):  9.0,  # 1700–1759
    (18, 19):  9.0,  # 1800–1859
    (19, 20):  9.0,  # 1900–1959
    (20, 24):  9.0,  # 2000–2359
}

# Aircraft type → acceptable type ratings (hard constraint)
# widebody_domestic (transcontinental 777/787) shares ratings with long-haul widebody
AIRCRAFT_RATING_MAP = {
    "widebody":          ["B777", "B787"],
    "widebody_domestic": ["B777", "B787", "B767"],
    "narrowbody":        ["B737", "A320"],
    "regional":          ["E175", "E170"],
}

# Minimum callout window by reserve availability status.
# "available" = at airport ops room; "long_call" = at home, standard 2-hour notice.
CALLOUT_WINDOWS = {
    "available":   30,
    "short_call":  60,
    "long_call":  120,
    "unavailable": 9999,
}
CREW_ACTIVATION_COST = 800.0   # per crew member, midpoint of $600–$1,000


def max_fdp(duty_start_hour: int) -> float:
    for (start, end), hours in PART_117_TABLE_B.items():
        if start <= duty_start_hour < end:
            return hours
    return 9.0


def remaining_fdp(crew: CrewMember, current_time: datetime) -> float:
    elapsed_hours = (current_time - crew.duty_start).total_seconds() / 3600
    limit = max_fdp(crew.duty_start.hour)
    return max(0.0, limit - elapsed_hours)


class CrewAgent:
    def __init__(self, reserve_pool: list):
        self.reserve_pool = reserve_pool
        self.committed: set = set()          # crew_ids committed to a flight this allocation pass

    def check_crew_legal(
        self,
        crew: CrewMember,
        flight: Flight,
        current_time: datetime,
        delay_minutes: int,
    ) -> tuple:
        """
        Returns (feasible: bool, violations: list[str]).
        All five hard constraints are checked in order.
        The caller must discard any option where feasible=False.
        """
        violations = []
        flight_departure = flight.scheduled_departure + timedelta(minutes=delay_minutes)
        flight_duration_hours = (
            (flight.scheduled_arrival - flight.scheduled_departure).total_seconds() / 3600
        )

        # Hard constraint 1: type rating
        required_ratings = AIRCRAFT_RATING_MAP.get(flight.aircraft_type, [])
        if crew.type_rating not in required_ratings:
            violations.append(
                f"TYPE_RATING: {crew.type_rating} not valid for "
                f"{flight.aircraft_type} (requires one of {required_ratings})"
            )

        # Hard constraint 2: domicile / current station
        if crew.current_station != flight.origin:
            violations.append(
                f"DOMICILE: crew at {crew.current_station}, flight departs {flight.origin}"
            )

        # Hard constraint 3: callout window — minimum notice varies by reserve status
        callout_min = CALLOUT_WINDOWS.get(crew.callout_status, 120)
        minutes_until_departure = (flight_departure - current_time).total_seconds() / 60
        if minutes_until_departure < callout_min:
            violations.append(
                f"CALLOUT: {minutes_until_departure:.0f} min until departure, "
                f"need {callout_min} min (status: {crew.callout_status})"
            )

        # Hard constraint 4: Part 117 FDP — duty hours from current_time to actual arrival.
        # Must include wait time at station, not just block time + delay.
        fdp_remaining = remaining_fdp(crew, current_time)
        actual_arrival = flight.scheduled_arrival + timedelta(minutes=delay_minutes)
        hours_needed = (actual_arrival - current_time).total_seconds() / 3600
        if fdp_remaining < hours_needed:
            violations.append(
                f"FDP_PART117: {fdp_remaining:.2f}h remaining FDP "
                f"(started {crew.duty_start.strftime('%H:%M')}, "
                f"max {max_fdp(crew.duty_start.hour):.1f}h), "
                f"need {hours_needed:.2f}h (now→actual arrival)"
            )

        # Hard constraint 5: already committed to another flight
        if crew.crew_id in self.committed:
            violations.append(f"COMMITTED: {crew.crew_id} already assigned to another flight")

        return len(violations) == 0, violations

    def get_legal_ca_options(
        self, flight: Flight, current_time: datetime, delay_minutes: int
    ) -> list:
        """Returns list of dicts {crew, feasible, violations} for all CA reserves."""
        results = []
        for crew in self.reserve_pool:
            if crew.role != "CA":
                continue
            feasible, violations = self.check_crew_legal(crew, flight, current_time, delay_minutes)
            results.append({"crew": crew, "feasible": feasible, "violations": violations})
        return results

    def get_legal_fo_for_ca(
        self, ca: CrewMember, flight: Flight, current_time: datetime, delay_minutes: int
    ) -> list:
        """Returns feasible FO options matching the given CA's type rating."""
        results = []
        for crew in self.reserve_pool:
            if crew.role != "FO":
                continue
            if crew.type_rating != ca.type_rating:
                continue
            feasible, violations = self.check_crew_legal(crew, flight, current_time, delay_minutes)
            if feasible:
                results.append(crew)
        return results

    def commit(self, crew_id: str) -> None:
        self.committed.add(crew_id)

    def release(self, crew_id: str) -> None:
        self.committed.discard(crew_id)

    def reset(self) -> None:
        self.committed = set()

    def print_constraint_report(
        self, flight: Flight, current_time: datetime, delay_minutes: int
    ) -> None:
        print(f"\n  Constraint check for {flight.flight_id} "
              f"({flight.aircraft_type}, dep +{delay_minutes}min):")
        for crew in self.reserve_pool:
            feasible, violations = self.check_crew_legal(crew, flight, current_time, delay_minutes)
            status = "PASS" if feasible else "FAIL"
            print(f"    [{status}] {crew.crew_id} ({crew.role}, {crew.type_rating})", end="")
            if violations:
                print(f" → {violations[0]}")
            else:
                print()
