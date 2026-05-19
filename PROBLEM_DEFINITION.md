# dCortex: Problem Definition

---

## The Model

An airline network is a directed graph. Airports are nodes. Flights are edges. Aircraft and crew are resources flowing through edges on a time schedule. Passengers are demand riding the flow.

When a node fails, resources stop flowing through it — in both directions. Flights that were supposed to depart from the failed node can't leave. Flights inbound to the failed node can't land and must hold or divert. Both create downstream chains of broken edges.

**This is a graph propagation problem.** The disruption is a node failure. The impact is a BFS traversal from that node. The solution is a resource reallocation that minimizes total cost across the full graph — not flight by flight, but simultaneously.

---

## What Does an Airport Shutdown Mean?

A lightning strike at ORD. Ramp crews clear the ramp. FAA issues a ground stop. No arrivals. No departures. A node that processes 90–100 operations per hour drops to zero.

**The queue effect.** When the ground stop lifts, ORD recovers at 50–60 ops/hour — reduced separation, aircraft sequencing out of holds, ramp re-staffing. 18 minutes of shutdown accumulates ~27 operations of backlog. Clearing at a net surplus of 10–20 ops/hour above baseline demand takes **60 to 90 minutes**.

An 18-minute ground stop is a 60–90 minute disruption window. Any system that models only the literal shutdown duration is solving the wrong problem.

---

## Who Gets Impacted?

**Aircraft.** Two failure modes. Downstream: an aircraft that should have landed turns late, its next departure cascades forward through every leg it operates for the rest of the day. Upstream: an aircraft inbound to ORD is held or diverted — it lands late, it may be at the wrong airport, and the flights it was supposed to operate from ORD are now missing their equipment. Cascade depth: 3–5 hops before dissipating (Bratu & Barnhart, *Journal of Scheduling*, 2006).

**Crew.** FAA 14 CFR Part 117 sets hard Flight Duty Period limits by start-hour and segment count. A minor delay tips crew over legal limits when combined with accumulated duty hours. Once over the limit, they can't fly — the flight can't operate on its scheduled crew. Replacement comes from the reserve pool, which is finite and shared across every concurrent disruption. Every reserve committed to one flight is unavailable for the next.

**Passengers.** Passengers are not on flights — they are on journeys. A 40-minute delay breaks a connecting itinerary two hops away in the graph. Standard metrics count flight delays. The real measure is journey failures — passengers who never appear in the disruption incident report of the original ground stop.

---

## What Does It Cost?

- **$74–100 per minute of delay per aircraft** in direct operating cost — fuel, crew time, gate fees, ownership (Eurocontrol Standard Inputs)
- A 60-minute effective disruption across 10 aircraft: **$44,000–$60,000** before passenger reaccommodation
- Missed connection reaccommodation: **$800–$1,200 per passenger** (hotel, meals, rebooking) — this cost is delay-dependent: a 20-minute delay breaks different connections than a 90-minute delay, so the passenger impact must be computed per option, not per flight
- Reserve activation: **$600–$1,000 per crew member** activated
- US airlines: **$8–10 billion in direct delay costs annually** (Airlines for America)

A moderate hub disruption — 30 minutes at ORD, 8–12 aircraft — reaches $200,000–$400,000 in total cost when the full cascade is counted.

The $74–100 figure is a fleet average. The joint allocation problem cannot be solved optimally with an average — a B737 ORD-DEN and a B777 ORD-NRT are not the same decision. The cost function must differentiate by aircraft type, route revenue, and passenger mix.

---

## What Was Mapped — and What Is Deliberately Out of Scope

The problem is larger than the first version. Every exclusion below was considered first. The lines are drawn deliberately, not by accident.

### Resources

**Aircraft — four categories, three cost tiers in v1:**

| Category | Examples | Turn time | Cost/min delay |
|----------|----------|-----------|----------------|
| Widebody international | B777, B787 | 90–120 min | $150–200+ |
| Widebody domestic | B767 (legacy) | 75–90 min | $120–150 |
| Narrowbody mainline | B737, A320 | 45–55 min | $74–100 |
| Regional jet | E170/E175 | 30–40 min | $30–50 |

A flat cost function treats a B777-300ER ORD-NRT identically to an E175 ORD-CID — an order-of-magnitude error in allocation priority. **v1 uses three tiers: widebody, narrowbody mainline, regional.** Per-type distinctions within tiers are implementation data, not problem-shaping. Type ratings, turn times, and gate compatibility are hard constraints in the option generator.

