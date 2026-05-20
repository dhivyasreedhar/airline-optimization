#  Airline Disruption Management

A reasoning layer for airline operations control centers. When a ground stop fires, dCortex runs a full cascade analysis, enumerates every legal crew allocation option, and recommends a priority-ordered plan that minimizes total cost across all affected flights simultaneously — not flight by flight.

---

## What It Does

An airport ground stop is a graph propagation problem. Aircraft, crew, and passengers stop flowing through a node. The impact fans out bidirectionally: outbound flights can't depart, inbound flights hold or divert, and the downstream chains compound across 3–5 hops.

The system models this in full:

- **Cascade analysis** — bidirectional BFS from the disrupted hub. Every affected airport and flight identified before the first phone call.
- **Pool-aware allocation** — ranks flights by marginal cost saved (uncovered vs covered), then commits crew and gate resources with full pool visibility before each decision. A reserve committed to Flight A is unavailable for B through H before those options are scored.
- **Greedy allocation** — processes flights in departure order, the way dispatchers typically work today. Shown as a baseline for comparison.
- **Cost delta** — the dollar difference between strategies, flight by flight.
- **Dispatcher review** — accept, override, or escalate each recommendation. Decisions are written to the trace.
- **Outcome recording** — actual cost vs predicted, per flight. The delta calibrates the model over time.

---

## Architecture

```

├── app.py                     # Streamlit UI — five tabs
├── main.py                    # CLI entry point for headless runs
├── models.py                  # Dataclasses: Flight, CrewMember, DisruptionEvent, AllocationOption
│
├── graph/
│   └── cascade.py             # build_flight_graph, bfs_cascade (bidirectional), get_affected_flights
│
├── agents/
│   ├── crew_agent.py          # FAA Part 117 FDP, type ratings, callout windows, committed pool
│   ├── aircraft_agent.py      # Cost-per-minute by fleet tier, ferry cost, gate compatibility
│   ├── passenger_agent.py     # Connection break rates by delay bucket and route type
│   └── coordinating_agent.py  # Orchestrates agents, pool-aware and greedy allocation, LLM narration
│
├── data/
│   └── synthesize.py          # generate_flights, generate_crew_roster, generate_triage_scenario
│
├── trace/
│   └── emitter.py             # emit_trace, update_trace_decisions, update_trace_outcome
│
└── traces/                    # JSON reasoning traces written on every run
```

---

## Setup

**Requirements:** Python 3.10+, an Anthropic API key (for LLM narration only — the simulation runs without it).

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Running

**Streamlit app (recommended):**

```bash
streamlit run app.py
```

**Headless CLI:**

```bash
python main.py
```

The CLI runs the triage scenario end-to-end, prints constraint reports and cost breakdowns to stdout, and writes a trace JSON to `traces/`.

---

## The UI

Five tabs:

| Tab | What it shows |
|-----|--------------|
| **Setup** | Editable flight and crew tables. Add, remove, or modify rows. Changes take effect on the next run. |
| **Cascade** | Interactive network graph. BFS depth colored by severity. Click a node to filter affected flights. |
| **Allocation** | Pool-aware vs greedy cost comparison. Strategy disagreement table. Part 117 FDP status for all reserves. Dispatcher review panel. |
| **Trace** | Full JSON reasoning trace. Download button. |
| **Outcomes** | Post-disruption actual cost entry, per-flight outcome recording. |

**Sidebar controls:**

- **Scenario preset** — load a pre-built fleet and crew configuration
- **Hub / Start hour / Duration** — disruption event parameters
- **Severity** — P50 (45-min delay), P75 (75-min), P90 (90-min), or Custom
- **Cost model** — $/min sliders per aircraft tier, reaccommodation cost per passenger
- **Gate availability** — widebody, narrowbody, regional gate counts at the hub
- **Run Simulation** — executes the full pipeline
- **Generate LLM Narration** — calls Claude to produce a dispatcher-readable recommendation summary

---

## Scenario Presets

