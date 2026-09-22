#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MID 5.0 (Correlates of War) -> список кейсов в формате collect_cases.py.

MID — это 2400+ вооружённых инцидентов между государствами с 1816 по 2014 год.
Для нас это способ увеличить выборку с 37 войн до сотен: именно столько нужно,
чтобы обучать взвешенную модель, а не одно правило.

Где взять файлы: https://correlatesofwar.org/data-sets/mids/
Нужны два файла из архива MID 5.0: MIDA 5.0.csv (уровень спора) и
MIDB 5.0.csv (уровень участника).

    python build_mid_cases.py --inspect "MIDA 5.0.csv" "MIDB 5.0.csv"
    python build_mid_cases.py "MIDA 5.0.csv" "MIDB 5.0.csv" --out mid_cases.csv

Важные решения, зашитые в скрипт:
  * берутся только споры с уровнем враждебности >= 4 (применение силы и война).
    Уровни 2-3 — это угрозы и демонстрации силы, они не то, что мы предсказываем;
  * нужна точная дата начала (stday > 0), иначе окно строить не из чего;
  * стороны берутся из MIDB по флагу sidea, кейсом становится каждая пара
    «участник стороны A + участник стороны B»;
  * коды стран COW переводятся в коды GDELT; неизвестные печатаются списком.

