import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
from datetime import datetime, timedelta
from models import Flight, CrewMember

# Route pools keyed by aircraft type — "HUB" substituted at generation time
_ROUTE_POOLS = {
    "regional": [
        # Hub-spoke (depth 1)
        ("HUB", "DTW"), ("HUB", "STL"), ("HUB", "MKE"), ("HUB", "CLE"),
        ("HUB", "CMI"), ("HUB", "MSN"), ("HUB", "BMI"), ("HUB", "MDW"),
        ("GRR", "HUB"), ("CMI", "HUB"), ("MDW", "HUB"), ("MKE", "HUB"),
        # Spoke-to-spoke depth-2 feeders (depth-1 spoke → new airport)
        # ABQ/BNA/PDX/SAN are only reachable via depth-1 spokes → cascade depth 2
        ("DTW", "TOL"),   # Toledo — depth 2 via HUB→DTW→TOL
        ("STL", "SGF"),   # Springfield MO — depth 2 via HUB→STL→SGF
        ("CLE", "YNG"),   # Youngstown — depth 2 via HUB→CLE→YNG
        # Depth-3 feeders (depth-2 airport → new airport)
        ("TOL", "FWA"),   # Fort Wayne — depth 3 via HUB→DTW→TOL→FWA
        ("SGF", "TUL"),   # Tulsa — depth 3 via HUB→STL→SGF→TUL
    ],
    "narrowbody": [
        # Hub-spoke (depth 1)
        ("HUB", "DEN"), ("HUB", "ATL"), ("HUB", "BOS"), ("HUB", "JFK"),
        ("HUB", "SFO"), ("HUB", "MSP"), ("HUB", "IAH"), ("HUB", "PHX"),
        ("HUB", "SEA"), ("HUB", "LAS"), ("HUB", "MIA"), ("HUB", "DCA"),
        ("LAX", "HUB"), ("BOS", "HUB"), ("ATL", "HUB"), ("JFK", "HUB"),
        ("DEN", "HUB"), ("MIA", "HUB"),
        # Spoke-to-spoke (depth 2) — connects depth-1 airports to new airports
        ("DEN", "ABQ"),   # Albuquerque — depth 2 via HUB→DEN→ABQ
        ("ATL", "BNA"),   # Nashville — depth 2 via HUB→ATL→BNA
        ("SEA", "PDX"),   # Portland — depth 2 via HUB→SEA→PDX
        ("LAX", "SAN"),   # San Diego — depth 2 via HUB→LAX→SAN (or LAX→HUB upstream)
        ("SFO", "SMF"),   # Sacramento — depth 2 via HUB→SFO→SMF
        # Depth-3 spokes
        ("ABQ", "ELP"),   # El Paso — depth 3 via HUB→DEN→ABQ→ELP
        ("BNA", "HSV"),   # Huntsville — depth 3 via HUB→ATL→BNA→HSV
        ("PDX", "EUG"),   # Eugene — depth 3 via HUB→SEA→PDX→EUG
    ],
    "widebody_domestic": [
        ("HUB", "LAX"), ("HUB", "SEA"), ("HUB", "HNL"),
        ("SEA", "HUB"), ("LAX", "HUB"),
    ],
    "widebody": [
        ("HUB", "NRT"), ("HUB", "LHR"), ("HUB", "GRU"),
        ("HUB", "CDG"), ("HUB", "FRA"),
        ("LHR", "HUB"), ("NRT", "HUB"),
    ],
}

# Block hours by type (min, max)
_BLOCK_HOURS = {
    "regional":          (1.0,  2.0),
    "narrowbody":        (2.0,  5.5),
    "widebody_domestic": (4.5,  6.5),
    "widebody":          (8.5, 14.0),
}

# Passenger counts by type (min, max)
_PAX_RANGE = {
    "regional":          (50,  76),
    "narrowbody":        (140, 180),
    "widebody_domestic": (200, 250),
    "widebody":          (280, 350),
}

# Aircraft type ratings used in tail numbers / labels
_TAIL_PREFIX = {
    "regional": "N75", "narrowbody": "N27",
    "widebody_domestic": "N67", "widebody": "N77",
}


