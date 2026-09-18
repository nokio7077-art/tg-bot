"""Общий код: агрегация GDELT по парам государств и расчёт признаков.

Используется и ноутбуком обучения (ml_pipeline.ipynb), и сканером (scan_dyads.py),
чтобы признаки считались ровно одинаково в обучении и в применении.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

# Колонки GDELT 1.0 Event Export в порядке следования (для файлов без заголовка)
GDELT_COLUMNS = [
    "GLOBALEVENTID","SQLDATE","MonthYear","Year","FractionDate",
    "Actor1Code","Actor1Name","Actor1CountryCode","Actor1KnownGroupCode","Actor1EthnicCode",
    "Actor1Religion1Code","Actor1Religion2Code","Actor1Type1Code","Actor1Type2Code","Actor1Type3Code",
    "Actor2Code","Actor2Name","Actor2CountryCode","Actor2KnownGroupCode","Actor2EthnicCode",
    "Actor2Religion1Code","Actor2Religion2Code","Actor2Type1Code","Actor2Type2Code","Actor2Type3Code",
    "IsRootEvent","EventCode","EventBaseCode","EventRootCode","QuadClass","GoldsteinScale",
    "NumMentions","NumSources","NumArticles","AvgTone",
    "Actor1Geo_Type","Actor1Geo_FullName","Actor1Geo_CountryCode","Actor1Geo_ADM1Code",
    "Actor1Geo_Lat","Actor1Geo_Long","Actor1Geo_FeatureID",
    "Actor2Geo_Type","Actor2Geo_FullName","Actor2Geo_CountryCode","Actor2Geo_ADM1Code",
    "Actor2Geo_Lat","Actor2Geo_Long","Actor2Geo_FeatureID",
    "ActionGeo_Type","ActionGeo_FullName","ActionGeo_CountryCode","ActionGeo_ADM1Code",
    "ActionGeo_Lat","ActionGeo_Long","ActionGeo_FeatureID",
    "DATEADDED","SOURCEURL",
]
USE_COLS = ["GLOBALEVENTID","DATEADDED","Actor1CountryCode","Actor2CountryCode",
            "EventRootCode","EventCode","QuadClass","GoldsteinScale","AvgTone","NumArticles"]

# --- окна расчёта: должны совпадать в обучении и применении -------------------
WINDOW_DAYS = 7      # окно оценки
BASE_WEEKS  = 4      # фон: столько недель перед окном
MIN_DAYS    = WINDOW_DAYS + BASE_WEEKS * 7   # 35 дней минимальной истории


def aggregate_pair_days(df: pd.DataFrame) -> pd.DataFrame:
    """Сворачивает сырые строки событий в агрегат «пара × день».

    Пара неупорядоченная: США-Иран и Иран-США считаются одним и тем же.
    """
    d = df.copy()
    for col in ("Actor1CountryCode", "Actor2CountryCode"):
        d[col] = d[col].astype("string").str.strip()
    d = d[d.Actor1CountryCode.notna() & d.Actor2CountryCode.notna()]
    d = d[(d.Actor1CountryCode != "") & (d.Actor2CountryCode != "")]
    d = d[d.Actor1CountryCode != d.Actor2CountryCode]
    if d.empty:
        return pd.DataFrame()

    a = d.Actor1CountryCode.values.astype(str)
    b = d.Actor2CountryCode.values.astype(str)
    d["pair"] = np.where(a < b, a + "-" + b, b + "-" + a)

    for col in ("AvgTone", "GoldsteinScale", "QuadClass", "EventRootCode", "EventCode"):
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["date"] = pd.to_datetime(d.DATEADDED.astype(str).str[:8], format="%Y%m%d", errors="coerce")
    d = d[d.date.notna()]

    d["is_mat"]   = (d.QuadClass == 4).astype("int8")
    d["is_hard"]  = d.EventRootCode.isin([18, 19, 20]).astype("int8")
    d["is_force"] = d.EventCode.isin([190, 193, 195]).astype("int8")

    g = d.groupby(["pair", "date"], sort=False)
    out = g.agg(n=("AvgTone", "size"), tone_sum=("AvgTone", "sum"),
                gold_sum=("GoldsteinScale", "sum"),
                mat=("is_mat", "sum"), hard=("is_hard", "sum"), force=("is_force", "sum"))
    return out.reset_index()


def combine_chunks(parts: list[pd.DataFrame]) -> pd.DataFrame:
    """Складывает агрегаты из нескольких кусков файла."""
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame()
    allp = pd.concat(parts, ignore_index=True)
    return (allp.groupby(["pair", "date"], sort=False)
                .sum(numeric_only=True).reset_index())


def pair_daily_frame(agg: pd.DataFrame, pair: str) -> pd.DataFrame:
    """Дневной ряд одной пары с заполненным календарём."""
    p = agg[agg.pair == pair].set_index("date").sort_index()
    idx = pd.date_range(p.index.min(), p.index.max())
    p = p.reindex(idx)
    p["n"] = p.n.fillna(0)
    for c in ("tone_sum", "gold_sum", "mat", "hard", "force"):
        p[c] = p[c].fillna(0)
    p["tone"]  = np.where(p.n > 0, p.tone_sum / p.n.replace(0, np.nan), np.nan)
    p["gold"]  = np.where(p.n > 0, p.gold_sum / p.n.replace(0, np.nan), np.nan)
    p["mat_s"] = np.where(p.n > 0, p.mat / p.n.replace(0, np.nan), np.nan)
    p["hard_s"]= np.where(p.n > 0, p.hard / p.n.replace(0, np.nan), np.nan)
    p["force_s"]=np.where(p.n > 0, p.force / p.n.replace(0, np.nan), np.nan)
    for c in ("tone", "gold", "mat_s", "hard_s", "force_s"):
        p[c] = p[c].interpolate().ffill().bfill()
    return p


def features_at(p: pd.DataFrame, end: int | None = None) -> dict | None:
    """Признаки для окна, заканчивающегося на позиции end (по умолчанию — конец ряда).

    Возвращает None, если истории меньше MIN_DAYS дней.
    """
    end = len(p) if end is None else end
    if end < MIN_DAYS:
        return None
    w = p.iloc[end - WINDOW_DAYS:end]
    b = p.iloc[end - MIN_DAYS:end - WINDOW_DAYS]
    blocks = [b.iloc[i:i + 7] for i in range(0, len(b) - 6, 7)]
    if len(blocks) < BASE_WEEKS:
        return None

    f = {}
    for col, sign, name in [("tone", -1, "tone"), ("gold", -1, "gold"),
                            ("mat_s", +1, "mat"), ("hard_s", +1, "hard"), ("force_s", +1, "force")]:
        bv = np.array([x[col].mean() for x in blocks])
        sd = bv.std(ddof=1)
        f[f"z_{name}"] = float(sign * (w[col].mean() - bv.mean()) / sd) if sd > 0 else 0.0
        f[f"lvl_{name}"] = float(w[col].mean())
    bn = np.log(np.maximum([x.n.mean() for x in blocks], 1))
    sdn = bn.std(ddof=1)
    f["z_vol"] = float((np.log(max(w.n.mean(), 1)) - bn.mean()) / sdn) if sdn > 0 else 0.0
    f["vol_ratio"] = float(w.n.mean() / max(b.n.mean(), 1))
    f["log_vol"] = float(np.log(max(w.n.mean(), 1)))
    y = p.tone.iloc[end - 21:end].values
    f["slope21_tone"] = float(np.polyfit(np.arange(len(y)), y, 1)[0]) if len(y) == 21 else 0.0
    f["events_total"] = float(p.n.iloc[:end].sum())
    f["events_week"]  = float(w.n.sum())
    return f


def tone_alarm_score(f: dict) -> float:
    """Балл правила TONE-1σ. Это НЕ вероятность войны — только ранжирующий балл."""
    return f["z_tone"]
