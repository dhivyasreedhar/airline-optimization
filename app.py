"""
app.py — dCortex Dispatcher Console
Streamlit front-end wired to the real disruption engine.
"""

import json
import os
import time as wall_time
import requests
import streamlit as st
from datetime import time as Time, datetime
from typing import Optional

from simulation import run, simulate, SimulationResult, DisruptionResult, SEVERITY_PROFILES

# ─── App config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="dCortex — Dispatcher Console",
    layout="wide",
    initial_sidebar_state="expanded",
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

AIRPORTS = {
    "ORD": "Chicago O'Hare",
    "LAX": "Los Angeles",
    "JFK": "New York JFK",
    "DEN": "Denver",
    "MIA": "Miami",
    "SEA": "Seattle",
    "SFO": "San Francisco",
}

SEVERITY = {
    "P50 — 18 min": 18,
    "P75 — 30 min": 30,
    "P90 — 45 min": 45,
}

CAUSE_OPTIONS = ["Weather", "ATC", "Mechanical", "Security", "Medical"]

IMPACT_BADGE = {
    "DEPARTURE_BLOCKED": ("DEP BLOCKED", "#cc2222"),
    "ARRIVAL_BLOCKED":   ("ARR BLOCKED", "#cc6600"),
    "CASCADE":           ("CASCADE",     "#997700"),
}

OPTION_BADGE = {
    "DELAY":         ("DELAY",     "#2d6bb3"),
    "CREW_SWAP":     ("CREW SWAP", "#2d8050"),
    "AIRCRAFT_SWAP": ("AC SWAP",   "#7b3fa0"),
    "CANCEL":        ("CANCEL",    "#c0392b"),
}

# ─── Styles ───────────────────────────────────────────────────────────────────

st.markdown("""
<style>
.badge {
    display: inline-block; padding: 2px 9px; border-radius: 3px;
    font-size: 10px; font-weight: 700; letter-spacing: 0.8px; color: white;
}
.mono { font-family: monospace; font-size: 13px; line-height: 1.7; }
.dimmed { color: #888; font-size: 12px; }
.section-title {
    font-size: 10px; font-weight: 700; letter-spacing: 1.5px;
    color: #777; margin-bottom: 2px;
}
.ok   { color: #2ecc71; font-weight: 700; font-size: 12px; }
.warn { color: #e74c3c; font-weight: 700; font-size: 12px; }
</style>
""", unsafe_allow_html=True)


def badge(label: str, color: str) -> str:
    return f'<span class="badge" style="background:{color}">{label}</span>'


# ─── Session state ────────────────────────────────────────────────────────────

for _k, _v in [
    ("stage",      "idle"),
    ("result",     None),
    ("sim",        None),
    ("briefing",   None),
    ("compute_ms", 0),
]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ─── Claude helpers ───────────────────────────────────────────────────────────

def _build_prompt(r: DisruptionResult) -> str:
    lines = [
        "You are dCortex, an airline operations AI. Write a concise dispatcher briefing.",
        "Use exactly these 4 section headers (each on its own line, ALL CAPS, followed by a colon):",
        "SITUATION SUMMARY:",
        "RECOMMENDED ACTIONS:",
        "RISK FLAGS:",
        "WHAT WE LEARNED:",
        "Under 300 words total. Direct, operational language. No filler.",
        "",
        f"DISRUPTION: {r.cause} ground stop at {r.airport}",
        f"  Duration: {r.duration_min} min | Effective window ends: {r.effective_end.strftime('%H:%M')}",
        f"  Direct impacts: {r.direct_count} | Cascade: {r.cascade_count} | Passengers: {r.total_pax}",
        f"  Plan cost: ${r.plan_cost:,.0f} | Do-nothing cost: ${r.donoth_cost:,.0f}",
        "",
        "FLIGHT ACTIONS:",
    ]
    for res in r.resolutions:
        f = res.impact.flight
        rec = res.recommended
        label, _ = IMPACT_BADGE.get(res.impact.impact_type, ("IMPACT", "#888"))
        rec_str = f"{rec.type} ${rec.cost:,.0f}" if rec else "NO OPTION"
        lines.append(
            f"  {f.id} {f.origin}->{f.destination} [{label}] "
            f"{res.impact.expected_delay_min}min {f.passengers}pax -> {rec_str}"
        )
    violations = [
        c.detail
        for res in r.resolutions
        for o in res.options
        for c in o.constraints
        if not c.passed
    ]
    if violations:
        lines.append("\nCONSTRAINT VIOLATIONS:")
        for v in violations[:5]:
            lines.append(f"  - {v}")
    return "\n".join(lines)