def generate_flights(
    hub: str = "ORD",
    num_regional: int = 6,
    num_narrowbody: int = 9,
    num_widebody_domestic: int = 3,
    num_widebody_intl: int = 4,
    window_start_hour: int = 7,
    window_hours: int = 6,
    seed: int = 42,
) -> list:
    rng = random.Random(seed)
    base = datetime(2025, 6, 15, window_start_hour, 0, 0)
    window_end = base + timedelta(hours=window_hours)

    counts = {
        "regional":          num_regional,
        "narrowbody":        num_narrowbody,
        "widebody_domestic": num_widebody_domestic,
        "widebody":          num_widebody_intl,
    }

    flights = []
    flight_num = 100

    for atype, count in counts.items():
        pool = _ROUTE_POOLS[atype][:]
        rng.shuffle(pool)
        block_min, block_max = _BLOCK_HOURS[atype]
        pax_min, pax_max = _PAX_RANGE[atype]
        tail_prefix = _TAIL_PREFIX[atype]
        gate = "widebody" if "widebody" in atype else atype

        for i in range(count):
            orig, dest = pool[i % len(pool)]
            orig = orig.replace("HUB", hub)
            dest = dest.replace("HUB", hub)

            dep_offset = timedelta(minutes=rng.randint(0, window_hours * 60 - 30))
            dep = base + dep_offset
            if dep >= window_end:
                dep = window_end - timedelta(minutes=30)

            block_h = rng.uniform(block_min, block_max)
            arr = dep + timedelta(hours=block_h)

            pax = rng.randint(pax_min, pax_max)
            fid = f"UA{flight_num}"
            tail = f"{tail_prefix}{flight_num}"
            flight_num += 1

            flights.append(Flight(
                flight_id=fid,
                origin=orig,
                destination=dest,
                aircraft_type=atype,
                scheduled_departure=dep,
                scheduled_arrival=arr,
                passenger_count=pax,
                tail_number=tail,
                crew_required={"CA": f"ACT_CA_{fid}", "FO": f"ACT_FO_{fid}"},
                gate_type=gate,
            ))

    # Sort by departure so the schedule reads naturally
    flights.sort(key=lambda f: f.scheduled_departure)
    return flights


