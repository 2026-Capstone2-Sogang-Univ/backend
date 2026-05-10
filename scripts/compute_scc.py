"""
SCC 사전 계산: .net.xml에서 가장 큰 강하게 연결된 컴포넌트(SCC)를 찾아 JSON으로 저장.

런타임에 매번 SCC를 다시 계산하지 않도록, 지도가 변경됐을 때 1회 실행하면 됨.

사용법:
    python scripts/compute_scc.py \
        --net    sumo_service/sumo_configs/NY/manhattan_car_only.net.xml \
        --output sumo_service/sumo_configs/NY/routable_scc.json

의존성 (서비스 컨테이너 외부에서만 필요):
    pip install sumolib
"""

import argparse
import json
import sys
from pathlib import Path

import sumolib


def _is_passenger_routable(edge) -> bool:
    """At least one lane allows passenger vehicles."""
    for lane in edge.getLanes():
        allowed = lane.getPermissions()
        # 빈 집합 → SUMO 기본값(전체 허용); "passenger" 포함 시도 허용
        if not allowed or "passenger" in allowed:
            return True
    return False


def kosaraju_scc(graph: dict[str, set[str]]) -> list[set[str]]:
    """Iterative Kosaraju — graph: node → set of outgoing nodes."""
    visited: set[str] = set()
    finish_order: list[str] = []
    for start in graph:
        if start in visited:
            continue
        visited.add(start)
        stack: list = [(start, iter(graph[start]))]
        while stack:
            node, neighbors = stack[-1]
            advanced = False
            for w in neighbors:
                if w not in visited:
                    visited.add(w)
                    stack.append((w, iter(graph.get(w, set()))))
                    advanced = True
                    break
            if not advanced:
                finish_order.append(node)
                stack.pop()

    rgraph: dict[str, set[str]] = {v: set() for v in graph}
    for v, outs in graph.items():
        for w in outs:
            rgraph.setdefault(w, set()).add(v)

    visited = set()
    sccs: list[set[str]] = []
    for start in reversed(finish_order):
        if start in visited:
            continue
        scc: set[str] = set()
        rstack = [start]
        visited.add(start)
        while rstack:
            node = rstack.pop()
            scc.add(node)
            for w in rgraph.get(node, set()):
                if w not in visited:
                    visited.add(w)
                    rstack.append(w)
        sccs.append(scc)
    return sccs


def main() -> None:
    parser = argparse.ArgumentParser(description=".net.xml → 최대 SCC JSON 변환")
    parser.add_argument("--net", required=True, help="입력 SUMO net.xml 경로")
    parser.add_argument("--output", required=True, help="출력 JSON 경로")
    args = parser.parse_args()

    print(f"[1/4] 네트워크 로드: {args.net}")
    net = sumolib.net.readNet(args.net, withInternal=False)

    print(f"[2/4] 통행 가능 엣지 필터링")
    routable_edges = [e for e in net.getEdges() if _is_passenger_routable(e)]
    print(f"      통행 가능: {len(routable_edges):,}개")
    if not routable_edges:
        print("ERROR: 통행 가능 엣지가 없습니다.")
        sys.exit(1)

    print(f"[3/4] 그래프 구축 + SCC 계산")
    edge_ids = {e.getID() for e in routable_edges}
    graph: dict[str, set[str]] = {}
    for edge in routable_edges:
        edge_id = edge.getID()
        outgoing: set[str] = set()
        for next_edge in edge.getOutgoing():
            next_id = next_edge.getID()
            if next_id in edge_ids and next_id != edge_id:
                outgoing.add(next_id)
        graph[edge_id] = outgoing

    sccs = kosaraju_scc(graph)
    largest = max(sccs, key=len)
    pct = len(largest) / len(graph) * 100
    print(f"      가장 큰 SCC: {len(largest):,}개 / 전체 {len(graph):,}개 ({pct:.1f}%)")

    print(f"[4/4] 저장: {args.output}")
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(sorted(largest), f)
    print(f"      완료: {len(largest):,}개 엣지 저장")


if __name__ == "__main__":
    main()
