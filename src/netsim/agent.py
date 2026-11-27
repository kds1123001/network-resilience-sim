from dataclasses import dataclass, field
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
import random
import signal
import socket
import threading
import time
import uuid
from rich.console import Console
from rich.table import Table


@dataclass
class AgentStats:
    endpoint: str
    unique_received: int = 0
    duplicates_suppressed: int = 0
    packets_forwarded: int = 0
    packets_dropped: int = 0
    auth_rejected: int = 0
    peers_discovered: int = 0
    active_peers: int = 0
    received_messages: list[dict] = field(default_factory=list)


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    if ":" not in endpoint:
        raise ValueError(f"Invalid endpoint '{endpoint}', expected ip:port")
    host, port_str = endpoint.rsplit(":", 1)
    host = host.strip()
    if not host:
        raise ValueError(f"Empty host in endpoint '{endpoint}'")
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"Invalid IP address in endpoint '{endpoint}': {host}") from exc
    if not (ip_obj.is_private or ip_obj.is_loopback):
        raise ValueError(f"Refusing non-private IP address in endpoint '{endpoint}': {host}")
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError(f"Invalid port in endpoint '{endpoint}'") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"Port out of range in endpoint '{endpoint}': {port}")
    return host, port


parse_host_port = parse_endpoint