def generate_crew_roster(
    b737_reserves: int = 6,
    b777_reserves: int = 3,
    b787_reserves: int = 3,
    e175_reserves: int = 3,
    near_limit_count: int = 4,
    hub: str = "ORD",
    seed: int = 42,
) -> tuple:
    rng = random.Random(seed)
    base_date = datetime(2025, 6, 15)

    def _dt(h, m=0):
        return base_date.replace(hour=h, minute=m)

    # Active crew (not configurable in detail — just background population)
    active_crew = []
    ratings_cycle = ["B737", "B737", "B737", "B777", "B787", "E175"]
    for i in range(20):
        rating = ratings_cycle[i % len(ratings_cycle)]
        duty_start = _dt(rng.randint(5, 8), rng.randint(0, 59))
        for role in ("CA", "FO"):
            active_crew.append(CrewMember(
                crew_id=f"ACT_{role}_{i:03d}", role=role,
                type_rating=rating, domicile=hub, current_station=hub,
                duty_start=duty_start, segments_flown=rng.randint(0, 3),
                is_reserve=False, callout_status="unavailable",
            ))

    reserve_crew = []
    pool_spec = [
        ("B737", b737_reserves),
        ("B777", b777_reserves),
        ("B787", b787_reserves),
        ("E175", e175_reserves),
    ]

    total_reserves = sum(n for _, n in pool_spec)
    # Distribute near-limit slots across types, proportionally
    near_limit_remaining = min(near_limit_count, total_reserves)

    for rating, count in pool_spec:
        roles = (["CA", "FO"] * ((count // 2) + 1))[:count]
        # How many of this type are near-limit
        type_near = round(near_limit_remaining * count / max(total_reserves, 1))
        type_near = min(type_near, count)
        near_limit_remaining -= type_near

        for i, role in enumerate(roles):
            is_near = i >= (count - type_near)
            if is_near:
                # Duty started 1–3 AM → near/over FDP limit at 10 AM
                h = rng.randint(0, 3)
                m = rng.randint(0, 59)
                segs = rng.randint(2, 4)
            else:
                # Duty started 5–8 AM → plenty of FDP remaining
                h = rng.randint(5, 8)
                m = rng.randint(0, 30)
                segs = rng.randint(0, 2)

            # Seniority rank: position within the full reserve list.
            # Lower index → more junior (activate first per CBA contract).
            seniority_rank = len(reserve_crew) * 5   # 0, 5, 10, …
            reserve_crew.append(CrewMember(
                crew_id=f"RSV_{rating}_{role}_{i:02d}",
                role=role, type_rating=rating,
                domicile=hub, current_station=hub,
                duty_start=_dt(h, m),
                segments_flown=segs,
                is_reserve=True, callout_status="available",
                seniority_rank=seniority_rank,
            ))

    return active_crew, reserve_crew


def generate_triage_scenario(hub: str = "ORD") -> tuple:
    """
    Constructed 10-flight, 12-reserve (6-pair) scenario engineered to showcase pool-aware
    allocation advantage. Design invariants:
      - Cheap regional/NB flights depart 09:00–09:50 (before the 10:00 AM ground stop)
      - Expensive WB-dom and WB-intl flights depart 10:00–10:30 (at/after the ground stop)
      - Exactly 3 WB-gate slots → pool-aware secures them for the 3 priciest WB flights
      - Greedy fills WB gates in departure order, wasting the last gate on WB-dom (SFO)
        and leaving the highest-value ORD→NRT widebody gate-blocked and uncovered
    Expected outcome at P90 (90-min delay, 270-min effective):
      - Both strategies cover 6/10 flights
      - Pool-aware saves ~$31K: WB delta ~$33K (UA109 vs UA106 uncovered cost)
        offset by ~$2K from covering higher-pax NB flights (UA104/UA105) instead of UA102/UA103
      - DOT fines cancel between strategies at P90 (both WB and domestic flights exceed
        their respective 240-min / 180-min tarmac thresholds at 270-min effective delay)
    Run at P75 (225-min effective) the DOT asymmetry reduces the delta to ~$3K because
    the international threshold (240 min) is not crossed, so use P90 as the demo default.
    """
    base = datetime(2025, 6, 15)

    def dt(h, m=0):
        return base.replace(hour=h, minute=m)

    flights = [
        # Regional — depart first, lowest aircraft cost ($40/min)
        Flight("UA100", hub, "MSP", "regional",          dt(9,  0), dt(10, 30),  80, "NE100", {}, "regional"),
        Flight("UA101", hub, "MKE", "regional",          dt(9, 15), dt(10, 30),  78, "NE101", {}, "regional"),
        # Narrowbody — depart mid-morning, medium cost ($87/min)
        Flight("UA102", hub, "DEN", "narrowbody",        dt(9, 20), dt(11, 45), 148, "NB102", {}, "narrowbody"),
        Flight("UA103", hub, "BOS", "narrowbody",        dt(9, 30), dt(12, 30), 152, "NB103", {}, "narrowbody"),
        Flight("UA104", hub, "DFW", "narrowbody",        dt(9, 40), dt(11, 30), 158, "NB104", {}, "narrowbody"),
        Flight("UA105", hub, "LAX", "narrowbody",        dt(9, 50), dt(12, 30), 163, "NB105", {}, "narrowbody"),
        # Widebody domestic — around ground stop start, high cost ($135/min)
        Flight("UA106", hub, "SFO", "widebody_domestic", dt(10,  0), dt(13,  0), 265, "WD106", {}, "widebody"),
        Flight("UA107", hub, "SEA", "widebody_domestic", dt(10, 10), dt(12, 45), 272, "WD107", {}, "widebody"),
        # Widebody international — depart last, highest cost ($175/min)
        # Block times are 8h (simulation values; FDP limits require short-haul proxy).
        Flight("UA108", hub, "LHR", "widebody",          dt(10, 20), dt(18, 20), 355, "WI108", {}, "widebody"),
        Flight("UA109", hub, "NRT", "widebody",          dt(10, 30), dt(18, 30), 420, "WI109", {}, "widebody"),
    ]

    base_dt = datetime(2025, 6, 15)

    def cm(crew_id, role, rating, h=6, m=0):
        return CrewMember(
            crew_id=crew_id, role=role, type_rating=rating,
            domicile=hub, current_station=hub,
            duty_start=base_dt.replace(hour=h, minute=m),
            segments_flown=0, is_reserve=True, callout_status="available",
        )

    reserve_crew = [
        # B737 — 2 pairs (for 4 narrowbody flights; greedy takes first 2, pool-aware takes best 2)
        cm("RSV_B737_CA_00", "CA", "B737", 6,  0),
        cm("RSV_B737_FO_01", "FO", "B737", 6,  0),
        cm("RSV_B737_CA_02", "CA", "B737", 6, 30),
        cm("RSV_B737_FO_03", "FO", "B737", 6, 30),
        # B777 — 1 pair (for widebody / WB-dom flights; duty 8 AM → 10h FDP at gs_start)
        cm("RSV_B777_CA_00", "CA", "B777", 8,  0),
        cm("RSV_B777_FO_01", "FO", "B777", 8,  0),
        # B787 — 2 pairs (for widebody / WB-dom flights — most valuable)
        cm("RSV_B787_CA_00", "CA", "B787", 8,  0),
        cm("RSV_B787_FO_01", "FO", "B787", 8,  0),
        cm("RSV_B787_CA_02", "CA", "B787", 8, 30),
        cm("RSV_B787_FO_03", "FO", "B787", 8, 30),
        # E175 — 1 pair (for 2 regional flights; greedy takes UA100, pool-aware takes UA101)
        cm("RSV_E175_CA_00", "CA", "E175", 6,  0),
        cm("RSV_E175_FO_01", "FO", "E175", 6,  0),
    ]

    return flights, reserve_crew


def print_roster_summary(active_crew: list, reserve_crew: list, flights: list) -> None:
    by_type = {}
    for f in flights:
        by_type[f.aircraft_type] = by_type.get(f.aircraft_type, 0) + 1
    total_pax = sum(f.passenger_count for f in flights)

    print("=" * 60)
    print("SYNTHETIC DATA SUMMARY")
    print("=" * 60)
    print(f"Total flights: {len(flights)}")
    for atype, count in sorted(by_type.items()):
        print(f"  {atype:22s}: {count}")
    print(f"Total passengers: {total_pax:,}")
    print(f"\nActive crew on duty: {len(active_crew)}")
    print(f"Reserve crew: {len(reserve_crew)}")
    by_rating = {}
    for c in reserve_crew:
        key = f"{c.type_rating} {c.role}"
        by_rating[key] = by_rating.get(key, 0) + 1
    for key, count in sorted(by_rating.items()):
        print(f"  {key:15s}: {count}")
    print("=" * 60)