| Preset | Hub | Default Severity | Description |
|--------|-----|-----------------|-------------|
| **ORD — Triage Demo** | ORD | P90 | 10 flights, 6 crew pairs, 3 widebody gates. 4 widebody-type flights compete for 3 gates. Pool-aware covers the highest-value flight (ORD→NRT, 420 pax, $175/min); greedy commits the last gate to an earlier cheaper departure. ~$31K delta. |
| **ORD — Standard** | ORD | P75 | 22-flight balanced day. Deep reserve pool. |
| **ORD — Reserve Shortage** | ORD | P75 | Thin reserves (2 WB pairs). Pool-aware advantage most visible. |
| **ORD — High Traffic** | ORD | P90 | 29 flights, 45-min ground stop, P90 worst-case. |
| **LAX — Afternoon Peak** | LAX | P75 | International-heavy fleet, afternoon timing. |

---

## Cost Model

| Fleet tier | $/min delay | Source range |
|------------|------------|-------------|
| Widebody (international) | $175 | $150–200+/min |
| Widebody domestic | $135 | $120–150/min |
| Narrowbody | $87 | $74–100/min |
| Regional | $40 | $30–50/min |

Additional cost components per uncovered flight:
- **Passenger reaccommodation** — $1,000/pax × connecting passengers × connection break rate (interpolated by delay bucket and route type)
- **Crew activation** — $800/crew member (CA + FO = $1,600/flight covered)
- **Ferry/repositioning** — inbound flights carry a 30% diversion probability; expected ferry cost included in uncovered option
- **DOT tarmac fine** — $27,500/departure if uncovered effective delay exceeds 3h domestic / 4h international

Uncovered flights face a 3× delay multiplier (the `UNCOVERED_DELAY_MULTIPLIER`) — an unmanaged 90-minute disruption cascades into ~270 minutes of effective delay for passengers and downstream operations.

---

## Constraints

**Hard constraints** — violation means the option is discarded entirely:

| Constraint | Rule |
|-----------|------|
| FAA Part 117 FDP | Remaining duty hours checked against Table B by duty-start hour; includes waiting time from decision point to arrival, not just block time |
| Type rating | Crew certified only for their rated aircraft type (B737/A320 → narrowbody; B777/B787 → widebody; E175/E170 → regional) |
| Crew role | CA and FO both required |
| Domicile | Reserve must be at flight's origin station |
| Callout window | 30 min for on-premises (available), 60 min for short-call, 120 min for long-call |
| Gate compatibility | Widebody aircraft require widebody gates; regional gates serve only regional |
| Committed pool | Reserve already assigned to another flight is unavailable |

**Soft constraints** — violation adds a penalty to option cost, never discards:

| Constraint | Penalty |
|-----------|---------|
| CBA seniority order | $5 × seniority rank per crew member (junior reserves activated first per contract) |

---

## Reasoning Traces

Every run writes a JSON file to `traces/`. The trace is immutable at decision time and contains:

- Full disruption event parameters
- Cascade graph (nodes, edges, depths)
- All affected flight IDs
- Every pool-aware and greedy option generated, with full cost breakdown
- Hard constraints checked and violated per option
- The selected option and predicted total cost
- Dispatcher decisions (written when the dispatcher submits the review panel)
- `actual_cost` and `outcome` — filled in via the Outcomes tab post-disruption

```json
{
  "trace_id": "trace_20260520_094950",
  "disruption_event": { "hub": "ORD", "scenario": "P90", ... },
  "allocation_comparison": {
    "pool_aware_total_cost": 642978,
    "greedy_total_cost": 673753,
    "delta": 30775
  },
  "options_generated": { "pool_aware": [...], "greedy": [...] },
  "dispatcher_decisions": {},
  "actual_cost": null,
  "outcome": null
}
```

---

## What v1 Does Not Cover

- Multi-disruption joint optimization (two ground stops competing for the same reserve pool)
- Crew pairing recovery across multiple days
- Flight attendant constraints
- Individual passenger rebooking (requires GDS access)
- Per-fare-class passenger value
- Pre-simulation mode (running P50/P75/P90 before a disruption fires for pre-positioning)
- EU 261/2004 compliance costs

---

## Key Files

| File | Purpose |
|------|---------|
| `models.py` | All dataclasses. Start here. |
| `agents/crew_agent.py` | Part 117 Table B implementation, constraint checking logic |
| `agents/coordinating_agent.py` | Pool-aware vs greedy algorithms, marginal cost ranking, LLM narration prompt |
| `graph/cascade.py` | BFS cascade from disrupted node |
| `data/synthesize.py` | Synthetic flight and crew generation; `generate_triage_scenario()` for the constructed demo |
| `trace/emitter.py` | Trace write and patch functions |
