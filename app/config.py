"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Application settings.

    ``recalc_delay_seconds`` deliberately simulates a non-trivial recalculation
    window so that scan intake and read-your-old-version behaviour can be
    observed in Swagger.  It must never be negative.
    """

    db_path: str = "data/verification.db"
    recalc_delay_seconds: float = 0.0

    @classmethod
    def from_env(cls) -> Settings:
        raw_delay = os.getenv("RECALC_DELAY_SECONDS", "0")
        try:
            delay = max(0.0, float(raw_delay))
        except ValueError as exc:  # pragma: no cover - startup guard
            raise ValueError(
                f"RECALC_DELAY_SECONDS must be a number, got {raw_delay!r}"
            ) from exc
        return cls(
            db_path=os.getenv("DB_PATH", "data/verification.db"),
            recalc_delay_seconds=delay,
        )
