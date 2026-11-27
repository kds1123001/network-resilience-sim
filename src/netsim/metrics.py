from dataclasses import dataclass
import statistics
from .engine import SimulationResult


@dataclass
class MonteCarloSummary:
    runs: int
    topology: str
    num_nodes: int
    full_reach_rate: float
    avg_reached_nodes: float
    mean_reach_time: float | None
    p50_reach_time: float | None
    p95_reach_time: float | None
    avg_duplicates: float
    avg_dropped: float


def summarize_runs(
    results: list[SimulationResult], topology: str, num_nodes: int
) -> MonteCarloSummary:
    """Compute statistical metrics across multiple simulation seeds."""
    total_runs = len(results)
    full_reaches = [r for r in results if r.full_reach_achieved]
    reach_times = sorted(
        r.full_reach_time for r in full_reaches if r.full_reach_time is not None
    )

    mean_time = statistics.mean(reach_times) if reach_times else None
    p50_time = statistics.median(reach_times) if reach_times else None
    p95_time = (
        reach_times[min(len(reach_times) - 1, int(len(reach_times) * 0.95))]
        if reach_times
        else None
    )

    return MonteCarloSummary(
        runs=total_runs,
        topology=topology,
        num_nodes=num_nodes,
        full_reach_rate=(len(full_reaches) / total_runs) * 100.0,
        avg_reached_nodes=statistics.mean(len(r.reached) for r in results),
        mean_reach_time=mean_time,
        p50_reach_time=p50_time,
        p95_reach_time=p95_time,
        avg_duplicates=statistics.mean(r.duplicates_suppressed for r in results),
        avg_dropped=statistics.mean(r.packets_dropped for r in results),
    )
