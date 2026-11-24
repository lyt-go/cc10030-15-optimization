"""路径还原领域服务：将轨迹分段匹配到路网路段，执行缺口插值和路径还原，生成路网匹配数据集。

在多批次交接场景下，本服务在原有单批次还原之外补充了一套“交接作业”能力：

* 道路图（graph）按版本登记，同一历史日期可以同时存在多张有效图；
* 每个批次固定一张实际采用的图，单个还原结果不允许把新旧路网的路段拼接在一起；
* 交付号（delivery_no）冻结交付时的里程与分段，历史数据仍可按交付号对账；
* 补算只产出“差额 + 对应分段”的补充记录，不改写已交付值；
* 续做时在“重试申请之后、正式交付之前”发现新图，并在同一次核对中给出
  最终线路、相对原计划的里程差，以及其他已确认批次的原值。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService

# 场景中使用的两张道路图编号（新图在重试申请之后才登记）。
GRAPH_OLD = "RN-2026-09-01"
GRAPH_NEW = "RN-2026-09-08"

# 场景中的四个批次，跨三个日期。
BATCH_DELIVERED_FULL = "BATCH-2026-08-20"
BATCH_DELIVERED_PARTIAL = "BATCH-2026-08-25"
BATCH_OLD_PARTIAL_CONFIRMED = "BATCH-2026-09-01"
BATCH_RETRY_NEW_GRAPH = "BATCH-2026-09-08"

# 轨迹端点吸附到路网节点的容差（米）。当前场景的分段都以路网节点为端点，
# 保留该常量用于后续按真实轨迹端点最近邻吸附。
SNAP_TOLERANCE_M = 80.0

# 模拟路网节点（仅用于轨迹展示，里程以边的 length_m 为准）。
_ROAD_NODES: dict[str, tuple[float, float]] = {
    "A": (39.9000, 116.4000),
    "B": (39.9000, 116.4200),
    "C": (39.9050, 116.4100),
    "D": (39.8950, 116.4100),
    "E": (39.9000, 116.4250),
}


def _edge(edge_id: str, start: str, end: str, name: str, length_m: float,
          status: str = "open") -> dict[str, Any]:
    return {
        "edge_id": edge_id,
        "from": start,
        "to": end,
        "name": name,
        "length_m": length_m,
        "status": status,
        "bidirectional": True,
    }


class RouteRestoreService(BaseModuleService):
    MODULE = "route_restore"
    ENTITY = "还原路段"
    OPERATION_TERM = "入湖"
    LIST_FIELDS = ["路段编号", "分段编号", "路段序列", "路网编号", "缺口数量", "插值方式", "还原里程", "还原版本"]
    REQUIRED_FIELDS = ["路段编号", "分段编号", "路段序列"]
    STATUS_ORDER = ["草稿", "待还原", "已还原", "还原失败"]
    ACTION_RULES = {"启动还原": "待还原", "确认还原": "已还原", "标记失败": "还原失败"}
    NEGATIVE_ACTIONS = []

    # 模拟路网数据（保留给旧的单批次还原路径）
    _ROAD_NETWORK = [
        {"road_id": "RN001", "name": "长安街", "start_lat": 39.905, "start_lon": 116.39, "end_lat": 39.91, "end_lon": 116.42},
        {"road_id": "RN002", "name": "二环路", "start_lat": 39.90, "start_lon": 116.40, "end_lat": 39.92, "end_lon": 116.41},
        {"road_id": "RN003", "name": "三环路", "start_lat": 39.88, "start_lon": 116.38, "end_lat": 39.93, "end_lon": 116.45},
        {"road_id": "RN004", "name": "朝阳路", "start_lat": 39.91, "start_lon": 116.45, "end_lat": 39.93, "end_lon": 116.47},
        {"road_id": "RN005", "name": "东直门外大街", "start_lat": 39.94, "start_lon": 116.43, "end_lat": 39.95, "end_lon": 116.46},
    ]

    # ------------------------------------------------------------------
    # 旧的单批次还原（保持原有行为）
    # ------------------------------------------------------------------
    def restore_from_segments(self, batch_key: str) -> dict[str, Any]:
        """读取轨迹分段数据集，将每个分段匹配到最近路网路段，执行线性插值还原缺口，生成路径还原数据集。"""
        # 读取轨迹分段
        try:
            seg_svc = self._safe_import("app.services.track_segment", "TrackSegmentService")()
            segments, _ = seg_svc.get_segments(batch_key)
        except Exception:
            seg_file = self._data_dir("track_segments") / f"{batch_key}_segments.jsonl"
            if not seg_file.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的轨迹分段数据不存在")
            segments = self._read_jsonl(seg_file)

        if not segments:
            return {"batch_key": batch_key, "restored_routes": 0, "segments": 0}

        restored = []
        route_id = 0
        total_gaps = 0

        for seg in segments:
            seg_mid_lat = (seg["start_lat"] + seg["end_lat"]) / 2
            seg_mid_lon = (seg["start_lon"] + seg["end_lon"]) / 2

            # 匹配最近路网路段
            best_road = None
            best_dist = float("inf")
            for road in self._ROAD_NETWORK:
                road_mid_lat = (road["start_lat"] + road["end_lat"]) / 2
                road_mid_lon = (road["start_lon"] + road["end_lon"]) / 2
                dist = self._haversine(seg_mid_lat, seg_mid_lon, road_mid_lat, road_mid_lon)
                if dist < best_dist:
                    best_dist = dist
                    best_road = road

            if best_road:
                route_id += 1
                gap_count = max(0, (seg["point_count"] - 1) // 5)
                total_gaps += gap_count
                restored.append({
                    "route_id": route_id,
                    "batch_key": batch_key,
                    "segment_id": seg["segment_id"],
                    "road_id": best_road["road_id"],
                    "road_name": best_road["name"],
                    "start_lat": seg["start_lat"],
                    "start_lon": seg["start_lon"],
                    "end_lat": seg["end_lat"],
                    "end_lon": seg["end_lon"],
                    "gap_count": gap_count,
                    "interpolation": "linear" if gap_count > 0 else "none",
                    "restored_mileage": seg.get("mileage", 0),
                    "road_match_distance": round(best_dist, 2),
                })

        output_file = self._data_dir("route_restore") / f"{batch_key}_routes.jsonl"
        written = self._write_jsonl(output_file, restored)

        # 创建还原记录
        for r in restored:
            entry, _ = self.create_entry({
                "路段编号": f"ROUTE-{batch_key}-{r['route_id']:03d}",
                "分段编号": str(r["segment_id"]),
                "路段序列": r["road_id"],
                "路网编号": r["road_id"],
                "缺口数量": r["gap_count"],
                "插值方式": r["interpolation"],
                "还原里程": f"{r['restored_mileage']}m",
                "还原版本": "v1.0",
            })
            if entry:
                self.run_action(entry["id"], "确认还原")

        return {
            "batch_key": batch_key,
            "segments_processed": len(segments),
            "restored_routes": written,
            "total_gaps": total_gaps,
            "output_file": str(output_file),
        }

    def get_restored_routes(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取批次路径还原数据集文件。"""
        file_path = self._data_dir("route_restore") / f"{batch_key}_routes.jsonl"
        data = self._read_jsonl(file_path)
        return data, str(file_path)

    # ------------------------------------------------------------------
    # 交接作业状态
    # ------------------------------------------------------------------
    def reset_handover(self) -> None:
        """清空交接作业内存状态与检查点，下一次访问时重新装载场景。"""
        self._handover = None
        self._handover_root = None
        cp_file = self._data_dir("checkpoints") / "route_handover_state.json"
        if cp_file.exists():
            cp_file.unlink()

    def _state(self) -> dict[str, Any]:
        import app.services.base as base_mod

        root = str(base_mod.DATA_ROOT)
        # 测试或换数据目录时，旧内存状态必须重新装载，避免串场景。
        if getattr(self, "_handover", None) is None or getattr(self, "_handover_root", None) != root:
            self._handover = None
            self._handover_root = root
            checkpoint = self._read_checkpoint("route_handover_state")
            if checkpoint and checkpoint.get("batches") and checkpoint.get("graphs"):
                self._handover = checkpoint
            else:
                self._handover = {
                    "graphs": {},
                    "batches": {},
                    "deliveries": {},
                    "seq": 0,
                    "clock": 0,
                }
                self._seed_handover()
        return self._handover

    def _persist(self) -> None:
        state = self._state()
        # 避免把内部缓存写成检查点的一部分。
        self._write_checkpoint("route_handover_state", state)

    def _tick(self, step: int = 10) -> int:
        state = self._state()
        # 时钟永远走在“已登记图的最晚登记时刻”之后，避免补算/解除封闭的时间倒退。
        latest = max((g["registered_at"] for g in state["graphs"].values()), default=0)
        latest = max(latest, max(
            (r["requested_at"] for b in state["batches"].values() for r in b["retry_requests"]),
            default=0,
        ))
        state["clock"] = max(int(state.get("clock", 0)), latest) + step
        return state["clock"]

    def _seed_handover(self) -> None:
        """写入跨三个日期的交接场景。"""
        self._register_graph(
            graph_id=GRAPH_OLD,
            name="2026-09-01 历史道路图",
            version="v1.0",
            registered_at=10,
            effective_from="2026-08-01",
            effective_to="2026-09-07",
            edges=[
                _edge("OLD-AC", "A", "C", "旧绕行西段", 700),
                _edge("OLD-CB", "C", "B", "旧绕行东段", 500),
            ],
            description="原计划沿旧路网绕行，A→C→B 合计 1200 米。",
        )
        self._register_graph(
            graph_id=GRAPH_NEW,
            name="2026-09-08 核准道路图",
            version="v2.0",
            registered_at=30,
            effective_from="2026-09-08",
            effective_to="2026-12-31",
            edges=[
                _edge("NEW-AC", "A", "C", "旧绕行西段（新图快照）", 700),
                _edge("NEW-CB", "C", "B", "旧绕行东段（新图快照）", 500),
                _edge("NEW-AD", "A", "D", "核准可通行道路西段", 400),
                _edge("NEW-DB", "D", "B", "核准可通行道路东段", 500),
                _edge("NEW-CD", "C", "D", "新图新增东西向支路", 450),
                _edge("NEW-AB", "A", "B", "直达临时便道", 300, status="closed"),
                _edge("NEW-EB", "E", "B", "东侧联络道", 200),
            ],
            retroactive_dates=["2026-09-01"],
            description="路网部门核准 A→D→B 可通行；直达便道有临时封闭记录。",
        )

        # 1) 历史批次：两个分段都已交接，可按交付号对账。
        delivered = self._make_batch(
            BATCH_DELIVERED_FULL, "2026-08-20", GRAPH_OLD, [("A", "C"), ("C", "B")],
            created_at=5,
        )
        self._deliver_segments(
            delivered, [1, 2], delivery_no="DLV-2026-08-20-001", confirmed_at=40,
        )
        self._record_usage(
            delivered, "首次交付", self._state()["graphs"][GRAPH_OLD],
            delivered["segments"], 0.0, at=40,
        )

        # 2) 旧日批次：只确认首段，剩余分段仍准备交付。
        partial = self._make_batch(
            BATCH_DELIVERED_PARTIAL, "2026-08-25", GRAPH_OLD, [("A", "C"), ("C", "B")],
            created_at=6,
        )
        self._deliver_segments(
            partial, [1], delivery_no="DLV-2026-08-25-001", confirmed_at=41,
        )
        self._record_usage(
            partial, "首次交付", self._state()["graphs"][GRAPH_OLD],
            [partial["segments"][0]], 0.0, at=41,
        )

        # 3) 尚未确认的批次：重试申请发生在 20，新图在 30 才登记。
        resumable = self._make_batch(
            BATCH_OLD_PARTIAL_CONFIRMED, "2026-09-01", GRAPH_OLD, [("A", "B")],
            created_at=7,
        )
        self._request_retry_internal(resumable, requested_at=20)

        # 4) 重试新批：同一路网增加了临时封闭记录，导致一个分段还原异常。
        retry_batch = self._make_batch(
            BATCH_RETRY_NEW_GRAPH, "2026-09-08", GRAPH_NEW, [("A", "B"), ("E", "B")],
            created_at=8,
        )
        self._request_retry_internal(retry_batch, requested_at=40)
        self._ensure_closure(GRAPH_NEW, "NEW-EB", reason="重试新批时的临时封闭记录", at=40)
        self._refresh_pending_segments(retry_batch)

        self._persist()

    def _refresh_pending_segments(self, batch: dict[str, Any]) -> None:
        """按各分段当前使用的图重新取路线；封闭路段会让对应分段转为还原失败。"""
        state = self._state()
        for segment in batch["segments"]:
            if segment["status"] == "已交付":
                continue
            graph = state["graphs"].get(segment["graph_id"])
            route = self._route_on(segment["graph_id"], segment["origin_node"],
                                   segment["dest_node"]) if graph else None
            segment["route"] = route
            segment["current_mileage_m"] = round(route["mileage_m"], 2) if route else None
            segment["mileage_delta_m"] = (
                round(segment["current_mileage_m"] - float(segment["plan_mileage_m"] or 0), 2)
                if route else 0.0
            )
            segment["status"] = "待确认" if route else "还原失败"
            segment["candidate_routes"] = self._candidate_routes_for(
                segment["graph_id"], segment["origin_node"], segment["dest_node"])
        if all(s["status"] == "已交付" for s in batch["segments"]):
            batch["status"] = "已交付"
        elif any(s["status"] == "还原失败" for s in batch["segments"]):
            batch["status"] = "还原异常"
        elif any(s["status"] == "已交付" for s in batch["segments"]):
            batch["status"] = "部分交付"
        else:
            batch["status"] = "待确认"

    # ------------------------------------------------------------------
    # 道路图登记与查询
    # ------------------------------------------------------------------
    def _register_graph(self, graph_id: str, name: str, version: str,
                        registered_at: int, effective_from: str, effective_to: str,
                        edges: list[dict[str, Any]], retroactive_dates: list[str] | None = None,
                        description: str = "") -> dict[str, Any]:
        state = self._state()
        graph = {
            "graph_id": graph_id,
            "name": name,
            "version": version,
            "registered_at": registered_at,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "retroactive_dates": list(retroactive_dates or []),
            "status": "有效",
            "description": description,
            "nodes": {node: {"lat": coord[0], "lon": coord[1]} for node, coord in _ROAD_NODES.items()},
            "edges": [dict(edge) for edge in edges],
            "closures": [],
        }
        state["graphs"][graph_id] = graph
        return graph

    def list_graphs(self, valid_on: str | None = None) -> list[dict[str, Any]]:
        """列出已登记道路图；传入日期时只返回该日期有效的图。"""
        state = self._state()
        graphs = sorted(state["graphs"].values(), key=lambda g: g["registered_at"])
        if valid_on:
            graphs = [g for g in graphs if self._graph_valid_on(g, valid_on)]
        return [self._graph_summary(g) for g in graphs]

    @staticmethod
    def _graph_valid_on(graph: dict[str, Any], date: str) -> bool:
        if date in graph.get("retroactive_dates", []):
            return True
        return graph["effective_from"] <= date <= graph["effective_to"]

    @staticmethod
    def _graph_summary(graph: dict[str, Any]) -> dict[str, Any]:
        closure_edges = [
            edge["edge_id"] for edge in graph["edges"] if edge.get("status") == "closed"
        ]
        return {
            "graph_id": graph["graph_id"],
            "name": graph["name"],
            "version": graph["version"],
            "registered_at": graph["registered_at"],
            "effective_from": graph["effective_from"],
            "effective_to": graph["effective_to"],
            "retroactive_dates": list(graph.get("retroactive_dates", [])),
            "status": graph["status"],
            "edge_count": len(graph["edges"]),
            "closed_edges": closure_edges,
            "closures": [dict(c) for c in graph.get("closures", [])],
            "description": graph.get("description", ""),
        }

    def _ensure_closure(self, graph_id: str, edge_id: str, reason: str,
                        at: int | None = None) -> dict[str, Any]:
        state = self._state()
        graph = state["graphs"][graph_id]
        edge = next((e for e in graph["edges"] if e["edge_id"] == edge_id), None)
        if edge is None:
            raise KeyError(f"道路图 {graph_id} 中不存在路段 {edge_id}")
        edge["status"] = "closed"
        existing = next(
            (c for c in graph["closures"] if c["edge_id"] == edge_id and c.get("active", True)),
            None,
        )
        if existing:
            return existing
        closure = {
            "closure_id": f"CLS-{graph_id}-{edge_id}",
            "graph_id": graph_id,
            "edge_id": edge_id,
            "road_name": edge["name"],
            "reason": reason,
            "temporary": True,
            "active": True,
            "created_at": at if at is not None else self._tick(),
        }
        graph["closures"].append(closure)
        return closure

    def release_closure(self, graph_id: str, edge_id: str) -> dict[str, Any]:
        """解除临时封闭，后续补算即可把该路段纳入可走路线。"""
        state = self._state()
        graph = state["graphs"].get(graph_id)
        if graph is None:
            raise KeyError(f"道路图 {graph_id} 不存在")
        edge = next((e for e in graph["edges"] if e["edge_id"] == edge_id), None)
        if edge is None:
            raise KeyError(f"道路图 {graph_id} 中不存在路段 {edge_id}")
        edge["status"] = "open"
        for closure in graph["closures"]:
            if closure["edge_id"] == edge_id:
                closure["active"] = False
                closure["released_at"] = self._tick()
        self._persist()
        return {"graph_id": graph_id, "edge_id": edge_id, "status": "open",
                "message": f"路段 {edge_id} 已解除封闭"}

    # ------------------------------------------------------------------
    # 路径计算：一次还原只允许使用一张图内的路段
    # ------------------------------------------------------------------
    def _enumerate_paths(self, graph: dict[str, Any], origin: str, dest: str,
                         limit: int = 8) -> list[dict[str, Any]]:
        edges = graph["edges"]
        adjacency: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            adjacency.setdefault(edge["from"], []).append(edge)
            if edge.get("bidirectional", True):
                adjacency.setdefault(edge["to"], []).append({**edge, "from": edge["to"], "to": edge["from"]})

        results: list[dict[str, Any]] = []

        def walk(node: str, visited: set[str], used: list[dict[str, Any]]) -> None:
            if len(results) >= limit * 4:
                return
            if node == dest and used:
                ordered: list[dict[str, Any]] = []
                names: list[str] = []
                node_sequence = [origin]
                for item in used:
                    edge_id = item["edge_id"]
                    if edge_id not in [e["edge_id"] for e in ordered]:
                        ordered.append(item)
                        names.append(item["name"])
                    node_sequence.append(item["to"])
                results.append({
                    "graph_id": graph["graph_id"],
                    "origin": origin,
                    "dest": dest,
                    "edge_ids": [e["edge_id"] for e in ordered],
                    "road_names": names,
                    "node_sequence": node_sequence,
                    "mileage_m": round(sum(e["length_m"] for e in ordered), 2),
                    "uses_closed": any(e.get("status") == "closed" for e in ordered),
                    "closed_edges": [e["edge_id"] for e in ordered if e.get("status") == "closed"],
                })
                return
            for edge in adjacency.get(node, []):
                if edge["to"] in visited:
                    continue
                walk(edge["to"], visited | {edge["to"]}, used + [edge])

        walk(origin, {origin}, [])
        # 去重并按“是否封闭、里程”排序，可走路线排前面。
        unique: dict[tuple[str, ...], dict[str, Any]] = {}
        for path in results:
            unique.setdefault(tuple(path["edge_ids"]), path)
        return sorted(unique.values(), key=lambda p: (p["uses_closed"], p["mileage_m"]))

    def _route_on(self, graph_id: str, origin: str, dest: str,
                  allow_closed: bool = False) -> dict[str, Any] | None:
        state = self._state()
        graph = state["graphs"].get(graph_id)
        if graph is None:
            return None
        if origin not in graph["nodes"] or dest not in graph["nodes"]:
            return None
        paths = self._enumerate_paths(graph, origin, dest)
        if not paths:
            return None
        open_paths = [p for p in paths if not p["uses_closed"]]
        if open_paths:
            return open_paths[0]
        return paths[0] if allow_closed else None

    @staticmethod
    def _validate_single_graph(graph: dict[str, Any], edge_ids: list[str]) -> None:
        edge_set = {edge["edge_id"] for edge in graph["edges"]}
        foreign = [edge_id for edge_id in edge_ids if edge_id not in edge_set]
        if foreign:
            raise ValueError(
                f"还原结果使用了道路图 {graph['graph_id']} 之外的路段：{foreign}，"
                "不允许拼接新旧路网"
            )

    # ------------------------------------------------------------------
    # 批次与分段
    # ------------------------------------------------------------------
    @staticmethod
    def _timestamp_for(batch_date: str, index: int) -> str:
        return f"{batch_date}T08:{index // 60:02d}:{index % 60:02d}"

    def _segment(self, batch_key: str, batch_date: str, graph_id: str,
                 segment_id: int, origin: str, dest: str) -> dict[str, Any]:
        state = self._state()
        graph = state["graphs"][graph_id]
        nodes = graph["nodes"]
        route = self._route_on(graph_id, origin, dest)
        closed_route = self._route_on(graph_id, origin, dest, allow_closed=True)
        plan_route = route or closed_route
        start = nodes.get(origin, _ROAD_NODES["A"])
        end = nodes.get(dest, _ROAD_NODES["B"])
        trajectory = []
        for step in range(5):
            ratio = step / 4
            trajectory.append({
                "point_id": step + 1,
                "lat": round(start["lat"] + (end["lat"] - start["lat"]) * ratio, 6),
                "lon": round(start["lon"] + (end["lon"] - start["lon"]) * ratio, 6),
                "timestamp": self._timestamp_for(batch_date, segment_id * 10 + step),
            })
        return {
            "segment_id": segment_id,
            "batch_key": batch_key,
            "origin_node": origin,
            "dest_node": dest,
            "start_lat": start["lat"],
            "start_lon": start["lon"],
            "end_lat": end["lat"],
            "end_lon": end["lon"],
            "point_count": len(trajectory),
            "graph_id": graph_id,
            "route": route,
            "plan_route": plan_route,
            "plan_mileage_m": round(plan_route["mileage_m"], 2) if plan_route else 0.0,
            "current_mileage_m": round(route["mileage_m"], 2) if route else None,
            "delivered_mileage_m": None,
            "delivery_no": None,
            "mileage_delta_m": 0.0,
            "status": "待确认" if route else "还原失败",
            "original_trajectory": trajectory,
            "candidate_routes": self._candidate_routes_for(graph_id, origin, dest),
        }

    def _candidate_routes_for(self, graph_id: str, origin: str, dest: str) -> list[dict[str, Any]]:
        state = self._state()
        graph = state["graphs"].get(graph_id)
        if graph is None:
            return []
        candidates = []
        for index, path in enumerate(self._enumerate_paths(graph, origin, dest)[:3], start=1):
            candidates.append({
                "rank": index,
                "graph_id": graph_id,
                "edge_ids": path["edge_ids"],
                "road_names": path["road_names"],
                "mileage_m": path["mileage_m"],
                "usable": not path["uses_closed"],
                "closed_edges": path["closed_edges"],
            })
        return candidates

    def _make_batch(self, batch_key: str, batch_date: str, graph_id: str,
                    segment_pairs: list[tuple[str, str]], created_at: int) -> dict[str, Any]:
        state = self._state()
        segments = [
            self._segment(batch_key, batch_date, graph_id, index, origin, dest)
            for index, (origin, dest) in enumerate(segment_pairs, start=1)
        ]
        abnormal = any(seg["status"] == "还原失败" for seg in segments)
        batch = {
            "batch_key": batch_key,
            "batch_date": batch_date,
            "graph_id": graph_id,
            "status": "还原异常" if abnormal else "待确认",
            "delivery_no": None,
            "segments": segments,
            "original_plan": {
                "graph_id": graph_id,
                "mileage_m": round(sum(seg["plan_mileage_m"] for seg in segments), 2),
                "description": "首次还原时采用的原计划线路",
            },
            "retry_requests": [],
            "graph_usages": [],
            "created_at": created_at,
            "updated_at": created_at,
        }
        state["batches"][batch_key] = batch
        return batch

    def _record_usage(self, batch: dict[str, Any], event: str, graph: dict[str, Any],
                      segments: list[dict[str, Any]], delta_m: float | None,
                      at: int | None = None) -> dict[str, Any]:
        """把“本次实际采用的道路图”和里程差挂到批次上，便于按批次回溯每次重跑。"""
        usage = {
            "seq": len(batch.get("graph_usages", [])) + 1,
            "event": event,
            "graph_id": graph["graph_id"],
            "graph_version": graph["version"],
            "graph_registered_at": graph["registered_at"],
            "segment_ids": [s["segment_id"] for s in segments],
            "mileage_m": round(sum(float(s["current_mileage_m"] or 0) for s in segments), 2),
            "mileage_delta_m": delta_m,
            "closed_edges": [e["edge_id"] for e in graph["edges"] if e.get("status") == "closed"],
            "at": at if at is not None else self._tick(),
        }
        batch.setdefault("graph_usages", []).append(usage)
        return usage

    def _batch_summary(self, batch: dict[str, Any]) -> dict[str, Any]:
        delivered = [s for s in batch["segments"] if s["status"] == "已交付"]
        pending = [s for s in batch["segments"] if s["status"] != "已交付"]
        failed = [s for s in batch["segments"] if s["status"] == "还原失败"]
        delivered_mileage = round(sum(float(s["delivered_mileage_m"] or 0) for s in delivered), 2)
        remaining_mileage = round(sum(
            float(s["current_mileage_m"] if s["current_mileage_m"] is not None
                  else s["plan_mileage_m"] or 0)
            for s in pending
        ), 2)
        graphs = sorted({s["graph_id"] for s in batch["segments"]})
        return {
            "batch_key": batch["batch_key"],
            "batch_date": batch["batch_date"],
            "status": batch["status"],
            "graph_id": batch["graph_id"],
            "actual_graph_ids": graphs,
            "delivery_no": batch["delivery_no"],
            "segment_count": len(batch["segments"]),
            "delivered_segments": [s["segment_id"] for s in delivered],
            "pending_segments": [s["segment_id"] for s in pending],
            "failed_segments": [s["segment_id"] for s in failed],
            "delivered_mileage_m": delivered_mileage,
            "remaining_mileage_m": remaining_mileage,
            "original_plan": dict(batch["original_plan"]),
            "retry_requests": [dict(r) for r in batch["retry_requests"]],
            "graph_usages": [dict(u) for u in batch.get("graph_usages", [])],
            "segments": [self._segment_view(batch, s) for s in batch["segments"]],
        }

    def _segment_view(self, batch: dict[str, Any], segment: dict[str, Any]) -> dict[str, Any]:
        route = segment.get("route")
        return {
            "segment_id": segment["segment_id"],
            "batch_key": batch["batch_key"],
            "origin_node": segment["origin_node"],
            "dest_node": segment["dest_node"],
            "graph_id": segment["graph_id"],
            "status": segment["status"],
            "edge_ids": list(route["edge_ids"]) if route else [],
            "road_names": list(route["road_names"]) if route else [],
            "mileage_m": segment["current_mileage_m"],
            "plan_mileage_m": segment["plan_mileage_m"],
            "delivered_mileage_m": segment["delivered_mileage_m"],
            "delivery_no": segment["delivery_no"],
            "mileage_delta_m": segment["mileage_delta_m"],
            "candidate_routes": [dict(c) for c in segment.get("candidate_routes", [])],
            "original_trajectory": list(segment.get("original_trajectory", [])),
            "start_lat": segment.get("start_lat"),
            "start_lon": segment.get("start_lon"),
            "end_lat": segment.get("end_lat"),
            "end_lon": segment.get("end_lon"),
            "point_count": segment.get("point_count"),
            "source_mileage_m": segment.get("source_mileage_m"),
        }

    # ------------------------------------------------------------------
    # 交付
    # ------------------------------------------------------------------
    def _next_delivery_no(self, batch: dict[str, Any]) -> str:
        state = self._state()
        while True:
            state["seq"] = int(state.get("seq", 0)) + 1
            candidate = f"DLV-{batch['batch_date']}-{state['seq']:03d}"
            if candidate not in state["deliveries"]:
                return candidate

    def _freeze_delivery(self, batch: dict[str, Any], segment_ids: list[int],
                         delivery_no: str, confirmed_at: int) -> dict[str, Any]:
        state = self._state()
        snapshot = state["deliveries"].get(delivery_no)
        if snapshot is None:
            snapshot = {
                "delivery_no": delivery_no,
                "batch_key": batch["batch_key"],
                "batch_date": batch["batch_date"],
                "graph_id": batch["graph_id"],
                "graph_version": state["graphs"][batch["graph_id"]]["version"],
                "segments": [],
                "mileage_m": 0.0,
                "confirmed_at": confirmed_at,
                "frozen": True,
            }
            state["deliveries"][delivery_no] = snapshot
        for segment in batch["segments"]:
            if segment["segment_id"] not in segment_ids:
                continue
            if segment.get("route") is None:
                raise ValueError(
                    f"批次 {batch['batch_key']} 的分段 {segment['segment_id']} 尚无可用还原结果"
                )
            self._validate_single_graph(
                state["graphs"][segment["graph_id"]], segment["route"]["edge_ids"])
            segment["status"] = "已交付"
            segment["delivery_no"] = delivery_no
            segment["delivered_mileage_m"] = round(float(segment["current_mileage_m"] or 0), 2)
            if not any(item["segment_id"] == segment["segment_id"] for item in snapshot["segments"]):
                snapshot["segments"].append({
                    "segment_id": segment["segment_id"],
                    "graph_id": segment["graph_id"],
                    "edge_ids": list(segment["route"]["edge_ids"]),
                    "mileage_m": segment["delivered_mileage_m"],
                })
        # 交付号冻结的是“这一天实际采用的图”，按已交付分段回填，避免沿用旧图编号。
        used_graphs = {item["graph_id"] for item in snapshot["segments"]}
        if len(used_graphs) == 1:
            snapshot["graph_id"] = next(iter(used_graphs))
            snapshot["graph_version"] = state["graphs"][snapshot["graph_id"]]["version"]
        else:
            snapshot["graph_id"] = "+".join(sorted(used_graphs)) or snapshot["graph_id"]
            snapshot["graph_version"] = "多图"
        snapshot["mixed_graph_used"] = len(used_graphs) > 1
        snapshot["mileage_m"] = round(sum(item["mileage_m"] for item in snapshot["segments"]), 2)
        if all(s["status"] == "已交付" for s in batch["segments"]):
            batch["status"] = "已交付"
        else:
            batch["status"] = "部分交付"
        batch["delivery_no"] = batch["delivery_no"] or delivery_no
        batch["updated_at"] = confirmed_at
        return snapshot

    def _deliver_segments(self, batch: dict[str, Any], segment_ids: list[int],
                          delivery_no: str | None = None,
                          confirmed_at: int | None = None) -> dict[str, Any]:
        delivery_no = delivery_no or self._next_delivery_no(batch)
        confirmed_at = confirmed_at if confirmed_at is not None else self._tick()
        return self._freeze_delivery(batch, segment_ids, delivery_no, confirmed_at)

    # ------------------------------------------------------------------
    # 重试申请与续做
    # ------------------------------------------------------------------
    def _request_retry_internal(self, batch: dict[str, Any], requested_at: int) -> dict[str, Any]:
        existing = [r for r in batch["retry_requests"] if r["status"] != "已完成"]
        if existing:
            return existing[-1]
        request = {
            "request_id": f"RETRY-{batch['batch_key']}-{len(batch['retry_requests']) + 1:02d}",
            "batch_key": batch["batch_key"],
            "requested_at": requested_at,
            "status": "待接续",
            "delivered_before_request": any(
                s["status"] == "已交付" for s in batch["segments"]
            ),
        }
        batch["retry_requests"].append(request)
        batch["updated_at"] = requested_at
        return request

    def request_retry(self, batch_key: str) -> dict[str, Any]:
        """登记重试申请。正式交付之前登记新图时，续做会在同一次核对中发现它。"""
        batch = self._get_batch(batch_key)
        if batch["status"] == "已交付":
            return {
                "ok": False,
                "message": f"批次 {batch_key} 已全部交付，重试申请不会改写交付值",
                "batch": self._batch_summary(batch),
            }
        requested_at = self._tick()
        request = self._request_retry_internal(batch, requested_at)
        # 重试新批时，同一路网补充一条临时封闭记录（幂等）。
        added_closure = None
        if batch_key == BATCH_RETRY_NEW_GRAPH and batch["graph_id"] == GRAPH_NEW:
            added_closure = self._ensure_closure(
                GRAPH_NEW, "NEW-EB", reason="重试新批时的临时封闭记录", at=requested_at,
            )
        self._persist()
        return {
            "ok": True,
            "batch_key": batch_key,
            "retry_request": dict(request),
            "source_graph_id": batch["graph_id"],
            "added_closure": dict(added_closure) if added_closure else None,
            "message": "重试申请已登记，等待续做",
        }

    def _get_batch(self, batch_key: str) -> dict[str, Any]:
        state = self._state()
        batch = state["batches"].get(batch_key)
        if batch is None:
            raise KeyError(f"交接批次 {batch_key} 不存在")
        return batch

    def _pick_adopted_graph(self, batch: dict[str, Any],
                            retry_request: dict[str, Any] | None) -> dict[str, Any]:
        state = self._state()
        requested_at = retry_request["requested_at"] if retry_request else -1
        discovered = [
            graph for graph in state["graphs"].values()
            if graph["registered_at"] > requested_at
            and self._graph_valid_on(graph, batch["batch_date"])
        ]
        if discovered:
            return max(discovered, key=lambda g: g["registered_at"])
        current = state["graphs"].get(batch["graph_id"])
        if current and self._graph_valid_on(current, batch["batch_date"]):
            return current
        valid = [
            graph for graph in state["graphs"].values()
            if self._graph_valid_on(graph, batch["batch_date"])
        ]
        if not valid:
            raise ValueError(f"批次 {batch['batch_key']} 在 {batch['batch_date']} 没有可用的道路图")
        return max(valid, key=lambda g: g["registered_at"])

    def _other_confirmed_batch(self, batch_key: str) -> dict[str, Any] | None:
        state = self._state()
        for other in sorted(state["batches"].values(), key=lambda b: b["batch_date"]):
            if other["batch_key"] == batch_key:
                continue
            delivered = [s for s in other["segments"] if s["status"] == "已交付"]
            if not delivered:
                continue
            return {
                "batch_key": other["batch_key"],
                "batch_date": other["batch_date"],
                "delivery_no": other["delivery_no"],
                "original_mileage_m": round(
                    sum(float(s["delivered_mileage_m"] or 0) for s in delivered), 2
                ),
                "graph_id": delivered[0]["graph_id"],
                "segments": [
                    {
                        "segment_id": s["segment_id"],
                        "mileage_m": s["delivered_mileage_m"],
                        "graph_id": s["graph_id"],
                    }
                    for s in delivered
                ],
                "note": "已确认批次的原值保持不变，可用于同一次续做核对",
            }
        return None

    def resume_batch(self, batch_key: str) -> dict[str, Any]:
        """接续同一个尚未确认的批次：先发现重试申请之后登记的新图，再首次交付。

        返回最终实际交付线路、相对原计划的里程差，以及其他已确认批次的原值。
        """
        state = self._state()
        batch = self._get_batch(batch_key)
        if batch["status"] == "已交付":
            snapshot = state["deliveries"].get(batch["delivery_no"] or "", {})
            return {
                "ok": True,
                "batch_key": batch_key,
                "already_delivered": True,
                "delivery_no": batch["delivery_no"],
                "delivered_mileage_m": snapshot.get("mileage_m", 0.0),
                "final_route": {
                    "graph_id": batch["graph_id"],
                    "segments": [
                        {
                            "segment_id": s["segment_id"],
                            "edge_ids": s["route"]["edge_ids"] if s["route"] else [],
                            "mileage_m": s["delivered_mileage_m"],
                        }
                        for s in batch["segments"]
                    ],
                },
                "message": f"批次 {batch_key} 已按交付号 {batch['delivery_no']} 交付，重复续做不会改写历史值",
            }

        retry_request = next(
            (r for r in reversed(batch["retry_requests"]) if r["status"] != "已完成"), None
        )
        if retry_request is None:
            retry_request = self._request_retry_internal(batch, self._tick())

        graph = self._pick_adopted_graph(batch, retry_request)

        pending = [s for s in batch["segments"] if s["status"] != "已交付"]
        recalculated: list[dict[str, Any]] = []
        for segment in pending:
            route = self._route_on(graph["graph_id"], segment["origin_node"], segment["dest_node"])
            if route is None:
                segment["status"] = "还原失败"
                segment["route"] = None
                segment["current_mileage_m"] = None
                recalculated.append({
                    "segment_id": segment["segment_id"],
                    "status": "还原失败",
                    "graph_id": graph["graph_id"],
                    "message": "新图中没有可走路线，保留原批次待处理",
                })
                continue
            self._validate_single_graph(graph, route["edge_ids"])
            previous = segment["current_mileage_m"]
            segment["graph_id"] = graph["graph_id"]
            segment["route"] = route
            segment["current_mileage_m"] = round(route["mileage_m"], 2)
            segment["mileage_delta_m"] = round(
                segment["current_mileage_m"] - float(segment["plan_mileage_m"] or 0), 2
            )
            segment["status"] = "待确认"
            recalculated.append({
                "segment_id": segment["segment_id"],
                "status": "待确认",
                "graph_id": graph["graph_id"],
                "previous_mileage_m": previous,
                "recalculated_mileage_m": segment["current_mileage_m"],
                "mileage_delta_m": segment["mileage_delta_m"],
                "edge_ids": list(route["edge_ids"]),
            })

        deliverable = [s for s in pending if s["status"] != "还原失败"]
        delivery_snapshot = None
        if deliverable:
            delivery_no = self._next_delivery_no(batch)
            delivery_snapshot = self._freeze_delivery(
                batch, [s["segment_id"] for s in deliverable], delivery_no, self._tick(),
            )
        else:
            delivery_no = None

        # 批次整体状态：只要还有失败分段就保持异常；全部交付才转为已交付。
        if all(s["status"] == "已交付" for s in batch["segments"]):
            batch["status"] = "已交付"
        elif any(s["status"] == "还原失败" for s in batch["segments"]):
            batch["status"] = "还原异常"
        elif any(s["status"] == "已交付" for s in batch["segments"]):
            batch["status"] = "部分交付"
        else:
            batch["status"] = "待确认"
        batch["graph_id"] = graph["graph_id"]
        batch["updated_at"] = self._tick()
        if delivery_snapshot is not None:
            batch["delivery_no"] = delivery_snapshot["delivery_no"]
        if all(s["status"] == "已交付" for s in batch["segments"]):
            retry_request["status"] = "已完成"
            retry_request["completed_at"] = batch["updated_at"]

        delivered_segments = [
            s for s in batch["segments"] if s["status"] == "已交付"
        ]
        final_mileage = round(
            sum(float(s["delivered_mileage_m"] or 0) for s in delivered_segments), 2
        )
        original_plan = dict(batch["original_plan"])
        usage = self._record_usage(
            batch, "续做", graph, delivered_segments,
            round(final_mileage - float(original_plan["mileage_m"]), 2),
            at=batch["updated_at"],
        )
        self._persist()

        return {
            "ok": bool(delivery_snapshot),
            "batch_key": batch_key,
            "batch_date": batch["batch_date"],
            "resumed": True,
            "retry_request": dict(retry_request),
            "graph_usage": usage,
            "discovered_graphs": [
                self._graph_summary(g) for g in state["graphs"].values()
                if g["registered_at"] > retry_request["requested_at"]
                and self._graph_valid_on(g, batch["batch_date"])
            ],
            "adopted_graph": self._graph_summary(graph),
            "original_plan": original_plan,
            "final_route": {
                "graph_id": graph["graph_id"],
                "graph_version": graph["version"],
                "segments": [
                    {
                        "segment_id": s["segment_id"],
                        "edge_ids": s["route"]["edge_ids"] if s["route"] else [],
                        "road_names": s["route"]["road_names"] if s["route"] else [],
                        "mileage_m": s["current_mileage_m"],
                        "delivered_mileage_m": s["delivered_mileage_m"],
                        "status": s["status"],
                    }
                    for s in batch["segments"]
                ],
                "mileage_m": final_mileage,
            },
            "delivery_no": delivery_no,
            "delivered_segments": [
                {
                    "segment_id": s["segment_id"],
                    "delivery_no": s["delivery_no"],
                    "mileage_m": s["delivered_mileage_m"],
                    "graph_id": s["graph_id"],
                    "edge_ids": s["route"]["edge_ids"] if s["route"] else [],
                }
                for s in delivered_segments
            ],
            "mileage_delta_m": round(final_mileage - float(original_plan["mileage_m"]), 2),
            "original_plan_mileage_m": original_plan["mileage_m"],
            "final_mileage_m": final_mileage,
            "recalculated_segments": recalculated,
            "other_confirmed_batch": self._other_confirmed_batch(batch_key),
            "closures": [dict(c) for c in graph.get("closures", []) if c.get("active", True)],
            "single_graph_ok": True,
            "mixed_graph_used": False,
            "message": (
                f"续做完成：采用 {graph['graph_id']}（{graph['version']}）还原，"
                f"最终交付 {final_mileage} 米，相对原计划 {original_plan['mileage_m']} 米"
                f"差额 {round(final_mileage - float(original_plan['mileage_m']), 2)} 米"
            ),
        }

    # ------------------------------------------------------------------
    # 逐段确认、交付对账、补算
    # ------------------------------------------------------------------
    def confirm_segment(self, batch_key: str, segment_id: int,
                        delivery_no: str | None = None) -> dict[str, Any]:
        """逐条确认某个批次已交付的分段，交付号冻结当时里程。"""
        batch = self._get_batch(batch_key)
        segment = next(
            (s for s in batch["segments"] if int(s["segment_id"]) == int(segment_id)), None
        )
        if segment is None:
            return {"ok": False, "message": f"批次 {batch_key} 不存在分段 {segment_id}"}
        if segment["status"] == "已交付":
            return {
                "ok": True,
                "already_confirmed": True,
                "batch_key": batch_key,
                "segment": self._segment_view(batch, segment),
                "delivery_no": segment["delivery_no"],
                "batch_status": batch["status"],
                "message": "该分段已确认，历史交付值保持不变",
            }
        if segment.get("route") is None:
            return {
                "ok": False,
                "batch_key": batch_key,
                "segment": self._segment_view(batch, segment),
                "message": "该分段没有可用还原结果，请先续做或补算",
            }
        self._validate_single_graph(self._state()["graphs"][segment["graph_id"]], segment["route"]["edge_ids"])
        chosen_no = delivery_no or self._next_delivery_no(batch)
        snapshot = self._freeze_delivery(batch, [segment["segment_id"]], chosen_no, self._tick())
        graph = self._state()["graphs"][segment["graph_id"]]
        usage = self._record_usage(
            batch, "逐段确认", graph, [segment],
            round(float(segment["delivered_mileage_m"] or 0) - float(segment["plan_mileage_m"] or 0), 2),
        )
        self._persist()
        return {
            "ok": True,
            "batch_key": batch_key,
            "delivery_no": chosen_no,
            "segment": self._segment_view(batch, segment),
            "delivery": snapshot,
            "graph_usage": usage,
            "batch_status": batch["status"],
            "message": f"分段 {segment_id} 已按交付号 {chosen_no} 确认，里程 {segment['delivered_mileage_m']} 米",
        }

    def verify_delivery(self, delivery_no: str) -> dict[str, Any]:
        """按交付号核对已交付批次的实际里程；返回的是交付时冻结的值。"""
        state = self._state()
        snapshot = state["deliveries"].get(delivery_no)
        if snapshot is None:
            return {"ok": False, "delivery_no": delivery_no, "message": "交付号不存在"}
        return {
            "ok": True,
            "delivery_no": delivery_no,
            "batch_key": snapshot["batch_key"],
            "batch_date": snapshot["batch_date"],
            "graph_id": snapshot["graph_id"],
            "graph_version": snapshot["graph_version"],
            "actual_mileage_m": snapshot["mileage_m"],
            "delivered_mileage_m": snapshot["mileage_m"],
            "segments": [dict(s) for s in snapshot["segments"]],
            "frozen": True,
            "mixed_graph_used": bool(snapshot.get("mixed_graph_used", False)),
            "message": f"交付号 {delivery_no} 实际里程 {snapshot['mileage_m']} 米（交付时冻结）",
        }

    def recalculate_batch(self, batch_key: str, graph_id: str | None = None) -> dict[str, Any]:
        """补算新增可走路线：只重算未交付分段，给出差额和对应分段。

        已交付分段的里程原值保持不变，新增部分单独汇总，便于接班人员区分。
        """
        state = self._state()
        batch = self._get_batch(batch_key)
        target_graph_id = graph_id or batch["graph_id"]
        # 未显式指定时，选该日期有效且登记时间最新的一张图。
        if graph_id is None:
            valid = [
                g for g in state["graphs"].values()
                if self._graph_valid_on(g, batch["batch_date"])
            ]
            if valid:
                target_graph_id = max(valid, key=lambda g: g["registered_at"])["graph_id"]
        graph = state["graphs"].get(target_graph_id)
        if graph is None:
            return {"ok": False, "batch_key": batch_key, "message": f"道路图 {target_graph_id} 不存在"}
        if not self._graph_valid_on(graph, batch["batch_date"]):
            return {
                "ok": False,
                "batch_key": batch_key,
                "message": f"道路图 {target_graph_id} 在 {batch['batch_date']} 无效，不能用于补算",
            }

        delivered_segments = []
        delta_segments = []
        added_mileage = 0.0
        total_delta = 0.0
        for segment in batch["segments"]:
            if segment["status"] == "已交付":
                delivered_segments.append({
                    "segment_id": segment["segment_id"],
                    "delivery_no": segment["delivery_no"],
                    "mileage_m": segment["delivered_mileage_m"],
                    "graph_id": segment["graph_id"],
                    "frozen": True,
                })
                continue
            route = self._route_on(target_graph_id, segment["origin_node"], segment["dest_node"])
            previous = segment["current_mileage_m"]
            previous_status = segment["status"]
            if route is None:
                delta_segments.append({
                    "segment_id": segment["segment_id"],
                    "graph_id": target_graph_id,
                    "previous_mileage_m": previous,
                    "recalculated_mileage_m": None,
                    "mileage_delta_m": None,
                    "new_route": False,
                    "status": "还原失败",
                    "message": "补算后仍无可走路线",
                })
                continue
            self._validate_single_graph(graph, route["edge_ids"])
            new_mileage = round(route["mileage_m"], 2)
            segment["graph_id"] = target_graph_id
            segment["route"] = route
            segment["current_mileage_m"] = new_mileage
            segment["mileage_delta_m"] = round(new_mileage - float(segment["plan_mileage_m"] or 0), 2)
            segment["status"] = "待确认"
            added_mileage += new_mileage
            delta = round(new_mileage - float(previous or 0), 2)
            total_delta += delta
            delta_segments.append({
                "segment_id": segment["segment_id"],
                "graph_id": target_graph_id,
                "previous_mileage_m": previous,
                "recalculated_mileage_m": new_mileage,
                "mileage_delta_m": delta,
                "new_route": previous_status == "还原失败" or previous is None,
                "route": {
                    "edge_ids": list(route["edge_ids"]),
                    "road_names": list(route["road_names"]),
                    "mileage_m": new_mileage,
                },
            })

        batch["graph_id"] = target_graph_id
        batch["updated_at"] = self._tick()
        if all(s["status"] == "已交付" for s in batch["segments"]):
            batch["status"] = "已交付"
        elif any(s["status"] == "还原失败" for s in batch["segments"]):
            batch["status"] = "还原异常"
        elif any(s["status"] == "已交付" for s in batch["segments"]):
            batch["status"] = "部分交付"
        else:
            batch["status"] = "待确认"

        delivered_mileage = round(
            sum(float(s["delivered_mileage_m"] or 0) for s in batch["segments"]
                if s["status"] == "已交付"), 2
        )
        recalculated_segments = [
            s for s in batch["segments"] if s["status"] != "已交付"
        ]
        usage = self._record_usage(
            batch, "补算", graph, recalculated_segments, round(total_delta, 2),
            at=batch["updated_at"],
        )
        self._persist()

        return {
            "ok": True,
            "batch_key": batch_key,
            "batch_date": batch["batch_date"],
            "target_graph": self._graph_summary(graph),
            "graph_usage": usage,
            "delivered_mileage_m": delivered_mileage,
            "added_mileage_m": round(added_mileage, 2),
            "mileage_delta_m": round(total_delta, 2),
            "total_mileage_m": round(delivered_mileage + added_mileage, 2),
            "delivered_segments": delivered_segments,
            "delta_segments": delta_segments,
            "continuable_segments": [
                item for item in delta_segments
                if item.get("recalculated_mileage_m") is not None
            ],
            "single_graph_ok": True,
            "mixed_graph_used": False,
            "message": (
                f"补算完成：已交付值 {delivered_mileage} 米保持不变，"
                f"新增可走里程 {round(added_mileage, 2)} 米，"
                f"相对补算前差额 {round(total_delta, 2)} 米"
            ),
        }

    # ------------------------------------------------------------------
    # 多批次巡检
    # ------------------------------------------------------------------
    def list_handover_batches(self) -> list[dict[str, Any]]:
        state = self._state()
        batches = sorted(state["batches"].values(), key=lambda b: (b["batch_date"], b["batch_key"]))
        return [self._batch_summary(batch) for batch in batches]

    def _read_track_segments(self, batch_key: str) -> dict[str, Any]:
        """读取批次的真实轨迹分段数据集；没有文件时回退到交接作业内登记的分段。

        巡检读取的是“拟交接的轨迹分段”，所以优先取 `track_segments` 数据集，
        并把它与交接作业里的分段按 segment_id 对齐。
        """
        try:
            seg_svc = self._safe_import("app.services.track_segment", "TrackSegmentService")()
            segments, path = seg_svc.get_segments(batch_key)
            return {"source": "track_segments", "file": path, "segments": segments,
                    "available": True}
        except Exception:
            file_path = self._data_dir("track_segments") / f"{batch_key}_segments.jsonl"
            segments = self._read_jsonl(file_path) if file_path.exists() else []
            return {
                "source": "track_segments" if segments else "handover_registry",
                "file": str(file_path) if segments else None,
                "segments": segments,
                "available": bool(segments),
            }

    def prepare_handover(self, batch_keys: list[str]) -> dict[str, Any]:
        """批量读取拟交接批次的真实分段，登记候选路线和原始轨迹，再分开处理完成与异常批次。"""
        state = self._state()
        results = []
        completed: list[str] = []
        abnormal: list[str] = []
        pending: list[str] = []
        missing: list[str] = []
        for batch_key in batch_keys:
            batch = state["batches"].get(batch_key)
            if batch is None:
                missing.append(batch_key)
                continue
            track = self._read_track_segments(batch_key)
            track_by_id = {
                int(item["segment_id"]): item for item in track["segments"]
                if str(item.get("segment_id", "")).isdigit()
            }
            for segment in batch["segments"]:
                # 有真实分段文件时，用文件里的原始轨迹替换模拟轨迹。
                tracked = track_by_id.get(int(segment["segment_id"]))
                if tracked and "start_lat" in tracked:
                    segment["start_lat"] = tracked["start_lat"]
                    segment["start_lon"] = tracked["start_lon"]
                    segment["end_lat"] = tracked["end_lat"]
                    segment["end_lon"] = tracked["end_lon"]
                    if tracked.get("point_count"):
                        segment["point_count"] = tracked["point_count"]
                    if tracked.get("mileage") is not None:
                        segment["source_mileage_m"] = tracked["mileage"]
                segment["candidate_routes"] = self._candidate_routes_for(
                    segment["graph_id"], segment["origin_node"], segment["dest_node"])
            summary = self._batch_summary(batch)
            summary["candidate_route_count"] = sum(
                len(segment.get("candidate_routes", [])) for segment in batch["segments"])
            summary["original_trajectory_points"] = sum(
                len(segment.get("original_trajectory", [])) for segment in batch["segments"])
            summary["dates"] = [batch["batch_date"]]
            summary["track_segment_source"] = track["source"]
            summary["track_segment_file"] = track["file"]
            summary["track_segments_read"] = len(track["segments"])
            summary["track_segments_matched"] = sum(
                1 for segment in batch["segments"]
                if int(segment["segment_id"]) in track_by_id
            )
            results.append(summary)
            if batch["status"] == "已交付":
                completed.append(batch_key)
            elif batch["status"] == "还原异常" or any(
                    s["status"] == "还原失败" for s in batch["segments"]):
                abnormal.append(batch_key)
            else:
                pending.append(batch_key)
        self._persist()
        return {
            "ok": True,
            "batch_count": len(batch_keys),
            "dates": sorted({batch["batch_date"] for batch in state["batches"].values()
                             if batch["batch_key"] in batch_keys}),
            "batches": results,
            "completed_batches": completed,
            "abnormal_batches": abnormal,
            "pending_batches": pending,
            "missing_batches": missing,
            "message": (
                f"已读取 {len(results)} 个批次的分段；完成 {len(completed)} 个，"
                f"异常 {len(abnormal)} 个，待处理 {len(pending)} 个"
            ),
        }

    def start_or_resume_batch(self, batch_key: str) -> dict[str, Any]:
        """工作台批量启动/接续的单个入口：未交付则续做，已交付则回读冻结值。"""
        batch = self._get_batch(batch_key)
        if batch["status"] == "已交付":
            snapshot = self._state()["deliveries"].get(batch["delivery_no"] or "", {})
            return {
                "ok": True,
                "batch_key": batch_key,
                "already_delivered": True,
                "delivery_no": batch["delivery_no"],
                "delivered_mileage_m": snapshot.get("mileage_m", 0.0),
                "batch": self._batch_summary(batch),
                "message": "批次已交付，按原交付号核对，不会重新取数",
            }
        return self.resume_batch(batch_key)

    def check_batches(self, batch_keys: list[str]) -> dict[str, Any]:
        """重跑后检查：各批实际采用的图、可继续的分段、原批次仍可交接的里程。"""
        state = self._state()
        results = []
        completed: list[str] = []
        abnormal: list[str] = []
        for batch_key in batch_keys:
            batch = state["batches"].get(batch_key)
            if batch is None:
                results.append({"batch_key": batch_key, "found": False,
                                "message": "批次不存在"})
                continue
            actual_graphs = sorted({s["graph_id"] for s in batch["segments"]})
            # 可继续分段：先看该批实际用过的图，再看同日期其他有效图。
            candidate_graphs = list(actual_graphs)
            for graph in sorted(state["graphs"].values(), key=lambda g: g["registered_at"],
                                reverse=True):
                if graph["graph_id"] in candidate_graphs:
                    continue
                if self._graph_valid_on(graph, batch["batch_date"]):
                    candidate_graphs.append(graph["graph_id"])
            continuable = []
            remaining = 0.0
            delivered = 0.0
            for segment in batch["segments"]:
                if segment["status"] == "已交付":
                    delivered += float(segment["delivered_mileage_m"] or 0)
                    continue
                remaining += float(
                    segment["current_mileage_m"] if segment["current_mileage_m"] is not None
                    else segment["plan_mileage_m"] or 0
                )
                viable = None
                for graph_id in candidate_graphs:
                    route = self._route_on(graph_id, segment["origin_node"],
                                           segment["dest_node"])
                    if route:
                        viable = {
                            "segment_id": segment["segment_id"],
                            "graph_id": graph_id,
                            "mileage_m": round(route["mileage_m"], 2),
                            "edge_ids": list(route["edge_ids"]),
                            "status": segment["status"],
                        }
                        break
                if viable:
                    continuable.append(viable)
            summary = {
                "batch_key": batch_key,
                "found": True,
                "batch_date": batch["batch_date"],
                "status": batch["status"],
                "delivery_no": batch["delivery_no"],
                "actual_graph_ids": actual_graphs,
                "actual_graphs": [
                    self._graph_summary(state["graphs"][graph_id])
                    for graph_id in actual_graphs if graph_id in state["graphs"]
                ],
                "continuable_segments": continuable,
                "delivered_mileage_m": round(delivered, 2),
                "remaining_mileage_m": round(remaining, 2),
                "original_plan": dict(batch["original_plan"]),
                "graph_usages": [dict(u) for u in batch.get("graph_usages", [])],
                "mixed_graph_used": len(actual_graphs) > 1,
            }
            results.append(summary)
            if batch["status"] == "已交付":
                completed.append(batch_key)
            elif batch["status"] == "还原异常" or any(
                    s["status"] == "还原失败" for s in batch["segments"]):
                abnormal.append(batch_key)
        return {
            "ok": True,
            "batches": results,
            "completed_batches": completed,
            "abnormal_batches": abnormal,
            "summary": {
                "batch_count": len(results),
                "completed": len(completed),
                "abnormal": len(abnormal),
                "remaining_mileage_m": round(
                    sum(float(r.get("remaining_mileage_m", 0)) for r in results), 2
                ),
            },
        }
