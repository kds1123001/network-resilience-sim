\# Network Resilience Simulator (`netsim`)

An offline, deterministic discrete-event simulator in Python for studying broadcast propagation, convergence latency, packet loss, node reboots, and fault tolerance across network topologies.

\## Features

\- \*\*Discrete-Event Simulation Engine (`engine.py`)\*\*: Priority-queue (`heapq`) event loop modeling `HEALTHY`, `REBOOTING`, and `OFFLINE` node states, configurable link delays, probabilistic packet drops, duplicate suppression, and timed cluster-wide reboot/recovery cycles.
\- \*\*Multiple Network Topologies (`topology.py`)\*\*: Built on `networkx` with support for `ring` (chordal ring), `mesh` (complete graph), `random` (Erdős–Rényi), and `scale-free` (Barabási–Albert) graphs, plus node isolation/failure injection.
\- \*\*Monte Carlo Benchmarking (`metrics.py`)\*\*: Runs multi-seed ensembles and calculates full-reach success rate, average reached nodes, mean/median (`p50`) convergence time, tail latency (`p95`), suppressed duplicates, and dropped packets.
\- \*\*Exports \& Diagnostics (`reporting.py`)\*\*: Exports structured JSON reports, flat CSV run logs, and two-panel `matplotlib` charts showing node reachability status and convergence time distributions.

\## Architecture
```text

network-resilience-sim/
├── pyproject.toml              # Package metadata, dependencies, and `netsim` entry point
├── README.md
├── src/
│   └── netsim/
│       ├── \_\_init\_\_.py
│       ├── topology.py         # Graph construction and node disconnection
│       ├── engine.py           # Discrete-event heap scheduler \& node state machine
│       ├── metrics.py          # Monte Carlo statistical aggregation
│       ├── reporting.py        # JSON, CSV, and matplotlib PNG exporters
│       └── cli.py              # Command-line interface (`netsim`)
└── tests/
&#x20;   └── test\_engine.py        # Automated pytest suite

## credits
- i created ts gng 
