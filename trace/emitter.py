import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import datetime
from models import DisruptionEvent, AllocationOption, Flight


def _option_to_dict(o: AllocationOption) -> dict:
    return {
        "flight_id": o.flight_id,
        "option_id": o.option_id,
        "reserve_ca": o.reserve_ca,
        "reserve_fo": o.reserve_fo,
        "estimated_delay_minutes": o.estimated_delay_minutes,
        "aircraft_cost": round(o.aircraft_cost, 2),
        "passenger_cost": round(o.passenger_cost, 2),
        "crew_activation_cost": round(o.crew_activation_cost, 2),
        "ferry_cost": round(o.ferry_cost, 2),
        "dot_fine": round(o.dot_fine, 2),
        "soft_constraint_penalties": o.soft_constraint_penalties,
        "total_cost": round(o.total_cost, 2),
        "hard_constraints_checked": o.hard_constraints_checked,
        "hard_constraints_violated": o.hard_constraints_violated,
        "feasible": o.feasible,
    }


def emit_trace(
    disruption: DisruptionEvent,
    cascade_graph_data: dict,
    affected_flights: list,
    pool_aware_options: list,
    greedy_options: list,
    selected_options: list,
    llm_narration: str,
    override_reason: str = None,
    dispatcher_decisions: dict = None,
    traces_dir: str = "traces",
) -> tuple:
    """
    Write a complete reasoning trace to disk as JSON.
    actual_cost is always null at emit time; filled in post-outcome via update_trace_outcome().
    """
    os.makedirs(traces_dir, exist_ok=True)
    trace_id = f"trace_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    pool_cost = sum(o.total_cost for o in pool_aware_options)
    greedy_cost = sum(o.total_cost for o in greedy_options)

    trace = {
        "trace_id": trace_id,
        "timestamp": datetime.now().isoformat(),
        "disruption_event": {
            "event_id": disruption.event_id,
            "hub": disruption.hub,
            "start_time": disruption.start_time.isoformat(),
            "duration_minutes": disruption.duration_minutes,
            "effective_window_minutes": disruption.effective_window_minutes,
            "scenario": disruption.scenario,
        },
        "cascade_graph": cascade_graph_data,
        "affected_flights": [f.flight_id for f in affected_flights],
        "options_generated": {
            "pool_aware": [_option_to_dict(o) for o in pool_aware_options],
            "greedy": [
                {
                    "flight_id": o.flight_id,
                    "feasible": o.feasible,
                    "total_cost": round(o.total_cost, 2),
                    "reserve_ca": o.reserve_ca,
                    "reserve_fo": o.reserve_fo,
                }
                for o in greedy_options
            ],
        },
        "allocation_comparison": {
            "pool_aware_total_cost": round(pool_cost, 2),
            "greedy_total_cost": round(greedy_cost, 2),
            "delta": round(greedy_cost - pool_cost, 2),
            "pool_aware_uncovered": len([o for o in pool_aware_options if not o.feasible]),
            "greedy_uncovered": len([o for o in greedy_options if not o.feasible]),
        },
        "option_selected": selected_options[0].option_id if selected_options else None,
        "predicted_cost": round(sum(o.total_cost for o in selected_options), 2),
        "dispatcher_decisions": dispatcher_decisions or {},
        "actual_cost": None,      # filled post-outcome via update_trace_outcome()
        "outcome": None,          # filled post-outcome via update_trace_outcome()
        "override_reason": override_reason,
        "llm_narration": llm_narration,
    }

    filepath = os.path.join(traces_dir, f"{trace_id}.json")
    with open(filepath, "w") as f:
        json.dump(trace, f, indent=2, default=str)

    return trace, filepath


def update_trace_decisions(filepath: str, decisions: dict) -> dict:
    """Patch dispatcher decisions into an existing trace file in place."""
    with open(filepath) as f:
        trace = json.load(f)
    trace["dispatcher_decisions"] = decisions
    with open(filepath, "w") as f:
        json.dump(trace, f, indent=2, default=str)
    return trace


def update_trace_outcome(
    filepath: str,
    actual_cost: float,
    notes: str,
    per_flight: dict,
) -> dict:
    """Patch post-disruption outcomes into an existing trace file in place."""
    with open(filepath) as f:
        trace = json.load(f)
    trace["actual_cost"] = actual_cost
    trace["outcome"] = {
        "recorded_at": datetime.now().isoformat(),
        "actual_total_cost": actual_cost,
        "notes": notes,
        "per_flight_outcomes": per_flight,
    }
    with open(filepath, "w") as f:
        json.dump(trace, f, indent=2, default=str)
    return trace