def _fallback_briefing(r: DisruptionResult) -> str:
    actions = "\n".join(
        f"- {res.impact.flight.id} {res.impact.flight.origin}->{res.impact.flight.destination}: "
        f"{res.recommended.type} (${res.recommended.cost:,.0f})"
        for res in r.resolutions if res.recommended
    ) or "- No feasible options found."
    violations = [
        c.detail
        for res in r.resolutions
        for o in res.options
        for c in o.constraints
        if not c.passed
    ]
    flags = "\n".join(f"- {v}" for v in violations[:4]) if violations else "- No critical violations."
    savings = r.donoth_cost - r.plan_cost
    return (
        f"SITUATION SUMMARY:\n"
        f"{r.cause} ground stop at {r.airport}. Shutdown {r.duration_min} min, effective window ends "
        f"{r.effective_end.strftime('%H:%M')} (queue recovery included). "
        f"{r.direct_count} direct and {r.cascade_count} cascade flights. "
        f"{r.total_pax} passengers at risk.\n\n"
        f"RECOMMENDED ACTIONS:\n{actions}\n\n"
        f"RISK FLAGS:\n{flags}\n\n"
        f"WHAT WE LEARNED:\n"
        f"Single-node failure propagated {r.cascade_count} hops. "
        f"Acting now saves ${savings:,.0f} vs do-nothing. "
        f"Trace locked — cost delta calibrates the model post-resolution."
    )


def _call_claude(prompt: str) -> Optional[str]:
    if not ANTHROPIC_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 700,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]
    except Exception as e:
        st.warning(f"Claude API unavailable ({e}). Using computed briefing.")
        return None


def _parse_sections(text: str) -> dict:
    keys = ["SITUATION SUMMARY", "RECOMMENDED ACTIONS", "RISK FLAGS", "WHAT WE LEARNED"]
    sections: dict = {}
    current, buf = None, []
    for line in text.splitlines():
        matched = False
        for k in keys:
            if line.strip().upper().startswith(k):
                if current:
                    sections[current] = "\n".join(buf).strip()
                current = k
                rest = line.strip()[len(k):].lstrip(":").strip()
                buf = [rest] if rest else []
                matched = True
                break
        if not matched and current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return sections


# ─── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## dCortex")
    st.caption("Airline disruption intelligence")
    st.divider()

    airport = st.selectbox(
        "Airport", list(AIRPORTS.keys()),
        format_func=lambda x: f"{x} — {AIRPORTS[x]}"
    )
    cause = st.selectbox("Cause", CAUSE_OPTIONS)
    sev_label = st.selectbox("Severity", list(SEVERITY.keys()))
    duration_min = SEVERITY[sev_label]
    start_t = st.time_input("Disruption start (local)", value=Time(14, 0))

    st.divider()
    run_btn = st.button("RUN SIMULATION", type="primary", use_container_width=True)

    if st.session_state.stage != "idle":
        if st.button("Reset", use_container_width=True):
            st.session_state.stage = "idle"
            st.session_state.result = None
            st.session_state.sim = None
            st.session_state.briefing = None
            st.session_state.compute_ms = 0
            st.rerun()

    st.divider()
    if not ANTHROPIC_API_KEY:
        st.markdown(
            '<p class="dimmed">No ANTHROPIC_API_KEY found.<br>'
            'Briefing uses computed fallback.</p>',
            unsafe_allow_html=True
        )
    st.markdown(
        '<p class="dimmed">All constraints are hard.<br>Part 117 is not a suggestion.</p>',
        unsafe_allow_html=True
    )


# ─── Run trigger ──────────────────────────────────────────────────────────────