**Crew — six dimensions of reserve heterogeneity:**

| Dimension | v1 treatment |
|-----------|-------------|
| Type rating | Hard constraint — B737 captain cannot fly B777 |
| Role (CA/FO) | Hard constraint — both must be available and correctly seated |
| Domicile | Hard constraint — reserve must be at the flight's origin station |
| Callout window (short/long-call) | Hard constraint — 90 min minimum from callout to flight-ready |
| Remaining FDP | Hard constraint — checked against Part 117 Table B exactly |
| CBA seniority order | Deferred — cost penalty in v2 |

**FA constraints are deferred.** Flight attendants can ground a flight just as pilots can (Part 121 governs, not Part 117; minimum FA count is exits-based by aircraft type). Pilot constraints are more complex and more binding in practice — adding FAs doubles crew agent complexity. Flagged; not solved in v1.

**Multi-day pairing recovery is deferred.** A delay that pushes a crew member past FDP limits breaks her entire 3–4 day pairing. The downstream coverage problem spans 2–4 days and multiple airports. That is a different problem structure — temporal, not geographic — and a separate product. The reasoning trace flags broken pairings for crew scheduling to resolve.

**Passengers — four journey types, aggregate treatment in v1:**

| Type | Disruption exposure | v1 treatment |
|------|---------------------|-------------|
| Point-to-point | Delay only | Included in passenger count |
| Single-connection | Missed connection if delay > MCT | Connection risk estimated by route type |
| Double-connection | Cascades two hops downstream | Flagged as elevated-risk |
| International connecting | Highest reaccommodation cost; potential EU 261 exposure | Aggregate estimate; EU 261 deferred |

Individual passenger rebooking requires GDS access (Sabre, Amadeus) — separate product, out of scope. Per-fare-class cost differentiation requires airline yield data — deferred to v2.

**Gates are a constraint, not an optimized resource.** Gate type compatibility (a widebody cannot use a regional gate) is a hard constraint in option generation. Gate congestion during recovery creates a secondary bottleneck — estimated to extend the effective disruption window by 10–20% beyond what the queue math alone predicts — but full gate optimization requires airport gate management integration.

---

### Disruption Categories

The system solves the effect, not the cause. The cause determines the duration profile and trigger source; the pipeline is the same.

| Category | Scope | Duration type | v1 status |
|----------|-------|--------------|-----------|
| Weather — convective | Single airport or regional | Uncertain (15 min to 4+ hours) | **Primary scenario** |
| Weather — systemic | Regional to network-wide | Predictable (hours to days) | Architecture supports it |
| Equipment / mechanical | Single aircraft and its rotation | Predictable (30–120 min) | Supported — different trigger |
| Crew disruption | Single crew chain, multi-day pairing | Reserve-dependent | Supported — subset of full problem |
| ATC constraint (GDP, AFP) | One airport's arrival flow | Externally imposed slots | Architecture supports it |
| Security event | Single terminal or full airport | Unpredictable (45 min to hours) | Supported — manual trigger |
| Infrastructure | Single airport, reduced throughput | Usually predictable | Supported |
| Network cascade | Spans multiple airports, no primary event | Upstream-driven | This is what v1 detects |

The graph model handles all categories through the same pipeline. What varies is the trigger source — SWIM feed for weather/ATC, airline ops system for mechanical/crew, manual entry for security. The trigger is a pluggable input, not an architectural constraint. **v1 validates on weather-driven ground stop at a single hub** — the most frequent high-cost event, exercising the full pipeline.

---

### Constraints

Three tiers, three code paths:

**Hard** (violation = illegal or impossible): Part 117 FDP limits by start-hour and segment count — Table B implemented exactly, because an approximation that clears an illegal crew member creates direct regulatory liability. Also: aircraft type rating, minimum rest requirements, DOT tarmac delay rule (3h domestic / 4h international, $27,500/passenger fine), gate type compatibility, reserve callout minimum.

**Soft** (violation = costly, not illegal): CBA seniority order, maintenance routing (aircraft ends day at wrong station — $10–50K ferry flight later), gate assignment preferences, on-time performance targets. Implemented as cost penalties — they increase option cost but do not eliminate the option.

