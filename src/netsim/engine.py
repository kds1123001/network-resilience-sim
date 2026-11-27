from dataclasses import dataclass, field
import heapq
import random
import networkx as nx


@dataclass
class SimulationConfig:
    num_nodes: int
    seed: int = 42
    delay_min: float = 0.5
    delay_max: float = 2.0
    packet_loss: float = 0.0
    reboot_at: float = -1.0
    reboot_duration: float = 1.5
    disconnected: set[int] = field(default_factory=set)
    source_node: int = 0


@dataclass
class SimulationResult:
    seed: int
    total_nodes: int
    reached: set[int]
    arrival_times: dict[int, float]
    elapsed_time: float
    full_reach_achieved: bool
    full_reach_time: float | None
    duplicates_suppressed: int
    packets_dropped: int
    reboots_triggered: int
    disconnected: list[int] = field(default_factory=list)


def _next_delay(rng: random.Random, graph: nx.Graph, u: int, v: int, cfg: SimulationConfig) -> float:
    """Compute link delay using measured host RTT when available, else configured bounds."""
    rtt_u = graph.nodes[u].get("rtt")
    rtt_v = graph.nodes[v].get("rtt")
    rtts = [x for x in (rtt_u, rtt_v) if isinstance(x, (int, float)) and x >= 0]
    if rtts:
        base = max(0.0001, max(rtts))
        return base * rng.uniform(0.85, 1.25)
    return rng.uniform(cfg.delay_min, cfg.delay_max)


def run_simulation(graph: nx.Graph, cfg: SimulationConfig) -> SimulationResult:
    rng = random.Random(cfg.seed)
    state: dict[int, str] = {
        n: ("OFFLINE" if n in cfg.disconnected else "HEALTHY")
        for n in range(cfg.num_nodes)
    }
    reached: set[int] = set()
    arrival_times: dict[int, float] = {}
    duplicates_suppressed = 0
    packets_dropped = 0
    reboots_triggered = 0
    clock = 0.0
    seq = 0

    heap: list[tuple[float, int, str, int]] = []

    def push(t: float, kind: str, target: int) -> None:
        nonlocal seq
        seq += 1
        heapq.heappush(heap, (t, seq, kind, target))

    if state.get(cfg.source_node) == "HEALTHY":
        push(0.0, "arrival", cfg.source_node)

    if cfg.reboot_at >= 0.0:
        push(cfg.reboot_at, "reboot_start", -1)

    while heap:
        t, _, kind, node = heapq.heappop(heap)
        clock = t

        if kind == "reboot_start":
            reboots_triggered += 1
            for n, st in list(state.items()):
                if st == "HEALTHY":
                    state[n] = "REBOOTING"
            reached.clear()
            arrival_times.clear()
            push(clock + cfg.reboot_duration, "reboot_end", -1)

        elif kind == "reboot_end":
            for n, st in list(state.items()):
                if st == "REBOOTING":
                    state[n] = "HEALTHY"
            if state.get(cfg.source_node) == "HEALTHY":
                push(clock, "arrival", cfg.source_node)

        elif kind == "arrival":
            if state.get(node) != "HEALTHY":
                continue
            if node in reached:
                duplicates_suppressed += 1
                continue

            reached.add(node)
            arrival_times[node] = clock

            for nbr in graph.neighbors(node):
                if state.get(nbr) != "HEALTHY":
                    continue
                if cfg.packet_loss > 0.0 and rng.random() < cfg.packet_loss:
                    packets_dropped += 1
                    continue
                delay = _next_delay(rng, graph, node, nbr, cfg)
                push(clock + delay, "arrival", nbr)

    full_reach = len(reached) == cfg.num_nodes and cfg.num_nodes > 0
    full_reach_time = max(arrival_times.values()) if full_reach else None

    return SimulationResult(
        seed=cfg.seed,
        total_nodes=cfg.num_nodes,
        reached=reached,
        arrival_times=arrival_times,
        elapsed_time=clock,
        full_reach_achieved=full_reach,
        full_reach_time=full_reach_time,
        duplicates_suppressed=duplicates_suppressed,
        packets_dropped=packets_dropped,
        reboots_triggered=reboots_triggered,
        disconnected=sorted(cfg.disconnected),
    )