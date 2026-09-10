from __future__ import annotations

from functools import lru_cache
from pathlib import Path

SIC_TABLE_PATH = Path(__file__).resolve().parent / "data" / "sic_major_groups.csv"
UNCLASSIFIED_SIC2 = "00"
UNCLASSIFIED_NAME = "Unclassified"


@lru_cache(maxsize=1)
def load_sic_major_groups(path: Path | None = None) -> dict[str, str]:
    table_path = Path(path) if path is not None else SIC_TABLE_PATH
    names: dict[str, str] = {}
    with table_path.open(encoding="utf-8") as handle:
        next(handle, None)
        for line in handle:
            line = line.strip()
            if not line:
                continue
            sic2, _, name = line.partition(",")
            names[sic2.strip().zfill(2)] = name.strip()
    return names


def sic2_from_code(sic_code: str | None) -> str:
    if sic_code is None:
        return UNCLASSIFIED_SIC2
    digits = "".join(ch for ch in str(sic_code) if ch.isdigit())
    if len(digits) < 2:
        return UNCLASSIFIED_SIC2
    return digits[:2].zfill(2)


def industry_name(sic2: str, names: dict[str, str] | None = None) -> str:
    lookup = names if names is not None else load_sic_major_groups()
    key = str(sic2).zfill(2)
    return lookup.get(key, UNCLASSIFIED_NAME)
