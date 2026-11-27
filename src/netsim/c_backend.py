from ctypes import (
    CDLL,
    POINTER,
    Structure,
    c_double,
    c_int,
    byref,
)
from pathlib import Path
import platform
import shutil
import subprocess
import networkx as nx
from .engine import SimulationConfig, SimulationResult


class _CSimConfig(Structure):
    _fields_ = [
        ("num_nodes", c_int),
        ("seed", c_int),
        ("delay_min", c_double),
        ("delay_max", c_double),
        ("packet_loss", c_double),
        ("reboot_at", c_double),
        ("reboot_duration", c_double),
        ("source_node", c_int),
        ("smart_fanout", c_int),
        ("max_retries", c_int),
    ]


class _CSimResult(Structure):
    _fields_ = [
        ("seed", c_int),
        ("total_nodes", c_int),
        ("reached_count", c_int),
        ("elapsed_time", c_double),
        ("full_reach_achieved", c_int),
        ("full_reach_time", c_double),
        ("duplicates_suppressed", c_int),
        ("packets_dropped", c_int),
        ("retries_scheduled", c_int),
        ("reboots_triggered", c_int),
        ("smart_efficiency", c_double),
    ]


def default_source_path() -> Path:
    pkg_src = Path(__file__).resolve().parent / "native" / "smart_sim.c"
    if pkg_src.exists():
        return pkg_src
    root = Path(__file__).resolve().parents[2]
    return root / "native" / "smart_sim.c"


def default_dll_path() -> Path:
    sys_name = platform.system()
    ext = ".dll" if sys_name == "Windows" else (".dylib" if sys_name == "Darwin" else ".so")
    lib_name = f"smart_sim{ext}"
    pkg_lib = Path(__file__).resolve().parent / "native" / lib_name
    if pkg_lib.exists():
        return pkg_lib
    root = Path(__file__).resolve().parents[2]
    return root / "native" / lib_name


def _find_64bit_gcc() -> str | None:
    candidates = [
        Path(r"C:\msys64\mingw64\bin\gcc.exe"),
        Path(r"C:\msys64\ucrt64\bin\gcc.exe"),
    ]
    for cand in candidates:
        if cand.exists():
            return str(cand)
    found = shutil.which("gcc")
    if found:
        try:
            machine = subprocess.check_output(
                [found, "-dumpmachine"], text=True, stderr=subprocess.DEVNULL
            ).strip()
            if any(k in machine for k in ("x86_64", "amd64", "aarch64", "arm64", "mingw64", "ucrt", "linux", "darwin")):
                return found
        except OSError:
            pass
    return None


def ensure_smart_c_dll(
    dll_path: str | Path | None = None,
    source_path: str | Path | None = None,
    auto_compile: bool = True,
) -> Path:
    """Ensure the compiled DLL exists and is newer than smart_sim.c, rebuilding if needed."""
    dll = Path(dll_path) if dll_path else default_dll_path()
    src = Path(source_path) if source_path else default_source_path()

    needs_build = not dll.exists()
    if not needs_build and src.exists():
        needs_build = src.stat().st_mtime > dll.stat().st_mtime + 1e-6

    if needs_build and auto_compile:
        if not src.exists():
            raise FileNotFoundError(f"C source not found at {src}")
        gcc = _find_64bit_gcc()
        if not gcc:
            raise FileNotFoundError(
                f"Compiled C engine not found at {dll} and no 64-bit gcc found. "
                "Compile manually with: C:\\msys64\\mingw64\\bin\\gcc.exe -O3 -shared -Wall -Wextra "
                "native\\smart_sim.c -o native\\smart_sim.dll -lm"
            )
        dll.parent.mkdir(parents=True, exist_ok=True)
        import os
        env = os.environ.copy()
        gcc_dir = str(Path(gcc).resolve().parent)
        env["PATH"] = gcc_dir + os.pathsep + env.get("PATH", "")
        cmd = [gcc, "-O3", "-shared", "-Wall", "-Wextra", str(src), "-o", str(dll), "-lm"]
        subprocess.run(cmd, check=True, env=env)

    if not dll.exists():
        raise FileNotFoundError(f"Compiled C engine not found at {dll}")
    return dll


