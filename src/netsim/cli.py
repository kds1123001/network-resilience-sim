import argparse
import sys
from rich.console import Console
from rich.table import Table
from .agent import run_udp_agent
from .c_backend import run_smart_c_simulation
from .topology import build_topology
from .engine import SimulationConfig, run_simulation
from .metrics import summarize_runs
from .probe import build_ip_topology, expand_private_subnet, probe_hosts
from .reporting import export_csv, export_json, generate_chart


def _run_agent_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="netsim agent",
        description="Run a live UDP gossip agent node on a private/loopback IP:port.",
    )
    parser.add_argument(
        "--bind",
        type=str,
        required=True,
        help="Private/loopback IP:port to listen on (e.g. 127.0.0.1:9001)",
    )
    parser.add_argument(
        "--peers",
        type=str,
        nargs="*",
        default=[],
        help="Neighbor peer endpoints in ip:port format (e.g. 127.0.0.1:9002 127.0.0.1:9003)",
    )
    parser.add_argument(
        "--send",
        type=str,
        default=None,
        help="Optional initial message payload to broadcast from this node",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="How long to listen for gossip packets in seconds (default: 10.0)",
    )
    parser.add_argument(
        "--packet-loss",
        type=float,
        default=0.0,
        help="Simulated outbound drop probability 0-1 (default: 0.0)",
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for packet drops")
    parser.add_argument(
        "--cluster-key",
        type=str,
        default=None,
        help="Shared secret key for HMAC-SHA256 packet authentication",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=2.0,
        help="Seconds between live peer heartbeat pings (default: 2.0)",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=None,
        help="Optional HTTP port exposing /health and Prometheus /metrics",
    )
    args = parser.parse_args(argv)

    if not 0.0 <= args.packet_loss <= 1.0:
        parser.error("Require 0.0 <= packet-loss <= 1.0")
    if args.duration <= 0.0:
        parser.error("Require duration > 0")
    if args.heartbeat_interval <= 0.0:
        parser.error("Require heartbeat-interval > 0")

    try:
        run_udp_agent(
            bind=args.bind,
            peers=args.peers,
            initial_message=args.send,
            duration=args.duration,
            packet_loss=args.packet_loss,
            seed=args.seed,
            cluster_key=args.cluster_key,
            heartbeat_interval=args.heartbeat_interval,
            metrics_port=args.metrics_port,
        )
    except ValueError as exc:
        parser.error(str(exc))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "agent":
        _run_agent_cli(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        prog="netsim",
        description="Discrete-event network resilience simulator (use 'netsim agent' for live UDP mode).",
    )
    parser.add_argument(
        "--engine",
        choices=["python", "c"],
        default="python",
        help="Simulation backend: 'python' (default) or native 'c' smart DLL",
    )
    parser.add_argument(
        "--smart-fanout",
        type=int,
        default=4,
        help="Max adaptive outbound neighbors per wave in C engine (0 = flood all, default: 4)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=1,
        help="Adaptive backoff retries on packet drop in C engine (default: 1)",
    )
    parser.add_argument(
        "--topology",
        choices=["ring", "mesh", "random", "scale-free"],
        default="ring",
        help="Network graph architecture",
    )
    parser.add_argument("--nodes", type=int, default=12, help="Number of nodes")
    parser.add_argument(
        "--ips",
        type=str,
        nargs="*",
        default=[],
        help="Private/loopback IP addresses to probe and map to nodes",
    )
    parser.add_argument(
        "--discover",
        type=str,
        default=None,
        help="Private CIDR subnet to scan concurrently (e.g. 192.168.1.0/24)",
    )
    parser.add_argument("--workers", type=int, default=32, help="Concurrent probe worker threads")
    parser.add_argument(
        "--include-offline",
        action="store_true",
        help="Keep unreachable discovered hosts as OFFLINE nodes during --discover",
    )
    parser.add_argument("--probe-port", type=int, default=80, help="TCP port for IP RTT probing")
    parser.add_argument("--probe-timeout", type=float, default=0.8, help="Probe timeout in seconds")
    parser.add_argument(
        "--source-node",
        type=int,
        default=0,
        help="Node ID that initiates the broadcast (default: 0)",
    )
    parser.add_argument("--runs", type=int, default=1, help="Monte Carlo iterations")
    parser.add_argument("--seed", type=int, default=42, help="Starting RNG seed")
    parser.add_argument("--min-delay", type=float, default=None)
    parser.add_argument("--max-delay", type=float, default=None)
    parser.add_argument(
        "--packet-loss", type=float, default=0.0, help="Packet drop probability (0-1)"
    )
    parser.add_argument("--reboot-at", type=float, default=-1.0)
    parser.add_argument("--reboot-duration", type=float, default=1.5)
    parser.add_argument(
        "--disconnect",
        type=int,
        nargs="*",
        default=[],
        help="Node IDs to disconnect",
    )
    parser.add_argument("--json-out", type=str, default=None, help="Write JSON report to path")
    parser.add_argument("--csv-out", type=str, default=None, help="Write CSV run log to path")
    parser.add_argument("--plot-out", type=str, default=None, help="Save PNG chart to path")
    args = parser.parse_args()

    if args.smart_fanout < 0:
        parser.error("Require --smart-fanout >= 0")
    if args.max_retries < 0:
        parser.error("Require --max-retries >= 0")

    console = Console()
    target_ips: list[str] = list(args.ips)
    if args.discover:
        try:
            target_ips.extend(expand_private_subnet(args.discover))
        except ValueError as exc:
            parser.error(str(exc))

    probe_results = []
    if target_ips:
        try:
            probe_results = probe_hosts(
                target_ips,
                port=args.probe_port,
                timeout=args.probe_timeout,
                workers=args.workers,
                only_reachable=bool(args.discover and not args.include_offline),
            )
        except ValueError as exc:
            parser.error(str(exc))

    num_nodes = len(probe_results) if target_ips else args.nodes

    if num_nodes < 1:
        parser.error("No active hosts found (or require --nodes >= 1 / --include-offline)")
    if args.source_node not in range(num_nodes):
        parser.error(f"--source-node must be between 0 and {num_nodes - 1}")
    if not 0.0 <= args.packet_loss <= 1.0:
        parser.error("Require 0.0 <= packet-loss <= 1.0")
    if any(n not in range(num_nodes) for n in args.disconnect):
        parser.error(f"Node IDs must be between 0 and {num_nodes - 1}")

    disconnected_set = set(args.disconnect)
    measured_min = 0.5
    measured_max = 2.0

    if target_ips:
        title = (
            f"Subnet Discovery ({args.discover})"
            if args.discover
            else f"Private IP Probe (TCP :{args.probe_port})"
        )
        probe_table = Table(title=title)
        probe_table.add_column("Node", style="cyan")
        probe_table.add_column("IP Address", style="bold white")
        probe_table.add_column("Status")
        probe_table.add_column("Measured RTT")
        for pr in probe_results:
            status_str = "[green]ONLINE[/green]" if pr.reachable else "[red]OFFLINE[/red]"
            rtt_str = f"{pr.rtt_seconds * 1000:.2f} ms" if pr.rtt_seconds is not None else "Timeout"
            probe_table.add_row(str(pr.node_id), pr.ip, status_str, rtt_str)
        console.print(probe_table)

        _, offline_from_probe, measured_min, measured_max = build_ip_topology(
            args.topology, probe_results, seed=args.seed, extra_disconnected=disconnected_set
        )
        disconnected_set = offline_from_probe

    delay_min = args.min_delay if args.min_delay is not None else measured_min
    delay_max = args.max_delay if args.max_delay is not None else measured_max

    if not 0 <= delay_min <= delay_max:
        parser.error("Require 0 <= min-delay <= max-delay")

    results = []
    efficiencies: list[float] = []
    latest_graph = None

    cfg = SimulationConfig(
        num_nodes=num_nodes,
        seed=args.seed,
        delay_min=delay_min,
        delay_max=delay_max,
        packet_loss=args.packet_loss,
        reboot_at=args.reboot_at,
        reboot_duration=args.reboot_duration,
        disconnected=disconnected_set,
        source_node=args.source_node,
    )

    for i in range(args.runs):
        current_seed = args.seed + i
        if target_ips:
            graph, _, _, _ = build_ip_topology(
                args.topology,
                probe_results,
                seed=current_seed,
                extra_disconnected=set(args.disconnect),
            )
        else:
            graph = build_topology(
                kind=args.topology,
                num_nodes=num_nodes,
                seed=current_seed,
                disconnected=disconnected_set,
            )

        run_cfg = SimulationConfig(
            num_nodes=cfg.num_nodes,
            seed=current_seed,
            delay_min=cfg.delay_min,
            delay_max=cfg.delay_max,
            packet_loss=cfg.packet_loss,
            reboot_at=cfg.reboot_at,
            reboot_duration=cfg.reboot_duration,
            disconnected=cfg.disconnected,
            source_node=cfg.source_node,
        )

        if args.engine == "c":
            try:
                sim_res, eff = run_smart_c_simulation(
                    graph,
                    run_cfg,
                    smart_fanout=args.smart_fanout,
                    max_retries=args.max_retries,
                )
            except FileNotFoundError as exc:
                parser.error(str(exc))
            results.append(sim_res)
            efficiencies.append(eff)
        else:
            results.append(run_simulation(graph, run_cfg))

        latest_graph = graph

    summary = summarize_runs(results, args.topology, num_nodes) if args.runs > 1 else None
    engine_tag = (
        f"C-SMART fanout={args.smart_fanout} retries={args.max_retries}"
        if args.engine == "c"
        else "PYTHON"
    )

    if args.runs == 1:
        res = results[0]
        table = Table(
            title=f"Simulation Report ({args.topology.upper()} | {engine_tag} | Seed {res.seed})"
        )
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="bold white")
        table.add_row("Reached Nodes", f"{len(res.reached)} / {res.total_nodes}")
        table.add_row("Elapsed Time", f"{res.elapsed_time:.4f}s")
        table.add_row(
            "Full Reach Time",
            f"{res.full_reach_time:.4f}s" if res.full_reach_achieved else "Not achieved",
        )
        table.add_row("Duplicates Suppressed", str(res.duplicates_suppressed))
        table.add_row("Packets Dropped", str(res.packets_dropped))
        table.add_row("Reboots Triggered", str(res.reboots_triggered))
        if efficiencies:
            table.add_row("Smart Fanout Saved", f"{efficiencies[0] * 100:.1f}%")
        console.print(table)
    else:
        assert summary is not None
        table = Table(
            title=f"Monte Carlo Resilience Benchmark ({args.runs} Runs | {engine_tag})"
        )
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="bold green")
        table.add_row("Topology", f"{summary.topology} ({summary.num_nodes} nodes)")
        table.add_row("Full Reach Success Rate", f"{summary.full_reach_rate:.1f}%")
        table.add_row("Avg Reached Nodes", f"{summary.avg_reached_nodes:.2f} / {summary.num_nodes}")
        table.add_row(
            "Mean Convergence Time",
            f"{summary.mean_reach_time:.4f}s" if summary.mean_reach_time else "N/A",
        )
        table.add_row(
            "Median (p50) Time",
            f"{summary.p50_reach_time:.4f}s" if summary.p50_reach_time else "N/A",
        )
        table.add_row(
            "Tail Latency (p95)",
            f"{summary.p95_reach_time:.4f}s" if summary.p95_reach_time else "N/A",
        )
        table.add_row("Avg Duplicates Suppressed", f"{summary.avg_duplicates:.1f}")
        table.add_row("Avg Packets Dropped", f"{summary.avg_dropped:.1f}")
        if efficiencies:
            avg_eff = sum(efficiencies) / len(efficiencies)
            table.add_row("Avg Smart Fanout Saved", f"{avg_eff * 100:.1f}%")
        console.print(table)

    if args.json_out:
        saved = export_json(args.json_out, cfg, args.topology, results, summary)
        console.print(f"[dim]JSON saved to {saved}[/dim]")

    if args.csv_out:
        saved = export_csv(args.csv_out, args.topology, results)
        console.print(f"[dim]CSV saved to {saved}[/dim]")

    if args.plot_out and latest_graph is not None:
        saved = generate_chart(args.plot_out, latest_graph, results[-1], results, args.topology)
        console.print(f"[dim]Chart saved to {saved}[/dim]")


if __name__ == "__main__":
    main()