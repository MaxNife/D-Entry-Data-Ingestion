"""Custom rule functions referenced by contracts/score_test/v1.yaml (spec s5, `custom`).

Written defensively so it works whether the pipeline hands it contract column names
or source headers, and raw strings or coerced values.
"""

import pandas as pd


def _column(df: pd.DataFrame, *names: str) -> pd.Series:
    for name in names:
        if name in df.columns:
            return df[name]
    raise KeyError(f"none of {names} in {list(df.columns)}")


def grade_a_requires_90(df: pd.DataFrame) -> pd.Series:
    """True where the row passes: a grade of A needs a score of at least 90."""
    grade = _column(df, "grade", "Grade").astype("string")
    score = pd.to_numeric(_column(df, "score", "Score"), errors="coerce")
    failing = (grade == "A") & (score < 90)
    return ~failing.fillna(False).astype(bool)
