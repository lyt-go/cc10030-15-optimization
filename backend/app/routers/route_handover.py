"""路径还原交接接口：多批次巡检、道路图登记、逐段确认、交付对账与补算。

统一资源路由只承载通用 CRUD/动作，交接作业需要带批次集合和交付号的专用入参，
因此单独挂在 `/api/route-handover` 下，避免和 `/{module}/{entry_id}` 的整数路径冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import service_for
from app.services.route_restore import RouteRestoreService

router = APIRouter(prefix="/api/route-handover", tags=["路径还原交接"])


def get_service() -> RouteRestoreService:
    service = service_for("route_restore")
    if not isinstance(service, RouteRestoreService):
        raise HTTPException(status_code=500, detail="路径还原服务未注册")
    return service


class BatchKeysPayload(BaseModel):
    batch_keys: list[str] = Field(default_factory=list)


class BatchKeyPayload(BaseModel):
    batch_key: str


class ConfirmPayload(BaseModel):
    batch_key: str
    segment_id: int
    delivery_no: str | None = None


class RecalculatePayload(BaseModel):
    batch_key: str
    graph_id: str | None = None
    release_edges: list[str] = Field(default_factory=list)


class ReleaseClosurePayload(BaseModel):
    graph_id: str
    edge_id: str


@router.get("/state")
def handover_state() -> dict[str, Any]:
    """交接总览：有效道路图、批次状态与交付号索引。"""
    service = get_service()
    state = service._state()
    return {
        "graphs": service.list_graphs(),
        "batches": service.list_handover_batches(),
        "deliveries": [
            {
                "delivery_no": snap["delivery_no"],
                "batch_key": snap["batch_key"],
                "batch_date": snap["batch_date"],
                "graph_id": snap["graph_id"],
                "mileage_m": snap["mileage_m"],
                "segments": [item["segment_id"] for item in snap["segments"]],
            }
            for snap in sorted(state["deliveries"].values(), key=lambda s: s["delivery_no"])
        ],
    }


@router.get("/graphs")
def list_graphs(valid_on: str | None = None) -> dict[str, Any]:
    """列出道路图；`valid_on` 用于确认某历史日期同时有几张图有效。"""
    return {"graphs": get_service().list_graphs(valid_on=valid_on)}


@router.post("/prepare")
def prepare(payload: BatchKeysPayload) -> dict[str, Any]:
    """读取拟交接批次的真实分段，登记候选路线和原始轨迹，并按完成/异常分开。"""
    if not payload.batch_keys:
        raise HTTPException(status_code=400, detail="请至少选择一个批次")
    return get_service().prepare_handover(payload.batch_keys)


@router.post("/start")
def start(payload: BatchKeysPayload) -> dict[str, Any]:
    """工作台批量启动或接续：已交付批次只回读冻结值，其余批次执行续做。"""
    if not payload.batch_keys:
        raise HTTPException(status_code=400, detail="请至少选择一个批次")
    service = get_service()
    results = []
    for batch_key in payload.batch_keys:
        try:
            results.append(service.start_or_resume_batch(batch_key))
        except KeyError as exc:
            results.append({"ok": False, "batch_key": batch_key, "message": str(exc)})
    return {
        "ok": all(item.get("ok", False) for item in results),
        "results": results,
        "message": f"已处理 {len(results)} 个批次",
    }


@router.post("/retry")
def retry(payload: BatchKeyPayload) -> dict[str, Any]:
    """登记重试申请；正式交付之前的图登记会在续做时被发现。"""
    try:
        return get_service().request_retry(payload.batch_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/resume")
def resume(payload: BatchKeyPayload) -> dict[str, Any]:
    """续做尚未确认的批次，并在同一次核对中给出最终线路与里程差。"""
    try:
        return get_service().resume_batch(payload.batch_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/confirm")
def confirm(payload: ConfirmPayload) -> dict[str, Any]:
    """逐条确认批次已交付的分段；重复确认不会改写历史值。"""
    try:
        return get_service().confirm_segment(
            payload.batch_key, payload.segment_id, payload.delivery_no)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/deliveries/{delivery_no}")
def verify_delivery(delivery_no: str) -> dict[str, Any]:
    """按交付号核对实际里程。"""
    result = get_service().verify_delivery(delivery_no)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "交付号不存在"))
    return result


@router.post("/closures/release")
def release_closure(payload: ReleaseClosurePayload) -> dict[str, Any]:
    """解除临时封闭记录，补算时即可走回该路段。"""
    try:
        result = get_service().release_closure(payload.graph_id, payload.edge_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result["ok"] = True
    return result


@router.post("/recalculate")
def recalculate(payload: RecalculatePayload) -> dict[str, Any]:
    """补算未交付分段，返回里程差额和对应分段；已交付值保持不变。"""
    service = get_service()
    try:
        for edge_id in payload.release_edges:
            graph_id = payload.graph_id or service._get_batch(payload.batch_key)["graph_id"]
            service.release_closure(graph_id, edge_id)
        return service.recalculate_batch(payload.batch_key, payload.graph_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/check")
def check(payload: BatchKeysPayload) -> dict[str, Any]:
    """重跑后检查：实际采用的图、可继续的分段、仍可交接的里程。"""
    return get_service().check_batches(payload.batch_keys)


@router.post("/reset")
def reset() -> dict[str, Any]:
    """恢复交接场景到初始状态，便于巡检重跑。"""
    get_service().reset_handover()
    return {"ok": True, "message": "交接场景已重置"}
