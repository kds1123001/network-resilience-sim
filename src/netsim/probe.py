from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import ipaddress
import re
import socket
import subprocess
import time
import networkx as nx


@dataclass
class HostProbeResult:
    ip: str
    node_id: int
    reachable: bool
    rtt_seconds: float | None
    error: str | None = None


def validate_private_ip(ip_str: str) -> str:
    """Ensure the address is a valid RFC1918 private or loopback IP."""
    try:
        addr = ipaddress.ip_address(ip_str.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid IP address: {ip_str!r}") from exc

    if not (addr.is_private or addr.is_loopback):
        raise ValueError(
            f"Refusing non-private IP {ip_str!r}: only private (RFC1918) or loopback IPs are allowed."
        )
    return str(addr)


def expand_private_subnet(cidr_str: str, max_hosts: int = 1024) -> list[str]:
    """Expand a private CIDR subnet (e.g. 192.168.1.0/24) into host IPs."""
    try:
        net = ipaddress.ip_network(cidr_str.strip(), strict=False)
    except ValueError as exc:
        raise ValueError(f"Invalid subnet CIDR: {cidr_str!r}") from exc

    if not (net.is_private or net.is_loopback):
        raise ValueError(
            f"Refusing non-private subnet {cidr_str!r}: only private subnets are allowed."
        )

    hosts = [str(h) for h in net.hosts()]
    if not hosts:
        hosts = [str(net.network_address)]
    if len(hosts) > max_hosts:
        raise ValueError(
            f"Subnet {cidr_str!r} has {len(hosts)} hosts (max allowed: {max_hosts}). Use a smaller prefix like /24."
        )
    return hosts


def _ping_icmp_rtt(ip: str, timeout_ms: int = 400) -> tuple[bool, float | None]:
    """Fallback ICMP echo probe via Windows ping for firewalled LAN hosts."""
    try:
        proc = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), ip],
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000.0) + 1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None

    if proc.returncode != 0:
        return False, None

    match = re.search(r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", proc.stdout, re.IGNORECASE)
    if match:
        return True, max(0.0005, float(match.group(1)) / 1000.0)
    return True, 0.001


def probe_tcp_rtt(
    ip: str, port: int = 80, timeout: float = 0.8, icmp_fallback: bool = True
) -> tuple[bool, float | None, str | None]:
    """Measure TCP handshake RTT to a private IP:port, with optional ICMP fallback."""
    validate_private_ip(ip)
    ports_to_try = list(dict.fromkeys([port, 445, 443, 80, 53, 22]))
    per_port_timeout = max(0.12, timeout / len(ports_to_try))
    last_err: str | None = None

    for candidate_port in ports_to_try:
        start = time.perf_counter()
        try:
            with socket.create_connection((ip, candidate_port), timeout=per_port_timeout):
                return True, time.perf_counter() - start, None
        except ConnectionRefusedError:
            return True, time.perf_counter() - start, None
        except (TimeoutError, OSError) as exc:
            last_err = str(exc)

    if icmp_fallback:
        alive, rtt = _ping_icmp_rtt(ip, timeout_ms=max(150, int(timeout * 1000)))
        if alive:
            return True, rtt, None

    return False, None, last_err


def probe_hosts(
    ips: list[str],
    port: int = 80,
    timeout: float = 0.8,
    workers: int = 32,
    only_reachable: bool = False,
) -> list[HostProbeResult]:
    """Concurrently probe private IPs and return per-host reachability and RTT."""
    cleaned = [validate_private_ip(raw) for raw in ips]
    raw_results: dict[int, HostProbeResult] = {}
    max_workers = max(1, min(workers, len(cleaned) or 1))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(probe_tcp_rtt, ip, port, timeout, True): (idx, ip)
            for idx, ip in enumerate(cleaned)
        }
        for fut in as_completed(futures):
            idx, ip = futures[fut]
            reachable, rtt, err = fut.result()
            raw_results[idx] = HostProbeResult(
                ip=ip,
                node_id=idx,
                reachable=reachable,
                rtt_seconds=rtt,
                error=err,
            )

    ordered = [raw_results[i] for i in range(len(cleaned))]
    if only_reachable:
        filtered = [r for r in ordered if r.reachable]
        return [
            HostProbeResult(
                ip=r.ip,
                node_id=new_idx,
                reachable=r.reachable,
                rtt_seconds=r.rtt_seconds,
                error=r.error,
            )
            for new_idx, r in enumerate(filtered)
        ]
    return ordered


def build_ip_topology(
    kind: str,
    probe_results: list[HostProbeResult],
    seed: int = 42,
    extra_disconnected: set[int] | None = None,
) -> tuple[nx.Graph, set[int], float, float]:
    """Build a graph labeled with private IPs, offline set, and measured RTT bounds."""
    from .topology import build_topology

    offline_nodes = {r.node_id for r in probe_results if not r.reachable}
    if extra_disconnected:
        offline_nodes |= extra_disconnected

    graph = build_topology(
        kind=kind,
        num_nodes=len(probe_results),
        seed=seed,
        disconnected=offline_nodes,
    )

    for r in probe_results:
        graph.nodes[r.node_id]["ip"] = r.ip
        graph.nodes[r.node_id]["rtt"] = r.rtt_seconds

    measured_rtts = [r.rtt_seconds for r in probe_results if r.rtt_seconds is not None]
    if measured_rtts:
        min_rtt = max(0.0001, min(measured_rtts))
        max_rtt = max(min_rtt, max(measured_rtts))
    else:
        min_rtt, max_rtt = 0.001, 0.01

    return graph, offline_nodes, min_rtt, max_rtt