if run_btn:
    t0 = wall_time.perf_counter()
    with st.spinner(f"Running simulation for {airport} — P50 / P75 / P90..."):
        sim_result = simulate(airport, start_t.hour, start_t.minute, cause)
    elapsed_ms = int((wall_time.perf_counter() - t0) * 1000)
    st.session_state.sim = sim_result
    st.session_state.result = sim_result.by_label(
        sev_label.split(" — ")[0]  # "P75 — 30 min" -> "P75"
    )
    st.session_state.compute_ms = elapsed_ms
    st.session_state.stage = "impact"
    st.session_state.briefing = None
    st.rerun()


# ─── Guard: stage sanity ──────────────────────────────────────────────────────

if st.session_state.stage != "idle" and (
    st.session_state.result is None or st.session_state.sim is None
):
    st.session_state.stage = "idle"
    st.rerun()


# ─── Idle ─────────────────────────────────────────────────────────────────────

if st.session_state.stage == "idle":
    st.markdown("# dCortex")
    st.markdown("**Disruption Intelligence for Airline Operations**")
    st.markdown("""
Select an airport, cause, and severity in the sidebar, then click **RUN SIMULATION**.

---

**Two modes. One engine.**

**Simulate first** — before the FAA issues a ground stop, run all three severity scenarios (P50 / P75 / P90) simultaneously. See the full cost envelope in milliseconds. Pre-position reserves for the most likely outcome before the disruption fires.

**Decide live** — once the EDCT is issued and duration is known, the selected scenario becomes the plan. Agents lock the trace at dispatcher decision time. Outcome recorded post-resolution calibrates the model for the next event.

---

**Five steps per scenario, run by four coordinating agents:**

1. **Impact** — BFS traversal of the flight graph, bidirectional from the disrupted node
2. **Enumeration** — CrewAgent, AircraftAgent, and PassengerAgent generate every feasible option per flight
3. **Constraint check** — Part 117 FDP limits (exact Table B), type ratings, reserve callout windows, station availability
4. **Allocation** — CoordinatingAgent assigns resources across all affected flights simultaneously, highest-pax first, full pool visibility before each commitment
5. **Trace** — every option considered, every constraint checked, every prediction made — immutable at decision time

A dispatcher today needs 15–20 minutes to build this picture manually, flight by flight.
dCortex does it before the first phone call.
""")
    st.stop()


# ─── Shared header (all non-idle stages) ──────────────────────────────────────

r: DisruptionResult = st.session_state.result

hdr_col, time_col = st.columns([4, 1])
with hdr_col:
    st.markdown(f"## {r.airport} — {r.cause.upper()} GROUND STOP")
    st.markdown(
        f'**{r.start.strftime("%H:%M")}** start &nbsp;·&nbsp; '
        f'**{r.duration_min} min** shutdown &nbsp;·&nbsp; '
        f'effective window ends **{r.effective_end.strftime("%H:%M")}**'
    )
with time_col:
    st.markdown(
        f'<p class="dimmed" style="text-align:right;margin-top:24px;">'
        f'Computed in {st.session_state.compute_ms} ms</p>',
        unsafe_allow_html=True
    )

m1, m2, m3, m4 = st.columns(4)
m1.metric("Direct impacts", r.direct_count)
m2.metric("Cascade", r.cascade_count)
m3.metric("Passengers at risk", f"{r.total_pax:,}")
m4.metric("Plan cost", f"${r.plan_cost:,.0f}")
st.markdown("---")


# ─── Stage: impact ────────────────────────────────────────────────────────────

