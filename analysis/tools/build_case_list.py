#!/usr/bin/env python3
"""UCDP/PRIO Armed Conflict Dataset -> список размеченных кейсов для обучения.

Берёт межгосударственные конфликты (type_of_conflict = 2), раскладывает эпизоды
на пары государств, переводит коды Gleditsch-Ward в коды стран GDELT и считает,
какое окно данных нужно скачать под каждый кейс.

    python build_case_list.py UcdpPrioConflict_v26_1.xlsx --out ../data/case_list.csv
"""
from __future__ import annotations
import argparse, sys
import pandas as pd

# Gleditsch-Ward -> код страны в GDELT (Actor1CountryCode / Actor2CountryCode).
# Заполнено для всех государств, встречающихся в межгосударственных эпизодах с 1979 года.
GW_TO_GDELT = {
    2: "USA", 55: "GRD", 95: "PAN", 130: "ECU", 135: "PER", 160: "ARG", 200: "GBR",
    365: "RUS", 369: "UKR", 432: "MLI", 439: "BFA", 471: "CMR", 475: "NGA", 483: "TCD",
    520: "SOM", 522: "DJI", 530: "ETH", 531: "ERI", 620: "LBY", 625: "SDN", 626: "SSD",
    630: "IRN", 645: "IRQ", 652: "SYR", 666: "ISR", 678: "YEM", 680: "YEM", 690: "KWT",
    700: "AFG", 702: "TJK", 703: "KGZ", 710: "CHN", 750: "IND", 770: "PAK", 800: "THA",
    811: "KHM", 812: "LAO", 816: "VNM", 900: "AUS",
}
# Границы покрытия GDELT 1.0: дневные файлы с апреля 2013, до этого месячные и годовые
DAILY_FROM   = pd.Timestamp("2013-04-01")
MONTHLY_FROM = pd.Timestamp("2006-01-01")
GDELT_FROM   = pd.Timestamp("1979-01-01")

WINDOW_DAYS = 60     # сколько дней до дня X нужно скачать


def split_side(names: str, codes: str) -> list[tuple[str, int]]:
    """«Government of UK, Government of USA» + «200,  2» -> [(UK,200),(USA,2)]"""
    ns = [s.strip().replace("Government of ", "") for s in str(names).split(",")]
    cs = [c.strip() for c in str(codes).split(",") if c.strip() not in ("", "nan")]
    out = []
    for n, c in zip(ns, cs):
        try:
            out.append((n, int(float(c))))
        except ValueError:
            continue
    return out


def coverage(day_x: pd.Timestamp) -> str:
    if day_x >= DAILY_FROM:
        return "дневные файлы"
    if day_x >= MONTHLY_FROM:
        return "месячные файлы"
    if day_x >= GDELT_FROM:
        return "годовые файлы"
    return "вне покрытия GDELT"


def main():
    ap = argparse.ArgumentParser(description="UCDP -> список кейсов для обучения")
    ap.add_argument("xlsx", help="файл UcdpPrioConflict_vXX.xlsx")
    ap.add_argument("--out", default="case_list.csv")
    ap.add_argument("--types", default="2",
                    help="типы конфликта через запятую (2 = межгосударственный)")
    ap.add_argument("--min-year", type=int, default=1979, help="не брать эпизоды раньше этого года")
    ap.add_argument("--exact-only", action="store_true",
                    help="только эпизоды с точной датой начала (start_prec2 = 1)")
    ap.add_argument("--window", type=int, default=WINDOW_DAYS, help="длина окна до дня X")
    args = ap.parse_args()

    df = pd.read_excel(args.xlsx)
    need = {"conflict_id", "side_a", "side_b", "gwno_a", "gwno_b",
            "start_date2", "start_prec2", "type_of_conflict", "intensity_level"}
    missing = need - set(df.columns)
    if missing:
        sys.exit(f"В файле нет колонок: {sorted(missing)}")

    df["start_date2"] = pd.to_datetime(df.start_date2, errors="coerce")
    types = [int(t) for t in args.types.split(",")]
    ep = df[df.type_of_conflict.isin(types)].drop_duplicates(subset=["conflict_id", "start_date2"])
    ep = ep[ep.start_date2.notna() & (ep.start_date2.dt.year >= args.min_year)]
    if args.exact_only:
        ep = ep[ep.start_prec2 == 1]

    rows, unmapped = [], set()
    for _, r in ep.iterrows():
        sides_a = split_side(r.side_a, r.gwno_a)
        sides_b = split_side(r.side_b, r.gwno_b)
        for na, ca in sides_a:
            for nb, cb in sides_b:
                code_a, code_b = GW_TO_GDELT.get(ca), GW_TO_GDELT.get(cb)
                if not code_a:
                    unmapped.add((ca, na))
                if not code_b:
                    unmapped.add((cb, nb))
                if not code_a or not code_b or code_a == code_b:
                    continue
                pair = "-".join(sorted([code_a, code_b]))
                day_x = r.start_date2
                rows.append(dict(
                    пара=pair, страна_A=na, страна_B=nb, код_A=code_a, код_B=code_b,
                    день_X=day_x.date(),
                    окно_с=(day_x - pd.Timedelta(days=args.window)).date(),
                    окно_по=(day_x - pd.Timedelta(days=1)).date(),
                    точность_даты=int(r.start_prec2),
                    интенсивность=int(r.intensity_level),
                    тип_конфликта=int(r.type_of_conflict),
                    покрытие_GDELT=coverage(day_x),
                    conflict_id=int(r.conflict_id),
                ))

    if not rows:
        sys.exit("Ни одного кейса не получилось — ослабьте фильтры")
    C = pd.DataFrame(rows).sort_values("день_X", ascending=False)
    C.to_csv(args.out, index=False, encoding="utf-8-sig")

    print(f"Эпизодов отобрано: {ep.shape[0]} | пар-кейсов получено: {len(C)}")
    print(f"Файл: {args.out}\n")
    print("ПО ПОКРЫТИЮ GDELT:")
    for cov, grp in C.groupby("покрытие_GDELT"):
        exact = int((grp.точность_даты == 1).sum())
        print(f"  {cov:<22} {len(grp):>3} кейсов (с точной датой: {exact})")
    print("\nПО ТОЧНОСТИ ДАТЫ (1 = известен день):")
    print(C.точность_даты.value_counts().sort_index().to_string())
    if unmapped:
        print("\nНЕТ КОДА GDELT (эти государства пропущены):")
        for c, n in sorted(unmapped):
            print(f"  {c} — {n}")
    print("\nКЕЙСЫ С ДНЕВНЫМ ПОКРЫТИЕМ (готовы к сбору данных):")
    ready = C[(C.покрытие_GDELT == "дневные файлы") & (C.точность_даты == 1)]
    cols = ["пара", "страна_A", "страна_B", "день_X", "окно_с", "окно_по", "интенсивность"]
    print(ready[cols].to_string(index=False) if len(ready) else "  нет")


if __name__ == "__main__":
    main()
