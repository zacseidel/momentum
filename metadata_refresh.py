from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd

MAX_METADATA_REFRESH = 15
UNIVERSE_LOG_PATH = Path("data/universe/change_log.csv")


def select_refresh_candidates(
    metadata: pd.DataFrame,
    universe_tickers: Iterable[str],
    added_tickers: Iterable[str] = (),
    limit: int = MAX_METADATA_REFRESH,
) -> list[str]:
    """Return tickers that should be fetched from Massive, capped per run.

    Universe adds are fetched even if a stale profile exists (symbol reuse).
    Other names are fetched only when sic_code is missing.
    """
    universe = list(dict.fromkeys(universe_tickers))
    universe_set = set(universe)
    adds = [t for t in dict.fromkeys(added_tickers) if t in universe_set]

    complete: set[str] = set()
    if not metadata.empty and "ticker" in metadata.columns:
        sic = metadata["sic_code"] if "sic_code" in metadata.columns else None
        if sic is not None:
            filled = metadata["ticker"].astype(str)[sic.notna() & sic.astype(str).str.strip().ne("")]
            complete = set(filled)

    missing = [t for t in universe if t not in complete]
    ordered = list(dict.fromkeys([*adds, *missing]))
    if limit is None or limit < 0:
        return ordered
    return ordered[:limit]


def added_tickers_for_date(run_date: date, log_path: Path = UNIVERSE_LOG_PATH) -> list[str]:
    if not log_path.exists():
        return []
    try:
        log = pd.read_csv(log_path)
    except Exception:
        return []
    if log.empty:
        return []
    target = run_date.isoformat()
    date_col = log["date"].astype(str)
    action_col = log["action"].astype(str).str.lower()
    cohort_col = log["cohort"].astype(str).str.lower() if "cohort" in log.columns else None
    mask = (date_col == target) & action_col.eq("add")
    if cohort_col is not None:
        mask &= cohort_col.isin({"sp500", "sp400"})
    return [str(s) for s in log.loc[mask, "symbol"].astype(str).tolist()]
