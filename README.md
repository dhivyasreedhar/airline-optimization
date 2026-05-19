# dCortex — Disruption Intelligence for Airline Operations

A simulation engine and dispatcher console for airline operational disruption handling. Models an airport ground stop as a node failure in a directed graph and propagates impact bidirectionally through the network in milliseconds.

---

## The Problem

A 30-minute ground stop at ORD is not a 30-minute problem. Queue recovery after the stop lifts runs at a net surplus of 10–20 ops/hour above baseline demand. An 18-minute shutdown is a 60–90 minute effective disruption window. Aircraft cascade 3–5 hops. Crew tip over FAA Part 117 duty limits. Passengers miss connections two airports away.

Dispatchers today spend 15–20 minutes manually correlating across systems to understand what is affected. In that window, options close. A reserve called at minute 2 is flight-ready by the time the flight needs to depart. The same call at minute 20 cannot reach the same windows.

**dCortex builds the complete picture before the first phone call.**

---

## Architecture

Two files. No external dependencies beyond Streamlit and Requests.

```
app.py            Streamlit dispatcher console (UI + Claude briefing layer)
simulation.py     Computation engine — four agents, two entry points
```

### Two modes

**`simulate(airport, hour, minute, cause)`** — pre-disruption. Runs P50/P75/P90 scenarios simultaneously before the FAA issues a ground stop. The OCC sees the full cost envelope in milliseconds, can pre-position reserves for the most likely outcome, and can practice disruptions on forecast data before they fire. This is the simulator: run scenarios before committing.

**`run(airport, hour, minute, duration, cause)`** — live decision. Single confirmed scenario, full plan generated, trace locked at dispatcher decision time. Called by `simulate()` for each severity tier; called directly in live mode once the EDCT duration is known.

### Four-agent architecture

```
simulate() or run()
        │
        ▼
[BFS Impact] — bidirectional from the disrupted node
        │   Departure-blocked / Arrival-blocked / Cascade
        ▼
[CoordinatingAgent] — manages shared resource pool, orchestrates:
        │
        ├── [CrewAgent]       Part 117 FDP limits (Table B, exact)
        │                     Reserve availability by type / role / station
        │
        ├── [AircraftAgent]   Type ratings, spare aircraft availability
        │                     Fleet cost by tier (widebody / narrowbody / regional)
        │
        └── [PassengerAgent]  Connection risk, delay-dependent misconnection cost
                              Different delays break different connections
        │
        ▼
Priority-ordered allocation — highest-pax flights first, shared pool tracked
        │   Resources committed to Flight A are unavailable for B, C, ...
        │   Full pool visibility before each option is scored
        ▼
[Trace] — immutable: every option, every constraint check, every prediction
          Dispatcher accepts or overrides; outcome recorded post-resolution
```

The CoordinatingAgent is what separates dCortex from sequential dispatcher allocation. Dispatchers allocate flight-by-flight. They can't see whether committing the best reserve to Flight A depletes the pool for Flights C through H in ways a different order would avoid. The coordinating agent processes all affected flights with full pool visibility before any commitment is made.

---

## Cost Model

Three aircraft tiers, per Eurocontrol Standard Inputs for Cost-Benefit Analyses:

| Tier | Types | Cost/min delay |
|------|-------|----------------|
| Widebody international | B777 | $180 |
| Narrowbody mainline | B737, B757, A320 | $74–95 |
| Regional jet | E175 | $40 |

Passenger misconnection cost is **delay-dependent**, not flat. Connections break above 45-min MCT. Cost scales linearly from zero at MCT to full at 2×MCT:

```python
fraction_broken = min(1.0, (delay_min - MCT) / MCT)
misconnect_cost = passengers × 0.30 × fraction_broken × $350
```

A B777-NRT with 280 pax at 90-min delay costs far more in misconnections than an E175-DTW with 68 pax. The cost function captures that difference per option.

---

## Simulated Network

7 airports (ORD hub + 6 spokes), 31 flights, 3 aircraft tiers, 19 aircraft, 50+ crew members.

Every airport is designed to produce at minimum:
- 1 departure (~14:00) → `DEPARTURE_BLOCKED` when that airport is disrupted
- 1 inbound arrival (~14:10–14:25) → `ARRIVAL_BLOCKED`
- 1 downstream flight using the delayed aircraft → `CASCADE`

**ORD disruption at 14:00 P75 (30 min)**: 13 flights affected, including widebody international (UA800 ORD→NRT, 280 pax, $180/min) and regional jets (UA900 ORD→DTW, 68 pax, $40/min), demonstrating the full three-tier cost spread.

---

## Constraints

**Hard** (violation = illegal — option is eliminated):
- FAA Part 117 Table B FDP limits by start-hour and segment count, implemented exactly
- Aircraft type rating
- Reserve callout minimum (90 min)
- Reserve location (must be at origin station)

**Soft** (violation = costly — implemented as cost penalty, option remains):
- CBA seniority order (deferred to v2)
- Maintenance routing (aircraft ends day at wrong station)

**What is deliberately not in scope for v1**: flight attendant constraints (Part 121), multi-day pairing recovery, gate optimization, individual passenger rebooking (requires GDS), EU 261 regulatory cost.

---

## Dispatcher Console (app.py)

Three-stage workflow:

**Stage 1 — Impact**: Per-flight rows showing impact type, delay, passenger count, recommended option, and violation count. Expander for all options and full constraint results.

**Stage 2 — Briefing**: Claude Haiku generates a structured dispatcher briefing (situation summary, recommended actions, risk flags, what we learned). Falls back to computed briefing if no API key. Dispatcher accepts or overrides; decision is hers.

**Stage 3 — Locked**: Trace immutably records the decision. Options generated, constraints checked, violations found. Outcome form captures actual cost vs predicted — the delta calibrates the model for the next similar event.

---

## Quick Start

```bash
pip install -r requirements.txt

# Optional: set API key for Claude briefing layer
export ANTHROPIC_API_KEY=sk-ant-...

streamlit run app.py
```

Without `ANTHROPIC_API_KEY`, the briefing stage uses a computed fallback — all engine functionality works normally.

---

## Files

| File | Purpose |
|------|---------|
| `simulation.py` | Computation engine: BFS impact, FAA Part 117 constraint checking, option generation, greedy allocation, trace builder |
| `app.py` | Streamlit dispatcher console: three-stage workflow, Claude API integration, outcome recording |
| `requirements.txt` | `streamlit>=1.30.0`, `requests>=2.28` |
| `PROBLEM_DEFINITION.md` | Full problem space, scoping decisions, v1 scope, deferred items, assumptions requiring airline validation |

---

## What This Is Not

A prototype, not a production system. It demonstrates the correct problem formulation, constraint architecture, and trace structure. Production additions:

- Real-time data ingestion (SWIM feed for weather/ATC, airline ops system for mechanical/crew)
- True joint optimization (integer programming across all flights simultaneously — v1 uses priority-ordered greedy with shared pool accounting)
- Individual passenger rebooking (requires GDS — Sabre, Amadeus)
- Multi-day pairing recovery (different problem structure — temporal, not geographic)
- Gate optimization (requires airport gate management integration)
- Stochastic duration modeling (P50/P75/P90 scenario planning already built in; Bayesian updating deferred)
- Trace corpus for prediction calibration (18+ months of live data → better rankings → dispatcher trust → adoption → more traces)
