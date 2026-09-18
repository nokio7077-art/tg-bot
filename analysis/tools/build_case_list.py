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
    # Америка
    2: "USA", 20: "CAN", 40: "CUB", 41: "HTI", 42: "DOM", 52: "TTO", 55: "GRD", 70: "MEX",
    90: "GTM", 91: "HND", 92: "SLV", 93: "NIC", 94: "CRI", 95: "PAN", 100: "COL", 101: "VEN",
    110: "GUY", 115: "SUR", 130: "ECU", 135: "PER", 140: "BRA", 145: "BOL", 150: "PRY",
    155: "CHL", 160: "ARG", 165: "URY",
    # Европа
    200: "GBR", 205: "IRL", 210: "NLD", 211: "BEL", 220: "FRA", 225: "CHE", 230: "ESP",
    235: "PRT", 255: "DEU", 290: "POL", 305: "AUT", 310: "HUN", 316: "CZE", 317: "SVK",
    325: "ITA", 339: "ALB", 343: "MKD", 344: "HRV", 345: "SRB", 346: "BIH", 349: "SVN",
    350: "GRC", 352: "CYP", 355: "BGR", 359: "MDA", 360: "ROU", 365: "RUS", 366: "EST",
    367: "LVA", 368: "LTU", 369: "UKR", 370: "BLR", 371: "ARM", 372: "GEO", 373: "AZE",
    375: "FIN", 380: "SWE", 385: "NOR", 390: "DNK",
    # Африка
    402: "CPV", 404: "GNB", 411: "GNQ", 420: "GMB", 432: "MLI", 433: "SEN", 434: "BEN",
    435: "MRT", 436: "NER", 437: "CIV", 438: "GIN", 439: "BFA", 450: "LBR", 451: "SLE",
    452: "GHA", 461: "TGO", 471: "CMR", 475: "NGA", 481: "GAB", 482: "CAF", 483: "TCD",
    484: "COG", 490: "COD", 500: "UGA", 501: "KEN", 510: "TZA", 516: "BDI", 517: "RWA",
    520: "SOM", 522: "DJI", 530: "ETH", 531: "ERI", 540: "AGO", 541: "MOZ", 551: "ZMB",
    552: "ZWE", 553: "MWI", 560: "ZAF", 565: "NAM", 570: "LSO", 571: "BWA", 580: "MDG",
    581: "COM", 590: "MUS", 600: "MAR", 615: "DZA", 616: "TUN", 620: "LBY", 625: "SDN",
    626: "SSD",
    # Ближний Восток и Азия
    630: "IRN", 640: "TUR", 645: "IRQ", 651: "EGY", 652: "SYR", 660: "LBN", 663: "JOR",
    666: "ISR", 670: "SAU", 678: "YEM", 680: "YEM", 690: "KWT", 692: "BHR", 694: "QAT",
    696: "ARE", 698: "OMN", 700: "AFG", 701: "TKM", 702: "TJK", 703: "KGZ", 704: "UZB",
    705: "KAZ", 710: "CHN", 712: "MNG", 713: "TWN", 731: "PRK", 732: "KOR", 740: "JPN",
    750: "IND", 760: "BTN", 770: "PAK", 771: "BGD", 775: "MMR", 780: "LKA", 781: "MDV",
    790: "NPL", 800: "THA", 811: "KHM", 812: "LAO", 816: "VNM", 817: "VNM", 820: "MYS",
    830: "SGP", 840: "PHL", 850: "IDN", 860: "TLS",
    # Океания
    900: "AUS", 910: "PNG", 920: "NZL",
    # Исторические образования без кода GDELT пропускаются намеренно: 751 (Хайдарабад)
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


def build_cases(df: pd.DataFrame, types: list[int], min_year: int,
                exact_only: bool, window: int) -> tuple[pd.DataFrame, set]:
    """Строит список кейсов из таблицы UCDP (диадной или конфликтной)."""
    df = df.copy()
    df["start_date2"] = pd.to_datetime(df.start_date2, errors="coerce")
    key = ["dyad_id", "start_date2"] if "dyad_id" in df.columns else ["conflict_id", "start_date2"]
    ep = df[df.type_of_conflict.isin(types)].drop_duplicates(subset=key)
    ep = ep[ep.start_date2.notna() & (ep.start_date2.dt.year >= min_year)]
    if exact_only:
        ep = ep[ep.start_prec2 == 1]

    rows, unmapped = [], set()
    for _, r in ep.iterrows():
        for na, ca in split_side(r.side_a, r.gwno_a):
            for nb, cb in split_side(r.side_b, r.gwno_b):
                code_a, code_b = GW_TO_GDELT.get(ca), GW_TO_GDELT.get(cb)
                if not code_a:
                    unmapped.add((ca, na))
                if not code_b:
                    unmapped.add((cb, nb))
                if not code_a or not code_b or code_a == code_b:
                    continue
                day_x = r.start_date2
                rows.append(dict(
                    пара="-".join(sorted([code_a, code_b])),
                    страна_A=na, страна_B=nb, код_A=code_a, код_B=code_b,
                    день_X=day_x.date(),
                    окно_с=(day_x - pd.Timedelta(days=window)).date(),
                    окно_по=(day_x - pd.Timedelta(days=1)).date(),
                    точность_даты=int(r.start_prec2),
                    интенсивность=int(r.intensity_level),
                    тип_конфликта=int(r.type_of_conflict),
                    покрытие_GDELT=coverage(day_x),
                    dyad_id=int(r.dyad_id) if "dyad_id" in df.columns else None,
                    conflict_id=int(r.conflict_id),
                ))
    C = pd.DataFrame(rows)
    if len(C):
        C = C.drop_duplicates(subset=["пара", "день_X"]).sort_values("день_X", ascending=False)
    return C, unmapped


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

    C, unmapped = build_cases(df, [int(t) for t in args.types.split(",")],
                              args.min_year, args.exact_only, args.window)
    if not len(C):
        sys.exit("Ни одного кейса не получилось — ослабьте фильтры")
    C.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"Пар-кейсов получено: {len(C)}")
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