def _canonical_signing_bytes(envelope: dict) -> bytes:
    unsigned = {k: v for k, v in envelope.items() if k != "hmac"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign_envelope(envelope: dict, cluster_key: str | None) -> dict:
    signed = dict(envelope)
    if cluster_key:
        digest = hmac.new(
            cluster_key.encode("utf-8"),
            _canonical_signing_bytes(signed),
            hashlib.sha256,
        ).hexdigest()
        signed["hmac"] = digest
    return signed


def _verify_envelope(envelope: dict, cluster_key: str | None) -> bool:
    if not cluster_key:
        return True
    provided = envelope.get("hmac")
    if not isinstance(provided, str) or not provided:
        return False
    expected = hmac.new(
        cluster_key.encode("utf-8"),
        _canonical_signing_bytes(envelope),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


def _start_observability_server(
    bind_host: str,
    metrics_port: int,
    self_endpoint: str,
    stats: AgentStats,
    known_peers: set[str],
    lock: threading.Lock,
    stop_event: threading.Event,
    started_at: float,
) -> ThreadingHTTPServer:
    class _ObservabilityHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            uptime = max(0.0, time.monotonic() - started_at)
            with lock:
                snap = {
                    "unique_received": stats.unique_received,
                    "duplicates_suppressed": stats.duplicates_suppressed,
                    "packets_forwarded": stats.packets_forwarded,
                    "packets_dropped": stats.packets_dropped,
                    "auth_rejected": stats.auth_rejected,
                    "peers_discovered": stats.peers_discovered,
                    "active_peers": len(known_peers),
                }

            if path == "/health":
                healthy = not stop_event.is_set()
                status_code = 200 if healthy else 503
                body = json.dumps(
                    {
                        "status": "ok" if healthy else "shutting_down",
                        "endpoint": self_endpoint,
                        "active_peers": snap["active_peers"],
                        "uptime_seconds": round(uptime, 3),
                    }
                ).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/metrics":
                lbl = f'endpoint="{self_endpoint}"'
                lines = [
                    "# HELP netsim_agent_unique_messages_total Unique gossip messages accepted",
                    "# TYPE netsim_agent_unique_messages_total counter",
                    f"netsim_agent_unique_messages_total{{{lbl}}} {snap['unique_received']}",
                    "# HELP netsim_agent_duplicates_suppressed_total Duplicate gossip packets dropped",
                    "# TYPE netsim_agent_duplicates_suppressed_total counter",
                    f"netsim_agent_duplicates_suppressed_total{{{lbl}}} {snap['duplicates_suppressed']}",
                    "# HELP netsim_agent_packets_forwarded_total Outbound gossip packets forwarded",
                    "# TYPE netsim_agent_packets_forwarded_total counter",
                    f"netsim_agent_packets_forwarded_total{{{lbl}}} {snap['packets_forwarded']}",
                    "# HELP netsim_agent_packets_dropped_total Simulated outbound packet drops",
                    "# TYPE netsim_agent_packets_dropped_total counter",
                    f"netsim_agent_packets_dropped_total{{{lbl}}} {snap['packets_dropped']}",
                    "# HELP netsim_agent_auth_rejected_total Packets rejected due to invalid HMAC or JSON",
                    "# TYPE netsim_agent_auth_rejected_total counter",
                    f"netsim_agent_auth_rejected_total{{{lbl}}} {snap['auth_rejected']}",
                    "# HELP netsim_agent_peers_discovered_total Peers dynamically learned via gossip or heartbeat",
                    "# TYPE netsim_agent_peers_discovered_total counter",
                    f"netsim_agent_peers_discovered_total{{{lbl}}} {snap['peers_discovered']}",
                    "# HELP netsim_agent_active_peers Current number of known active peers",
                    "# TYPE netsim_agent_active_peers gauge",
                    f"netsim_agent_active_peers{{{lbl}}} {snap['active_peers']}",
                    "# HELP netsim_agent_uptime_seconds Agent uptime in seconds",
                    "# TYPE netsim_agent_uptime_seconds gauge",
                    f"netsim_agent_uptime_seconds{{{lbl}}} {uptime:.3f}",
                ]
                body = ("\n".join(lines) + "\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.end_headers()

    httpd = ThreadingHTTPServer((bind_host, metrics_port), _ObservabilityHandler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    return httpd


def run_udp_agent(
    bind: str,
    peers: list[str] | None = None,
    initial_message: str | None = None,
    duration: float = 10.0,
    packet_loss: float = 0.0,
    seed: int = 42,
    cluster_key: str | None = None,
    heartbeat_interval: float = 2.0,
    metrics_port: int | None = None,
) -> AgentStats:
    """
    Run a UDP gossip + heartbeat discovery node with optional HTTP /health + /metrics
    and graceful SIGINT/SIGTERM shutdown.
    """
    console = Console()
    bind_host, bind_port = parse_endpoint(bind)
    self_endpoint = f"{bind_host}:{bind_port}"

    if metrics_port is not None and not (1 <= metrics_port <= 65535):
        raise ValueError(f"Metrics port out of range: {metrics_port}")

    peer_targets: list[tuple[str, int]] = []
    known_peers: set[str] = set()
    lock = threading.Lock()

    def register_peer(endpoint: str) -> bool:
        if not endpoint or endpoint == self_endpoint:
            return False
        host, port = parse_endpoint(endpoint)
        norm_ep = f"{host}:{port}"
        with lock:
            if norm_ep in known_peers:
                return False
            known_peers.add(norm_ep)
            peer_targets.append((host, port))
            return True

    for p in peers or []:
        register_peer(p)

    rng = random.Random(seed)
    seen_ids: set[str] = set()
    stats = AgentStats(endpoint=self_endpoint)
    started_at = time.monotonic()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind_host, bind_port))
    sock.settimeout(0.25)

    stop_event = threading.Event()
    shutdown_reason: list[str] = []

    previous_handlers: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        def _handle_shutdown(signum: int, _frame) -> None:
            if not stop_event.is_set():
                try:
                    sig_name = signal.Signals(signum).name
                except ValueError:
                    sig_name = str(signum)
                shutdown_reason.append(sig_name)
                stop_event.set()

        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig_obj = getattr(signal, sig_name, None)
            if sig_obj is not None:
                try:
                    previous_handlers[sig_obj] = signal.getsignal(sig_obj)
                    signal.signal(sig_obj, _handle_shutdown)
                except (ValueError, OSError):
                    pass

    httpd: ThreadingHTTPServer | None = None
    if metrics_port is not None:
        httpd = _start_observability_server(
            bind_host=bind_host,
            metrics_port=metrics_port,
            self_endpoint=self_endpoint,
            stats=stats,
            known_peers=known_peers,
            lock=lock,
            stop_event=stop_event,
            started_at=started_at,
        )

    def send_raw_to(host: str, port: int, envelope: dict, count_drop: bool = True) -> None:
        if count_drop and packet_loss > 0.0 and rng.random() < packet_loss:
            with lock:
                stats.packets_dropped += 1
            return
        signed = _sign_envelope(envelope, cluster_key)
        raw = json.dumps(signed).encode("utf-8")
        try:
            sock.sendto(raw, (host, port))
        except OSError:
            pass

    def broadcast_gossip(msg: dict, exclude_peer: str | None = None) -> None:
        with lock:
            targets = list(peer_targets)
        for host, port in targets:
            target_ep = f"{host}:{port}"
            if exclude_peer and target_ep == exclude_peer:
                continue
            send_raw_to(host, port, msg, count_drop=True)
            with lock:
                stats.packets_forwarded += 1

    def heartbeat_loop() -> None:
        interval = max(0.25, heartbeat_interval)
        while not stop_event.wait(interval):
            with lock:
                targets = list(peer_targets)
            ping_pkt = {
                "type": "ping",
                "sender": self_endpoint,
                "ts": time.time(),
            }
            for host, port in targets:
                send_raw_to(host, port, ping_pkt, count_drop=False)

    hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    hb_thread.start()

    auth_mode = "HMAC-SHA256" if cluster_key else "OPEN"
    metrics_info = f", metrics: http://{bind_host}:{metrics_port}/metrics" if metrics_port else ""
    console.print(
        f"UDP Gossip Agent listening on {self_endpoint} "
        f"(peers: {len(peer_targets)}, auth: {auth_mode}, duration: {duration:.1f}s{metrics_info})"
    )

    if initial_message is not None and not stop_event.is_set():
        msg_id = str(uuid.uuid4())[:8]
        with lock:
            seen_ids.add(msg_id)
            stats.unique_received += 1
        origin_msg = {
            "type": "gossip",
            "msg_id": msg_id,
            "origin": self_endpoint,
            "sender": self_endpoint,
            "sent_at": time.time(),
            "hops": 0,
            "payload": initial_message,
        }
        with lock:
            targets = list(peer_targets)
        for host, port in targets:
            send_raw_to(
                host,
                port,
                {"type": "ping", "sender": self_endpoint, "ts": time.time()},
                count_drop=False,
            )
        broadcast_gossip(origin_msg)
        console.print(f"[SEND] msg_id={msg_id} payload={initial_message!r}")

    deadline = time.monotonic() + duration
    try:
        while not stop_event.is_set() and time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                pkt = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                with lock:
                    stats.auth_rejected += 1
                continue

            if not isinstance(pkt, dict) or not _verify_envelope(pkt, cluster_key):
                with lock:
                    stats.auth_rejected += 1
                console.print(
                    f"[AUTH-DROP] Rejected unauthenticated/invalid packet from {addr[0]}:{addr[1]}"
                )
                continue

            pkt_type = pkt.get("type", "gossip")
            sender_ep = pkt.get("sender")
            if isinstance(sender_ep, str):
                try:
                    if register_peer(sender_ep):
                        with lock:
                            stats.peers_discovered += 1
                        console.print(f"[PEER-DISCOVERED] {sender_ep}")
                except ValueError:
                    pass

            if pkt_type == "ping":
                if isinstance(sender_ep, str):
                    try:
                        shost, sport = parse_endpoint(sender_ep)
                        send_raw_to(
                            shost,
                            sport,
                            {"type": "pong", "sender": self_endpoint, "ts": time.time()},
                            count_drop=False,
                        )
                    except ValueError:
                        pass
                continue

            if pkt_type == "pong":
                continue

            msg_id = pkt.get("msg_id")
            if not isinstance(msg_id, str) or not msg_id:
                continue

            with lock:
                if msg_id in seen_ids:
                    stats.duplicates_suppressed += 1
                    continue
                seen_ids.add(msg_id)
                stats.unique_received += 1
                stats.received_messages.append(dict(pkt))

            hops = int(pkt.get("hops", 0)) + 1
            sent_at = float(pkt.get("sent_at", time.time()))
            latency_ms = max(0.0, (time.time() - sent_at) * 1000.0)
            payload = pkt.get("payload", "")

            console.print(
                f"[RECV] msg_id={msg_id} hops={hops} latency={latency_ms:.2f}ms payload={payload!r}"
            )

            forwarded = {
                "type": "gossip",
                "msg_id": msg_id,
                "origin": pkt.get("origin", self_endpoint),
                "sender": self_endpoint,
                "sent_at": sent_at,
                "hops": hops,
                "payload": payload,
            }
            broadcast_gossip(forwarded, exclude_peer=sender_ep if isinstance(sender_ep, str) else None)
    finally:
        stop_event.set()
        if shutdown_reason:
            console.print(f"[SHUTDOWN] Graceful shutdown triggered by {shutdown_reason[0]}")
        hb_thread.join(timeout=1.0)
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        sock.close()
        for sig_obj, prev in previous_handlers.items():
            try:
                signal.signal(sig_obj, prev)
            except (ValueError, OSError):
                pass

    with lock:
        stats.active_peers = len(known_peers)

    table = Table(title=f"UDP Agent Summary\n({self_endpoint})")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold white")
    table.add_row("Unique Messages", str(stats.unique_received))
    table.add_row("Duplicates Suppressed", str(stats.duplicates_suppressed))
    table.add_row("Packets Forwarded", str(stats.packets_forwarded))
    table.add_row("Packets Dropped", str(stats.packets_dropped))
    table.add_row("Auth Rejected", str(stats.auth_rejected))
    table.add_row("Peers Discovered", str(stats.peers_discovered))
    table.add_row("Active Peers", str(stats.active_peers))
    console.print(table)
    return stats