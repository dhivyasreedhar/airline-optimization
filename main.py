import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import json
from datetime import datetime, timedelta
import anthropic

from data.synthesize import generate_flights, generate_crew_roster, print_roster_summary
from graph.cascade import (
    build_flight_graph, bfs_cascade, get_affected_flights,
    print_cascade_report, draw_cascade_graph,
)
from agents.crew_agent import CrewAgent, remaining_fdp, max_fdp
from agents.aircraft_agent import AircraftAgent
from agents.passenger_agent import PassengerAgent
from agents.coordinating_agent import CoordinatingAgent
from models import DisruptionEvent
from trace.emitter import emit_trace


DISRUPTION_START    = datetime(2025, 6, 15, 10, 0, 0)
DISRUPTION_DURATION = 30   # actual FAA ground stop minutes
EFFECTIVE_WINDOW    = 90   # after queue math


# ── Section 1: Synthetic data ──────────────────────────────────────────────
print("\n" + "=" * 65)
print("SECTION 1: SYNTHETIC DATA")
print("=" * 65)

flights = generate_flights()
active_crew, reserve_crew = generate_crew_roster()
print_roster_summary(active_crew, reserve_crew, flights)


# ── Section 2: Flight graph + BFS cascade ─────────────────────────────────
print("\n" + "=" * 65)
print("SECTION 2: FLIGHT GRAPH + CASCADE ANALYSIS")
print("=" * 65)

disruption = DisruptionEvent(
    event_id="EVT_ORD_20250615_1000",
    hub="ORD",
    start_time=DISRUPTION_START,
    duration_minutes=DISRUPTION_DURATION,
    effective_window_minutes=EFFECTIVE_WINDOW,
    scenario="P75",
)

G                = build_flight_graph(flights)
affected_nodes   = bfs_cascade(G, disrupted_node="ORD", max_depth=5)
affected_flights = get_affected_flights(G, affected_nodes, flights)

# Drop flights whose delayed departure still falls before the disruption start.
# These have already pushed back and cannot be re-crewed.
DELAY_P75 = 75
affected_flights = [
    f for f in affected_flights
    if f.scheduled_departure + timedelta(minutes=DELAY_P75) > DISRUPTION_START
]

print(f"\n30-min FAA ground stop → {EFFECTIVE_WINDOW}-min effective window (queue math).")
print(f"Airports in cascade: {len(affected_nodes)}")
print(f"Flights affected   : {len(affected_flights)}")

print_cascade_report(affected_nodes, affected_flights, DISRUPTION_DURATION)
draw_cascade_graph(G, affected_nodes, "ORD", output_path="cascade_graph.png")


# ── Section 3: Agents + Part 117 FDP status ───────────────────────────────
print("\n" + "=" * 65)
print("SECTION 3: AGENTS — PART 117 FDP STATUS")
print("=" * 65)

client          = anthropic.Anthropic()
crew_agent      = CrewAgent(reserve_pool=reserve_crew)
aircraft_agent  = AircraftAgent()
passenger_agent = PassengerAgent()
coordinator     = CoordinatingAgent(crew_agent, aircraft_agent, passenger_agent, client, hub="ORD")

current_time = DISRUPTION_START

print(f"\n{'Crew ID':<22} {'Role':<5} {'Rating':<8} {'Duty Start':<12} "
      f"{'Max FDP':>8} {'Elapsed':>8} {'Remaining':>10}  Status")
print("-" * 90)
for c in reserve_crew:
    elapsed = (current_time - c.duty_start).total_seconds() / 3600
    max_h   = max_fdp(c.duty_start.hour)
    rem     = remaining_fdp(c, current_time)
    status  = "*** NEAR/OVER LIMIT ***" if rem < 3.0 else "OK"
    print(f"{c.crew_id:<22} {c.role:<5} {c.type_rating:<8} "
          f"{c.duty_start.strftime('%H:%M'):<12} "
          f"{max_h:>8.1f}h {elapsed:>8.1f}h {rem:>10.2f}h  {status}")


