from __future__ import annotations

import pytest

from app.models import BatchIn, CorrectionIn, ScanIn


def test_scan_accepts_carton_with_intact_seal():
    scan = ScanIn(
        scan_id="SC-1",
        receiving_point="A",
        carton_no="CTN-1",
        seal_intact=True,
    )
    assert scan.kind == "carton"


@pytest.mark.parametrize(
    "payload",
    [
        {"scan_id": "S", "receiving_point": "A", "carton_no": "C"},  # no seal
        {"scan_id": "S", "receiving_point": "A", "carton_no": "C", "seal_intact": False},
        {
            "scan_id": "S",
            "receiving_point": "A",
            "carton_no": "C",
            "serial_no": "X",
            "seal_intact": True,
        },
        {"scan_id": "S", "receiving_point": "A"},
        {"scan_id": "S", "receiving_point": "A", "serial_no": "X", "seal_intact": True},
        {"scan_id": "S", "receiving_point": "A", "serial_no": " "},
        {"scan_id": "S", "receiving_point": "A", "serial_no": "X", "extra": 1},
    ],
)
def test_scan_rejects_invalid_shapes(payload):
    with pytest.raises(ValueError):
        ScanIn(**payload)


def test_batch_rejects_empty_carton_and_duplicate_serials():
    with pytest.raises(ValueError):
        BatchIn(batch_id="B", cartons=[{"carton_no": "C", "serials": []}])
    with pytest.raises(ValueError):
        BatchIn(
            batch_id="B",
            cartons=[{"carton_no": "C", "serials": ["S", "S"]}],
        )
    with pytest.raises(ValueError):
        BatchIn(
            batch_id="B",
            cartons=[
                {"carton_no": "C1", "serials": ["S"]},
                {"carton_no": "C2", "serials": ["S"]},
            ],
        )
    with pytest.raises(ValueError):
        BatchIn(
            batch_id="B",
            cartons=[
                {"carton_no": "C1", "serials": ["S1"]},
                {"carton_no": "C1", "serials": ["S2"]},
            ],
        )


def test_correction_rejects_duplicate_moves():
    with pytest.raises(ValueError):
        CorrectionIn(
            moves=[
                {"serial_no": "S", "carton_no": "C1"},
                {"serial_no": "S", "carton_no": "C2"},
            ]
        )
