#!/usr/bin/env python3
"""扫描表缺行修复演示：为 Swagger 准备漏行样例并串联修复场景。

模拟“扫描表从旧备份恢复后缺行”：直接连 SQLite 删除若干 scans 行（历史核对
结果中的扫描明细仍完整保留），再调用 `POST /batches/{id}/scans/restore`。

本地运行（服务以默认 DB_PATH=data/verification.db 启动）：

    python3 scripts/demo_restore.py

容器内运行（docker compose up 之后，脚本已随镜像分发）：

    docker compose exec api python /srv/scripts/demo_restore.py

环境变量：API_BASE（默认 http://127.0.0.1:8000）、DB_PATH（默认
data/verification.db，容器内为 /data/verification.db）、DEMO_PREFIX（批次号前缀）。

脚本只使用标准库。结尾会留下一个未修复的漏行批次，便于在 Swagger
<http://localhost:8000/docs> 里亲手调用修复接口。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.getenv("API_BASE", "http://127.0.0.1:8000").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "data/verification.db")
PREFIX = os.getenv("DEMO_PREFIX", f"RESTORE-{int(time.time())}")


def call(method: str, path: str, payload: dict | None = None, expect: int | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            body = json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = json.loads(exc.read() or b"null")
    if expect is not None and status != expect:
        raise SystemExit(f"{method} {path} -> {status}, expected {expect}: {body}")
    return status, body


def show(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 66 - len(title)))


def sql_exec(statements: list[tuple[str, tuple]]) -> None:
    """直接写 SQLite，模拟旧备份恢复 / 恢复数据与历史不符。

    服务运行期间 WAL 允许一个写者进入；脚本只占用极短的写事务。
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement, params in statements:
            conn.execute(statement, params)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def drop_scan(batch_id: str, scan_id: str) -> None:
    """删除一行扫描并把自增序列回退到剩余最大序号，模拟旧备份恢复。"""
    sql_exec(
        [
            ("DELETE FROM scans WHERE batch_id = ? AND scan_id = ?", (batch_id, scan_id)),
            ("DELETE FROM sqlite_sequence WHERE name = 'scans'", ()),
            (
                "INSERT INTO sqlite_sequence (name, seq) "
                "SELECT 'scans', COALESCE(MAX(id), 0) FROM scans",
                (),
            ),
        ]
    )


def scan_ids(batch_id: str) -> list[str]:
    _, scans = call("GET", f"/batches/{batch_id}/scans", expect=200)
    return [row["scan_id"] for row in scans]


