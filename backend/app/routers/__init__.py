"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, route_handover

# route_handover 必须排在通用资源路由之前：`/api/{module}/{entry_id}` 会把
# `/api/route-handover/state` 当成 entry_id 解析成整数而返回 422。
ROUTERS = [route_handover, resource]
