# 地理空间轨迹与网格聚合分析平台

面向定位点采集、坐标标准化、网格切分、轨迹分段驻留识别、空间关联聚合与区域维度产出的地理空间数据分析平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 定位源接入 | `geo_source` | 采集批次 | 批次编号、采集终端、定位方式 |
| 坐标标准化 | `coord_transform` | 转换规则 | 规则编号、源坐标系、目标坐标系 |
| 轨迹切分 | `track_segment` | 轨迹分段 | 分段编号、终端编号、起始时间 |
| 驻留识别 | `dwell_detect` | 驻留事件 | 事件编号、分段编号、中心坐标 |
| 网格索引 | `grid_index` | 网格单元 | 网格编号、网格层级、边界范围 |
| 围栏判定 | `geofence_rule` | 电子围栏 | 围栏编号、围栏形状、顶点序列 |
| 空间关联 | `spatial_join` | 关联任务 | 任务编号、左数据集、右数据集 |
| 兴趣点匹配 | `poi_match` | 匹配候选 | 候选编号、驻留事件、兴趣点编号 |
| 区域维度 | `region_dim` | 区域层级 | 区域编号、上级区域、区域名称 |
| 热力聚合 | `heatmap_aggregate` | 聚合窗口 | 窗口编号、网格层级、时间粒度 |
| 起止流量 | `od_matrix` | 流量单元 | 单元编号、起点网格、终点网格 |
| 路径还原 | `route_restore` | 还原路段 | 路段编号、分段编号、路段序列 |
| 密度估算 | `density_estimate` | 密度网格 | 估算编号、网格编号、带宽参数 |
| 轨迹质量 | `geo_quality` | 质量报告 | 报告编号、批次编号、漂移点数 |
| 点位归并 | `dedup_merge` | 归并批次 | 归并编号、批次编号、去重键列 |
| 分析快照 | `geo_snapshot` | 版本快照 | 快照编号、分析口径、网格层级 |
| 历史回补 | `geo_backfill` | 回补任务 | 回补编号、目标数据集、起始日期 |
| 结果导出 | `geo_export` | 导出任务 | 导出编号、快照编号、导出格式 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 路径还原多批次交接

`route_restore` 在原有单批次还原之外，提供一套多批次交接与失败恢复能力：
道路图按版本登记、每个批次固定实际采用的图、交付号冻结交付里程、补算只产出
差额与对应分段。工作台“路径还原”页顶部即为交接面板，支持跨日期多选批次。

后端接口挂在 `/api/route-handover`（`app/routers/route_handover.py`）：

| 接口 | 说明 |
| --- | --- |
| `GET /state` | 有效道路图、批次状态、交付号索引 |
| `GET /graphs?valid_on=` | 按日期查看同时有效的道路图 |
| `POST /prepare` | 批量读取分段，登记候选路线与原始轨迹，分开完成/异常/待处理 |
| `POST /start` | 批量启动或接续；已交付批次只回读冻结值 |
| `POST /retry` | 登记重试申请；同一路网可追加临时封闭记录 |
| `POST /resume` | 发现重试申请之后登记的新图，首次交付并给出最终线路与里程差 |
| `POST /confirm` | 逐条确认已交付分段，每次确认冻结一个交付号 |
| `GET /deliveries/{交付号}` | 按交付号核对实际里程（交付时冻结） |
| `POST /closures/release` | 解除临时封闭 |
| `POST /recalculate` | 补算未交付分段，返回新增可走里程、差额和对应分段 |
| `POST /check` | 重跑后核对：实际采用的图、可继续的分段、剩余可交接里程 |

内置四个跨日期批次：两个已交付（一个全量、一个只确认首段）、一个续做批次
（原计划旧图绕行 1200 米，重试申请后登记的新图可还原 900 米）、一个带临时
封闭记录的重试新批。`POST /reset` 可把场景恢复到初始状态。

测试：`cd backend && .venv/bin/python -m pytest -q tests`

领域：数据工程
子方向：数据分析与数据应用
