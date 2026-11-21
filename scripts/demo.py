#!/usr/bin/env python3
"""End-to-end demonstration of the two required Swagger scenarios.

Run against a live service (default http://127.0.0.1:8000):

    python3 scripts/demo.py
    API_BASE=http://localhost:8000 python3 scripts/demo.py

Scenario 1: the same item scanned twice at one receiving point counts once;
            the same item reported by two points becomes a conflict whose
            report lists each point's scan ids; items with no evidence are
            listed separately.
Scenario 2: correct an item's carton.  While the new packing-list version is
            recalculating, scans keep being accepted and queries keep serving
            the previous version's result.  The new version then includes the
            scans accepted during the window in one atomic snapshot.

The script only uses the standard library so it also works inside the image.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("API_BASE", "http://127.0.0.1:8000").rstrip("/")
BATCH = os.getenv("DEMO_BATCH", f"DEMO-{int(time.time())}")


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


def main() -> int:
    show("0. 服务健康检查")
    status, body = call("GET", "/health", expect=200)
    print(status, body)

    show("1. 登记装箱单")
    _, created = call(
        "POST",
        "/batches",
        {
            "batch_id": BATCH,
            "cartons": [
                {"carton_no": "CTN-A", "serials": ["SN-1001", "SN-1002"]},
                {"carton_no": "CTN-B", "serials": ["SN-1003", "SN-1004"]},
                {"carton_no": "CTN-C", "serials": ["SN-1005", "SN-1006"]},
            ],
        },
        expect=201,
    )
    print(json.dumps(created["result"], ensure_ascii=False, indent=2)[:600], "...")
    print("初始无凭证:", [i["serial_no"] for i in created["result"]["unevidenced"]])

    show("2. 同件重复扫描 + 多点上报冲突")
    # Same item, same point, reported three ways: twice by serial + once by carton.
    call(
        "POST",
        f"/batches/{BATCH}/scans",
        {
            "scan_id": "SC-A-001",
            "receiving_point": "收货点A",
            "serial_no": "SN-1001",
        },
        expect=201,
    )
    call(
        "POST",
        f"/batches/{BATCH}/scans",
        {
            "scan_id": "SC-A-002",
            "receiving_point": "收货点A",
            "serial_no": "SN-1001",
        },
        expect=201,
    )
    call(
        "POST",
        f"/batches/{BATCH}/scans",
        {
            "scan_id": "SC-A-003",
            "receiving_point": "收货点A",
            "carton_no": "CTN-A",
            "seal_intact": True,
        },
        expect=201,
    )
    # SN-1003 is reported by a second point -> conflict.
    call(
        "POST",
        f"/batches/{BATCH}/scans",
        {
            "scan_id": "SC-B-001",
            "receiving_point": "收货点B",
            "serial_no": "SN-1003",
        },
        expect=201,
    )
    call(
        "POST",
        f"/batches/{BATCH}/scans",
        {
            "scan_id": "SC-A-004",
            "receiving_point": "收货点A",
            "serial_no": "SN-1003",
        },
        expect=201,
    )
    # A second intact-seal carton at point B also evidences SN-1004.
    call(
        "POST",
        f"/batches/{BATCH}/scans",
        {
            "scan_id": "SC-B-002",
            "receiving_point": "收货点B",
            "carton_no": "CTN-B",
            "seal_intact": True,
        },
        expect=201,
    )
    # CTN-C is never scanned -> its two items have no delivery evidence.
    _, before = call("GET", f"/batches/{BATCH}/result", expect=200)
    print("实收:", json.dumps(before["received"], ensure_ascii=False, indent=2))
    print("冲突:", json.dumps(before["conflicts"], ensure_ascii=False, indent=2))
    print("无凭证:", json.dumps(before["unevidenced"], ensure_ascii=False, indent=2))

    show("3. 封签不完整的整箱扫描会被拒绝")
    status, body = call(
        "POST",
        f"/batches/{BATCH}/scans",
        {
            "scan_id": "SC-BAD-SEAL",
            "receiving_point": "收货点A",
            "carton_no": "CTN-A",
            "seal_intact": False,
        },
    )
    print(status, body)

    show("4. 更正装箱单：SN-1002 由 CTN-A 更正到 CTN-B")
    # Start the correction in the background so we can observe the window.
    import threading

    outcome: dict = {}

    def correction() -> None:
        outcome["status"], outcome["body"] = call(
            "POST",
            f"/batches/{BATCH}/packing-lists/corrections",
            {
                "reason": "开箱复核发现 SN-1002 实际在 CTN-B",
                "moves": [{"serial_no": "SN-1002", "carton_no": "CTN-B"}],
            },
        )

    worker = threading.Thread(target=correction)
    worker.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _, batch = call("GET", f"/batches/{BATCH}", expect=200)
        if batch["recalculation_in_progress"]:
            break
        time.sleep(0.05)
    print("重算中状态:", json.dumps(batch, ensure_ascii=False))

    _, during = call("GET", f"/batches/{BATCH}/result", expect=200)
    print(
        "重算期间查询仍为装箱单版本",
        during["packing_list_version"],
        "| 实收",
        [i["serial_no"] for i in during["received"]],
    )

    # This scan lands inside the recalculation window and must appear in v2.
    _, scan_during = call(
        "POST",
        f"/batches/{BATCH}/scans",
        {
            "scan_id": "SC-A-DURING",
            "receiving_point": "收货点A",
            "serial_no": "SN-1004",
        },
        expect=201,
    )
    print("重算期间受理扫描, result_refreshed =", scan_during["result_refreshed"])

    _, still_old = call("GET", f"/batches/{BATCH}/result", expect=200)
    print(
        "重算期间再次查询: 版本",
        still_old["packing_list_version"],
        "结果编号",
        still_old["result_id"],
        "| 实收",
        [i["serial_no"] for i in still_old["received"]],
    )

    worker.join(timeout=60)
    if worker.is_alive():
        raise SystemExit("更正请求超时")
    if outcome["status"] != 200:
        raise SystemExit(f"更正失败: {outcome}")
    body = outcome["body"]
    print(
        "新版生效:",
        body["previous_packing_list_version"],
        "->",
        body["packing_list_version"],
    )
    print(json.dumps(body["result"], ensure_ascii=False, indent=2))

    show("5. 核对结果已保存，可查询历史版本")
    _, results = call("GET", f"/batches/{BATCH}/results", expect=200)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    old_id = next(r["result_id"] for r in results if not r["active"])
    _, old = call("GET", f"/batches/{BATCH}/results/{old_id}", expect=200)
    print(
        f"历史结果 {old_id} 依据装箱单版本 {old['packing_list_version']}:",
        "实收",
        [i["serial_no"] for i in old["received"]],
    )
    _, versions = call("GET", f"/batches/{BATCH}/packing-lists/versions", expect=200)
    print("装箱单版本:", json.dumps(versions, ensure_ascii=False, indent=2))

    print(f"\nSwagger: {BASE}/docs  |  批次: {BATCH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
