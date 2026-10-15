"""Custom contract rules. Referenced as e.g. ``ingest.rules.score:grade_score_matches``.

Each function receives the chunk DataFrame (contract column names) and returns
a boolean Series where True means the row passes.
"""

import pandas as pd


def _column(df: pd.DataFrame, *names: str) -> pd.Series:
    for name in names:
        if name in df.columns:
            return df[name]
    raise KeyError(f"none of {names} in {list(df.columns)}")


def grade_score_matches(df: pd.DataFrame) -> pd.Series:
    """A grade of A needs a score of at least 90."""
    grade = _column(df, "grade", "Grade").astype("string")
    score = pd.to_numeric(_column(df, "score", "Score"), errors="coerce")
    failing = (grade == "A") & (score < 90)
    return ~failing.fillna(False).astype(bool)
