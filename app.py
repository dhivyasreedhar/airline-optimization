import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import json
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")

import pandas as pd
import plotly.graph_objects as go
import networkx as nx
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="dCortex — Disruption Management",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Global CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* suppress default metric widget — we use _metric_card() HTML instead */
[data-testid="metric-container"] { display: none !important; }

/* Tabs */
.stTabs [data-baseweb="tab-list"] {
    background: #f1f5f9;
    border-radius: 8px;
    padding: 4px;
    gap: 2px;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 6px;
    padding: 6px 18px;
    font-size: 13px;
    font-weight: 500;
    color: #475569;
}
.stTabs [aria-selected="true"] {
    background: white !important;
    color: #1d4ed8 !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}

/* DataFrames */
[data-testid="stDataFrame"] {
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    overflow: hidden;
}

/* Sidebar */
[data-testid="stSidebar"] {
    background: #0f172a !important;
    border-right: none;
}
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] span,
[data-testid="stSidebar"] div.stMarkdown {
    color: #cbd5e1 !important;
}
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3 {
    color: #f1f5f9 !important;
}
[data-testid="stSidebar"] [data-testid="stExpander"] {
    border: 1px solid #1e293b !important;
    border-radius: 6px;
    background: #1e293b;
}
[data-testid="stSidebar"] hr {
    border-color: #1e293b !important;
}
[data-testid="stSidebar"] .stSlider > label {
    color: #94a3b8 !important;
    font-size: 12px !important;
}
[data-testid="stSidebar"] .stButton > button {
    background: #1d4ed8;
    color: white;
    border: none;
    font-weight: 600;
}
[data-testid="stSidebar"] .stButton > button[kind="secondary"] {
    background: #334155;
    color: #e2e8f0;
}
[data-testid="stSidebar"] .stRadio > label {
    color: #94a3b8 !important;
}
[data-testid="stSidebar"] .stTextInput > label {
    color: #94a3b8 !important;
}

/* Buttons */
button[kind="primary"] { border-radius: 6px !important; font-weight: 600 !important; }

/* Info/success boxes */
[data-testid="stAlert"] { border-radius: 8px; }

