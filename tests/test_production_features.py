import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import pytest

from netsim.agent import run_udp_agent
from netsim.c_backend import (
    _find_64bit_gcc,
    default_source_path,
    ensure_smart_c_dll,
    run_smart_c_simulation,
)
from netsim.engine import SimulationConfig
from netsim.topology import build_topology


HAS_64BIT_GCC = _find_64bit_gcc() is not None


def _get_free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _sign_packet(pkt: dict, key: str) -> bytes:
    unsigned = {k: v for k, v in pkt.items() if k != "hmac"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signed = dict(unsigned)
    signed["hmac"] = hmac.new(key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return json.dumps(signed).encode("utf-8")


@pytest.mark.skipif(not HAS_64BIT_GCC, reason="64-bit gcc toolchain not available")
def test_c_backend_exact_arrival_times_and_disconnected_nodes():
    disconnected = {3, 7}
    g = build_topology("mesh", 10, seed=42, disconnected=disconnected)
    cfg = SimulationConfig(
        num_nodes=10,
        seed=42,
        delay_min=0.005,
        delay_max=0.02,
        packet_loss=0.0,
        disconnected=disconnected,
        source_node=0,
    )

    res, efficiency = run_smart_c_simulation(g, cfg, smart_fanout=0, max_retries=1)

    assert res.reached == {0, 1, 2, 4, 5, 6, 8, 9}
    assert set(res.arrival_times.keys()) == res.reached
    assert res.arrival_times[0] == 0.0
    for node_id in res.reached - {0}:
        assert res.arrival_times[node_id] > 0.0
    for offline_id in disconnected:
        assert offline_id not in res.reached
        assert offline_id not in res.arrival_times
    assert res.full_reach_achieved is False
    assert res.full_reach_time is None
    assert efficiency == 0.0


@pytest.mark.skipif(not HAS_64BIT_GCC, reason="64-bit gcc toolchain not available")
def test_c_backend_full_reach_time_matches_max_arrival():
    g = build_topology("scale-free", 30, seed=7)
    cfg = SimulationConfig(
        num_nodes=30,
        seed=7,
        delay_min=0.002,
        delay_max=0.01,
        packet_loss=0.0,
        source_node=0,
    )

    res, eff = run_smart_c_simulation(g, cfg, smart_fanout=4, max_retries=1)

    assert res.full_reach_achieved is True
    assert len(res.reached) == 30
    assert res.full_reach_time is not None
    assert pytest.approx(res.full_reach_time, rel=1e-9) == max(res.arrival_times.values())
    assert 0.0 <= eff <= 1.0


@pytest.mark.skipif(not HAS_64BIT_GCC, reason="64-bit gcc toolchain not available")
def test_c_backend_auto_compiles_and_rebuilds_when_source_newer(tmp_path):
    src = default_source_path()
    temp_src = tmp_path / "smart_sim.c"
    temp_dll = tmp_path / "smart_sim.dll"
    temp_src.write_bytes(src.read_bytes())

    # 1. Missing DLL triggers initial compile
    built_path = ensure_smart_c_dll(dll_path=temp_dll, source_path=temp_src, auto_compile=True)
    assert built_path == temp_dll
    assert temp_dll.exists()
    first_mtime = temp_dll.stat().st_mtime

    # 2. Up-to-date DLL is reused without recompiling
    time.sleep(0.05)
    ensure_smart_c_dll(dll_path=temp_dll, source_path=temp_src, auto_compile=True)
    assert temp_dll.stat().st_mtime == first_mtime

    # 3. Touching source forward in time triggers automatic rebuild
    future_ts = first_mtime + 5.0
    os.utime(temp_src, (future_ts, future_ts))
    ensure_smart_c_dll(dll_path=temp_dll, source_path=temp_src, auto_compile=True)
    assert temp_dll.stat().st_mtime > first_mtime


@pytest.mark.skipif(not HAS_64BIT_GCC, reason="64-bit gcc toolchain not available")
def test_cli_engine_c_single_and_monte_carlo_json(tmp_path):
    json_path = tmp_path / "c_report.json"
    cmd = [
        sys.executable,
        "-m",
        "netsim.cli",
        "--engine",
        "c",
        "--topology",
        "ring",
        "--nodes",
        "12",
        "--smart-fanout",
        "2",
        "--max-retries",
        "2",
        "--runs",
        "3",
        "--json-out",
        str(json_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "C-SMART fanout=2 retries=2" in proc.stdout
    assert json_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(payload["runs"]) == 3
    first_run = payload["runs"][0]
    assert first_run["reached_count"] == 12
    assert first_run["full_reach_achieved"] is True
    assert first_run["full_reach_time"] > 0.0


def test_cli_rejects_negative_smart_fanout_and_retries():
    for bad_flag in (["--smart-fanout", "-1"], ["--max-retries", "-2"]):
        proc = subprocess.run(
            [sys.executable, "-m", "netsim.cli", "--engine", "c", *bad_flag],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert "Require" in proc.stderr


def test_udp_agent_hmac_auth_and_heartbeat_discovery():
    port_a = _get_free_udp_port()
    port_b = _get_free_udp_port()
    ep_a = f"127.0.0.1:{port_a}"
    ep_b = f"127.0.0.1:{port_b}"
    secret = "cluster-test-secret-key"

    results: dict[str, object] = {}

    def run_node_a():
        results["a"] = run_udp_agent(
            bind=ep_a,
            peers=[],
            duration=2.2,
            cluster_key=secret,
            heartbeat_interval=0.4,
        )

    def run_node_b():
        time.sleep(0.25)
        results["b"] = run_udp_agent(
            bind=ep_b,
            peers=[ep_a],
            initial_message="hello-authenticated-cluster",
            duration=1.8,
            cluster_key=secret,
            heartbeat_interval=0.4,
        )

    t_a = threading.Thread(target=run_node_a)
    t_b = threading.Thread(target=run_node_b)
    t_a.start()
    t_b.start()

    time.sleep(0.35)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as attacker:
        forged = json.dumps(
            {
                "type": "gossip",
                "msg_id": "forged99",
                "sender": "127.0.0.1:59999",
                "sent_at": time.time(),
                "hops": 0,
                "payload": "evil",
            }
        ).encode("utf-8")
        attacker.sendto(forged, ("127.0.0.1", port_a))

        wrong_sig = _sign_packet(
            {
                "type": "gossip",
                "msg_id": "wrong99",
                "sender": "127.0.0.1:59998",
                "sent_at": time.time(),
                "hops": 0,
                "payload": "tampered",
            },
            "wrong-secret-key",
        )
        attacker.sendto(wrong_sig, ("127.0.0.1", port_a))

    t_a.join(timeout=5.0)
    t_b.join(timeout=5.0)

    assert results["a"].auth_rejected == 2
    assert results["a"].unique_received == 1
    assert results["a"].peers_discovered >= 1
    assert results["a"].active_peers >= 1


def test_udp_agent_health_and_prometheus_metrics():
    udp_port = _get_free_udp_port()
    http_port = _get_free_tcp_port()
    ep = f"127.0.0.1:{udp_port}"
    secret = "obs-secret"

    t = threading.Thread(
        target=run_udp_agent,
        kwargs={
            "bind": ep,
            "peers": [],
            "duration": 2.0,
            "cluster_key": secret,
            "metrics_port": http_port,
        },
        daemon=True,
    )
    t.start()

    # Wait for HTTP server readiness
    health_url = f"http://127.0.0.1:{http_port}/health"
    metrics_url = f"http://127.0.0.1:{http_port}/metrics"
    for _ in range(30):
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as resp:
                if resp.status == 200:
                    break
        except OSError:
            time.sleep(0.05)

    # Send one valid signed gossip packet to increment counters
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        pkt = _sign_packet(
            {
                "type": "gossip",
                "msg_id": "obs-msg-1",
                "sender": "127.0.0.1:58888",
                "sent_at": time.time(),
                "hops": 0,
                "payload": "metrics-check",
            },
            secret,
        )
        sender.sendto(pkt, ("127.0.0.1", udp_port))

    time.sleep(0.25)

    with urllib.request.urlopen(health_url, timeout=1.0) as h_resp:
        assert h_resp.status == 200
        health_data = json.loads(h_resp.read().decode("utf-8"))
        assert health_data["status"] == "ok"
        assert health_data["endpoint"] == ep
        assert health_data["active_peers"] == 1

    with urllib.request.urlopen(metrics_url, timeout=1.0) as m_resp:
        assert m_resp.status == 200
        metrics_text = m_resp.read().decode("utf-8")
        assert f'netsim_agent_unique_messages_total{{endpoint="{ep}"}} 1' in metrics_text
        assert f'netsim_agent_peers_discovered_total{{endpoint="{ep}"}} 1' in metrics_text
        assert f'netsim_agent_active_peers{{endpoint="{ep}"}} 1' in metrics_text

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"http://127.0.0.1:{http_port}/unknown", timeout=1.0)
    assert exc_info.value.code == 404

    t.join(timeout=4.0)


def test_udp_agent_graceful_sigint_shutdown():
    udp_port = _get_free_udp_port()
    http_port = _get_free_tcp_port()
    ep = f"127.0.0.1:{udp_port}"

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "netsim.cli",
            "agent",
            "--bind",
            ep,
            "--duration",
            "30",
            "--metrics-port",
            str(http_port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
    )

    health_url = f"http://127.0.0.1:{http_port}/health"
    for _ in range(40):
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as resp:
                if resp.status == 200:
                    break
        except OSError:
            time.sleep(0.05)

    t0 = time.monotonic()
    if os.name == "nt":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)

    stdout, stderr = proc.communicate(timeout=5.0)
    elapsed = time.monotonic() - t0

    assert proc.returncode == 0
    assert elapsed < 3.0
    assert "UDP Agent Summary" in stdout