def load_smart_c_lib(
    dll_path: str | Path | None = None,
    source_path: str | Path | None = None,
    auto_compile: bool = True,
) -> CDLL:
    path = ensure_smart_c_dll(dll_path=dll_path, source_path=source_path, auto_compile=auto_compile)
    lib = CDLL(str(path))
    lib.netsim_run_smart_c.argtypes = [
        POINTER(_CSimConfig),
        POINTER(c_int),
        POINTER(c_int),
        POINTER(c_double),
        POINTER(c_int),
        POINTER(_CSimResult),
        POINTER(c_int),
        POINTER(c_double),
    ]
    lib.netsim_run_smart_c.restype = None
    return lib


def _graph_to_csr(
    graph: nx.Graph, num_nodes: int, cfg: SimulationConfig
) -> tuple[list[int], list[int], list[float]]:
    row_ptr = [0] * (num_nodes + 1)
    col_idx: list[int] = []
    edge_cost: list[float] = []

    for u in range(num_nodes):
        nbrs = sorted(graph.neighbors(u)) if u in graph else []
        for v in nbrs:
            col_idx.append(v)
            rtt_u = graph.nodes[u].get("rtt") if u in graph else None
            rtt_v = graph.nodes[v].get("rtt") if v in graph else None
            rtts = [x for x in (rtt_u, rtt_v) if isinstance(x, (int, float)) and x >= 0]
            if rtts:
                edge_cost.append(max(0.0001, max(rtts)))
            else:
                edge_cost.append(max(0.0001, (cfg.delay_min + cfg.delay_max) * 0.5))
        row_ptr[u + 1] = len(col_idx)

    return row_ptr, col_idx, edge_cost


def run_smart_c_simulation(
    graph: nx.Graph,
    cfg: SimulationConfig,
    smart_fanout: int = 3,
    max_retries: int = 1,
    dll_path: str | Path | None = None,
    auto_compile: bool = True,
) -> tuple[SimulationResult, float]:
    """Execute the smart C discrete-event engine and return (SimulationResult, smart_efficiency)."""
    lib = load_smart_c_lib(dll_path=dll_path, auto_compile=auto_compile)
    n = cfg.num_nodes
    row_ptr, col_idx, edge_cost = _graph_to_csr(graph, n, cfg)
    disc_flags = [1 if i in cfg.disconnected else 0 for i in range(n)]

    c_cfg = _CSimConfig(
        num_nodes=n,
        seed=cfg.seed,
        delay_min=cfg.delay_min,
        delay_max=cfg.delay_max,
        packet_loss=cfg.packet_loss,
        reboot_at=cfg.reboot_at,
        reboot_duration=cfg.reboot_duration,
        source_node=cfg.source_node,
        smart_fanout=smart_fanout,
        max_retries=max_retries,
    )
    c_res = _CSimResult()

    arr_row = (c_int * len(row_ptr))(*row_ptr)
    arr_col = (c_int * max(1, len(col_idx)))(*(col_idx or [0]))
    arr_cost = (c_double * max(1, len(edge_cost)))(*(edge_cost or [0.001]))
    arr_disc = (c_int * max(1, len(disc_flags)))(*(disc_flags or [0]))
    out_reached_arr = (c_int * max(1, n))()
    out_arrival_arr = (c_double * max(1, n))()

    lib.netsim_run_smart_c(
        byref(c_cfg),
        arr_row,
        arr_col,
        arr_cost,
        arr_disc,
        byref(c_res),
        out_reached_arr,
        out_arrival_arr,
    )

    reached_set: set[int] = set()
    arrival_times: dict[int, float] = {}
    for i in range(n):
        if out_reached_arr[i]:
            reached_set.add(i)
            arrival_times[i] = float(out_arrival_arr[i])

    sim_res = SimulationResult(
        seed=c_res.seed,
        total_nodes=c_res.total_nodes,
        reached=reached_set,
        arrival_times=arrival_times,
        elapsed_time=c_res.elapsed_time,
        full_reach_achieved=bool(c_res.full_reach_achieved),
        full_reach_time=c_res.full_reach_time if c_res.full_reach_achieved else None,
        duplicates_suppressed=c_res.duplicates_suppressed,
        packets_dropped=c_res.packets_dropped,
        reboots_triggered=c_res.reboots_triggered,
        disconnected=sorted(cfg.disconnected),
    )
    return sim_res, c_res.smart_efficiency