from dataclasses import dataclass
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("ASHARE_PANIC_DATA", "data")).expanduser().resolve()
INDICES = {
    "000001.SH": "上证指数", "000016.SH": "上证50", "000300.SH": "沪深300",
    "000905.SH": "中证500", "000852.SH": "中证1000", "399006.SZ": "创业板指",
}
# Column positions in OptBBS k.csv, matching the public AKShare adapter.
QVIX = {
    "50etf": ("50ETF", 1, "vix50.csv"),
    "300etf": ("300ETF", 9, "vix300.csv"),
    "500etf": ("500ETF", 67, "vix500.csv"),
    "1000index": ("中证1000股指", 25, "vixindex1000.csv"),
}
FACTORS = ("below_ma20", "new_low20", "drop5_share", "basket_loss5", "downside_vol20")
PANIC_METHOD = "panic-v1"
QVIX_METHOD = "optbbs-v1"
INDEX_METHOD = "index-v1"
# Fixed comparison windows, requiring at least 80% of sessions to be observed.
RANK_WINDOWS = {252: 202, 756: 605}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = DATA
    local_daily: Path | None = None
    actions_db: Path | None = None
    window: int = 756
    min_history: int = 252
    max_age_seconds: int = 300
    timeout: float = 12
    risk_free_rate: float = .015
    rps_counts: Path | None = None
    rps_history: Path | None = None

    def __post_init__(self):
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser().resolve())
        for name, relative in (("local_daily", "market/daily"), ("actions_db", "market/actions.sqlite3"),
                               ("rps_counts", "rps-history-source.csv"), ("rps_history", "rps-history")):
            value = getattr(self, name)
            object.__setattr__(self, name, Path(value).expanduser().resolve() if value is not None else self.data_dir / relative)
        if not 1 <= self.min_history <= self.window:
            raise ValueError("Require 1 <= min_history <= window")
        if self.max_age_seconds <= 0 or self.timeout <= 0:
            raise ValueError("Age limit and timeout must be positive")
        if not -.05 <= self.risk_free_rate <= .2:
            raise ValueError("risk_free_rate must be between -0.05 and 0.2")
