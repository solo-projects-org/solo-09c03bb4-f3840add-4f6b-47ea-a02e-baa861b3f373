"""Pydantic request/response models.

Field names are English so generated OpenAPI clients stay conventional;
descriptions and examples are Chinese to match the operational context.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

ShortCode = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]
SerialNo = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
PointName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
ScanId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]


class CartonIn(BaseModel):
    """A declared carton and the serial numbers physically inside it."""

    model_config = ConfigDict(extra="forbid")

    carton_no: ShortCode = Field(
        description="箱号，在本批次内唯一",
        examples=["CTN-001"],
    )
    serials: list[SerialNo] = Field(
        min_length=1,
        max_length=10_000,
        description="箱内序列号列表，不能为空且不能重复",
        examples=[["SN-0001", "SN-0002"]],
    )

    @model_validator(mode="after")
    def check_unique_serials(self) -> CartonIn:
        if len(set(self.serials)) != len(self.serials):
            raise ValueError(f"箱 {self.carton_no} 内存在重复序列号")
        return self


class BatchIn(BaseModel):
    """Packing-list registration for one delivery batch."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "batch_id": "BATCH-2026-001",
                    "cartons": [
                        {"carton_no": "CTN-001", "serials": ["SN-0001", "SN-0002"]},
                        {"carton_no": "CTN-002", "serials": ["SN-0003", "SN-0004"]},
                    ],
                }
            ]
        },
    )

    batch_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
    ] = Field(
        description="批次编号，由客户端指定，全局唯一", examples=["BATCH-2026-001"]
    )
    cartons: list[CartonIn] = Field(
        min_length=1,
        max_length=10_000,
        description="装箱单中的箱及箱内序列号",
    )

    @model_validator(mode="after")
    def check_unique(self) -> BatchIn:
        carton_numbers = [carton.carton_no for carton in self.cartons]
        if len(set(carton_numbers)) != len(carton_numbers):
            raise ValueError("装箱单内存在重复箱号")
        seen: set[str] = set()
        for carton in self.cartons:
            for serial in carton.serials:
                if serial in seen:
                    raise ValueError(f"序列号 {serial} 在装箱单中重复出现")
                seen.add(serial)
        return self


class ScanIn(BaseModel):
    """An accepted receiving scan.

    Exactly one of ``carton_no`` / ``serial_no`` must be supplied.  A carton
    scan is only admissible with ``seal_intact=true``; a single-item scan must
    not carry a seal value at all.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "scan_id": "SCAN-0001",
                    "receiving_point": "收货点A",
                    "carton_no": "CTN-001",
                    "seal_intact": True,
                },
                {
                    "scan_id": "SCAN-0002",
                    "receiving_point": "收货点A",
                    "serial_no": "SN-0001",
                },
            ]
        },
    )

    scan_id: ScanId = Field(description="扫描编号，本批次内唯一")
    receiving_point: PointName = Field(description="收货点名称")
    carton_no: ShortCode | None = Field(
        default=None, description="整箱扫描时填写箱号；与 serial_no 二选一"
    )
    serial_no: SerialNo | None = Field(
        default=None, description="单件扫描时填写序列号；与 carton_no 二选一"
    )
    seal_intact: bool | None = Field(
        default=None,
        description="整箱扫描必须为 true（封签完好）；单件扫描不得填写",
    )

    @model_validator(mode="after")
    def check_shape(self) -> ScanIn:
        if self.carton_no is None and self.serial_no is None:
            raise ValueError("carton_no 与 serial_no 必须填写其中一个")
        if self.carton_no is not None and self.serial_no is not None:
            raise ValueError("carton_no 与 serial_no 不能同时填写")
        if self.carton_no is not None and self.seal_intact is not True:
            raise ValueError("整箱扫描仅在封签完好（seal_intact=true）时才能上报")
        if self.serial_no is not None and self.seal_intact is not None:
            raise ValueError("单件扫描不应填写 seal_intact")
        return self

    @property
    def kind(self) -> Literal["carton", "serial"]:
        return "carton" if self.carton_no is not None else "serial"


class MoveIn(BaseModel):
    """Reassign one serial number to another carton."""

    model_config = ConfigDict(extra="forbid")

    serial_no: SerialNo = Field(description="需要更正的序列号")
    carton_no: ShortCode = Field(description="更正后所属箱号；可以是装箱单中已有的箱")


class CorrectionIn(BaseModel):
    """Packing-list correction: move items between cartons."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "reason": "复核发现 SN-0001 实际在 CTN-002 内",
                    "moves": [{"serial_no": "SN-0001", "carton_no": "CTN-002"}],
                }
            ]
        },
    )

    reason: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None
    ) = Field(default=None, description="更正原因，便于审计")
    moves: list[MoveIn] = Field(
        min_length=1,
        max_length=10_000,
        description="单件所属箱的更正列表",
    )

    @model_validator(mode="after")
    def check_unique_moves(self) -> CorrectionIn:
        serials = [move.serial_no for move in self.moves]
        if len(set(serials)) != len(serials):
            raise ValueError("同一序列号在一次更正请求中重复出现")
        return self


