"""HTTP routes.

Route ordering note: ``/batches/{batch_id}/packing-lists`` and friends are
distinct namespaces, so no shadowing occurs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status

from .. import models, service
from ..config import Settings

router = APIRouter()


def get_db(request: Request):
    return request.app.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get(
    "/health",
    response_model=models.HealthOut,
    tags=["运维"],
    summary="健康检查",
)
def health(request: Request):
    with request.app.state.db.read() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "database": "ok"}


@router.post(
    "/batches",
    response_model=models.BatchCreatedOut,
    status_code=status.HTTP_201_CREATED,
    tags=["批次与装箱单"],
    summary="登记装箱单（箱号与箱内序列号）",
    description=(
        "登记一个批次的装箱单，立即生成初始核对结果（全部为无到货凭证）。"
        "箱号与序列号在批次内必须唯一。"
    ),
)
def create_batch(
    payload: models.BatchIn,
    db=Depends(get_db),
):
    data = payload.model_dump()
    result_id = service.create_batch(db, data)
    return {
        "batch": service.get_batch(db, payload.batch_id),
        "packing_list": service.get_packing_list(db, payload.batch_id, None),
        "result": service.get_result(db, payload.batch_id, result_id),
    }


@router.get(
    "/batches/{batch_id}",
    response_model=models.BatchOut,
    tags=["批次与装箱单"],
    summary="查询批次状态",
    description="`recalculation_in_progress` 为 true 时，查询仍返回原装箱单的核对结果。",
)
def get_batch(batch_id: str, db=Depends(get_db)):
    return service.get_batch(db, batch_id)


@router.get(
    "/batches/{batch_id}/packing-lists",
    response_model=models.PackingListOut,
    tags=["批次与装箱单"],
    summary="查询装箱单（默认当前生效版本）",
)
def get_packing_list(
    batch_id: str,
    version: int | None = Query(default=None, description="指定版本；省略则为当前生效版本"),
    db=Depends(get_db),
):
    return service.get_packing_list(db, batch_id, version)


@router.get(
    "/batches/{batch_id}/packing-lists/versions",
    response_model=list[models.VersionSummaryOut],
    tags=["批次与装箱单"],
    summary="列出装箱单版本及状态",
)
def list_packing_list_versions(batch_id: str, db=Depends(get_db)):
    return service.list_versions(db, batch_id)


@router.post(
    "/batches/{batch_id}/packing-lists/corrections",
    response_model=models.CorrectionOut,
    tags=["批次与装箱单"],
    summary="更正单件所属的箱并重算",
    description=(
        "生成新的装箱单版本并重算核对结果。重算期间照常接收扫描，查询仍使用原装箱单的核对结果；"
        "新版生效后一次性纳入截至生效时已受理的全部扫描（实收、冲突、无凭证与明细一起生效）。"
        "变更失败（校验或写入失败）时原装箱单与原核对结果继续供数。"
    ),
)
def correct_packing_list(
    batch_id: str,
    payload: models.CorrectionIn,
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    previous, current, result_id = service.correct_packing_list(
        db,
        batch_id,
        payload.model_dump(),
        recalculation_delay_seconds=settings.recalc_delay_seconds,
    )
    return {
        "batch_id": batch_id,
        "previous_packing_list_version": previous,
        "packing_list_version": current,
        "result": service.get_result(db, batch_id, result_id),
    }


@router.post(
    "/batches/{batch_id}/scans",
    response_model=models.ScanAcceptedOut,
    status_code=status.HTTP_201_CREATED,
    tags=["扫描接收"],
    summary="接收扫描记录（整箱或单件）",
    description=(
        "整箱扫描：填写 `carton_no` 且 `seal_intact=true`（封签完好时才可上报）。"
        "单件扫描：填写 `serial_no`，不得填写 `seal_intact`。"
        "同一批次内 `scan_id` 唯一。装箱单重算期间扫描照常受理，"
        "响应中 `result_refreshed` 为 false，待新版生效后一并纳入。"
    ),
)
def accept_scan(
    batch_id: str,
    payload: models.ScanIn,
    db=Depends(get_db),
):
    outcome = service.accept_scan(db, batch_id, payload.model_dump())
    scan = outcome["scan"]
    return {
        "batch_id": batch_id,
        "result_refreshed": outcome["result_refreshed"],
        "scan": {
            "batch_id": scan["batch_id"],
            "scan_id": scan["scan_id"],
            "receiving_point": scan["receiving_point"],
            "kind": "carton" if scan["carton_no"] is not None else "serial",
            "carton_no": scan["carton_no"],
            "serial_no": scan["serial_no"],
            "seal_intact": (
                None if scan["seal_intact"] is None else bool(scan["seal_intact"])
            ),
            "accepted_at": scan["accepted_at"],
        },
    }


@router.get(
    "/batches/{batch_id}/scans",
    response_model=list[models.ScanOut],
    tags=["扫描接收"],
    summary="列出已受理扫描",
)
def list_scans(batch_id: str, db=Depends(get_db)):
    return service.list_scans(db, batch_id)


@router.get(
    "/batches/{batch_id}/result",
    response_model=models.VerificationResultOut,
    tags=["核对结果"],
    summary="查询当前生效核对结果",
    description=(
        "返回实收、冲突、无到货凭证及扫描明细。重算期间继续返回原装箱单的核对结果，"
        "`packing_list_version` 可据此判断结果依据的装箱单版本。"
    ),
)
def get_active_result(batch_id: str, db=Depends(get_db)):
    return service.get_result(db, batch_id)


@router.get(
    "/batches/{batch_id}/results",
    response_model=list[models.ResultSummaryOut],
    tags=["核对结果"],
    summary="列出该批次的历史核对结果",
)
def list_results(batch_id: str, db=Depends(get_db)):
    return service.list_results(db, batch_id)


@router.get(
    "/batches/{batch_id}/results/{result_id}",
    response_model=models.VerificationResultOut,
    tags=["核对结果"],
    summary="按结果编号查询历史核对结果",
)
def get_result_by_id(batch_id: str, result_id: int, db=Depends(get_db)):
    return service.get_result(db, batch_id, result_id)


@router.post(
    "/batches/{batch_id}/result/refresh",
    response_model=models.RefreshOut,
    tags=["核对结果"],
    summary="手动重算当前生效装箱单的核对结果",
    description="将截至当前已受理的全部扫描纳入结果；重算期间返回 409。",
)
def refresh_result(batch_id: str, db=Depends(get_db)):
    result_id = service.refresh_active_result(db, batch_id)
    return {
        "batch_id": batch_id,
        "result": service.get_result(db, batch_id, result_id),
    }