if st.session_state.stage == "impact":

    if not r.resolutions:
        st.info(
            f"No flights fall within the disruption window at {r.airport}. "
            "Try a different start time or severity."
        )
        st.stop()

    # ── Scenario envelope ─────────────────────────────────────────────────────
    sim: SimulationResult = st.session_state.sim
    st.markdown("### SCENARIO ENVELOPE")
    st.caption(
        "All three severity scenarios computed simultaneously. "
        "Position resources now for the P75. Act when confirmed duration is known."
    )
    env_cols = st.columns(3)
    for col, scenario in zip(env_cols, sim.scenarios):
        sr = scenario.result
        is_selected = scenario.duration_min == r.duration_min
        label = f"**{scenario.label}** — {scenario.duration_min} min"
        if is_selected:
            label += " ← selected"
        col.metric(
            label,
            f"${sr.plan_cost:,.0f}",
            delta=f"{len(sr.resolutions)} flights · {sr.total_pax:,} pax",
            delta_color="off",
        )
    st.markdown("---")

    st.markdown("### IMPACT ASSESSMENT")

    for res in r.resolutions:
        f = res.impact.flight
        imp_label, imp_color = IMPACT_BADGE.get(res.impact.impact_type, ("IMPACT", "#555"))
        rec = res.recommended
        n_violations = sum(1 for o in res.options for c in o.constraints if not c.passed)

        hdr, d_col, p_col, rec_col, status_col = st.columns([2.5, 0.9, 0.9, 2.8, 1.2])
        with hdr:
            st.markdown(
                f"**{f.id}** &nbsp; {f.origin} → {f.destination} &nbsp;&nbsp;"
                f"{badge(imp_label, imp_color)}",
                unsafe_allow_html=True
            )
            st.markdown(
                f'<span class="dimmed">{res.impact.cause}</span>',
                unsafe_allow_html=True
            )
        with d_col:
            st.metric("Delay", f"{res.impact.expected_delay_min} min")
        with p_col:
            st.metric("Pax", f"{f.passengers:,}")
        with rec_col:
            if rec:
                opt_label, opt_color = OPTION_BADGE.get(rec.type, ("ACTION", "#555"))
                st.markdown(
                    f"{badge(opt_label, opt_color)} &nbsp; **{rec.description}**  \n"
                    f'<span class="dimmed">Cost: ${rec.cost:,.0f}</span>',
                    unsafe_allow_html=True
                )
            else:
                st.markdown(badge("NO OPTION", "#c0392b"), unsafe_allow_html=True)
        with status_col:
            if n_violations:
                st.markdown(
                    f'<span class="warn">'
                    f'{n_violations} violation{"s" if n_violations > 1 else ""}</span>',
                    unsafe_allow_html=True
                )
            else:
                st.markdown('<span class="ok">OK</span>', unsafe_allow_html=True)

        with st.expander(f"All options & constraints — {f.id}"):
            for opt in res.options:
                opt_label, opt_color = OPTION_BADGE.get(opt.type, ("ACT", "#555"))
                rec_tag = " &nbsp; **← RECOMMENDED**" if opt.recommended else ""
                infeas_tag = " &nbsp; *(infeasible)*" if not opt.feasible else ""
                st.markdown(
                    f"{badge(opt_label, opt_color)} &nbsp; "
                    f"**{opt.description}** &nbsp;—&nbsp; ${opt.cost:,.0f}"
                    f"{rec_tag}{infeas_tag}",
                    unsafe_allow_html=True
                )
                for c in opt.constraints:
                    color = "#2ecc71" if c.passed else "#e74c3c"
                    icon = "+" if c.passed else "x"
                    st.markdown(
                        f'<span class="mono" style="color:{color}">'
                        f'&nbsp;&nbsp;[{icon}] {c.name}: {c.detail}</span>',
                        unsafe_allow_html=True
                    )
                st.markdown("")

        st.markdown("---")

    # Cost comparison
    savings = r.donoth_cost - r.plan_cost
    sc1, sc2, sc3 = st.columns(3)
    sc1.metric("Plan cost", f"${r.plan_cost:,.0f}")
    sc2.metric("Do-nothing cost", f"${r.donoth_cost:,.0f}")
    sc3.metric("Savings from acting now", f"${savings:,.0f}")

    st.markdown("")
    if st.button("GENERATE BRIEFING", type="primary"):
        with st.spinner("Calling dCortex reasoning layer..."):
            prompt = _build_prompt(r)
            raw = _call_claude(prompt)
            if raw is None:
                raw = _fallback_briefing(r)
            st.session_state.briefing = raw
        st.session_state.stage = "briefing"
        st.rerun()


# ─── Stage: briefing ──────────────────────────────────────────────────────────

