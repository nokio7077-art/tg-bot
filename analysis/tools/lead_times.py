#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Только лид-тайм: за сколько дней до дня X сигнал срабатывает впервые.

Считает лид-тайм и для войн, и для контрольных окон. Второе обязательно:
сигнал, который срабатывает в 37 войнах из 37, ничего не стоит, если он же
срабатывает в 34 спокойных окнах из 34. Разница между этими двумя долями —
и есть вся польза сигнала.

Ничего кроме pandas и numpy не нужно.

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

ВЕРСИЯ = "lead_times.py v3 — войны И контроли, чтобы видеть ложные срабатывания"
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
    # В суточных файлах есть DATEADDED (день попадания в новости), в старых
    # месячных и годовых его нет — там ориентир SQLDATE (день события).
    поле = "DATEADDED" if "DATEADDED" in d.columns and d.DATEADDED.notna().any() else "SQLDATE"
    d["date"] = pd.to_datetime(d[поле].astype(str).str[:8], format="%Y%m%d", errors="coerce")
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
    rows = []
    for k, (_, r) in enumerate(C.iterrows(), start=1):
        суффикс = "" if int(r.метка) == 1 else "_control"
        path = os.path.join(args.cases, f"{r.пара}_{r.день_X}{суффикс}.csv")
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
            rows.append(dict(пара=r.пара, день_X=r.день_X, метка=int(r.метка),
                             название=r.get("название", ""),
                             признак=feat, порог=thr, лид_тайм_дней=first,
                             цензурировано=bool(first is not None and first >= потолок),
                             потолок_дней=потолок))
        print(f"  {k}/{len(C)}  {r.пара} {r.день_X}"
              f"{' (контроль)' if суффикс else ''}", flush=True)

    L = pd.DataFrame(rows)
    if not len(L):
        sys.exit("Ни одного кейса посчитать не удалось")
    L.to_csv(args.out, index=False, encoding="utf-8-sig")

    ключ = ["пара", "день_X", "метка"]
    n_w = L[L.метка == 1][ключ].drop_duplicates().shape[0]
    n_c = L[L.метка == 0][ключ].drop_duplicates().shape[0]
    print(f"\nОбработано: войн {n_w}, контролей {n_c}\n")

    print("СРАБАТЫВАНИЕ ХОТЯ БЫ РАЗ ЗА ОКНО НАБЛЮДЕНИЯ")
    print(f"{'признак':<12}{'порог':>6}{'в войнах':>12}{'в контролях':>14}"
          f"{'разница':>10}{'медиана лид-тайма':>20}")
    итог = []
    for (feat, thr), grp in L.groupby(["признак", "порог"], sort=False):
        w = grp[grp.метка == 1]; c = grp[grp.метка == 0]
        tpr = w.лид_тайм_дней.notna().mean() if len(w) else float("nan")
        fpr = c.лид_тайм_дней.notna().mean() if len(c) else float("nan")
        med = w.лид_тайм_дней.dropna().median()
        итог.append((feat, thr, tpr, fpr, med))
        print(f"{feat:<12}{thr:>6.1f}{f'{tpr:.0%}':>12}{f'{fpr:.0%}':>14}"
              f"{f'{tpr-fpr:+.0%}':>10}{(f'{med:.0f} дн.' if med == med else '—'):>20}")

    print("\nРазница — это и есть польза сигнала. Если она около нуля, сигнал")
    print("срабатывает одинаково часто перед войной и в спокойное время.")

    print("\n\nЛИД-ТАЙМ ТОЛЬКО ПО ВОЙНАМ, С УЧЁТОМ ПОТОЛКА НАБЛЮДЕНИЯ")
    print(f"{'признак':<12}{'порог':>6}{'сработал':>11}{'медиана':>9}"
          f"{'квартили':>12}{'на потолке':>12}")
    for (feat, thr), grp in L[L.метка == 1].groupby(["признак", "порог"], sort=False):
        got = grp.лид_тайм_дней.dropna(); cens = int(grp.цензурировано.sum())
        if not len(got):
            print(f"{feat:<12}{thr:>6.1f}{'0':>11}{'—':>9}{'—':>12}{'—':>12}"); continue
        print(f"{feat:<12}{thr:>6.1f}{len(got):>5}/{n_w:<5}{got.median():>9.0f}"
              f"{f'{got.quantile(.25):.0f}-{got.quantile(.75):.0f}':>12}"
              f"{f'{cens}/{len(got)}':>12}")

    print(f"\nФайл: {args.out}")
    print("«На потолке» — сигнал уже горел на самом раннем дне, который можно посчитать.")
    print("Для таких кейсов настоящий лид-тайм больше указанного, насколько — не видно.")


if __name__ == "__main__":
    main()
