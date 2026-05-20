import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import deque
from models import Flight

try:
    import networkx as nx
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    _HAS_VIZ = True
except ImportError:
    _HAS_VIZ = False

# Depth → color for cascade visualization
DEPTH_COLORS = {
    0: "#e74c3c",   # red   — disrupted hub
    1: "#e67e22",   # orange — direct impact
    2: "#f1c40f",   # yellow — secondary cascade
    3: "#2ecc71",   # green  — tertiary
    4: "#3498db",   # blue   — deep cascade
    5: "#9b59b6",   # purple — distant
}


def build_flight_graph(flights: list) -> object:
    if not _HAS_VIZ:
        raise ImportError("networkx not installed")

    G = nx.DiGraph()
    for flight in flights:
        G.add_node(flight.origin, node_type="airport")
        G.add_node(flight.destination, node_type="airport")
        G.add_edge(
            flight.origin,
            flight.destination,
            flight_id=flight.flight_id,
            aircraft_type=flight.aircraft_type,
            scheduled_departure=flight.scheduled_departure,
            tail_number=flight.tail_number,
        )
    return G


def bfs_cascade(G, disrupted_node: str, max_depth: int = 5) -> dict:
    """
    Bidirectional BFS from the disrupted node.
    Returns {airport: cascade_depth} for all reachable nodes.
    """
    affected = {disrupted_node: 0}
    queue = deque([(disrupted_node, 0)])

    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue

        # downstream: airports this node's flights go to
        for successor in G.successors(node):
            if successor not in affected:
                affected[successor] = depth + 1
                queue.append((successor, depth + 1))

        # upstream: airports whose flights come here
        for predecessor in G.predecessors(node):
            if predecessor not in affected:
                affected[predecessor] = depth + 1
                queue.append((predecessor, depth + 1))

    return affected


def get_affected_flights(G, affected_nodes: dict, flights: list) -> list:
    """Return all flights where origin or destination is in the affected set."""
    affected_ids = set()
    for u, v, data in G.edges(data=True):
        if u in affected_nodes or v in affected_nodes:
            affected_ids.add(data["flight_id"])
    return [f for f in flights if f.flight_id in affected_ids]


def print_cascade_report(
    affected_nodes: dict, affected_flights: list, disruption_duration: int
) -> None:
    effective_window = disruption_duration * 3
    print("=" * 60)
    print("CASCADE ANALYSIS")
    print("=" * 60)
    print(f"Ground stop duration   : {disruption_duration} min")
    print(f"Effective window (est) : {effective_window}–{disruption_duration * 5} min")
    print(f"Airports affected      : {len(affected_nodes)}")
    print(f"Flights affected       : {len(affected_flights)}")

    by_depth = {}
    for airport, depth in affected_nodes.items():
        by_depth.setdefault(depth, []).append(airport)

    print("\nCascade depth:")
    for depth in sorted(by_depth):
        label = {0: "disrupted hub", 1: "direct impact", 2: "secondary"}.get(
            depth, f"depth-{depth}"
        )
        print(f"  Depth {depth} ({label:15s}): {', '.join(sorted(by_depth[depth]))}")

    print("\nAffected flights by depth:")
    for f in sorted(affected_flights, key=lambda x: x.scheduled_departure):
        depth_o = affected_nodes.get(f.origin, "-")
        depth_d = affected_nodes.get(f.destination, "-")
        min_depth = min(
            (d for d in [depth_o, depth_d] if isinstance(d, int)), default="-"
        )
        print(
            f"  {f.flight_id:8s} {f.origin}→{f.destination}  "
            f"{f.aircraft_type:20s}  {f.passenger_count:3d} pax  "
            f"dep {f.scheduled_departure.strftime('%H:%M')}  "
            f"cascade depth {min_depth}"
        )
    print("=" * 60)


def draw_cascade_graph(
    G,
    affected_nodes: dict,
    disrupted_node: str,
    output_path: str = "cascade_graph.png",
) -> None:
    if not _HAS_VIZ:
        print("matplotlib/networkx not available — skipping graph draw")
        return

    fig, ax = plt.subplots(figsize=(16, 10))

    node_colors = [
        DEPTH_COLORS.get(affected_nodes.get(n, 99), "#cccccc")
        for n in G.nodes()
    ]
    node_sizes = [
        2500 if n == disrupted_node else 1200
        for n in G.nodes()
    ]

    pos = nx.spring_layout(G, seed=42, k=2.5)

    nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=node_sizes, ax=ax)
    nx.draw_networkx_labels(G, pos, font_size=8, font_weight="bold", ax=ax)

    edge_labels = {
        (u, v): f"{d['flight_id']}\n({d['aircraft_type'][:3]})"
        for u, v, d in G.edges(data=True)
    }
    nx.draw_networkx_edges(
        G, pos, edge_color="#555555", arrows=True,
        arrowsize=20, connectionstyle="arc3,rad=0.1", ax=ax
    )
    nx.draw_networkx_edge_labels(
        G, pos, edge_labels=edge_labels, font_size=6, ax=ax
    )

    legend_patches = [
        mpatches.Patch(color=DEPTH_COLORS[0], label="Depth 0 — Disrupted hub"),
        mpatches.Patch(color=DEPTH_COLORS[1], label="Depth 1 — Direct impact"),
        mpatches.Patch(color=DEPTH_COLORS[2], label="Depth 2 — Secondary cascade"),
        mpatches.Patch(color="#cccccc",       label="Not affected"),
    ]
    ax.legend(handles=legend_patches, loc="upper left", fontsize=9)
    ax.set_title(
        f"dCortex Cascade Graph — {disrupted_node} Ground Stop\n"
        f"Node color = cascade depth | Edges = flights",
        fontsize=12,
    )
    ax.axis("off")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Cascade graph saved → {output_path}")