elif st.session_state.stage == "briefing":
    st.markdown("### DISPATCHER BRIEFING")

    raw: str = st.session_state.briefing
    sections = _parse_sections(raw)

    if not sections:
        st.markdown(raw)
    else:
        labels = {
            "SITUATION SUMMARY":   "Situation",
            "RECOMMENDED ACTIONS": "Actions",
            "RISK FLAGS":          "Risk flags",
            "WHAT WE LEARNED":     "What we learned",
        }
        for key in ["SITUATION SUMMARY", "RECOMMENDED ACTIONS", "RISK FLAGS", "WHAT WE LEARNED"]:
            text = sections.get(key, "")
            if not text:
                continue
            st.markdown(
                f'<p class="section-title">{labels[key]}</p>',
                unsafe_allow_html=True
            )
            st.markdown(text)
            st.markdown("---")

    if r.reserved_resources:
        st.markdown("**Resources committed in this plan:**")
        for rid, fid in r.reserved_resources.items():
            st.markdown(f"- `{rid}` locked to **{fid}**")
        st.markdown("---")

    lock_col, override_col = st.columns(2)
    with lock_col:
        if st.button("LOCK TRACE — ACCEPT", type="primary", use_container_width=True):
            r.trace["chosen_by"] = "DISPATCHER_ACCEPTED"
            st.session_state.stage = "locked"
            st.rerun()
    with override_col:
        if st.button("LOCK TRACE — OVERRIDE", use_container_width=True):
            r.trace["chosen_by"] = "DISPATCHER_OVERRIDE"
            st.session_state.stage = "locked"
            st.rerun()


# ─── Stage: locked ────────────────────────────────────────────────────────────

elif st.session_state.stage == "locked":
    locked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trace = r.trace

    st.markdown(f"### TRACE LOCKED — {locked_at}")
    st.markdown(
        f'Decision by: **{trace.get("chosen_by", "SYSTEM")}** &nbsp;·&nbsp; '
        f'Trace ID: `{trace["id"]}`'
    )
    st.markdown("---")

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Decisions recorded**")
        for fid, action in trace.get("recommendations", {}).items():
            st.markdown(f"- `{fid}` → {action}")
    with col_b:
        st.markdown("**Resources committed**")
        committed = trace.get("resources_committed", [])
        if committed:
            for entry in committed:
                st.markdown(f"- `{entry}`")
        else:
            st.markdown("- None")

    st.markdown("---")

    tm1, tm2, tm3 = st.columns(3)
    tm1.metric("Options generated", trace.get("options_generated", 0))
    tm2.metric("Constraints checked", trace.get("constraints_checked", 0))
    tm3.metric("Violations found", len(trace.get("constraint_violations", [])))

    violations = trace.get("constraint_violations", [])
    if violations:
        st.markdown("**Constraint violations logged:**")
        for v in violations:
            st.markdown(
                f'- <span style="color:#e74c3c">{v}</span>',
                unsafe_allow_html=True
            )
        st.markdown("---")

    # Record outcome
    st.markdown("### RECORD OUTCOME")
    st.markdown(
        "Enter the actual cost after resolution to calibrate the model. "
        "The delta trains the prediction for the next similar event."
    )

    with st.form("outcome_form"):
        actual_cost = st.number_input(
            "Actual total cost ($)",
            min_value=0.0,
            value=float(trace.get("predicted_cost", 0.0)),
            step=1000.0,
            format="%.0f",
        )
        dispatcher_notes = st.text_area("Dispatcher notes (optional)", height=80)
        submitted = st.form_submit_button("RECORD & CLOSE")

        if submitted:
            predicted = float(trace.get("predicted_cost", 0.0))
            delta = actual_cost - predicted
            pct = (delta / predicted * 100) if predicted else 0.0
            trace["actual_cost"] = actual_cost
            trace["dispatcher_notes"] = dispatcher_notes
            trace["cost_delta"] = delta
            st.success(
                f"Outcome recorded. Predicted ${predicted:,.0f} -> Actual ${actual_cost:,.0f} "
                f"(delta {delta:+,.0f}, {pct:+.1f}%). "
                f"This trace calibrates the model for the next "
                f"{r.cause.lower()} event at {r.airport}."
            )

    with st.expander("Full trace (JSON)"):
        st.code(json.dumps(trace, indent=2, default=str), language="json")