**Informal** (not in any system): Two distinct categories. Some are formalizable — "always protect ORD-NRT" is a route priority weight, configurable as a parameter, and surfaced over time by systematic override patterns. Others are truly informal — a specific reserve's passport issue, a gate jetbridge that adds five minutes to every turn. Configurable priority weights capture the first category. The override mechanism captures the second. Every override is a signal the model is missing a constraint.

---

### v1 Scope, Deferred Items, and Assumptions

**In v1:**

| Element | Detail |
|---------|--------|
| Disruption type | Weather-driven ground stop at a single hub |
| Aircraft | Three-tier cost: widebody ($150–200+/min), narrowbody ($74–100/min), regional ($30–50/min) |
| Crew — pilots | Part 117 Table B exact. CA/FO distinction. Type rating as hard constraint. |
| Crew — reserves | Type rating, role, domicile, callout window, remaining FDP — all checked |
| Passengers | Aggregate connection risk per flight. Route-type-based cost estimate. |
| Constraints | Hard exact. Soft as cost penalties. Informal via override + learning loop. |
| Allocation | Priority-ordered with shared pool visibility across all affected flights simultaneously |
| Trace | Options generated, constraints checked, prediction, dispatcher action, outcome |
| Duration uncertainty | P50/P75/P90 scenario planning |
| Cascade | Bidirectional BFS, 3–5 hop depth |

**Deferred to v2+:**

| Element | Why | Dependency |
|---------|-----|------------|
| Flight attendant constraints | Adds Part 121 complexity; pilot constraints more binding in practice | FA duty rule implementation |
| Multi-day pairing recovery | Different problem structure — temporal, not geographic. Separate product. | Crew scheduling system integration |
| Gate optimization | Real recovery bottleneck. Requires airport gate management integration. | Airport system API |
| Individual passenger rebooking | Separate product. Requires GDS access. | Sabre / Amadeus integration |
| Per-fare-class passenger value | More accurate cost function. Requires airline yield data. | Revenue management data feed |
| EU 261 / international regulatory cost | Real cost driver. Requires per-route legal validation. | Legal review + regulatory database |
| Predictive pre-positioning | Pre-position resources before disruption fires using forecast + trace history | 12+ months of trace data |
| True joint optimization | Integer-program allocation across all flights simultaneously. v1 uses priority-ordered allocation with shared pool tracking — structurally better than dispatcher-pace sequential, short of full combinatorial optimal. | Validated problem structure first |

**Assumptions requiring validation with airline partner:**

| Assumption | Basis | Validation |
|------------|-------|-----------|
| 10–20 reserve pilots per type at a major hub | Industry estimate | Actual reserve pool depth from carrier |
| 45–55 min minimum turn for B737 | Published ops documentation | Carrier-specific turn times by aircraft type |
| $74–100/min direct delay cost | Eurocontrol Standard Inputs | Carrier-specific operating cost by fleet type |
| 15–20 min dispatcher awareness window | Practitioner accounts | OCC observation during live disruption |
| Dispatchers allocate sequentially today | Industry understanding | OCC observation — confirm or refute |
| Gate congestion adds 10–20% to disruption window | Estimate from reasoning | Airport ops data or OCC observation |
| Outcome recording adoption rate > 50% | Required for learning loop to function | Dispatcher feedback during pilot deployment |

---

## What Will You Solve For?

Not the 18 minutes. The cascade. Three things the current system cannot do:

**1. Complete situational awareness in seconds, not minutes.** Dispatchers today spend 15–20 minutes manually correlating across systems to understand what is affected. In that window, options close. A reserve called at minute 2 is flight-ready 90 minutes later. The reserve called at minute 20 cannot reach the same windows. Speed is asymmetric — earlier action opens solutions that later action cannot.

**2. Structured allocation across a shared resource pool, not sequential greedy.** Dispatchers allocate resources flight by flight — best available reserve for Flight A, then best remaining for Flight B. They don't know whether that sequence depletes the pool for Flights C through H in ways a different order would avoid. dCortex processes all affected flights with full pool visibility: ordering by flight value, tracking pool depletion across the entire impact set, evaluating every commitment before the next option is scored. This is not true joint optimization in the integer-programming sense — that is deferred. It is priority-ordered allocation with shared pool accounting: structurally different from how dispatchers work today, and the gap is largest exactly when disruptions are most expensive.