# ── Section 4: Constraint checking + P75 allocation ───────────────────────
print("\n" + "=" * 65)
print("SECTION 4: PART 117 CONSTRAINT CHECKING + P75 ALLOCATION")
print("=" * 65)

# Show explicit constraint report for a narrowbody — reveals FDP rejections
sample_nb = next(
    (f for f in affected_flights if f.aircraft_type == "narrowbody"), None
)
if sample_nb:
    print(f"\nPart 117 check: {sample_nb.flight_id} {sample_nb.origin}→{sample_nb.destination} (narrowbody, P75 scenario)")
    crew_agent.reset()
    crew_agent.print_constraint_report(sample_nb, current_time, DELAY_P75)

# Show widebody check — reveals type-rating + FDP rejections
sample_wb = next(
    (f for f in affected_flights if f.aircraft_type == "widebody"), None
)
if sample_wb:
    print(f"\nPart 117 check: {sample_wb.flight_id} {sample_wb.origin}→{sample_wb.destination} (widebody, P75 scenario)")
    crew_agent.print_constraint_report(sample_wb, current_time, DELAY_P75)

# Run both strategies
pool_aware_p75 = coordinator.allocate_pool_aware(affected_flights, current_time, DELAY_P75)
greedy_p75     = coordinator.allocate_greedy(affected_flights, current_time, DELAY_P75)

pool_by_fid   = {o.flight_id: o for o in pool_aware_p75}
greedy_by_fid = {o.flight_id: o for o in greedy_p75}

print("\nFlights where pool-aware and greedy disagree:")
print(f"{'Flight':<10} {'Type':<20} {'Pool-Aware':>22} {'Greedy':>22}")
print("-" * 80)
for fid in sorted(set(pool_by_fid) | set(greedy_by_fid)):
    po = pool_by_fid[fid]
    gr = greedy_by_fid[fid]
    if po.feasible == gr.feasible:
        continue
    f = next(x for x in affected_flights if x.flight_id == fid)
    po_str = f"COVERED  ${po.total_cost:>10,.0f}" if po.feasible else f"UNCOVERED ${po.total_cost:>9,.0f}"
    gr_str = f"COVERED  ${gr.total_cost:>10,.0f}" if gr.feasible else f"UNCOVERED ${gr.total_cost:>9,.0f}"
    print(f"{fid:<10} {f.aircraft_type:<20} {po_str:>22} {gr_str:>22}")

print("\nFull allocation table:")
print(f"{'Flight':<10} {'Type':<20} {'Pool Cost':>14} {'Greedy Cost':>14} {'Pool':>10} {'Greedy':>10}")
print("-" * 85)
for fid in sorted(set(pool_by_fid) | set(greedy_by_fid)):
    po = pool_by_fid[fid]
    gr = greedy_by_fid[fid]
    f  = next(x for x in affected_flights if x.flight_id == fid)
    print(f"{fid:<10} {f.aircraft_type:<20} ${po.total_cost:>12,.0f} ${gr.total_cost:>12,.0f} "
          f"{'COVERED' if po.feasible else 'UNCOVERED':>10} "
          f"{'COVERED' if gr.feasible else 'UNCOVERED':>10}")

pa_total = sum(o.total_cost for o in pool_aware_p75)
gr_total = sum(o.total_cost for o in greedy_p75)
delta    = gr_total - pa_total
print("-" * 85)
print(f"{'TOTAL':<10} {'':<20} ${pa_total:>12,.0f} ${gr_total:>12,.0f}")
print(f"\nPool-aware saves ${abs(delta):,.0f} vs greedy on P75.")
print("Mechanism: greedy commits B737 reserves to UA212 (early dep, lower pax) "
      "before UA205 (high pax), leaving the more expensive flight uncovered.")


# ── Section 5: LLM narration ───────────────────────────────────────────────
print("\n" + "=" * 65)
print("SECTION 5: DISPATCHER RECOMMENDATION (Claude narration)")
print("=" * 65)

narration = coordinator.narrate(pool_aware_p75, greedy_p75, disruption)
print("\n" + narration)


