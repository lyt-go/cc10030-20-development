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
| 归并复审 | `merge_review` | 归并复审 | 复审编号、归并组编号、坏点编号、复审判定、证据快照版本 |
| 分析快照 | `geo_snapshot` | 版本快照 | 快照编号、分析口径、网格层级 |
| 历史回补 | `geo_backfill` | 回补任务 | 回补编号、目标数据集、起始日期 |
| 结果导出 | `geo_export` | 导出任务 | 导出编号、快照编号、导出格式 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 点位归并出处与归并复审

归并产物分两层，支持从代表点回溯原始观测：

- `data/dedup_groups/{批次}_groups.jsonl`：归并组明细，记录代表点、被吸收点、
  全部原始上报点出处以及每个点的最新质检结论；
- `data/dedup_merged/{批次}_merged.jsonl`：每组一条归并记录，`orig_point_ids` 保留全组出处；
- `data/point_verdicts/{批次}_verdicts.jsonl`：点位质检结论台账（待复核 / 确认漂移 /
  复核通过），更正以追加修订号方式保留完整历史。

代表点选择受质检结论约束：确认漂移的坏点、待复核的点都不得当代表；
一组全部无合法代表时标记为「待补点」，不产出归并记录。

归并复审（`merge_review`，专用前缀 `/api/merge-review`）：

- 发起复审即冻结当次依据的质检结论快照，后续更正不覆盖快照，原处置证据可随时查回；
- 复审中可接入新获准的质检更正（如第三点复核通过），评估给出「挂起整组待补点」
  或「可立即换用合法代表点」；
- 处置分「仅撤坏点 / 挂起整组」（整组转待补点，旧合并关系仍可回查）与
  「换点补采」（换用复核通过的合法点继续补采，坏点保留在出处中仅失去代表身份）；
- 前端工作台位于 `/merge-review`，支持按归并组明细和质检结论定位原始上报点。

领域：数据工程
子方向：数据分析与数据应用
