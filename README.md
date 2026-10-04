# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/scheduling.py`：时段账纯规则——圈次容量占座、同一卫星窗口防重叠、排队顺延。
- `src/repository.py`：SQLite、事务、乐观版本、目录同步、圈次占座与对账。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供以下接口（身份使用 `X-User-Id`、`X-Role` 请求头）：

- `GET /health`、`GET /api/state`、`GET /api/items`
- `POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`
- `POST /api/directory` / `GET /api/directory`：同步、查看外部指挥目录的圈次（归属、容量、时段以目录为准）
- `POST /api/reconcile`、`POST /api/items/<id>/reconcile`：与外部目录对账

## 时段账规则

- **批准即占座**：`approve` 必须带 `window_start`/`window_end`（或 `maneuver_window`）与
  `directory_ref`；窗口必须落在目录圈次内，且圈次归属卫星须等于接近事件主物体。
  不传 `directory_ref` 时按电话约窗建立本地兜底圈次（`source=local`）。
- **容量不足自动顺延**：目标圈次占满（`slot_full`）或同一颗卫星窗口重叠
  （`satellite_window_overlap`）时，事件进入 `queued`，窗口按相对偏移顺延到后续圈次，并记录顺延原因。
- **同一卫星窗口不可重叠**；不同卫星可并行占用同一圈次，容量按圈次计数。
- **先批先占**：分配按批准到达顺序（`booking id`），后到的批准不会挤掉更早的占座；
  跨事件分配在 `BEGIN IMMEDIATE` 事务内串行化，杜绝并发超卖。
- **归属看外部目录，对账分流**：目录圈次消失、取消或改期时，
  未执行的批准立即失效、事件退回 `assessed` 重议（原批准记入 `voided_maneuvers` 与审计）；
  已执行的占用保留原记录，仅登记 `reconciliation_notes`。仅目录版本号变化而时段不变时自动续用。
  前序占用释放/失效后，排队中的占用自动重排补位。
- **窗口修改乐观并发**：`modify_window`（调度员）需要 `expected_version`；
  两名调度员同时改同一窗口时先写生效，后写方收到 `version_conflict`，重读最新版本后可重排。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道、运营方意见冲突，
以及圈次容量占座、排队顺延与原因、同卫星窗口防重叠、并发批准不超卖（多线程）、
窗口修改乐观锁、外部目录改期后对账失效/保留分流。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
