"""Review data shared by the offline report, with explicit missing observations."""
import pandas as pd

from .config import FACTORS, QVIX
from .store import clean
from .damage import DAMAGE_SCALES


def review_data(daily):
    scored = daily.get("score", pd.Series(index=daily.index, dtype=float))
    triggered = daily.get("panic", pd.Series(False, index=daily.index)).fillna(False).astype(bool)
    years, events = [], []
    for year, group in daily.groupby(daily.index.year):
        scores = scored.reindex(group.index).dropna()
        years.append({"year": int(year), "sessions": len(group), "valid": len(scores),
                      "high": int((scores >= 85).sum()), "panic": int(triggered.reindex(group.index).sum()),
                      "mean": scores.mean(), "peak": scores.max(),
                      "peak_date": str(scores.idxmax().date()) if len(scores) else None,
                      "long_high": int(group.get("damage_score", pd.Series(dtype=float)).ge(85).sum()),
                      "combined_high": int(group.get("pressure_score", pd.Series(dtype=float)).ge(85).sum())})
    # An episode is a run of triggered market sessions. Missing/untriggered days
    # break an episode; each session remains available in the daily CSV.
    groups = triggered.ne(triggered.shift()).cumsum()
    for _, group in daily.loc[triggered].groupby(groups[triggered]):
        peak_date = scored.reindex(group.index).idxmax()
        row = daily.loc[peak_date]
        events.append({"start": str(group.index[0].date()), "end": str(group.index[-1].date()),
                       "days": len(group), "date": str(peak_date.date()), "score": row.get("score"),
                       "drop5": row.get("drop5_share"), "loss5": row.get("basket_loss5")})
    fields = ["score", "panic", "count", "adv_share", "coverage_ratio", *FACTORS,
              *("p_" + name for name in FACTORS), "damage_score", "pressure_score", "damage_count", "damage_valid",
              *DAMAGE_SCALES, *("d_" + name for name in DAMAGE_SCALES)]
    days = {str(date.date()): row.reindex(fields).to_dict() for date, row in daily.iterrows()}
    coverage = []
    for name, (label, _, _) in QVIX.items():
        values = daily[name].dropna()
        coverage.append({"label": label, "count": len(values), "total": len(daily),
                         "first": str(values.index[0].date()) if len(values) else None,
                         "last": str(values.index[-1].date()) if len(values) else None})
    damage_events = []
    for kind, field in [("long", "damage_score"), ("combined", "pressure_score")]:
        values = daily.get(field, pd.Series(index=daily.index, dtype=float))
        mask = values.ge(85)
        for _, group in daily.loc[mask].groupby(mask.ne(mask.shift()).cumsum()[mask]):
            peak = values.reindex(group.index).idxmax()
            damage_events.append({"kind": kind, "start": str(group.index[0].date()), "end": str(group.index[-1].date()),
                                  "days": len(group), "date": str(peak.date()), "score": values.loc[peak]})
    return clean({"years": years, "events": list(reversed(events)), "damage_events": list(reversed(damage_events)),
                  "days": days, "coverage": coverage,
                  "score_coverage": {"valid": int(scored.notna().sum()), "total": len(daily),
                                     "missing": daily.index[scored.isna()].strftime("%Y-%m-%d").tolist()}})
