"""
Three-state signal status shared by every layer (Domain / Chart / Flex).

Why an enum instead of ``bool | None``: ``None`` gets interpreted
differently by different layers (``if not x`` treats it as False). With an
explicit INSUFFICIENT_DATA member, a layer that forgets to handle it fails
loudly (e.g. in a ``match`` statement or a dict lookup) instead of silently
rendering "否".

Existing code still carries ``bool | None``; convert at the boundary with
``SignalStatus.from_optional_bool`` rather than refactoring StockFeatures.
"""

from __future__ import annotations

from enum import StrEnum


class SignalStatus(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

    @classmethod
    def from_optional_bool(cls, value: bool | None) -> "SignalStatus":
        if value is None:
            return cls.INSUFFICIENT_DATA
        return cls.TRUE if value else cls.FALSE
