# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。
- `src/ledger.py`：时段账——圈次容量、窗口重叠、顺延、外部目录归属与对账。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources` 和 `POST /api/items/<id>/actions`。身份使用 `X-User-Id`、`X-Role` 请求头。

## 时段账

批准规避时会占住圈次（`POST /api/items/<id>/actions` 的 `approve`）：申请圈次容量满或与同卫星已有窗口重叠，就顺延到下一个可用圈次，并在窗口的 `queue_reason` 里说明原因；同一颗卫星的窗口时间不能重叠。

归属看外部指挥目录：`POST /api/catalog` 同步目录条目后，对应圈次标记为 `catalog` 归属，批准只占目录已授权的圈次；目录未覆盖该卫星时才用本地圈次。`GET /api/catalog` 查看目录，`GET /api/windows?satellite_id=SAT-1` 查看时段账。

对账 `POST /api/reconcile` 以外部目录为准：目录不再分配的圈次属多占，目录已分配而本地未占属少占。多占或少占时，未执行的批准立即失效、窗口作废并把事件退回 `reopened` 重议；已执行的窗口留原记录。

两名调度员同时修改窗口时（`POST /api/windows/<id>/reschedule`），先写的生效，另一方携带旧 `expected_version` 会收到 `version_conflict`（409），重新读取后再重排。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道、运营方意见冲突、圈次容量与顺延、同卫星窗口不重叠、外部目录归属、对账多占少占、重议后重批和窗口修改的乐观锁。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