# ── Section 6: Emit trace ──────────────────────────────────────────────────
print("\n" + "=" * 65)
print("SECTION 6: REASONING TRACE")
print("=" * 65)

cascade_graph_data = {
    "nodes": list(G.nodes()),
    "edges": [
        {"from": u, "to": v,
         "flight_id": d.get("flight_id"),
         "aircraft_type": d.get("aircraft_type")}
        for u, v, d in G.edges(data=True)
    ],
}

trace, filepath = emit_trace(
    disruption=disruption,
    cascade_graph_data=cascade_graph_data,
    affected_flights=affected_flights,
    pool_aware_options=pool_aware_p75,
    greedy_options=greedy_p75,
    selected_options=[o for o in pool_aware_p75 if o.feasible],
    llm_narration=narration,
    traces_dir="traces",
)

print(f"\nTrace saved → {filepath}")
print(f"\nallocation_comparison:")
print(json.dumps(trace["allocation_comparison"], indent=2))
print(f"\nactual_cost: {trace['actual_cost']}  ← null until outcome recorded")


# ── Section 7: P50 / P75 / P90 cost envelope ──────────────────────────────
print("\n" + "=" * 65)
print("SECTION 7: P50 / P75 / P90 SCENARIO ENVELOPE")
print("=" * 65)
print(f"\n{'Scenario':<8} {'Delay':>6} {'Pool-Aware':>14} {'Greedy':>12} {'Delta':>16} Uncovered")
print("-" * 70)

scenario_results = {}

for label, delay_min, description in [
    ("P50", 45,  "Best case — queue clears quickly"),
    ("P75", 75,  "Expected — moderate queue buildup"),
    ("P90", 90,  "Worst case — extended recovery"),
]:
    evt = DisruptionEvent(
        event_id=f"EVT_ORD_20250615_{label}",
        hub="ORD",
        start_time=DISRUPTION_START,
        duration_minutes=DISRUPTION_DURATION,
        effective_window_minutes=delay_min * 2,
        scenario=label,
    )
    pa = coordinator.allocate_pool_aware(affected_flights, current_time, delay_min)
    gr = coordinator.allocate_greedy(affected_flights, current_time, delay_min)

    pa_cost   = sum(o.total_cost for o in pa)
    gr_cost   = sum(o.total_cost for o in gr)
    scenario_delta = gr_cost - pa_cost
    uncovered = len([o for o in pa if not o.feasible])

    scenario_results[label] = dict(
        delay=delay_min, pa_cost=pa_cost, gr_cost=gr_cost,
        delta=scenario_delta, uncovered=uncovered,
        pool_aware=pa, greedy=gr, event=evt,
    )

    print(f"{label:<8} {delay_min:>5}m {pa_cost:>13,.0f} {gr_cost:>12,.0f} "
          f"{scenario_delta:>+15,.0f}  {uncovered}/{len(pa)}")
    print(f"         {description}")

p50_cost = scenario_results["P50"]["pa_cost"]
p90_cost = scenario_results["P90"]["pa_cost"]
print("-" * 70)
print(f"\nCost envelope: ${p50_cost:,.0f} (P50 best case) → ${p90_cost:,.0f} (P90 worst case)")
print(f"Exposure range: ${p90_cost - p50_cost:,.0f}")

# Emit traces for all three scenarios
print("\nEmitting traces for all scenarios...")
for label, res in scenario_results.items():
    scenario_narration = (
        f"[{label} scenario, delay={res['delay']}min, "
        f"pool-aware ${res['pa_cost']:,.0f} vs greedy ${res['gr_cost']:,.0f}] "
        f"See P75 narration for full dispatcher recommendation."
    )
    _, fp = emit_trace(
        disruption=res["event"],
        cascade_graph_data=cascade_graph_data,
        affected_flights=affected_flights,
        pool_aware_options=res["pool_aware"],
        greedy_options=res["greedy"],
        selected_options=[o for o in res["pool_aware"] if o.feasible],
        llm_narration=scenario_narration,
        traces_dir="traces",
    )
    print(f"  {label}: {fp}")

print("\nAll traces written. Corpus initialized.")