Нужен только pandas:   pip install pandas
"""
from __future__ import annotations
import argparse, sys
import pandas as pd

ВЕРСИЯ = "build_mid_cases.py v1"
DAILY_FROM   = pd.Timestamp("2013-04-01")
MONTHLY_FROM = pd.Timestamp("2006-01-01")
GDELT_FROM   = pd.Timestamp("1979-01-01")
WINDOW_DAYS  = 60

# Коды Correlates of War -> коды стран GDELT. Нумерация COW почти совпадает с
# Gleditsch-Ward, поэтому основа взята оттуда; отличия COW дописаны ниже.
COW_TO_GDELT = {
    2:"USA",20:"CAN",40:"CUB",41:"HTI",42:"DOM",51:"JAM",52:"TTO",53:"BRB",54:"DMA",
    55:"GRD",56:"LCA",57:"VCT",58:"ATG",60:"KNA",70:"MEX",80:"BLZ",90:"GTM",91:"HND",
    92:"SLV",93:"NIC",94:"CRI",95:"PAN",100:"COL",101:"VEN",110:"GUY",115:"SUR",
    130:"ECU",135:"PER",140:"BRA",145:"BOL",150:"PRY",155:"CHL",160:"ARG",165:"URY",
    200:"GBR",205:"IRL",210:"NLD",211:"BEL",212:"LUX",220:"FRA",225:"CHE",230:"ESP",
    235:"PRT",255:"DEU",260:"DEU",265:"DEU",290:"POL",305:"AUT",310:"HUN",315:"CZE",
    316:"CZE",317:"SVK",325:"ITA",327:"VAT",331:"SMR",338:"MLT",339:"ALB",343:"MKD",
    344:"HRV",345:"SRB",346:"BIH",347:"XKX",349:"SVN",350:"GRC",352:"CYP",355:"BGR",
    359:"MDA",360:"ROU",365:"RUS",366:"EST",367:"LVA",368:"LTU",369:"UKR",370:"BLR",
    371:"ARM",372:"GEO",373:"AZE",375:"FIN",380:"SWE",385:"NOR",390:"DNK",395:"ISL",
    402:"CPV",403:"STP",404:"GNB",411:"GNQ",420:"GMB",432:"MLI",433:"SEN",434:"BEN",
    435:"MRT",436:"NER",437:"CIV",438:"GIN",439:"BFA",450:"LBR",451:"SLE",452:"GHA",
    461:"TGO",471:"CMR",475:"NGA",481:"GAB",482:"CAF",483:"TCD",484:"COG",490:"COD",
    500:"UGA",501:"KEN",510:"TZA",516:"BDI",517:"RWA",520:"SOM",522:"DJI",530:"ETH",
    531:"ERI",540:"AGO",541:"MOZ",551:"ZMB",552:"ZWE",553:"MWI",560:"ZAF",565:"NAM",
    570:"LSO",571:"BWA",572:"SWZ",580:"MDG",581:"COM",590:"MUS",600:"MAR",615:"DZA",
    616:"TUN",620:"LBY",625:"SDN",626:"SSD",630:"IRN",640:"TUR",645:"IRQ",651:"EGY",
    652:"SYR",660:"LBN",663:"JOR",666:"ISR",670:"SAU",678:"YEM",679:"YEM",680:"YEM",
    690:"KWT",692:"BHR",694:"QAT",696:"ARE",698:"OMN",700:"AFG",701:"TKM",702:"TJK",
    703:"KGZ",704:"UZB",705:"KAZ",710:"CHN",712:"MNG",713:"TWN",731:"PRK",732:"KOR",
    740:"JPN",750:"IND",760:"BTN",770:"PAK",771:"BGD",775:"MMR",780:"LKA",781:"MDV",
    790:"NPL",800:"THA",811:"KHM",812:"LAO",816:"VNM",817:"VNM",820:"MYS",830:"SGP",
    835:"BRN",840:"PHL",850:"IDN",860:"TLS",900:"AUS",910:"PNG",920:"NZL",940:"SLB",
    950:"FJI",
}


def найти(df: pd.DataFrame, *варианты: str) -> str | None:
    """Ищет колонку без учёта регистра и пробелов — в MID они пишутся по-разному."""
    норм = {c.strip().lower().replace(" ", "").replace("_", ""): c for c in df.columns}
    for v in варианты:
        k = v.lower().replace(" ", "").replace("_", "")
        if k in норм:
            return норм[k]
    return None


def покрытие(день: pd.Timestamp) -> str:
    if день >= DAILY_FROM:   return "дневные файлы"
    if день >= MONTHLY_FROM: return "месячные файлы"
    if день >= GDELT_FROM:   return "годовые файлы"
    return "вне покрытия GDELT"


def читать(path: str) -> pd.DataFrame:
    for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            return pd.read_csv(path, encoding=enc, low_memory=False)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, encoding="latin-1", low_memory=False, on_bad_lines="skip")


def дата(df, prefix="st"):
    """Собирает дату из трёх колонок; -9 и 0 означают «неизвестно»."""
    y = найти(df, f"{prefix}year"); m = найти(df, f"{prefix}mon"); d = найти(df, f"{prefix}day")
    if not all([y, m, d]):
        return None, (y, m, d)
    Y = pd.to_numeric(df[y], errors="coerce")
    M = pd.to_numeric(df[m], errors="coerce")
    D = pd.to_numeric(df[d], errors="coerce")
    плохо = Y.isna() | (Y <= 0) | M.isna() | (M <= 0) | D.isna() | (D <= 0)
    out = pd.to_datetime(dict(year=Y.where(~плохо, 1900), month=M.where(~плохо, 1),
                              day=D.where(~плохо, 1)), errors="coerce")
    return out.where(~плохо), (y, m, d)


def main():
    ap = argparse.ArgumentParser(description="MID 5.0 -> список кейсов")
    ap.add_argument("mida", help="MIDA 5.0.csv — уровень спора")
    ap.add_argument("midb", help="MIDB 5.0.csv — уровень участника")
    ap.add_argument("--out", default="mid_cases.csv")
    ap.add_argument("--min-hostlev", type=int, default=4,
                    help="минимальный уровень враждебности: 4 = применение силы, 5 = война")
    ap.add_argument("--min-year", type=int, default=1979, help="раньше GDELT не начинается")
    ap.add_argument("--window", type=int, default=WINDOW_DAYS)
    ap.add_argument("--controls", type=int, default=1,
                    help="сколько контрольных окон добавить на кейс (0 — не добавлять)")
    ap.add_argument("--inspect", action="store_true",
                    help="только показать, какие колонки нашлись, и выйти")
    args = ap.parse_args()
    print(ВЕРСИЯ + "\n")

    A, B = читать(args.mida), читать(args.midb)
    print(f"MIDA: {len(A)} строк, {len(A.columns)} колонок")
    print(f"MIDB: {len(B)} строк, {len(B.columns)} колонок")

    поля = dict(
        disp_A=найти(A, "dispnum", "dispnum3", "dispnum4"),
        hostlev=найти(A, "hostlev", "hostlevel"),
        disp_B=найти(B, "dispnum", "dispnum3", "dispnum4"),
        ccode=найти(B, "ccode", "statea", "state"),
        sidea=найти(B, "sidea", "side_a", "sidea1"),
    )
    датаA, колA = дата(A)
    датаB, колB = дата(B)
    поля["дата_MIDA"] = "+".join(str(x) for x in колA)
    поля["дата_MIDB"] = "+".join(str(x) for x in колB)
    print("\nНайденные колонки:")
    for k, v in поля.items():
        print(f"  {k:<12} -> {v if v else 'НЕ НАЙДЕНА'}")
    if args.inspect:
        print("\nПервые колонки MIDA:", list(A.columns)[:18])
        print("Первые колонки MIDB:", list(B.columns)[:18])
        return
    нет = [k for k, v in поля.items() if not v or "None" in str(v)]
    if нет:
        sys.exit(f"\nНе нашлись колонки: {нет}. Запустите с --inspect и пришлите вывод.")

    # --- отбор споров -------------------------------------------------------
    A = A.copy(); A["день_X"] = датаA
    A["hostlev_n"] = pd.to_numeric(A[поля["hostlev"]], errors="coerce")
    всего = len(A)
    A = A[A.день_X.notna()]
    с_датой = len(A)
    A = A[A.день_X.dt.year >= args.min_year]
    с_года = len(A)
    A = A[A.hostlev_n >= args.min_hostlev]
    print(f"\nСпоров всего {всего} -> с точной датой {с_датой} -> с {args.min_year} года "
          f"{с_года} -> уровень >= {args.min_hostlev}: {len(A)}")

    # --- участники -> пары --------------------------------------------------
    B = B.copy()
    B["ccode_n"] = pd.to_numeric(B[поля["ccode"]], errors="coerce")
    B["sidea_n"] = pd.to_numeric(B[поля["sidea"]], errors="coerce")
    строки, без_кода = [], set()
    for _, sp in A.iterrows():
        уч = B[B[поля["disp_B"]] == sp[поля["disp_A"]]]
        a = [int(x) for x in уч[уч.sidea_n == 1].ccode_n.dropna()]
        b = [int(x) for x in уч[уч.sidea_n == 0].ccode_n.dropna()]
        for ca in a:
            for cb in b:
                ga, gb = COW_TO_GDELT.get(ca), COW_TO_GDELT.get(cb)
                if not ga: без_кода.add(ca)
                if not gb: без_кода.add(cb)
                if not ga or not gb or ga == gb:
                    continue
                dx = sp.день_X
                строки.append(dict(
                    пара="-".join(sorted([ga, gb])), код_A=ga, код_B=gb,
                    эпизод=int(sp[поля["disp_A"]]), название=f"MID {int(sp[поля['disp_A']])}",
                    группа="MID", источник="MID",
                    день_X=dx.date(), точность=1, гос_против_гос=True,
                    окно_с=(dx - pd.Timedelta(days=args.window)).date(),
                    окно_по=(dx - pd.Timedelta(days=1)).date(),
                    метка=1, дней_в_окне=args.window, фон_загрязнён="",
                    примечание=f"уровень враждебности {int(sp.hostlev_n)}",
                    покрытие_GDELT=покрытие(dx)))
    C = pd.DataFrame(строки)
    if not len(C):
        sys.exit("Ни одного кейса не получилось — ослабьте --min-hostlev или --min-year")
    C = C.drop_duplicates(subset=["пара", "день_X"]).sort_values("день_X", ascending=False)

    # --- контрольные окна ---------------------------------------------------
    if args.controls > 0:
        C["день_X"] = pd.to_datetime(C.день_X)
        все = C[["код_A", "код_B", "день_X", "название"]].copy()
        сегодня = pd.Timestamp.today().normalize()
        доп = []
        for _, r in C.iterrows():
            добавлено = 0
            for лет in (1, 2, 3, 4, 5):
                if добавлено >= args.controls:
                    break
                dx = r.день_X - pd.Timedelta(days=365 * лет)
                wf = dx - pd.Timedelta(days=args.window)
                if wf < GDELT_FROM or dx > сегодня:
                    continue
                страны = {r.код_A, r.код_B}
                # окно должно быть чистым: ни одного эпизода с участием любой
                # из двух стран ни в самом окне, ни в буфере по обе стороны
                стык = все[(все.код_A.isin(страны) | все.код_B.isin(страны))
                           & (все.день_X >= wf - pd.Timedelta(days=args.window))
                           & (все.день_X <= dx + pd.Timedelta(days=args.window))]
                if len(стык):
                    continue
                c = r.copy()
                c["день_X"] = dx.date()
                c["окно_с"] = wf.date(); c["окно_по"] = (dx - pd.Timedelta(days=1)).date()
                c["метка"] = 0
                c["название"] = f"контроль к «{r.название}» (−{лет} г.)"
                c["покрытие_GDELT"] = покрытие(dx)
                доп.append(c)
                добавлено += 1
        if доп:
            C["день_X"] = C.день_X.dt.date
            C = pd.concat([C, pd.DataFrame(доп)], ignore_index=True)
        C = C.sort_values(["пара", "день_X"])

    войн = int((C.метка == 1).sum())
    print(f"\nПар-кейсов получено: {len(C)} (войн {войн}, контролей {len(C)-войн}) "
          f"| уникальных пар государств: {C.пара.nunique()}")
    print("\nПО ПОКРЫТИЮ GDELT:")
    for cov, grp in C.groupby("покрытие_GDELT"):
        print(f"  {cov:<22} {len(grp):>4} кейсов, пар {grp.пара.nunique():>3}")
    print("\nСАМЫЕ ЧАСТЫЕ ПАРЫ (войны):")
    print(C[C.метка == 1].пара.value_counts().head(12).to_string())
    if без_кода:
        print(f"\nКоды COW без соответствия в GDELT ({len(без_кода)}): "
              f"{sorted(без_кода)}")
        print("Эти участники пропущены. Допишите их в COW_TO_GDELT, если они важны.")
    C.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"\nФайл: {args.out}")
    print("Дальше: отобрать нужное покрытие и подать в collect_cases.py через --cases")


if __name__ == "__main__":
    main()