/* Section dividers */
hr { border-color: #e2e8f0 !important; margin: 24px 0 !important; }
</style>
""", unsafe_allow_html=True)

# ── Backend imports ───────────────────────────────────────────────────────────
from data.synthesize import generate_flights, generate_crew_roster, generate_triage_scenario
from graph.cascade import build_flight_graph, bfs_cascade, get_affected_flights
from agents.crew_agent import CrewAgent, remaining_fdp, max_fdp
from agents.aircraft_agent import AircraftAgent
from agents.passenger_agent import PassengerAgent
from agents.coordinating_agent import CoordinatingAgent
from models import Flight, CrewMember, DisruptionEvent
from trace.emitter import emit_trace, update_trace_decisions, update_trace_outcome

BASE_DATE = datetime(2025, 6, 15)

# ── Scenario presets ──────────────────────────────────────────────────────────
PRESETS = {
    "ORD — Triage Demo": dict(
        hub="ORD", gs_hour=10, gs_duration=30, scenario="P90",
        triage=True,
        cost_wb=175, cost_wb_dom=135, cost_nb=87, cost_reg=40, cost_pax=1000,
        gates_wb=3, gates_nb=15, gates_reg=20,
        description=(
            "Constructed 10-flight scenario. 6 crew pairs, 3 widebody gates. "
            "Greedy wastes the last WB gate on ORD→SFO (10:00 AM departure) — "
            "leaving ORD→NRT uncovered. Pool-aware secures ORD→NRT first. ~$31K delta at P90."
        ),
    ),
    "ORD — Standard": dict(
        hub="ORD", gs_hour=10, gs_duration=30, scenario="P75",
        num_regional=6, num_narrowbody=9, num_wb_dom=3, num_wb_intl=4,
        b737_res=6, b777_res=4, b787_res=4, e175_res=4, near_limit=4,
        cost_wb=175, cost_wb_dom=135, cost_nb=87, cost_reg=40, cost_pax=1000,
        description="Standard ORD hub disruption. Balanced reserves and fleet.",
    ),
    "ORD — Reserve Shortage": dict(
        hub="ORD", gs_hour=10, gs_duration=30, scenario="P75",
        num_regional=4, num_narrowbody=8, num_wb_dom=2, num_wb_intl=3,
        b737_res=4, b777_res=2, b787_res=2, e175_res=2, near_limit=4,
        cost_wb=175, cost_wb_dom=135, cost_nb=87, cost_reg=40, cost_pax=1000,
        description="Thin reserve pool. Pool-aware advantage is most dramatic here.",
    ),
    "ORD — High Traffic": dict(
        hub="ORD", gs_hour=9, gs_duration=45, scenario="P90",
        num_regional=8, num_narrowbody=12, num_wb_dom=4, num_wb_intl=5,
        b737_res=8, b777_res=4, b787_res=4, e175_res=4, near_limit=5,
        cost_wb=175, cost_wb_dom=135, cost_nb=87, cost_reg=40, cost_pax=1000,
        description="Busy summer day, longer ground stop. P90 worst-case scenario.",
    ),
    "LAX — Afternoon Peak": dict(
        hub="LAX", gs_hour=14, gs_duration=25, scenario="P75",
        num_regional=3, num_narrowbody=10, num_wb_dom=3, num_wb_intl=5,
        b737_res=6, b777_res=4, b787_res=4, e175_res=2, near_limit=4,
        cost_wb=175, cost_wb_dom=135, cost_nb=87, cost_reg=40, cost_pax=1000,
        description="LAX afternoon ground stop, international-heavy fleet.",
    ),
}

SCENARIO_DELAYS = {"P50": 45, "P75": 75, "P90": 90, "Custom": None}
AIRCRAFT_TYPES   = ["regional", "narrowbody", "widebody_domestic", "widebody"]
CREW_ROLES       = ["CA", "FO"]
TYPE_RATINGS     = ["B737", "A320", "B777", "B787", "B767", "E175", "E170"]
CALLOUT_STATUSES = ["available", "short_call", "long_call", "unavailable"]
ATYPE_SHORT      = {"widebody": "WB", "widebody_domestic": "WB-Dom", "narrowbody": "NB", "regional": "Rg"}


# ── HTML rendering helpers ────────────────────────────────────────────────────
_TH = 'style="padding:10px 14px;text-align:left;font-weight:600;color:#374151;white-space:nowrap;font-size:12px;background:#f8fafc;border-bottom:2px solid #e2e8f0;"'
_TD = 'style="padding:9px 14px;color:#1e293b;font-size:13px;border-bottom:1px solid #f1f5f9;"'
_TABLE_WRAP = 'style="width:100%;border-collapse:collapse;font-family:system-ui,sans-serif;"'


def _badge(text: str, style: str) -> str:
    """Pill badge. style: 'green' | 'red' | 'amber' | 'blue' | 'gray'"""
    colors = {
        "green": ("background:#dcfce7;color:#15803d", ),
        "red":   ("background:#fee2e2;color:#b91c1c", ),
        "amber": ("background:#fef3c7;color:#b45309", ),
        "blue":  ("background:#dbeafe;color:#1d4ed8", ),
        "gray":  ("background:#f1f5f9;color:#64748b", ),
    }
    bg = colors.get(style, colors["gray"])[0]
    return (
        f'<span style="{bg};display:inline-block;padding:2px 10px;'
        f'border-radius:20px;font-weight:700;font-size:11px;white-space:nowrap;">{text}</span>'
    )


def _html_allocation(rows: list) -> str:
    headers = ["Flight", "Route", "Type", "Pool Cost", "Greedy Cost", "Ferry", "Pool", "Greedy"]
    out = [f'<div style="overflow-x:auto;border:1px solid #e2e8f0;border-radius:8px;">',
           f'<table {_TABLE_WRAP}><thead><tr>']
    for h in headers:
        out.append(f'<th {_TH}>{h}</th>')
    out.append('</tr></thead><tbody>')

    for i, row in enumerate(rows):
        uncov = row["Pool Status"] == "UNCOVERED"
        bg = "#fff5f5" if uncov else ("white" if i % 2 == 0 else "#fafafa")
        out.append(f'<tr style="background:{bg};">')
        out.append(f'<td style="padding:9px 14px;font-family:monospace;font-weight:700;color:#1d4ed8;font-size:13px;">{row["Flight"]}</td>')
        out.append(f'<td style="padding:9px 14px;font-family:monospace;color:#374151;font-size:13px;">{row["Route"]}</td>')
        out.append(f'<td {_TD}>{ATYPE_SHORT.get(row["Type"], row["Type"])}</td>')
        out.append(f'<td style="padding:9px 14px;font-weight:700;color:#0f172a;font-size:13px;">${row["Pool Cost"]:,}</td>')
        out.append(f'<td style="padding:9px 14px;color:#64748b;font-size:13px;">${row["Greedy Cost"]:,}</td>')
        ferry = row.get("Ferry Cost", 0)
        ferry_html = f'<span style="color:#b45309;font-weight:600;">${ferry:,}</span>' if ferry > 0 else '<span style="color:#cbd5e1;">—</span>'
        out.append(f'<td {_TD}>{ferry_html}</td>')
        for key in ("Pool Status", "Greedy Status"):
            s = row[key]
            b = _badge("COVERED", "green") if s == "COVERED" else _badge("UNCOVERED", "red")
            out.append(f'<td style="padding:9px 14px;">{b}</td>')
        out.append('</tr>')

    out.append('</tbody></table></div>')
    return ''.join(out)


def _html_fdp(fdp_rows: list) -> str:
    headers = ["Crew ID", "Role", "Rating", "Duty Start", "Max FDP", "Elapsed", "Remaining", "Status"]
    out = [f'<div style="overflow-x:auto;border:1px solid #e2e8f0;border-radius:8px;">',
           f'<table {_TABLE_WRAP}><thead><tr>']
    for h in headers:
        out.append(f'<th {_TH}>{h}</th>')
    out.append('</tr></thead><tbody>')

    for i, row in enumerate(fdp_rows):
        s = row["Status"]
        bg = "#fff5f5" if s == "OVER LIMIT" else ("#fffbeb" if s == "NEAR LIMIT" else ("white" if i % 2 == 0 else "#fafafa"))
        out.append(f'<tr style="background:{bg};">')
        out.append(f'<td style="padding:9px 14px;font-family:monospace;font-size:12px;color:#374151;">{row["Crew ID"]}</td>')
        out.append(f'<td style="padding:9px 14px;font-weight:600;color:#374151;">{row["Role"]}</td>')
        out.append(f'<td {_TD}>{row["Rating"]}</td>')
        out.append(f'<td style="padding:9px 14px;font-family:monospace;color:#374151;">{row["Duty Start"]}</td>')
        out.append(f'<td {_TD}>{row["Max FDP (h)"]}</td>')
        out.append(f'<td {_TD}>{row["Elapsed (h)"]}</td>')
        rem = row["Remaining (h)"]
        rem_color = "#b91c1c" if s == "OVER LIMIT" else ("#b45309" if s == "NEAR LIMIT" else "#15803d")
        out.append(f'<td style="padding:9px 14px;font-weight:700;color:{rem_color};font-size:13px;">{rem}h</td>')
        badge = (_badge("OVER LIMIT", "red") if s == "OVER LIMIT"
                 else _badge("NEAR LIMIT", "amber") if s == "NEAR LIMIT"
                 else _badge("OK", "green"))
        out.append(f'<td style="padding:9px 14px;">{badge}</td>')
        out.append('</tr>')

    out.append('</tbody></table></div>')
    return ''.join(out)


def _html_flights(flight_rows: list) -> str:
    headers = ["Flight", "Route", "Type", "Pax", "Dep", "Arr", "Cascade"]
    out = [f'<div style="overflow-x:auto;border:1px solid #e2e8f0;border-radius:8px;">',
           f'<table {_TABLE_WRAP}><thead><tr>']
    for h in headers:
        out.append(f'<th {_TH}>{h}</th>')
    out.append('</tr></thead><tbody>')

    depth_color = {0: "#dc2626", 1: "#ea580c", 2: "#ca8a04", 3: "#65a30d", 99: "#94a3b8"}
    for i, row in enumerate(flight_rows):
        bg = "white" if i % 2 == 0 else "#fafafa"
        out.append(f'<tr style="background:{bg};">')
        out.append(f'<td style="padding:9px 14px;font-family:monospace;font-weight:700;color:#1d4ed8;font-size:13px;">{row["Flight"]}</td>')
        out.append(f'<td style="padding:9px 14px;font-family:monospace;color:#374151;font-size:13px;">{row["Route"]}</td>')
        out.append(f'<td {_TD}>{ATYPE_SHORT.get(row.get("Type",""), row.get("Type",""))}</td>')
        out.append(f'<td style="padding:9px 14px;font-weight:600;color:#0f172a;">{row["Pax"]}</td>')
        out.append(f'<td style="padding:9px 14px;font-family:monospace;color:#374151;">{row.get("Dep Time","")}</td>')
        out.append(f'<td style="padding:9px 14px;font-family:monospace;color:#374151;">{row.get("Arr Time","")}</td>')
        d = row.get("Cascade Depth", 99)
        dc = depth_color.get(d, "#94a3b8")
        out.append(f'<td style="padding:9px 14px;font-weight:700;color:{dc};">{d}</td>')
        out.append('</tr>')

    out.append('</tbody></table></div>')
    return ''.join(out)


def _html_disagree(rows: list) -> str:
    headers = ["Flight", "Route", "Type", "Pool Cost", "Greedy Cost", "Pool", "Greedy"]
    out = [f'<div style="overflow-x:auto;border:1px solid #fcd34d;border-radius:8px;background:#fffbeb;">',
           f'<table {_TABLE_WRAP}><thead><tr>']
    for h in headers:
        out.append(f'<th style="padding:10px 14px;text-align:left;font-weight:600;color:#92400e;font-size:12px;background:#fef3c7;border-bottom:2px solid #fcd34d;">{h}</th>')
    out.append('</tr></thead><tbody>')
    for i, row in enumerate(rows):
        bg = "white" if i % 2 == 0 else "#fffbeb"
        out.append(f'<tr style="background:{bg};">')
        out.append(f'<td style="padding:9px 14px;font-family:monospace;font-weight:700;color:#1d4ed8;font-size:13px;">{row["Flight"]}</td>')
        out.append(f'<td style="padding:9px 14px;font-family:monospace;color:#374151;font-size:13px;">{row["Route"]}</td>')
        out.append(f'<td {_TD}>{ATYPE_SHORT.get(row["Type"], row["Type"])}</td>')
        out.append(f'<td style="padding:9px 14px;font-weight:700;color:#0f172a;">${row["Pool Cost"]:,}</td>')
        out.append(f'<td style="padding:9px 14px;color:#64748b;">${row["Greedy Cost"]:,}</td>')
        for key in ("Pool Status", "Greedy Status"):
            s = row[key]
            b = _badge("COVERED", "green") if s == "COVERED" else _badge("UNCOVERED", "red")
            out.append(f'<td style="padding:9px 14px;">{b}</td>')
        out.append('</tr>')
    out.append('</tbody></table></div>')
    return ''.join(out)


def _label(text: str, sub: str = "") -> None:
    st.markdown(
        f'<p style="font-size:15px;font-weight:700;color:#0f172a;margin:20px 0 2px 0;">{text}</p>'
        + (f'<p style="font-size:12px;color:#94a3b8;margin:0 0 12px 0;">{sub}</p>' if sub else ""),
        unsafe_allow_html=True,
    )


def _metric_card(
    label: str,
    value: str,
    delta: str = None,
    delta_positive: bool = True,
    value_color: str = "#0f172a",
) -> str:
    delta_html = ""
    if delta:
        dc = "#15803d" if delta_positive else "#b91c1c"
        delta_html = (
            f'<p style="margin:5px 0 0 0;font-size:13px;font-weight:600;color:{dc};">{delta}</p>'
        )
    return (
        '<div style="background:white;border:1px solid #e2e8f0;border-radius:10px;'
        'padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,0.06);">'
        f'<p style="margin:0 0 6px 0;font-size:11px;font-weight:600;color:#64748b;'
        f'text-transform:uppercase;letter-spacing:0.07em;">{label}</p>'
        f'<p style="margin:0;font-size:28px;font-weight:700;color:{value_color};'
        f'letter-spacing:-0.02em;line-height:1;">{value}</p>'
        f'{delta_html}'
        '</div>'
    )


# ── DataFrame ↔ dataclass helpers ────────────────────────────────────────────
def flights_to_df(flights: list) -> pd.DataFrame:
    rows = []
    for f in flights:
        rows.append({
            "flight_id":       f.flight_id,
            "origin":          f.origin,
            "destination":     f.destination,
            "aircraft_type":   f.aircraft_type,
            "dep_time":        f.scheduled_departure.strftime("%H:%M"),
            "arr_time":        f.scheduled_arrival.strftime("%H:%M"),
            "passenger_count": f.passenger_count,
        })
    return pd.DataFrame(rows)


def df_to_flights(df: pd.DataFrame) -> list:
    flights = []
    for _, row in df.iterrows():
        dep = datetime.strptime(f"2025-06-15 {row['dep_time']}", "%Y-%m-%d %H:%M")
        arr = datetime.strptime(f"2025-06-15 {row['arr_time']}", "%Y-%m-%d %H:%M")
        if arr <= dep:
            arr += timedelta(days=1)
        atype = row["aircraft_type"]
        gate  = "widebody" if "widebody" in atype else atype
        fid   = str(row["flight_id"])
        flights.append(Flight(
            flight_id=fid,
            origin=str(row["origin"]).upper(),
            destination=str(row["destination"]).upper(),
            aircraft_type=atype,
            scheduled_departure=dep,
            scheduled_arrival=arr,
            passenger_count=int(row["passenger_count"]),
            tail_number=f"N{fid}",
            crew_required={"CA": f"ACT_CA_{fid}", "FO": f"ACT_FO_{fid}"},
            gate_type=gate,
        ))
    return flights


def crew_to_df(reserve_crew: list) -> pd.DataFrame:
    rows = []
    for c in reserve_crew:
        rows.append({
            "crew_id":        c.crew_id,
            "role":           c.role,
            "type_rating":    c.type_rating,
            "duty_start":     c.duty_start.strftime("%H:%M"),
            "segments_flown": c.segments_flown,
            "callout_status": c.callout_status,
        })
    return pd.DataFrame(rows)


def df_to_crew(df: pd.DataFrame, hub: str) -> list:
    crew = []
    for _, row in df.iterrows():
        duty_start = datetime.strptime(f"2025-06-15 {row['duty_start']}", "%Y-%m-%d %H:%M")
        crew.append(CrewMember(
            crew_id=str(row["crew_id"]),
            role=str(row["role"]),
            type_rating=str(row["type_rating"]),
            domicile=hub,
            current_station=hub,
            duty_start=duty_start,
            segments_flown=int(row["segments_flown"]),
            is_reserve=True,
            callout_status=str(row["callout_status"]),
        ))
    return crew


# ── Cached generators ─────────────────────────────────────────────────────────
@st.cache_data
def _gen_flights(hub, num_regional, num_narrowbody, num_wb_dom, num_wb_intl):
    return generate_flights(
        hub=hub, num_regional=num_regional, num_narrowbody=num_narrowbody,
        num_widebody_domestic=num_wb_dom, num_widebody_intl=num_wb_intl,
    )


@st.cache_data
def _gen_crew(hub, b737, b777, b787, e175, near_limit):
    return generate_crew_roster(
        hub=hub, b737_reserves=b737, b777_reserves=b777,
        b787_reserves=b787, e175_reserves=e175, near_limit_count=near_limit,
    )


@st.cache_resource
def get_anthropic_client():
    import anthropic
    return anthropic.Anthropic()


# ── Plotly network graph ──────────────────────────────────────────────────────
DEPTH_COLORS = {0: "#ef4444", 1: "#f97316", 2: "#eab308", 99: "#94a3b8"}
DEPTH_LABELS = {0: "Disrupted hub", 1: "Direct impact", 2: "Secondary cascade", 99: "Unaffected"}


def build_plotly_graph(G, affected_nodes, disrupted_node, all_flights, delay_minutes):
    pos = nx.spring_layout(G, seed=42, k=2.8)

    flights_by_airport: dict = {}
    for f in all_flights:
        for ap in (f.origin, f.destination):
            flights_by_airport.setdefault(ap, [])
            if f not in flights_by_airport[ap]:
                flights_by_airport[ap].append(f)

    edge_x, edge_y = [], []
    for u, v in G.edges():
        x0, y0 = pos[u]; x1, y1 = pos[v]
        edge_x += [x0, x1, None]; edge_y += [y0, y1, None]

    traces = [go.Scatter(
        x=edge_x, y=edge_y, mode="lines",
        line=dict(width=1.5, color="#cbd5e1"),
        hoverinfo="none", showlegend=False,
    )]

    nodes_by_depth: dict = {}
    for node in G.nodes():
        nodes_by_depth.setdefault(affected_nodes.get(node, 99), []).append(node)

    for depth in sorted(nodes_by_depth):
        nodes = nodes_by_depth[depth]
        hover_texts = []
        for node in nodes:
            node_flights = sorted(flights_by_airport.get(node, []), key=lambda f: f.scheduled_departure)
            flight_lines = "<br>".join(
                f"  {f.flight_id}  {f.origin}→{f.destination}  dep {f.scheduled_departure.strftime('%H:%M')}  ({f.passenger_count} pax)"
                for f in node_flights
            ) or "  No flights in window"
            hover_texts.append(
                f"<b>{node}</b><br>{DEPTH_LABELS.get(depth, f'Depth {depth}')}<br>"
                f"{'─'*12}<br>{flight_lines}"
            )
        traces.append(go.Scatter(
            x=[pos[n][0] for n in nodes], y=[pos[n][1] for n in nodes],
            mode="markers+text",
            name=DEPTH_LABELS.get(depth, f"Depth {depth}"),
            marker=dict(
                size=34 if depth == 0 else 22,
                color=DEPTH_COLORS.get(depth, "#94a3b8"),
                line=dict(width=2, color="white"),
            ),
            text=nodes, textposition="top center",
            textfont=dict(size=10, color="#1e293b", family="monospace"),
            hovertext=hover_texts, hoverinfo="text",
            customdata=nodes,
        ))

    return go.Figure(
        data=traces,
        layout=go.Layout(
            title=dict(
                text=f"{disrupted_node} Ground Stop — BFS Cascade  ({delay_minutes}-min delay)",
                font=dict(size=14, color="#0f172a"),
            ),
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                        font=dict(size=12)),
            hovermode="closest",
            margin=dict(b=10, l=10, r=10, t=55),
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            height=520,
            plot_bgcolor="#f8fafc",
            paper_bgcolor="white",
        ),
    )


# ── Session state init ────────────────────────────────────────────────────────
if "current_preset" not in st.session_state:
    st.session_state.current_preset = "ORD — Triage Demo"

if "flights_df" not in st.session_state or "crew_df" not in st.session_state:
    p = PRESETS["ORD — Triage Demo"]
    raw_flights, raw_crew = generate_triage_scenario(p["hub"])
    st.session_state.flights_df = flights_to_df(raw_flights)
    st.session_state.crew_df    = crew_to_df(raw_crew)

if "ran" not in st.session_state:
    st.session_state.ran = False

if "dispatcher_decisions" not in st.session_state:
    st.session_state.dispatcher_decisions = {}


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        '<h2 style="margin:0;font-size:20px;font-weight:800;color:#f1f5f9;letter-spacing:-0.02em;">dCortex</h2>'
        '<p style="margin:2px 0 0 0;font-size:11px;color:#64748b;font-weight:500;letter-spacing:0.05em;text-transform:uppercase;">Disruption Intelligence</p>',
        unsafe_allow_html=True,
    )
    st.divider()

    st.markdown('<p style="font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.08em;margin:0 0 6px 0;">Scenario</p>', unsafe_allow_html=True)
    preset_name = st.selectbox("Scenario", options=list(PRESETS.keys()),
                                index=list(PRESETS.keys()).index(st.session_state.current_preset),
                                label_visibility="collapsed")
    p = PRESETS[preset_name]
    st.markdown(f'<p style="font-size:12px;color:#64748b;margin:4px 0 10px 0;">{p["description"]}</p>', unsafe_allow_html=True)
    load_preset_btn = st.button("Load Preset", use_container_width=True)

    if load_preset_btn:
        if p.get("triage"):
            raw_flights, raw_crew = generate_triage_scenario(p["hub"])
        else:
            raw_flights = _gen_flights(p["hub"], p["num_regional"], p["num_narrowbody"], p["num_wb_dom"], p["num_wb_intl"])
            _, raw_crew = _gen_crew(p["hub"], p["b737_res"], p["b777_res"], p["b787_res"], p["e175_res"], p["near_limit"])
        st.session_state.flights_df      = flights_to_df(raw_flights)
        st.session_state.crew_df         = crew_to_df(raw_crew)
        st.session_state.current_preset  = preset_name
        st.session_state.ran             = False
        st.rerun()

    st.divider()

    with st.expander("Disruption Event", expanded=True):
        hub         = st.text_input("Hub (IATA)", value=p["hub"], max_chars=3).upper()
        gs_hour     = st.slider("Start hour", 6, 20, p["gs_hour"])
        gs_duration = st.slider("Duration (min)", 15, 60, p["gs_duration"], step=5)

    with st.expander("Severity", expanded=True):
        scenario_choice = st.radio("Scenario", list(SCENARIO_DELAYS.keys()),
                                   index=list(SCENARIO_DELAYS.keys()).index(p["scenario"]),
                                   horizontal=True)
        custom_delay  = st.slider("Custom delay (min)", 30, 120, 60, step=5,
                                  disabled=(scenario_choice != "Custom"))
        delay_minutes = custom_delay if scenario_choice == "Custom" else SCENARIO_DELAYS[scenario_choice]

    with st.expander("Cost Model"):
        cost_wb     = st.slider("Widebody $/min",         100, 300, p["cost_wb"],     step=5)
        cost_wb_dom = st.slider("Widebody-Dom $/min",      80, 200, p["cost_wb_dom"], step=5)
        cost_nb     = st.slider("Narrowbody $/min",         50, 150, p["cost_nb"],     step=1)
        cost_reg    = st.slider("Regional $/min",           20,  80, p["cost_reg"],    step=1)
        cost_pax    = st.slider("Reaccommodation $/pax",  500, 2000, p["cost_pax"],   step=50)

    with st.expander("Gate Availability"):
        st.markdown('<p style="font-size:11px;color:#94a3b8;margin-bottom:8px;">Available gates at hub. Shortages force uncovered status.</p>', unsafe_allow_html=True)
        gates_wb  = st.number_input("Widebody",   min_value=1, max_value=30, value=p.get("gates_wb",  8), step=1)
        gates_nb  = st.number_input("Narrowbody", min_value=1, max_value=50, value=p.get("gates_nb", 15), step=1)
        gates_reg = st.number_input("Regional",   min_value=1, max_value=50, value=p.get("gates_reg", 20), step=1)

    st.divider()
    run_btn     = st.button("Run Simulation",         type="primary",   use_container_width=True)
    narrate_btn = st.button("Generate LLM Narration", type="secondary", use_container_width=True)
    st.markdown('<p style="font-size:11px;color:#475569;text-align:center;margin-top:4px;">Narration calls Claude API</p>', unsafe_allow_html=True)


# ── Page header ───────────────────────────────────────────────────────────────
st.markdown(
    '<h1 style="font-size:28px;font-weight:800;color:#0f172a;margin-bottom:0;letter-spacing:-0.02em;">dCortex</h1>'
    '<p style="color:#64748b;margin-top:2px;font-size:14px;">Airline disruption management · pool-aware crew allocation · FAA Part 117</p>',
    unsafe_allow_html=True,
)

tab_setup, tab_cascade, tab_allocation, tab_trace, tab_outcomes = st.tabs(
    ["  Setup  ", "  Cascade  ", "  Allocation  ", "  Trace  ", "  Outcomes  "]
)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — SETUP
# ══════════════════════════════════════════════════════════════════════════════
with tab_setup:
    st.markdown('<p style="color:#64748b;font-size:13px;margin-bottom:20px;">Edit flights and crew directly in the tables below. Changes take effect when you click Run Simulation.</p>', unsafe_allow_html=True)

    col_l, col_r = st.columns(2, gap="large")

    with col_l:
        _label("Flights", f"{len(st.session_state.flights_df)} scheduled — edit routes, times, pax")
        edited_flights_df = st.data_editor(
            st.session_state.flights_df,
            key="flights_editor",
            use_container_width=True,
            num_rows="dynamic",
            column_config={
                "flight_id":       st.column_config.TextColumn("Flight", width="small"),
                "origin":          st.column_config.TextColumn("Origin", width="small", max_chars=3),
                "destination":     st.column_config.TextColumn("Dest", width="small", max_chars=3),
                "aircraft_type":   st.column_config.SelectboxColumn("Type", options=AIRCRAFT_TYPES, width="medium"),
                "dep_time":        st.column_config.TextColumn("Dep", width="small"),
                "arr_time":        st.column_config.TextColumn("Arr", width="small"),
                "passenger_count": st.column_config.NumberColumn("Pax", min_value=1, max_value=600, width="small"),
            },
            hide_index=True,
        )
        st.session_state.flights_df = edited_flights_df

    with col_r:
        _label("Reserve Crew", f"{len(st.session_state.crew_df)} available — edit ratings, duty starts, roles")
        edited_crew_df = st.data_editor(
            st.session_state.crew_df,
            key="crew_editor",
            use_container_width=True,
            num_rows="dynamic",
            column_config={
                "crew_id":         st.column_config.TextColumn("Crew ID"),
                "role":            st.column_config.SelectboxColumn("Role", options=CREW_ROLES, width="small"),
                "type_rating":     st.column_config.SelectboxColumn("Rating", options=TYPE_RATINGS, width="small"),
                "duty_start":      st.column_config.TextColumn("Duty Start", width="small"),
                "segments_flown":  st.column_config.NumberColumn("Segs", min_value=0, max_value=10, width="small"),
                "callout_status":  st.column_config.SelectboxColumn("Callout", options=CALLOUT_STATUSES),
            },
            hide_index=True,
        )
        st.session_state.crew_df = edited_crew_df

    st.markdown(
        '<p style="font-size:12px;color:#94a3b8;margin-top:16px;">Add rows with the + button. Select a row and press Backspace to delete. Load a preset from the sidebar to reset.</p>',
        unsafe_allow_html=True,
    )


# ── Run simulation ────────────────────────────────────────────────────────────
if run_btn:
    with st.spinner("Running simulation..."):
        try:
            flights      = df_to_flights(st.session_state.flights_df)
            reserve_crew = df_to_crew(st.session_state.crew_df, hub)
        except Exception as e:
            st.error(f"Data error: {e}. Check flight times are HH:MM format.")
            st.stop()

        G                = build_flight_graph(flights)
        affected_nodes   = bfs_cascade(G, disrupted_node=hub, max_depth=5)
        affected_flights = get_affected_flights(G, affected_nodes, flights)

        gs_start_time = datetime(2025, 6, 15, gs_hour, 0, 0)

        # Drop flights whose delayed departure would still be before the ground stop starts.
        # These have already departed and cannot be re-crewed.
        affected_flights = [
            f for f in affected_flights
            if f.scheduled_departure + timedelta(minutes=delay_minutes) > gs_start_time
        ]

        cost_overrides = {
            "widebody": float(cost_wb), "widebody_domestic": float(cost_wb_dom),
            "narrowbody": float(cost_nb), "regional": float(cost_reg),
        }
        hub_gates    = {"widebody": int(gates_wb), "narrowbody": int(gates_nb), "regional": int(gates_reg)}
        crew_agent   = CrewAgent(reserve_pool=reserve_crew)
        acft_agent   = AircraftAgent(cost_overrides=cost_overrides)
        pax_agent    = PassengerAgent(reaccommodation_cost=float(cost_pax))
        coordinator  = CoordinatingAgent(crew_agent, acft_agent, pax_agent,
                                         get_anthropic_client(), hub=hub, hub_gates=hub_gates)

        pool_aware    = coordinator.allocate_pool_aware(affected_flights, gs_start_time, delay_minutes)
        greedy        = coordinator.allocate_greedy(affected_flights, gs_start_time, delay_minutes)

        disruption = DisruptionEvent(
            event_id=f"EVT_{hub}_20250615_{scenario_choice}",
            hub=hub, start_time=gs_start_time,
            duration_minutes=gs_duration,
            effective_window_minutes=delay_minutes * 2,
            scenario=scenario_choice,
        )
        cascade_graph_data = {
            "nodes": list(G.nodes()),
            "edges": [{"from": u, "to": v,
                       "flight_id": d.get("flight_id"),
                       "aircraft_type": d.get("aircraft_type")}
                      for u, v, d in G.edges(data=True)],
        }
        trace, trace_filepath = emit_trace(
            disruption=disruption,
            cascade_graph_data=cascade_graph_data,
            affected_flights=affected_flights,
            pool_aware_options=pool_aware,
            greedy_options=greedy,
            selected_options=[o for o in pool_aware if o.feasible],
            llm_narration="",
            traces_dir="traces",
        )
        st.session_state.update(dict(
            ran=True, G=G, affected_nodes=affected_nodes,
            affected_flights=affected_flights, pool_aware=pool_aware,
            greedy=greedy, disruption=disruption,
            cascade_graph_data=cascade_graph_data,
            trace=trace, trace_filepath=trace_filepath,
            llm_narration=None, delay_minutes=delay_minutes,
            scenario_label=scenario_choice, hub=hub,
            reserve_crew=reserve_crew, selected_airport=None,
            dispatcher_decisions={},
        ))
    st.success(f"Simulation complete — {len(affected_flights)} flights affected.")


# ── LLM narration ─────────────────────────────────────────────────────────────
if narrate_btn:
    if not st.session_state.ran:
        st.sidebar.warning("Run the simulation first.")
    else:
        with st.spinner("Calling Claude API..."):
            cost_overrides = {
                "widebody": float(cost_wb), "widebody_domestic": float(cost_wb_dom),
                "narrowbody": float(cost_nb), "regional": float(cost_reg),
            }
            hub_gates   = {"widebody": int(gates_wb), "narrowbody": int(gates_nb), "regional": int(gates_reg)}
            crew_agent  = CrewAgent(reserve_pool=st.session_state.reserve_crew)
            acft_agent  = AircraftAgent(cost_overrides=cost_overrides)
            pax_agent   = PassengerAgent(reaccommodation_cost=float(cost_pax))
            coordinator = CoordinatingAgent(crew_agent, acft_agent, pax_agent,
                                            get_anthropic_client(),
                                            hub=st.session_state.hub, hub_gates=hub_gates)
            narration = coordinator.narrate(st.session_state.pool_aware,
                                            st.session_state.greedy,
                                            st.session_state.disruption)
            st.session_state.llm_narration = narration
            trace, fp = emit_trace(
                disruption=st.session_state.disruption,
                cascade_graph_data=st.session_state.cascade_graph_data,
                affected_flights=st.session_state.affected_flights,
                pool_aware_options=st.session_state.pool_aware,
                greedy_options=st.session_state.greedy,
                selected_options=[o for o in st.session_state.pool_aware if o.feasible],
                llm_narration=narration, traces_dir="traces",
            )
            st.session_state.trace = trace


# ── Guard: results tabs need a completed run ──────────────────────────────────
if not st.session_state.ran:
    for _tab in (tab_cascade, tab_allocation, tab_trace):
        with _tab:
            st.info("Configure a scenario in the sidebar, then click Run Simulation.")
    st.stop()

# Unpack session state
G                    = st.session_state.G
affected_nodes       = st.session_state.affected_nodes
affected_flights     = st.session_state.affected_flights
pool_aware           = st.session_state.pool_aware
greedy               = st.session_state.greedy
trace                = st.session_state.trace
delay_minutes        = st.session_state.delay_minutes
scenario_label       = st.session_state.scenario_label
llm_narration        = st.session_state.get("llm_narration")
reserve_crew         = st.session_state.reserve_crew
hub_used             = st.session_state.hub
dispatcher_decisions = st.session_state.get("dispatcher_decisions", {})

pool_by_fid   = {o.flight_id: o for o in pool_aware}
greedy_by_fid = {o.flight_id: o for o in greedy}
flight_by_fid = {f.flight_id: f for f in affected_flights}


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — CASCADE
# ══════════════════════════════════════════════════════════════════════════════
with tab_cascade:
    depth_1 = [ap for ap, d in affected_nodes.items() if d == 1]
    depth_2 = [ap for ap, d in affected_nodes.items() if d == 2]

    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(_metric_card("Airports Affected", str(len(affected_nodes))), unsafe_allow_html=True)
    c2.markdown(_metric_card("Flights Affected",  str(len(affected_flights))), unsafe_allow_html=True)
    c3.markdown(_metric_card("Depth-1 Airports",  str(len(depth_1))), unsafe_allow_html=True)
    c4.markdown(_metric_card("Depth-2 Airports",  str(len(depth_2))), unsafe_allow_html=True)
    st.divider()

    fig = build_plotly_graph(G, affected_nodes, hub_used, affected_flights, delay_minutes)
    try:
        event = st.plotly_chart(fig, use_container_width=True,
                                on_select="rerun", selection_mode="points",
                                key="cascade_graph")
        clicked = (event.selection.get("points", [])
                   if event and hasattr(event, "selection") and event.selection else [])
        if clicked:
            ap = clicked[0].get("customdata")
            if ap:
                st.session_state.selected_airport = ap
    except TypeError:
        st.plotly_chart(fig, use_container_width=True)

    st.markdown('<p style="font-size:12px;color:#94a3b8;margin-bottom:20px;">Hover nodes to see flights. Click a node to filter the table below.</p>', unsafe_allow_html=True)
    st.divider()

    def _depth(f):
        c = [d for d in [affected_nodes.get(f.origin), affected_nodes.get(f.destination)] if d is not None]
        return min(c) if c else 99

    selected_airport = st.session_state.get("selected_airport")
    if selected_airport:
        display_flights = [f for f in affected_flights
                           if f.origin == selected_airport or f.destination == selected_airport]
        hcol, bcol = st.columns([5, 1])
        hcol.markdown(f'<p style="font-size:15px;font-weight:700;color:#0f172a;">Flights at {selected_airport} ({len(display_flights)})</p>', unsafe_allow_html=True)
        if bcol.button("Clear filter", use_container_width=True):
            st.session_state.selected_airport = None
            st.rerun()
    else:
        display_flights = affected_flights
        _label(f"All Affected Flights", f"{len(display_flights)} in disruption window")

    flight_rows = [{
        "Flight": f.flight_id,
        "Route":  f"{f.origin}→{f.destination}",
        "Type":   f.aircraft_type,
        "Pax":    f.passenger_count,
        "Dep Time": f.scheduled_departure.strftime("%H:%M"),
        "Arr Time": f.scheduled_arrival.strftime("%H:%M"),
        "Cascade Depth": _depth(f),
    } for f in sorted(display_flights, key=lambda x: x.scheduled_departure)]
    st.markdown(_html_flights(flight_rows), unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — ALLOCATION
# ══════════════════════════════════════════════════════════════════════════════
with tab_allocation:
    pool_total   = sum(o.total_cost for o in pool_aware)
    greedy_total = sum(o.total_cost for o in greedy)
    savings      = greedy_total - pool_total
    n_uncov_pool = sum(1 for o in pool_aware if not o.feasible)

    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(_metric_card("Pool-Aware Total", f"${pool_total:,.0f}"), unsafe_allow_html=True)
    c2.markdown(_metric_card("Greedy Total",     f"${greedy_total:,.0f}"), unsafe_allow_html=True)
    c3.markdown(
        _metric_card("Pool-Aware Saves", f"${abs(savings):,.0f}",
                     delta=f"${savings:+,.0f}", delta_positive=(savings >= 0)),
        unsafe_allow_html=True,
    )
    uncov_color = "#b91c1c" if n_uncov_pool > 0 else "#15803d"
    c4.markdown(
        _metric_card("Uncovered (pool)", str(n_uncov_pool), value_color=uncov_color),
        unsafe_allow_html=True,
    )
    st.divider()

    # Build rows
    rows, disagree_rows = [], []
    for fid in sorted(set(pool_by_fid) | set(greedy_by_fid)):
        po = pool_by_fid.get(fid)
        gr = greedy_by_fid.get(fid)
        f  = flight_by_fid.get(fid)
        if not (po and gr and f):
            continue
        row = {
            "Flight":        fid,
            "Route":         f"{f.origin}→{f.destination}",
            "Type":          f.aircraft_type,
            "Pool Cost":     round(po.total_cost),
            "Greedy Cost":   round(gr.total_cost),
            "Ferry Cost":    round(po.ferry_cost),
            "Pool Status":   "COVERED" if po.feasible else "UNCOVERED",
            "Greedy Status": "COVERED" if gr.feasible else "UNCOVERED",
        }
        rows.append(row)
        if po.feasible != gr.feasible:
            disagree_rows.append(row)

    if disagree_rows:
        _label("Strategy Disagreement", "Flights covered by one strategy but not the other")
        st.markdown(_html_disagree(disagree_rows), unsafe_allow_html=True)
        st.divider()
    else:
        st.success("Both strategies produce identical coverage for all flights.")
        st.divider()

    _label("Full Allocation Table", "Pool-aware vs greedy cost comparison — uncovered rows highlighted")
    st.markdown(_html_allocation(rows), unsafe_allow_html=True)
    st.markdown(
        '<p style="font-size:11px;color:#94a3b8;margin-top:6px;">Ferry Cost: expected repositioning cost for uncovered inbound flights (30% diversion probability applied).</p>',
        unsafe_allow_html=True,
    )

    st.divider()
    _label("Part 117 FDP — Reserve Crew", "Flight Duty Period status at ground stop start time")
    gs_time  = st.session_state.disruption.start_time
    fdp_rows = []
    for c in reserve_crew:
        elapsed_h = (gs_time - c.duty_start).total_seconds() / 3600
        max_h     = max_fdp(c.duty_start.hour)
        rem_h     = remaining_fdp(c, gs_time)
        status    = "OVER LIMIT" if rem_h < 1.0 else ("NEAR LIMIT" if rem_h < 3.0 else "OK")
        fdp_rows.append({
            "Crew ID":      c.crew_id,
            "Role":         c.role,
            "Rating":       c.type_rating,
            "Duty Start":   c.duty_start.strftime("%H:%M"),
            "Max FDP (h)":  round(max_h, 1),
            "Elapsed (h)":  round(elapsed_h, 1),
            "Remaining (h)": round(rem_h, 1),
            "Status":       status,
        })
    st.markdown(_html_fdp(fdp_rows), unsafe_allow_html=True)

    if llm_narration:
        st.divider()
        _label("Dispatcher Recommendation")
        import re
        _nar = llm_narration.replace("$", "&#36;")
        _nar = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', _nar)
        _nar = re.sub(r'\*(.*?)\*',     r'<em>\1</em>',          _nar)
        _nar = _nar.replace("\n\n", '</p><p style="margin:10px 0 0 0;">').replace("\n", "<br>")
        st.markdown(
            f'<div style="background:#f8fafc;border:1px solid #e2e8f0;border-left:4px solid #3b82f6;'
            f'border-radius:6px;padding:16px 20px;font-size:14px;color:#1e293b;line-height:1.75;">'
            f'<p style="margin:0;">{_nar}</p></div>',
            unsafe_allow_html=True,
        )

    st.divider()
    _label("Dispatcher Review", "Accept or override each pool-aware recommendation — decisions are written to the trace")
    review_rows = []
    for o in pool_aware:
        f = flight_by_fid.get(o.flight_id)
        existing = dispatcher_decisions.get(o.flight_id, {})
        review_rows.append({
            "Flight":          o.flight_id,
            "Route":           f"{f.origin}→{f.destination}" if f else "—",
            "Recommendation":  "COVERED" if o.feasible else "UNCOVERED",
            "Action":          existing.get("action", "Accept"),
            "Override Reason": existing.get("reason", ""),
        })

    edited_review = st.data_editor(
        pd.DataFrame(review_rows),
        key="dispatcher_review_editor",
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "Flight":          st.column_config.TextColumn("Flight", disabled=True, width="small"),
            "Route":           st.column_config.TextColumn("Route", disabled=True, width="small"),
            "Recommendation":  st.column_config.TextColumn("Rec.", disabled=True, width="small"),
            "Action":          st.column_config.SelectboxColumn(
                "Action", options=["Accept", "Override", "Escalate"], width="small"),
            "Override Reason": st.column_config.TextColumn("Reason (if overriding)", width="large"),
        },
        hide_index=True,
    )

    if st.button("Record Dispatcher Decision", use_container_width=True):
        decisions = {
            row["Flight"]: {"action": row["Action"], "reason": row["Override Reason"]}
            for _, row in edited_review.iterrows()
        }
        st.session_state.dispatcher_decisions = decisions
        fp = st.session_state.get("trace_filepath")
        if fp and os.path.exists(fp):
            try:
                updated = update_trace_decisions(fp, decisions)
                st.session_state.trace = updated
                st.success("Dispatcher decisions recorded in trace.")
            except Exception as e:
                st.warning(f"Trace update failed: {e}")
        else:
            st.success("Decisions saved (no trace file to patch).")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — TRACE
# ══════════════════════════════════════════════════════════════════════════════
with tab_trace:
    _label("Reasoning Trace", "Full JSON emitted on every run — actual_cost filled in the Outcomes tab")
    trace_str = json.dumps(trace, indent=2, default=str)

    col_dl, col_info = st.columns([2, 3])
    col_dl.download_button(
        "Download JSON",
        data=trace_str,
        file_name=f"{trace.get('trace_id', 'trace')}.json",
        mime="application/json",
        use_container_width=True,
    )
    col_info.markdown(
        f'<p style="font-size:12px;color:#64748b;padding-top:8px;">trace_id: <code>{trace.get("trace_id","—")}</code></p>',
        unsafe_allow_html=True,
    )
    st.json(trace, expanded=False)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — OUTCOMES
# ══════════════════════════════════════════════════════════════════════════════
with tab_outcomes:
    _label("Post-Disruption Outcomes",
           "Fill this in ~90 minutes after the disruption window closes. Actual vs predicted cost calibrates the model.")

    if not st.session_state.ran:
        st.info("Run a simulation first.")
    else:
        existing_outcome = st.session_state.trace.get("outcome")
        if existing_outcome:
            st.success(
                f"Outcomes already recorded at {existing_outcome.get('recorded_at','')[:16]}. "
                "Submit again to overwrite."
            )
            with st.expander("View recorded outcome"):
                st.json(existing_outcome)

        predicted = st.session_state.trace.get("predicted_cost", 0) or 0
        st.markdown(
            f'<p style="font-size:14px;color:#374151;margin-bottom:20px;">'
            f'Predicted cost (pool-aware): <strong style="color:#0f172a;">${predicted:,.0f}</strong></p>',
            unsafe_allow_html=True,
        )

        with st.form("outcome_form"):
            a_col, n_col = st.columns([1, 2])
            actual_cost = a_col.number_input(
                "Actual total cost ($)", min_value=0, max_value=10_000_000,
                value=int(predicted), step=1000,
            )
            notes = n_col.text_area(
                "Notes",
                placeholder="e.g. ground stop lifted 30 min early; UA205 cancelled by ops",
                height=80,
            )

            st.divider()
            _label("Per-flight outcomes")
            outcome_options = ["covered", "delayed", "cancelled", "diverted", "unknown"]
            per_flight_out  = {}
            cols_per_row    = 2
            flights_sorted  = sorted(affected_flights, key=lambda x: x.scheduled_departure)

            for i in range(0, len(flights_sorted), cols_per_row):
                row_cols = st.columns(cols_per_row)
                for j, f in enumerate(flights_sorted[i:i + cols_per_row]):
                    po      = pool_by_fid.get(f.flight_id)
                    default = "covered" if (po and po.feasible) else "delayed"
                    with row_cols[j]:
                        st.markdown(
                            f'<p style="font-size:12px;font-weight:600;color:#374151;margin-bottom:2px;font-family:monospace;">'
                            f'{f.flight_id}  {f.origin}→{f.destination}</p>'
                            f'<p style="font-size:11px;color:#94a3b8;margin-top:0;">{f.passenger_count} pax · dep {f.scheduled_departure.strftime("%H:%M")}</p>',
                            unsafe_allow_html=True,
                        )
                        per_flight_out[f.flight_id] = st.selectbox(
                            f"out_{f.flight_id}", outcome_options,
                            index=outcome_options.index(default),
                            label_visibility="collapsed",
                            key=f"pf_{f.flight_id}",
                        )

            submitted = st.form_submit_button("Submit Outcomes", type="primary", use_container_width=True)

        if submitted:
            fp = st.session_state.get("trace_filepath")
            if fp and os.path.exists(fp):
                try:
                    updated = update_trace_outcome(
                        fp, actual_cost=float(actual_cost),
                        notes=notes, per_flight=per_flight_out,
                    )
                    st.session_state.trace = updated
                    variance = actual_cost - predicted
                    st.success(
                        f"Outcomes recorded. actual_cost = ${actual_cost:,.0f} · "
                        f"variance: {'+' if variance >= 0 else ''}${variance:,.0f} vs predicted"
                    )
                except Exception as e:
                    st.error(f"Failed to update trace: {e}")
            else:
                st.warning("No trace file on disk. Run simulation first.")
