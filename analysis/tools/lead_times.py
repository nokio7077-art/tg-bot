#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Только лид-тайм: за сколько дней до дня X сигнал срабатывает впервые.

Отдельный маленький скрипт, чтобы его нельзя было перепутать со старой версией
analyze_cases.py. Ничего кроме pandas и numpy не нужно.

    python lead_times.py cases
    python lead_times.py cases --out lead_times_v2.csv

Почему пороги разные у разных признаков: сравнивать с числом можно только
признаки, нормированные на собственный фон пары. z_tone и z_vol измеряются
в сигмах, vol_ratio — это «во сколько раз неделя выше фона». А log_vol —
логарифм числа событий, у него «больше 1.0» выполняется почти всегда, поэтому
его тут нет: именно на этом сломался прошлый расчёт.
"""
from __future__ import annotations
import argparse, os, sys
import numpy as np
import pandas as pd

ВЕРСИЯ = "lead_times.py v2 — пороги только по нормированным признакам"
WINDOW_DAYS, BASE_WEEKS = 7, 4
MIN_DAYS = WINDOW_DAYS + BASE_WEEKS * 7          # 35 дней истории на расчёт фона
SPECS = [("z_tone", 1.0), ("z_tone", 2.0),
         ("z_vol", 1.0), ("z_vol", 2.0),
         ("vol_ratio", 1.5), ("vol_ratio", 2.5)]


def daily_from_case(path: str) -> pd.DataFrame:
    d = pd.read_csv(path, dtype=str, low_memory=False)
    if not len(d):
        return pd.DataFrame()
    for c in ("AvgTone", "GoldsteinScale", "QuadClass", "EventRootCode", "EventCode"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["date"] = pd.to_datetime(d.DATEADDED.astype(str).str[:8], format="%Y%m%d", errors="coerce")
    d = d[d.date.notna()]
    if not len(d):
        return pd.DataFrame()
    g = d.groupby("date", sort=True).agg(n=("AvgTone", "size"), tone=("AvgTone", "mean"))
    idx = pd.date_range(g.index.min(), g.index.max())
    g = g.reindex(idx)
    g["n"] = g.n.fillna(0)
    g["tone"] = g.tone.interpolate().ffill().bfill()
    return g


def features_at(p: pd.DataFrame, end: int) -> dict | None:
    if end < MIN_DAYS:
        return None
    w = p.iloc[end - WINDOW_DAYS:end]
    b = p.iloc[end - MIN_DAYS:end - WINDOW_DAYS]
    blocks = [b.iloc[i:i + 7] for i in range(0, len(b) - 6, 7)]
    if len(blocks) < BASE_WEEKS:
        return None
    f = {}
    bv = np.array([x.tone.mean() for x in blocks])
    sd = bv.std(ddof=1)
    f["z_tone"] = float(-(w.tone.mean() - bv.mean()) / sd) if sd > 0 else 0.0
    bn = np.log(np.maximum([x.n.mean() for x in blocks], 1))
    sdn = bn.std(ddof=1)
    f["z_vol"] = float((np.log(max(w.n.mean(), 1)) - bn.mean()) / sdn) if sdn > 0 else 0.0
    f["vol_ratio"] = float(w.n.mean() / max(b.n.mean(), 1))
    return f


def main():
    ap = argparse.ArgumentParser(description="Лид-тайм по собранным кейсам")
    ap.add_argument("cases", help="папка, сделанная collect_cases.py")
    ap.add_argument("--out", default="lead_times_v2.csv")
    args = ap.parse_args()
    print(ВЕРСИЯ + "\n")

    cl = os.path.join(args.cases, "case_list.csv")
    if not os.path.exists(cl):
        sys.exit(f"Нет файла {cl}")
    C = pd.read_csv(cl)
    W = C[C.метка == 1]
    rows = []
    for k, (_, r) in enumerate(W.iterrows(), start=1):
        path = os.path.join(args.cases, f"{r.пара}_{r.день_X}.csv")
        if not os.path.exists(path):
            continue
        p = daily_from_case(path)
        if len(p) < MIN_DAYS:
            continue
        ends = list(range(MIN_DAYS, len(p) + 1))
        vals = {e: features_at(p, e) for e in ends}
        потолок = len(ends)                     # раньше сигнал увидеть нельзя
        for feat, thr in SPECS:
            first = None
            for e in ends:
                f = vals[e]
                if f and f[feat] >= thr:
                    first = len(p) - e + 1
                    break
            rows.append(dict(пара=r.пара, день_X=r.день_X, название=r.get("название", ""),
                             признак=feat, порог=thr, лид_тайм_дней=first,
                             цензурировано=bool(first is not None and first >= потолок),
                             потолок_дней=потолок))
        print(f"  {k}/{len(W)}  {r.пара} {r.день_X}", flush=True)

    L = pd.DataFrame(rows)
    if not len(L):
        sys.exit("Ни одного кейса посчитать не удалось")
    L.to_csv(args.out, index=False, encoding="utf-8-sig")

    n_wars = L[["пара", "день_X"]].drop_duplicates().shape[0]
    print(f"\nВойн обработано: {n_wars}\n")
    print(f"{'признак':<12}{'порог':>7}{'сработал':>11}{'медиана':>10}{'квартили':>12}"
          f"{'на потолке':>13}")
    for (feat, thr), grp in L.groupby(["признак", "порог"], sort=False):
        got = grp.лид_тайм_дней.dropna()
        if not len(got):
            print(f"{feat:<12}{thr:>7.1f}{'0':>11}{'—':>10}{'—':>12}{'—':>13}")
            continue
        print(f"{feat:<12}{thr:>7.1f}{len(got):>6}/{n_wars:<4}{got.median():>10.0f}"
              f"{f'{got.quantile(.25):.0f}-{got.quantile(.75):.0f}':>12}"
              f"{f'{int(grp.цензурировано.sum())}/{len(got)}':>13}")
    print(f"\nФайл: {args.out}")
    print("«На потолке» — сигнал уже горел на самом раннем дне, который можно посчитать.")
    print("Для таких кейсов настоящий лид-тайм больше указанного, насколько — не видно.")


if __name__ == "__main__":
    main()
