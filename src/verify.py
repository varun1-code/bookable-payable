"""Cross-check an assembled payable against the ERP oracle (erp.py)."""
from __future__ import annotations

import importlib.util
import sys

from . import config

_spec = importlib.util.spec_from_file_location("erp", config.ROOT / "erp.py")
_erp = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("erp", _erp)
_spec.loader.exec_module(_erp)

erp_book = _erp.erp_book
num = _erp.num


def check(payable: dict) -> tuple[bool, float, float]:
    """Return (matches, erp_gross, stated_gross)."""
    stated = num(payable.get("gross_total"))
    result = erp_book(payable)
    booked = result["will_book_gross"]
    return (abs(booked - stated) < 0.005, booked, stated)