def main() -> int:
    show("0. 服务健康检查")
    status, body = call("GET", "/health", expect=200)
    print(status, body, "| DB_PATH =", DB_PATH)

    # ------------------------------------------------------------------
    show("场景 1：补回缺行并重建现行结果；重试返回无需补回")
    batch = f"{PREFIX}-1"
    call(
        "POST",
        "/batches",
        {
            "batch_id": batch,
            "cartons": [
                {"carton_no": "CTN-1", "serials": ["SN-1", "SN-2"]},
                {"carton_no": "CTN-2", "serials": ["SN-3", "SN-4"]},
            ],
        },
        expect=201,
    )
    call("POST", f"/batches/{batch}/scans",
         {"scan_id": "SC-1", "receiving_point": "收货点A", "serial_no": "SN-1"}, expect=201)
    call("POST", f"/batches/{batch}/scans",
         {"scan_id": "SC-2", "receiving_point": "收货点A", "carton_no": "CTN-1",
          "seal_intact": True}, expect=201)
    call("POST", f"/batches/{batch}/scans",
         {"scan_id": "SC-3", "receiving_point": "收货点B", "serial_no": "SN-3"}, expect=201)
    call("POST", f"/batches/{batch}/scans",
         {"scan_id": "SC-4", "receiving_point": "收货点B", "serial_no": "SN-4"}, expect=201)

    drop_scan(batch, "SC-3")
    drop_scan(batch, "SC-4")
    print("旧备份恢复后现存扫描:", scan_ids(batch))
    _, history = call("GET", f"/batches/{batch}/results", expect=200)
    _, latest = call(
        "GET", f"/batches/{batch}/results/{history[0]['result_id']}", expect=200
    )
    print(
        f"历史结果 {latest['result_id']} 仍留有当时的扫描明细:",
        [d["scan_id"] for d in latest["details"]],
    )

    _, restored = call("POST", f"/batches/{batch}/scans/restore", expect=200)
    print("修复响应:")
    print(json.dumps({k: v for k, v in restored.items() if k != "result"},
                     ensure_ascii=False, indent=2))
    print(
        "重建结果: 编号", restored["result"]["result_id"],
        "| 装箱单版本", restored["result"]["packing_list_version"],
        "| 实收", [i["serial_no"] for i in restored["result"]["received"]],
    )
    print("修复后现存扫描:", scan_ids(batch))

    _, again = call("POST", f"/batches/{batch}/scans/restore", expect=200)
    print("重试:", again["status"], "-", again["message"])

    # ------------------------------------------------------------------
    show("场景 2：入库序号被新扫描复用 -> 409 冲突并停止修复")
    batch = f"{PREFIX}-2"
    call(
        "POST",
        "/batches",
        {
            "batch_id": batch,
            "cartons": [{"carton_no": "CTN-1", "serials": ["SN-1", "SN-2"]}],
        },
        expect=201,
    )
    for scan_id, serial in [("SC-1", "SN-1"), ("SC-2", "SN-2"), ("SC-3", "SN-2")]:
        call("POST", f"/batches/{batch}/scans",
             {"scan_id": scan_id, "receiving_point": "收货点A", "serial_no": serial},
             expect=201)
    drop_scan(batch, "SC-3")  # 序号 3 被释放
    call("POST", f"/batches/{batch}/scans",
         {"scan_id": "SC-NEW-1", "receiving_point": "收货点B", "serial_no": "SN-1"},
         expect=201)
    print("恢复后继续受理的新扫描复用了被删序号:", scan_ids(batch))

    status, conflict = call("POST", f"/batches/{batch}/scans/restore")
    print(status, json.dumps(conflict, ensure_ascii=False, indent=2))
    print("修复已停止，现存扫描未变:", scan_ids(batch))

    # ------------------------------------------------------------------
    show("场景 3：同批扫描号指向不同原始记录 -> 409 冲突")
    batch = f"{PREFIX}-3"
    call(
        "POST",
        "/batches",
        {
            "batch_id": batch,
            "cartons": [{"carton_no": "CTN-1", "serials": ["SN-1", "SN-2"]}],
        },
        expect=201,
    )
    call("POST", f"/batches/{batch}/scans",
         {"scan_id": "SC-1", "receiving_point": "收货点A", "serial_no": "SN-1"}, expect=201)
    call("POST", f"/batches/{batch}/scans",
         {"scan_id": "SC-2", "receiving_point": "收货点A", "serial_no": "SN-2"}, expect=201)
    # 模拟恢复回来的数据与历史明细不符。
    sql_exec(
        [(
            "UPDATE scans SET receiving_point = '收货点Z' "
            "WHERE batch_id = ? AND scan_id = 'SC-2'",
            (batch,),
        )]
    )
    status, conflict = call("POST", f"/batches/{batch}/scans/restore")
    print(status, json.dumps(conflict, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------------
    show("场景 4：装箱单更正重算期间修复 -> 409 稍后重试；生效后按新版重建")
    batch = f"{PREFIX}-4"
    call(
        "POST",
        "/batches",
        {
            "batch_id": batch,
            "cartons": [
                {"carton_no": "CTN-1", "serials": ["SN-1"]},
                {"carton_no": "CTN-2", "serials": ["SN-2"]},
            ],
        },
        expect=201,
    )
    call("POST", f"/batches/{batch}/scans",
         {"scan_id": "SC-1", "receiving_point": "收货点A", "serial_no": "SN-1"}, expect=201)
    call("POST", f"/batches/{batch}/scans",
         {"scan_id": "SC-2", "receiving_point": "收货点A", "serial_no": "SN-2"}, expect=201)
    drop_scan(batch, "SC-2")

    outcome: dict = {}

    def correct() -> None:
        outcome["status"], outcome["body"] = call(
            "POST",
            f"/batches/{batch}/packing-lists/corrections",
            {"moves": [{"serial_no": "SN-2", "carton_no": "CTN-1"}]},
        )

    worker = threading.Thread(target=correct)
    worker.start()
    deadline = time.monotonic() + 3
    pending_seen = False
    while time.monotonic() < deadline:
        _, state = call("GET", f"/batches/{batch}", expect=200)
        if state["recalculation_in_progress"]:
            pending_seen = True
            break
        time.sleep(0.05)
    if pending_seen:
        status, body = call("POST", f"/batches/{batch}/scans/restore")
        print("重算窗口内修复:", status, body)
    else:
        print("未捕获重算窗口（RECALC_DELAY_SECONDS=0 时更正立即完成），跳过 409 演示")
    worker.join(timeout=60)
    if worker.is_alive():
        raise SystemExit("更正请求超时")
    print("更正完成:", outcome["status"])

    _, restored = call("POST", f"/batches/{batch}/scans/restore", expect=200)
    print(
        "更正生效后修复:",
        restored["status"],
        "| 补回", [r["scan_id"] for r in restored["restored"]],
        "| 重建结果依据装箱单版本", restored["result"]["packing_list_version"],
    )

    # ------------------------------------------------------------------
    show("场景 5：留一个漏行批次供 Swagger 亲手修复")
    batch = f"{PREFIX}-SWAGGER"
    call(
        "POST",
        "/batches",
        {
            "batch_id": batch,
            "cartons": [
                {"carton_no": "CTN-1", "serials": ["SN-1", "SN-2"]},
                {"carton_no": "CTN-2", "serials": ["SN-3"]},
            ],
        },
        expect=201,
    )
    call("POST", f"/batches/{batch}/scans",
         {"scan_id": "SC-1", "receiving_point": "收货点A", "carton_no": "CTN-1",
          "seal_intact": True}, expect=201)
    call("POST", f"/batches/{batch}/scans",
         {"scan_id": "SC-2", "receiving_point": "收货点B", "serial_no": "SN-3"}, expect=201)
    drop_scan(batch, "SC-2")
    print("批次", batch, "现存扫描:", scan_ids(batch), "（历史明细中仍有 SC-2）")
    print(f"在 Swagger 打开 POST /batches/{batch}/scans/restore 点击 Try it out 修复。")

    print(f"\nSwagger: {BASE}/docs  |  批次前缀: {PREFIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
