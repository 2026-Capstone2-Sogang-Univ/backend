"""
Tests for the Kosaraju SCC algorithm used to filter routable edges.

The graph format used: dict[str, set[str]] — node → set of outgoing nodes.
"""

from tests.test_passenger_spawn import _traci_stub  # noqa: F401  ensure stub loaded

from app.simulation import _kosaraju_scc


# ---------------------------------------------------------------------------
# Pure-algorithm tests (no TraCI required)
# ---------------------------------------------------------------------------

def test_empty_graph_returns_empty():
    assert _kosaraju_scc({}) == []


def test_single_node_one_scc():
    sccs = _kosaraju_scc({"a": set()})
    assert len(sccs) == 1
    assert sccs[0] == {"a"}


def test_two_nodes_one_way_two_sccs():
    # a → b but no return path → 두 개의 size-1 SCC
    sccs = _kosaraju_scc({"a": {"b"}, "b": set()})
    sets = sorted([frozenset(s) for s in sccs], key=lambda s: sorted(s))
    assert sets == [frozenset({"a"}), frozenset({"b"})]


def test_two_nodes_cycle_one_scc():
    # a ⇄ b → 단일 SCC
    sccs = _kosaraju_scc({"a": {"b"}, "b": {"a"}})
    assert len(sccs) == 1
    assert sccs[0] == {"a", "b"}


def test_three_node_cycle_one_scc():
    # a → b → c → a
    graph = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
    sccs = _kosaraju_scc(graph)
    assert len(sccs) == 1
    assert sccs[0] == {"a", "b", "c"}


def test_two_disjoint_cycles():
    # {a,b}는 사이클, {c,d}도 사이클, 두 사이클 간 연결 없음
    graph = {
        "a": {"b"}, "b": {"a"},
        "c": {"d"}, "d": {"c"},
    }
    sccs = _kosaraju_scc(graph)
    sets = sorted([frozenset(s) for s in sccs], key=lambda s: sorted(s))
    assert sets == [frozenset({"a", "b"}), frozenset({"c", "d"})]


def test_cycle_with_appendix_branch():
    # 메인 사이클 {a, b, c} + 외부 노드 d (a → d, d는 어디로도 안 감)
    # → SCC: {a,b,c}, {d}
    graph = {
        "a": {"b", "d"},
        "b": {"c"},
        "c": {"a"},
        "d": set(),
    }
    sccs = _kosaraju_scc(graph)
    sccs_sorted = sorted(sccs, key=lambda s: -len(s))
    assert sccs_sorted[0] == {"a", "b", "c"}
    assert sccs_sorted[1] == {"d"}


def test_largest_scc_selection():
    # 큰 SCC {a,b,c,d}와 작은 SCC {e,f} — max(sccs, key=len)이 큰 것 반환
    graph = {
        "a": {"b"}, "b": {"c"}, "c": {"d"}, "d": {"a"},
        "e": {"f"}, "f": {"e"},
    }
    sccs = _kosaraju_scc(graph)
    largest = max(sccs, key=len)
    assert largest == {"a", "b", "c", "d"}


def test_isolated_nodes_each_one_scc():
    # 모든 노드가 고립 → 각각 size-1 SCC
    graph = {"a": set(), "b": set(), "c": set()}
    sccs = _kosaraju_scc(graph)
    assert len(sccs) == 3
    assert all(len(s) == 1 for s in sccs)


def test_complex_graph():
    # 복합 케이스:
    # SCC1: {1,2,3} cycle
    # SCC2: {4,5} cycle, reachable from 3
    # SCC3: {6} sink, reachable from 5
    graph = {
        "1": {"2"}, "2": {"3"}, "3": {"1", "4"},
        "4": {"5"}, "5": {"4", "6"},
        "6": set(),
    }
    sccs = _kosaraju_scc(graph)
    sccs_by_size = sorted(sccs, key=len, reverse=True)
    assert {"1", "2", "3"} in [set(s) for s in sccs_by_size]
    assert {"4", "5"} in [set(s) for s in sccs_by_size]
    assert {"6"} in [set(s) for s in sccs_by_size]
