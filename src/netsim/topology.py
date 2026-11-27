import networkx as nx


def build_topology(
    kind: str,
    num_nodes: int,
    seed: int = 42,
    disconnected: set[int] | None = None,
) -> nx.Graph:
    """Create a network graph and remove links for disconnected nodes."""
    disconnected = disconnected or set()

    if kind == "ring":
        graph = nx.cycle_graph(num_nodes)
        for node in range(num_nodes // 2):
            graph.add_edge(node, node + num_nodes // 2)
    elif kind == "mesh":
        graph = nx.complete_graph(num_nodes)
    elif kind == "random":
        graph = nx.erdos_renyi_graph(num_nodes, p=0.3, seed=seed)
    elif kind == "scale-free":
        m = min(2, max(1, num_nodes - 1))
        graph = nx.barabasi_albert_graph(num_nodes, m=m, seed=seed)
    else:
        raise ValueError(f"Unknown topology type: {kind}")

    for node in disconnected:
        if node in graph:
            edges = list(graph.edges(node))
            graph.remove_edges_from(edges)

    return graph
