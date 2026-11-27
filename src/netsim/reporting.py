import csv
from dataclasses import asdict
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
from .engine import SimulationConfig, SimulationResult
from .metrics import MonteCarloSummary


def export_json(
    path: str | Path,
    config: SimulationConfig,
    topology: str,
    results: list[SimulationResult],
    summary: MonteCarloSummary | None = None,
) -> Path:
    """Write simulation configuration, per-run results, and optional summary to JSON."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "topology": topology,
        "config": {
            **asdict(config),
            "disconnected": sorted(config.disconnected or set()),
        },
        "summary": asdict(summary) if summary else None,
        "runs": [
            {
                "seed": r.seed,
                "total_nodes": r.total_nodes,
                "reached_count": len(r.reached),
                "full_reach_achieved": r.full_reach_achieved,
                "full_reach_time": r.full_reach_time,
                "elapsed_time": r.elapsed_time,
                "duplicates_suppressed": r.duplicates_suppressed,
                "packets_dropped": r.packets_dropped,
                "reboots_triggered": r.reboots_triggered,
                "disconnected": r.disconnected,
                "reached_arrival_times": {str(k): v for k, v in sorted(r.arrival_times.items())},
            }
            for r in results
        ],
    }

    import json

    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def export_csv(
    path: str | Path,
    topology: str,
    results: list[SimulationResult],
) -> Path:
    """Write per-run simulation metrics to a CSV file."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "seed",
        "topology",
        "total_nodes",
        "reached_count",
        "full_reach_achieved",
        "full_reach_time",
        "elapsed_time",
        "duplicates_suppressed",
        "packets_dropped",
        "reboots_triggered",
        "disconnected",
    ]

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "seed": r.seed,
                    "topology": topology,
                    "total_nodes": r.total_nodes,
                    "reached_count": len(r.reached),
                    "full_reach_achieved": r.full_reach_achieved,
                    "full_reach_time": r.full_reach_time if r.full_reach_time is not None else "",
                    "elapsed_time": f"{r.elapsed_time:.6f}",
                    "duplicates_suppressed": r.duplicates_suppressed,
                    "packets_dropped": r.packets_dropped,
                    "reboots_triggered": r.reboots_triggered,
                    "disconnected": ",".join(str(n) for n in r.disconnected),
                }
            )

    return out_path


def generate_chart(
    path: str | Path,
    graph: nx.Graph,
    latest_result: SimulationResult,
    all_results: list[SimulationResult],
    topology: str,
) -> Path:
    """Render a side-by-side topology propagation map and latency distribution chart."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))
    ax_graph, ax_hist = axes

    pos = nx.spring_layout(graph, seed=42)
    disconnected_set = set(latest_result.disconnected)

    node_colors = []
    labels = {}
    for n in graph.nodes():
        if n in disconnected_set:
            node_colors.append("#ef4444")
        elif n in latest_result.reached:
            node_colors.append("#22c55e")
        else:
            node_colors.append("#f59e0b")

        ip = graph.nodes[n].get("ip")
        labels[n] = f"{n}\n({ip})" if ip else str(n)

    nx.draw_networkx_edges(graph, pos, ax=ax_graph, edge_color="#94a3b8", width=1.4)
    nx.draw_networkx_nodes(
        graph,
        pos,
        ax=ax_graph,
        node_color=node_colors,
        node_size=750 if any(graph.nodes[n].get("ip") for n in graph.nodes()) else 520,
        edgecolors="#1e293b",
        linewidths=1.2,
    )
    nx.draw_networkx_labels(
        graph,
        pos,
        labels=labels,
        ax=ax_graph,
        font_size=8 if any(graph.nodes[n].get("ip") for n in graph.nodes()) else 9,
        font_weight="bold",
        font_color="#0f172a",
    )
    ax_graph.set_title(
        f"{topology.upper()} Topology (Seed {latest_result.seed})\n"
        "Green = Reached | Amber = Unreached | Red = Offline",
        fontsize=10,
    )
    ax_graph.axis("off")

    reach_times = [r.full_reach_time for r in all_results if r.full_reach_time is not None]
    if reach_times:
        ax_hist.hist(reach_times, bins=min(15, max(5, len(reach_times))), color="#3b82f6", edgecolor="#1e3a8a")
        ax_hist.set_xlabel("Full Convergence Time (s)")
        ax_hist.set_ylabel("Run Count")
        ax_hist.set_title(f"Convergence Latency Distribution ({len(reach_times)}/{len(all_results)} Full Reach)")
    else:
        ax_hist.text(
            0.5,
            0.5,
            "No runs achieved 100% node reach",
            ha="center",
            va="center",
            fontsize=11,
            color="#64748b",
        )
        ax_hist.set_title("Convergence Latency Distribution")

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path