**3. Capture the reasoning, not just the decision.** Legacy systems log what was decided — crew X reassigned to Flight Y. They don't capture what options were considered, what constraints were binding, what was predicted, or whether the prediction matched reality. Without the reasoning trace, there is no learning signal. Every disruption starts from scratch. The organization accumulates experience in individual dispatchers who retire, and never in the institution.

---

## What We Build

A reasoning layer on top of the airline's existing systems. Not a replacement — a synthesis. When a disruption fires:

1. **Impact in seconds** — BFS traversal of the flight graph, bidirectional, complete picture before the first phone call
2. **Every option enumerated** — constraint-checked against Part 117, aircraft type, reserve availability; no illegal option presented, no feasible option omitted
3. **Resources allocated with full pool visibility** — all affected flights processed simultaneously, ordered by flight value, each commitment tracked against the shared pool before the next option is evaluated
4. **Reasoning trace locked** — every option considered, every constraint checked, every prediction made — immutable at time of decision
5. **Human says yes or no** — dispatcher accepts, overrides, or modifies; the decision is hers; the airline holds regulatory responsibility
6. **Outcome recorded** — actual cost vs. predicted; the delta calibrates the model for next time

**Three properties, non-negotiable:**
- **Transparent** — every recommendation fully auditable; no black box
- **Deterministic** — hard constraints are hard; Part 117 is not a suggestion
- **Weighted** — dispatcher controls the tradeoff: cost, passenger protection, delay minimization

---

## How We Are Better

**Speed creates options that don't exist otherwise.** Earlier awareness is not incrementally better — it accesses solution spaces that later awareness cannot.

**Enumeration beats intuition at scale.** For 2–3 disruptions, experienced dispatchers usually find the right answer. For 10–15 simultaneous disruptions with a stressed reserve pool, structured allocation with full pool visibility is structurally better. The gap is largest when disruptions are most expensive.

**The corpus is the moat.** An algorithm can be copied. Eighteen months of structured reasoning traces from a live carrier — with actual outcomes recorded — cannot. More traces → better calibration → better rankings → more dispatcher trust → more adoption → more traces. That gap compounds. It cannot be fast-followed.

---

## Where the System Can Fail

A recommendation is only as good as its inputs. Four failure modes matter:

**Stale data.** If the crew system is 5 minutes behind, a reserve marked available may have already called out sick. If aircraft positions are delayed, the upstream cascade is undercounted. The system should surface data freshness explicitly — a confident recommendation over stale inputs is worse than no recommendation.

**False feasibility.** The system checks the constraints it knows about. It doesn't know that the chief dispatcher always protects the ORD-NRT departure regardless of cost. Two categories of missing rules: some are formalizable — a route priority weight on ORD-NRT can be configured as a parameter, and systematic overrides surface these over time. Others are truly informal — the dispatcher knows a specific reserve has a passport issue, or that a gate's jetbridge adds five minutes to every turn. Configurable priority weights capture the first category. The override mechanism captures the second. Every override is a signal the model is missing a constraint.

**Prediction divergence.** The cost model is calibrated on historical data. When a disruption is larger than anything the model has seen, predictions are extrapolated outside the data range. The system should flag when it's operating outside its confidence bounds.

**Reserve double-commitment.** If two disruptions are processed in close sequence, the same reserve could appear as available to both before either dispatcher has acted. The coordination layer must maintain a global reservation lock across concurrent disruptions.

---

## How the Dispatcher Actually Uses It

During a live disruption, a dispatcher is simultaneously on the phone with crew scheduling, coordinating with gate agents, and communicating with flight crews. dCortex cannot compete for her attention — it has to fit around her workflow.

Two principles:

**Push, not pull.** The briefing surfaces automatically when a disruption fires. The dispatcher does not query the system — the system finds her.

**Peripheral, not primary.** The recommendation must be readable in 30 seconds. The accept/override must be a single action. The system maintains a live status of what's committed and what's still open without requiring active management.

If using the system requires pulling focus away from the disruption itself, it won't be used — regardless of how good the recommendations are. Integration design is not a UI problem. It is a trust problem.

---

## What We Don't Know Yet

The 15–20 minute awareness window, the true joint allocation failure rate, and the real cost definition all require OCC observation to validate. This document is a hypothesis. The first OCC access converts it to data.

---

*Sources: FAA NEXTOR Total Delay Impact Study; Airlines for America Annual Economic Report; Eurocontrol Standard Inputs for Cost-Benefit Analyses; Bratu & Barnhart (2006), Journal of Scheduling; FAA ASPM data.*
