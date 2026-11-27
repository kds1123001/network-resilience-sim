import csv
import json
import socket
import threading
import pytest
from netsim import cli
from netsim.agent import parse_endpoint, run_udp_agent
from netsim.topology import build_topology
from netsim.engine import SimulationConfig, run_simulation
from netsim.probe import expand_private_subnet, validate_private_ip
from netsim.reporting import export_csv, export_json, generate_chart


def test_determinism():
    graph1 = build_topology("ring", 12, seed=42)
    graph2 = build_topology("ring", 12, seed=42)
    cfg = SimulationConfig(num_nodes=12, seed=42)

    res1 = run_simulation(graph1, cfg)
    res2 = run_simulation(graph2, cfg)

    assert res1.reached == res2.reached
    assert res1.elapsed_time == res2.elapsed_time


def test_source_disconnected():
    graph = build_topology("ring", 12, seed=42, disconnected={0})
    cfg = SimulationConfig(num_nodes=12, seed=42, disconnected={0})

    res = run_simulation(graph, cfg)
    assert len(res.reached) == 0
    assert not res.full_reach_achieved


def test_reboot_recovery():
    graph = build_topology("ring", 12, seed=42)
    cfg = SimulationConfig(
        num_nodes=12, seed=42, reboot_at=1.0, reboot_duration=1.0
    )

    res = run_simulation(graph, cfg)
    assert res.reboots_triggered == 1
    assert res.full_reach_achieved
    assert res.full_reach_time is not None and res.full_reach_time >= 2.0


def test_json_csv_and_chart_export(tmp_path):
    graph = build_topology("ring", 12, seed=42, disconnected={3})
    cfg = SimulationConfig(num_nodes=12, seed=42, disconnected={3})
    res = run_simulation(graph, cfg)

    json_file = export_json(tmp_path / "report.json", cfg, "ring", [res])
    csv_file = export_csv(tmp_path / "runs.csv", "ring", [res])
    chart_file = generate_chart(tmp_path / "chart.png", graph, res, [res], "ring")

    assert json_file.exists()
    loaded = json.loads(json_file.read_text(encoding="utf-8"))
    assert loaded["topology"] == "ring"
    assert len(loaded["runs"]) == 1

    assert csv_file.exists()
    with csv_file.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["disconnected"] == "3"

    assert chart_file.exists() and chart_file.stat().st_size > 0


def test_custom_source_node(monkeypatch, tmp_path):
    report_path = tmp_path / "source_report.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "netsim",
            "--topology",
            "ring",
            "--nodes",
            "12",
            "--source-node",
            "5",
            "--disconnect",
            "0",
            "--seed",
            "42",
            "--json-out",
            str(report_path),
        ],
    )

    cli.main()

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    run_data = payload["runs"][0]
    assert payload["config"]["source_node"] == 5
    assert run_data["reached_arrival_times"]["5"] == 0.0
    assert "0" not in run_data["reached_arrival_times"]
    assert run_data["reached_count"] == 11


@pytest.mark.parametrize(
    "bad_args",
    [
        ["--source-node", "12", "--nodes", "12"],
        ["--source-node", "-1"],
        ["--nodes", "0"],
        ["--min-delay", "2.5", "--max-delay", "1.0"],
        ["--packet-loss", "1.5"],
        ["--disconnect", "99", "--nodes", "12"],
        ["--discover", "8.8.8.0/24"],
    ],
)
def test_invalid_cli_flags(monkeypatch, bad_args):
    monkeypatch.setattr("sys.argv", ["netsim", *bad_args])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2


def test_private_ip_and_subnet_validation():
    assert validate_private_ip("127.0.0.1") == "127.0.0.1"
    assert validate_private_ip("192.168.1.10") == "192.168.1.10"
    assert validate_private_ip("10.0.0.5") == "10.0.0.5"

    hosts = expand_private_subnet("192.168.1.0/29")
    assert len(hosts) == 6
    assert "192.168.1.1" in hosts

    with pytest.raises(ValueError, match="non-private"):
        validate_private_ip("8.8.8.8")

    with pytest.raises(ValueError, match="non-private"):
        expand_private_subnet("1.1.1.0/24")


def test_per_node_rtt_simulation():
    graph = build_topology("ring", 3, seed=42)
    graph.nodes[0]["rtt"] = 0.002
    graph.nodes[1]["rtt"] = 0.005
    graph.nodes[2]["rtt"] = 0.010
    cfg = SimulationConfig(num_nodes=3, seed=42, delay_min=10.0, delay_max=20.0)
    res = run_simulation(graph, cfg)
    assert res.full_reach_achieved
    assert res.full_reach_time is not None and res.full_reach_time < 0.1


def test_loopback_ip_probe_mode(monkeypatch, tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = listener.getsockname()[1]

        report_path = tmp_path / "ip_report.json"
        monkeypatch.setattr(
            "sys.argv",
            [
                "netsim",
                "--topology",
                "ring",
                "--ips",
                "127.0.0.1",
                "127.0.0.1",
                "--probe-port",
                str(port),
                "--probe-timeout",
                "0.5",
                "--json-out",
                str(report_path),
            ],
        )

        cli.main()

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["config"]["num_nodes"] == 2
    assert payload["runs"][0]["reached_count"] == 2


def test_udp_agent_endpoint_validation():
    assert parse_endpoint("127.0.0.1:9001") == ("127.0.0.1", 9001)
    with pytest.raises(ValueError, match="non-private"):
        parse_endpoint("8.8.8.8:9001")


def test_udp_agent_three_node_live_gossip():
    # Pick 3 ephemeral UDP ports on loopback
    ports = []
    for _ in range(3):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            ports.append(s.getsockname()[1])

    p0, p1, p2 = ports
    results: dict[int, object] = {}

    def worker(idx: int, bind_ep: str, peer_eps: list[str], msg: str | None) -> None:
        results[idx] = run_udp_agent(
            bind=bind_ep,
            peers=peer_eps,
            initial_message=msg,
            duration=1.2,
        )

    # Full triangle of 3 live UDP agents: node 0 broadcasts, nodes 1 & 2 deduplicate cross-forwards
    t1 = threading.Thread(
        target=worker, args=(1, f"127.0.0.1:{p1}", [f"127.0.0.1:{p0}", f"127.0.0.1:{p2}"], None)
    )
    t2 = threading.Thread(
        target=worker, args=(2, f"127.0.0.1:{p2}", [f"127.0.0.1:{p0}", f"127.0.0.1:{p1}"], None)
    )
    t1.start()
    t2.start()

    results[0] = run_udp_agent(
        bind=f"127.0.0.1:{p0}",
        peers=[f"127.0.0.1:{p1}", f"127.0.0.1:{p2}"],
        initial_message="hello-live-mesh",
        duration=1.2,
    )
    t1.join()
    t2.join()

    assert results[0].unique_received == 1
    assert results[1].unique_received == 1
    assert results[2].unique_received == 1
    assert results[1].received_messages[0]["payload"] == "hello-live-mesh"
    assert results[2].received_messages[0]["payload"] == "hello-live-mesh"