class BatchOut(BaseModel):
    batch_id: str
    active_packing_list_version: int
    latest_packing_list_version: int
    active_result_id: int | None
    recalculation_in_progress: bool
    created_at: str
    updated_at: str


class PackingCartonOut(BaseModel):
    carton_no: str
    serials: list[str]


class PackingListOut(BaseModel):
    batch_id: str
    version: int
    status: Literal["pending", "active", "superseded", "failed"]
    reason: str | None
    failure_reason: str | None
    created_at: str
    activated_at: str | None
    cartons: list[PackingCartonOut]


class ScanIdsGroup(BaseModel):
    receiving_point: str
    scan_ids: list[str]


class ReceivedItemOut(BaseModel):
    serial_no: str
    carton_no: str
    receiving_point: str
    scan_ids: list[str]


class ConflictItemOut(BaseModel):
    serial_no: str
    carton_no: str
    reports: list[ScanIdsGroup]


class UnevidencedItemOut(BaseModel):
    serial_no: str
    carton_no: str


class ScanDetailOut(BaseModel):
    seq: int = Field(description="扫描受理顺序（扫描记录序号）")
    scan_id: str
    receiving_point: str
    kind: Literal["carton", "serial"]
    carton_no: str | None
    serial_no: str | None
    seal_intact: bool | None
    accepted_at: str
    expanded_serials: list[str] = Field(
        description="按本结果对应装箱单展开后的序列号；未知箱为空列表"
    )
    unmatched: bool = Field(
        description="扫描未能映射到装箱单（未知箱号，或序列号不在装箱单中）"
    )


class VerificationResultOut(BaseModel):
    batch_id: str
    result_id: int
    packing_list_version: int
    scan_high_watermark: int = Field(
        description="生成该结果时纳入的最大扫描序号；之后受理的扫描留待下次重算"
    )
    expected_count: int
    received_count: int
    conflict_count: int
    unevidenced_count: int
    unmatched_scan_count: int
    created_at: str
    received: list[ReceivedItemOut]
    conflicts: list[ConflictItemOut]
    unevidenced: list[UnevidencedItemOut]
    details: list[ScanDetailOut]


class CorrectionOut(BaseModel):
    batch_id: str
    previous_packing_list_version: int
    packing_list_version: int
    result: VerificationResultOut


class ScanAcceptedOut(BaseModel):
    batch_id: str
    result_refreshed: bool = Field(
        description="是否已刷新生效核对结果；装箱单重算期间为 false"
    )
    scan: ScanOut


class BatchCreatedOut(BaseModel):
    batch: BatchOut
    packing_list: PackingListOut
    result: VerificationResultOut


class ScanOut(BaseModel):
    batch_id: str
    scan_id: str
    receiving_point: str
    kind: Literal["carton", "serial"]
    carton_no: str | None
    serial_no: str | None
    seal_intact: bool | None
    accepted_at: str


class ResultSummaryOut(BaseModel):
    result_id: int
    packing_list_version: int
    scan_high_watermark: int
    expected_count: int
    received_count: int
    conflict_count: int
    unevidenced_count: int
    unmatched_scan_count: int
    created_at: str
    active: bool


class VersionSummaryOut(BaseModel):
    version: int
    status: Literal["pending", "active", "superseded", "failed"]
    reason: str | None
    failure_reason: str | None
    created_at: str
    activated_at: str | None


class RefreshOut(BaseModel):
    batch_id: str
    result: VerificationResultOut


class HealthOut(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]


ScanAcceptedOut.model_rebuild()
