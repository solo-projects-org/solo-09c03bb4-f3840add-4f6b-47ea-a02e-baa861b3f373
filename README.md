# 采购验收分批核对 API

按装箱单与各收货点的扫描记录核对一批仪器的实收情况。FastAPI + SQLite，单容器通过
Docker Compose 启动。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 先登记装箱单（箱号、箱内序列号）；箱号与序列号在批次内唯一 | `app/models.py` `BatchIn` |
| 扫描记录二选一：整箱箱号 + `seal_intact=true`，或单件序列号 | `app/models.py` `ScanIn`、`app/db.py` CHECK 约束 |
| 封签不完好的整箱扫描拒绝上报（接口 422，数据库层也拒绝） | `ScanIn.check_shape`、`scans` 表 CHECK |
| 整箱扫描按**本次核对所用装箱单版本**展开为箱内每件 | `app/service.py` `compute_verification` |
| 同一件在同一收货点重复扫描只算一次，扫描编号全部保留 | 同上，按 `(serial, point)` 去重 |
| 同一件由多个收货点上报 → 冲突清单，附各点扫描编号 | `result_conflicts.reports` |
| 没有任何到货凭证的件 → 无凭证清单，与实收分列 | `result_unevidenced` |
| 核对结果持久化，可按结果编号查历史 | `verification_results` 及 `result_*` 表 |
| 装箱单可更正单件所属的箱，生成新版本 | `POST .../packing-lists/corrections` |
| 重算期间照常接收扫描，查询仍返回**原装箱单**的核对结果 | 待生效版本提交后计算在事务外进行；查询只读 `active_result_id` |
| 新版一次性纳入已受理扫描：实收、冲突、无凭证、明细同一事务生效 | `correct_packing_list` 的激活事务 |
| 变更失败继续按原装箱单供数 | 失败分支：原版本/原结果不被修改，并记录 `failed` 版本 |
| 未知箱号 / 不在装箱单中的序列号 | 计为 `unmatched` 扫描，不产生假实收 |

补充约定：

- 整箱扫描的展开以**核对时所用装箱单版本**为准。因此更正把某件移出箱后，该箱此前的
  整箱扫描不再为它提供凭证；直接单件扫描始终有效。
- 扫描一旦受理即不可删除；更正只能改“件属于哪个箱”，不能改扫描事实。
- 服务重启时，中断的 `pending` 版本会标记为 `failed`，并把期间已受理的扫描补算进原
  生效版本，避免旧结果失真。
- `RECALC_DELAY_SECONDS` 只用于演示重算窗口，生产可设为 0。

## 启动（Docker Compose）

```bash
docker compose up --build
```

- Swagger: <http://localhost:8000/docs>
- 健康检查: <http://localhost:8000/health>
- SQLite 数据卷: `verification-data`，容器内 `/data/verification.db`
- 默认 `RECALC_DELAY_SECONDS=3`，便于在 Swagger 观察“重算期间仍按原版本供数”。
  设为 0 可关闭：

```bash
RECALC_DELAY_SECONDS=0 docker compose up --build
```

端口可用 `API_PORT` 覆盖。

## 本地运行与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

```bash
pip install pytest httpx
python3 -m pytest -q
```

## Swagger 演示

启动服务后运行：

```bash
python3 scripts/demo.py
# 或指定地址 / 批次
API_BASE=http://localhost:8000 DEMO_BATCH=DEMO-1 python3 scripts/demo.py
```

脚本用标准库串联以下场景，输出可直接对照 Swagger 的 `Try it out`：

1. 登记 3 箱 6 件装箱单 → 初始 6 件无凭证。
2. 同一件 `SN-1001` 在收货点 A 被扫 3 次（两次单件 + 一次整箱）→ 实收 1 条，
   `scan_ids` 保留 `SC-A-001/002/003`。
3. `SN-1003` 同时被收货点 A、B 上报 → 进入冲突清单，分点列出扫描编号。
4. `CTN-C` 从未扫描 → 其中 2 件单列无凭证。
5. 封签不完好的整箱扫描 → 422。
6. 更正 `SN-1002` 从 CTN-A 到 CTN-B：
   - 重算窗口内 `GET /batches/{id}` 显示 `recalculation_in_progress=true`；
   - 窗口内 `GET /result` 仍返回版本 1 的结果（结果编号不变）；
   - 窗口内 `POST /scans` 照常 201，`result_refreshed=false`；
   - 新版本生效后，窗口内受理的扫描已包含在版本 2 的结果中。
7. 历史结果与装箱单版本可分别查询。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 登记装箱单，返回初始核对结果 |
| GET | `/batches/{id}` | 批次状态（含 `recalculation_in_progress`） |
| GET | `/batches/{id}/packing-lists?version=` | 查询装箱单，默认生效版本 |
| GET | `/batches/{id}/packing-lists/versions` | 版本与状态（active/superseded/failed） |
| POST | `/batches/{id}/packing-lists/corrections` | 更正单件所属箱并重算 |
| POST | `/batches/{id}/scans` | 接收整箱/单件扫描 |
| GET | `/batches/{id}/scans` | 已受理扫描列表 |
| GET | `/batches/{id}/result` | 生效核对结果 |
| GET | `/batches/{id}/results` | 历史结果列表 |
| GET | `/batches/{id}/results/{result_id}` | 指定历史结果 |
| POST | `/batches/{id}/result/refresh` | 手动重算生效版本 |
| GET | `/health` | 健康检查 |

## 并发与一致性

- SQLite 开启 WAL；写事务用 `BEGIN IMMEDIATE` 串行化，读使用快照事务。
- 更正分三步：短事务写入 `pending` 版本 → 事务外重算（不持有写锁，扫描可继续提交）
  → 单事务写入全部结果行并原子切换 `active_result_id` 与版本状态。
- 读接口只认 `active_result_id`，因此任何时刻都返回某一版本的完整结果，不会看到
  “实收已更新、明细未更新”的中间态。
- `scan_high_watermark` 记录每个结果纳入的最大扫描序号，可用于核对结果与扫描的对应关系。

## 目录

```
app/
  main.py        应用工厂、异常映射、启动恢复
  config.py      环境变量配置
  db.py          SQLite schema 与事务封装
  models.py      Pydantic 请求/响应模型
  service.py     核对规则、版本切换、查询
  api/routes.py  HTTP 路由
scripts/demo.py  Swagger 演示脚本（标准库）
tests/           pytest + FastAPI TestClient
```
