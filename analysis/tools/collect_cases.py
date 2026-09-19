#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сбор обучающих данных GDELT по перечню межгосударственных эпизодов 2013-2026.

Что делает одной командой:
  1. берёт встроенный перечень эпизодов (39 штук, из аналитического документа);
  2. раскладывает их на кейсы «пара государств + день X»;
  3. отсеивает то, что для обучения не годится (нет точной даты, не государство
     против государства, окно выходит за пределы дневных файлов GDELT);
  4. при желании добавляет контрольные окна — те же пары в спокойное время,
     это отрицательные примеры, без них модель обучаться не может;
  5. качает суточные выгрузки GDELT, оставляет строки нужной пары в обе стороны
     и складывает по одному CSV на кейс в указанную папку.

Нужен только pandas:   pip install pandas

    python collect_cases.py --list                     показать перечень и выйти
    python collect_cases.py --out cases --dry-run      план сбора, без скачивания
    python collect_cases.py --out cases                собрать
    python collect_cases.py --out cases --controls 1   вместе с контрольными окнами
    python collect_cases.py --export cases.csv         выгрузить перечень в CSV
    python collect_cases.py --cases cases.csv --out cases   собрать по своему CSV

Файл самодостаточный: кроме него ничего класть рядом не нужно.
"""
from __future__ import annotations
import argparse, glob, io, os, sys, time, zipfile
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import pandas as pd

BASE_URL    = "http://data.gdeltproject.org/events/{date}.export.CSV.zip"
DAILY_FROM  = pd.Timestamp("2013-04-01")   # раньше GDELT отдаёт только месячные/годовые файлы
WINDOW_DAYS = 60                           # сколько дней до дня X собирать
MIN_DAYS    = 35                           # меньше — окна не хватит даже на один расчёт признаков
MB_PER_DAY  = 25                           # грубая оценка размера суточного архива
CHUNK       = 100_000
A1, A2 = "Actor1CountryCode", "Actor2CountryCode"

# Колонки GDELT 1.0 Event Export в порядке следования (в суточных файлах заголовка нет)
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

# --------------------------------------------------------------------------- #
#  Перечень эпизодов (из аналитического документа «Межгосударственные
#  вооружённые конфликты 2013-2026»). Правится руками: добавить строку —
#  значит добавить кейс.
#
#    id     номер позиции в документе
#    name   название
#    A, B   стороны как коды стран GDELT; кейсом становится каждая пара A x B
#    day    дата начала эпизода
#    prec   1 = известен день, 2 = только месяц, 3 = только год или период
#    grp    A/B/C/D — категория интенсивности из документа
#    state  True = обе стороны признанные государства с регулярной армией
#    note   оговорка из документа
# --------------------------------------------------------------------------- #
EPISODES = [
    # --- A. Полномасштабные войны и вторжения ---------------------------------
    dict(id=1,  name="Аннексия Крыма",                      A=["RUS"], B=["UKR"], day="2014-02-20", prec=1, grp="A", state=True),
    dict(id=2,  name="Война в Донбассе",                    A=["RUS"], B=["UKR"], day="2014-04-01", prec=2, grp="A", state=True,
         note="участие кадровых российских частей, но формально через ДНР/ЛНР"),
    dict(id=3,  name="Полномасштабное вторжение в Украину", A=["RUS"], B=["UKR"], day="2022-02-24", prec=1, grp="A", state=True),
    dict(id=4,  name="Вторая карабахская война",            A=["AZE"], B=["ARM"], day="2020-09-27", prec=1, grp="A", state=True),
    dict(id=5,  name="ДР Конго — Руанда",                   A=["RWA"], B=["COD"], day="2022-01-01", prec=3, grp="A", state=True,
         note="Руанда действует через движение М23; точная дата начала не определена"),
    dict(id=6,  name="Индия — Пакистан, операция «Синдур»", A=["IND"], B=["PAK"], day="2025-05-07", prec=1, grp="A", state=True),
    dict(id=7,  name="Двенадцатидневная война",             A=["ISR","USA"], B=["IRN"], day="2025-06-13", prec=1, grp="A", state=True),
    dict(id=8,  name="Афгано-пакистанская война",           A=["PAK"], B=["AFG"], day="2026-02-21", prec=1, grp="A", state=True),
    dict(id=9,  name="Ирано-израильская война 2026",        A=["ISR","USA"], B=["IRN"], day="2026-02-28", prec=1, grp="A", state=True),
    # --- B. Пограничные столкновения регулярных армий -------------------------
    dict(id=10, name="Депсанг",                             A=["IND"], B=["CHN"], day="2013-04-15", prec=1, grp="B", state=True),
    dict(id=11, name="Чумар",                               A=["IND"], B=["CHN"], day="2014-09-01", prec=2, grp="B", state=True),
    dict(id=12, name="Доклам",                              A=["IND","BTN"], B=["CHN"], day="2017-06-16", prec=1, grp="B", state=True),
    dict(id=13, name="Долина Галван",                       A=["IND"], B=["CHN"], day="2020-06-15", prec=1, grp="B", state=True),
    dict(id=14, name="Янцзы (Таванг)",                      A=["IND"], B=["CHN"], day="2022-12-09", prec=1, grp="B", state=True),
    dict(id=15, name="Четырёхдневная война",                A=["AZE"], B=["ARM"], day="2016-04-01", prec=1, grp="B", state=True),
    dict(id=16, name="Бои в Товузском районе",              A=["AZE"], B=["ARM"], day="2020-07-12", prec=1, grp="B", state=True),
    dict(id=17, name="Приграничный кризис 2021",            A=["AZE"], B=["ARM"], day="2021-05-12", prec=1, grp="B", state=True),
    dict(id=18, name="Столкновения сентября 2022",          A=["AZE"], B=["ARM"], day="2022-09-12", prec=1, grp="B", state=True),
    dict(id=19, name="Операция в Нагорном Карабахе",        A=["AZE"], B=["ARM"], day="2023-09-19", prec=1, grp="B", state=False,
         note="противник — непризнанная Республика Арцах, а не армия Армении"),
    dict(id=20, name="Киргизия — Таджикистан, апрель 2021", A=["KGZ"], B=["TJK"], day="2021-04-28", prec=1, grp="B", state=True),
    dict(id=21, name="Киргизия — Таджикистан, сент. 2022",  A=["KGZ"], B=["TJK"], day="2022-09-14", prec=1, grp="B", state=True),
    dict(id=22, name="Аль-Фашага",                          A=["SDN"], B=["ETH"], day="2020-12-15", prec=1, grp="B", state=True),
    dict(id=23, name="Стычка у храма Та Муэн Тхом",         A=["THA"], B=["KHM"], day="2025-05-28", prec=1, grp="B", state=True),
    dict(id=24, name="Пограничная война июля 2025",         A=["THA"], B=["KHM"], day="2025-07-24", prec=1, grp="B", state=True),
    dict(id=25, name="Возобновление боёв, декабрь 2025",    A=["THA"], B=["KHM"], day="2025-12-01", prec=2, grp="B", state=True),
    # --- C. Прямые удары и ракетно-авиационные обмены -------------------------
    dict(id=26, name="Сбитый Су-24",                        A=["TUR"], B=["RUS"], day="2015-11-24", prec=1, grp="C", state=True),
    dict(id=27, name="Удар по авиабазе Шайрат",             A=["USA"], B=["SYR"], day="2017-04-07", prec=1, grp="C", state=True),
    dict(id=28, name="Удары по объектам ОМУ в Сирии",       A=["USA","GBR","FRA"], B=["SYR"], day="2018-04-14", prec=1, grp="C", state=True),
    dict(id=29, name="Платформа HYSY-981",                  A=["CHN"], B=["VNM"], day="2014-05-01", prec=2, grp="C", state=True,
         note="столкновения судов береговой охраны, без боевых потерь"),
    dict(id=30, name="Сулеймани и удар по Айн-эль-Асад",    A=["USA"], B=["IRN"], day="2020-01-03", prec=1, grp="C", state=True),
    dict(id=31, name="Операция «Весенний щит»",             A=["TUR"], B=["SYR"], day="2020-02-01", prec=2, grp="C", state=True),
    dict(id=32, name="Иран — Пакистан, взаимные удары",     A=["IRN"], B=["PAK"], day="2024-01-16", prec=1, grp="C", state=True),
    dict(id=33, name="Консульство в Дамаске и ответ Ирана", A=["ISR"], B=["IRN"], day="2024-04-01", prec=1, grp="C", state=True),
    dict(id=34, name="Удар по Исфахану",                    A=["ISR"], B=["IRN"], day="2024-04-19", prec=1, grp="C", state=True),
    dict(id=35, name="«Истинное обещание — 2»",             A=["IRN"], B=["ISR"], day="2024-10-01", prec=1, grp="C", state=True),
    dict(id=36, name="Удары по военным объектам Ирана",     A=["ISR"], B=["IRN"], day="2024-10-26", prec=1, grp="C", state=True),
    dict(id=37, name="Вторжение в Южный Ливан",             A=["ISR"], B=["LBN"], day="2024-09-17", prec=1, grp="C", state=False,
         note="противник — «Хезболла», а не регулярная армия Ливана"),
    # --- D. Морские и приграничные инциденты низкой интенсивности -------------
    dict(id=38, name="Китай — Филиппины на море",           A=["CHN"], B=["PHL"], day="2023-01-01", prec=3, grp="D", state=False,
         note="десятки эпизодов 2023-2025, единой даты нет; суда береговой охраны"),
    dict(id=39, name="Западная Сахара",                     A=["MAR"], B=["ESH"], day="2020-11-13", prec=1, grp="D", state=False,
         note="противник — Фронт ПОЛИСАРИО/САДР; код ESH в GDELT встречается редко"),
    # --- вне документа: кейсы из нашей прежней работы, данные по ним уже собраны --
    dict(id=101, name="Саудовская коалиция в Йемене",       A=["SAU"], B=["YEM"], day="2015-03-26", prec=1, grp="A", state=False, src="наши",
         note="интервенция в гражданскую войну — документ такие эпизоды исключает"),
    dict(id=102, name="Балакот и ответ Пакистана",          A=["IND"], B=["PAK"], day="2019-02-26", prec=1, grp="C", state=True, src="наши",
         note="в документе отсутствует, хотя подходит под его же критерий"),
    dict(id=103, name="Операция США против Мадуро",         A=["USA"], B=["VEN"], day="2026-01-03", prec=1, grp="A", state=True, src="наши"),
]

def expand(eps: list[dict], window: int) -> pd.DataFrame:
    """Эпизод -> по одному кейсу на каждую пару «страна из A + страна из B»."""
    rows = []
    for e in eps:
        day_x = pd.Timestamp(e["day"])
        for a in e["A"]:
            for b in e["B"]:
                if a == b:
                    continue
                rows.append(dict(
                    пара="-".join(sorted([a, b])), код_A=a, код_B=b,
                    эпизод=e["id"], название=e["name"], группа=e["grp"],
                    источник=e.get("src", "документ"),
                    день_X=day_x, точность=e["prec"],
                    гос_против_гос=bool(e["state"]),
                    окно_с=day_x - pd.Timedelta(days=window),
                    окно_по=day_x - pd.Timedelta(days=1),
                    метка=1, примечание=e.get("note", ""),
                ))
    return pd.DataFrame(rows).sort_values(["пара", "день_X"]).reset_index(drop=True)


def days_available(row, window: int) -> int:
    """Сколько дней окна реально покрыто суточными файлами GDELT."""
    start = max(row.окно_с, DAILY_FROM)
    return max(0, (row.окно_по - start).days + 1)


def thin_out(C: pd.DataFrame, min_gap: int) -> tuple[pd.DataFrame, list[str]]:
    """Из серии близких эпизодов одной пары оставляет первый.

    Зачем: у второго удара по той же паре через две недели фон окна состоит
    из первого удара. Такой кейс измеряет не «мир перед войной», а «война
    перед войной» — в обучении это мусор.
    """
    keep, dropped = [], []
    for pair, grp in C.groupby("пара", sort=False):
        last = None
        for _, r in grp.sort_values("день_X").iterrows():
            if last is not None and (r.день_X - last).days < min_gap:
                dropped.append(f"{r.пара} {r.день_X.date()} «{r.название}» "
                               f"— в пределах {min_gap} дней после предыдущего эпизода")
                continue
            keep.append(r.name)
            last = r.день_X
    return C.loc[sorted(keep)].copy(), dropped


def mark_contamination(C: pd.DataFrame, all_eps: pd.DataFrame) -> pd.DataFrame:
    """Отмечает кейсы, в чьё окно попадает другой эпизод той же пары."""
    notes = []
    for _, r in C.iterrows():
        hits = all_eps[(all_eps.пара == r.пара) & (all_eps.эпизод != r.эпизод)
                       & (all_eps.день_X >= r.окно_с) & (all_eps.день_X <= r.окно_по)]
        notes.append("; ".join(f"{h.день_X.date()} {h.название}" for _, h in hits.iterrows()))
    C = C.copy()
    C["фон_загрязнён"] = notes
    return C


def add_controls(C: pd.DataFrame, all_eps: pd.DataFrame, n: int, window: int,
                 today: pd.Timestamp) -> pd.DataFrame:
    """Контрольные окна: та же пара за год (два, три) до дня X, метка 0.

    Берутся только такие сдвиги, в чьё окно не попадает ни один известный
    эпизод этой пары — иначе «спокойный период» окажется не спокойным.
    """
    if n <= 0:
        return C
    out = [C]
    for _, r in C[C.метка == 1].iterrows():
        added = 0
        for years in (1, 2, 3, 4, 5):
            if added >= n:
                break
            day_x = r.день_X - pd.Timedelta(days=365 * years)
            w_from, w_to = day_x - pd.Timedelta(days=window), day_x - pd.Timedelta(days=1)
            if w_from < DAILY_FROM or day_x > today:
                continue
            # буфер в одно окно с обеих сторон: слева — чтобы фон не состоял из
            # прошлой войны, справа — чтобы «спокойное» окно само не оказалось
            # предвоенным для эпизода, случившегося вскоре после него
            clash = all_eps[(all_eps.пара == r.пара)
                            & (all_eps.день_X >= w_from - pd.Timedelta(days=window))
                            & (all_eps.день_X <= day_x + pd.Timedelta(days=window))]
            if len(clash):
                continue
            c = r.copy()
            c["день_X"], c["окно_с"], c["окно_по"] = day_x, w_from, w_to
            c["метка"] = 0
            c["название"] = f"контроль к «{r.название}» (−{years} г.)"
            c["фон_загрязнён"] = ""
            out.append(pd.DataFrame([c]))
            added += 1
    return pd.concat(out, ignore_index=True).sort_values(["пара", "день_X"]).reset_index(drop=True)


def select(args) -> tuple[pd.DataFrame, list[str]]:
    """Собирает итоговый список кейсов и человеческий отчёт о том, что выкинуто."""
    eps = EPISODES
    all_eps = expand(eps, args.window)
    C, report = all_eps.copy(), []

    if args.only_document:
        C = C[C.источник == "документ"]
    C = C[C.группа.isin(list(args.categories))]
    n0 = len(all_eps)
    report.append(f"эпизодов в перечне: {len(eps)} -> пар-кейсов: {n0}")
    report.append(f"оставлены категории {args.categories}: {len(C)}")

    if not args.include_nonstate:
        drop = C[~C.гос_против_гос]
        for _, r in drop.iterrows():
            report.append(f"  — не государство против государства: {r.пара} {r.день_X.date()} "
                          f"«{r.название}» ({r.примечание})")
        C = C[C.гос_против_гос]

    maxprec = {"exact": 1, "month": 2, "all": 3}[args.precision]
    drop = C[C.точность > maxprec]
    for _, r in drop.iterrows():
        report.append(f"  — дата слишком неточная: {r.пара} {r.день_X.date()} «{r.название}»")
    C = C[C.точность <= maxprec]

    C["дней_в_окне"] = [days_available(r, args.window) for _, r in C.iterrows()]
    drop = C[C.дней_в_окне < MIN_DAYS]
    for _, r in drop.iterrows():
        report.append(f"  — окно вне дневных файлов GDELT (есть {r.дней_в_окне} дн.): "
                      f"{r.пара} {r.день_X.date()} «{r.название}»")
    C = C[C.дней_в_окне >= MIN_DAYS]

    C, dropped = thin_out(C, args.min_gap)
    for d in dropped:
        report.append("  — " + d)

    C = mark_contamination(C, all_eps)
    C = add_controls(C, all_eps, args.controls, args.window, pd.Timestamp.today().normalize())
    C["дней_в_окне"] = [days_available(r, args.window) for _, r in C.iterrows()]

    if args.only:
        C = C[C.пара == args.only]
    if args.limit:
        C = C.head(args.limit)
    return C.reset_index(drop=True), report


# --------------------------------------------------------------------------- #
#  Скачивание
# --------------------------------------------------------------------------- #
def fetch_day(date: str, retries: int = 3) -> bytes | None:
    """Качает суточный архив. None — такого файла на сервере нет."""
    url = BASE_URL.format(date=date)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=180) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries:
                raise
        except Exception:
            if attempt == retries:
                raise
        time.sleep(2.0 * attempt)
    return None


def split_by_pairs(raw: bytes, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], pd.DataFrame]:
    """Разбирает архив кусками и раскладывает строки по нужным парам за один проход."""
    out = {p: [] for p in pairs}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        with zf.open(zf.namelist()[0]) as fh:
            reader = pd.read_csv(fh, sep="\t", header=None, names=GDELT_COLUMNS, dtype=str,
                                 chunksize=CHUNK, on_bad_lines="skip",
                                 encoding="utf-8", encoding_errors="replace")
            for chunk in reader:
                a1, a2 = chunk[A1], chunk[A2]
                for ca, cb in pairs:
                    mask = ((a1 == ca) & (a2 == cb)) | ((a1 == cb) & (a2 == ca))
                    if mask.any():
                        out[(ca, cb)].append(chunk[mask])
    return {p: (pd.concat(v, ignore_index=True) if v else pd.DataFrame(columns=GDELT_COLUMNS))
            for p, v in out.items()}


def adopt(C: pd.DataFrame, folder: str, dry: bool) -> dict:
    """Зачитывает уже собранные CSV (те же 58 колонок GDELT) в кейсы.

    Для каждого файла определяет пару и диапазон дат по самим данным и отдаёт
    кейсу ту часть его окна, которую файл покрывает. Предполагается, что внутри
    своего диапазона файл сплошной — так его и собирали.
    """
    covered: dict[int, set] = {i: set() for i in C.index}
    if not folder:
        return covered
    files = sorted(glob.glob(os.path.join(folder, "*.csv")))
    print(f"\nПереиспользование: просмотрено файлов {len(files)} в {folder}")
    for f in files:
        try:
            d = pd.read_csv(f, dtype=str, low_memory=False)
        except Exception:
            continue
        if list(d.columns) != GDELT_COLUMNS:
            continue
        day = d.DATEADDED.astype(str).str[:8]
        pair = pd.Series(["-".join(sorted([str(a), str(b)])) for a, b in zip(d[A1], d[A2])])
        for pr, idx in pair.groupby(pair).groups.items():
            part, pday = d.loc[idx], day.loc[idx]
            lo, hi = pday.min(), pday.max()
            for i, r in C[C.пара == pr].iterrows():
                w_from = max(r.окно_с, DAILY_FROM).strftime("%Y%m%d")
                w_to = r.окно_по.strftime("%Y%m%d")
                a, b = max(lo, w_from), min(hi, w_to)
                if a > b:
                    continue
                days = {x.strftime("%Y%m%d") for x in pd.date_range(a, b)} - covered[i]
                if not days:
                    continue
                sel = part[pday.isin(days)]
                print(f"  {os.path.basename(f)} -> {r.пара} {r.день_X.date()}: "
                      f"дней {len(days)}, строк {len(sel)}")
                covered[i] |= days
                if not dry:
                    if not os.path.exists(r.файл):
                        pd.DataFrame(columns=GDELT_COLUMNS).to_csv(r.файл, index=False)
                    if len(sel):
                        sel[GDELT_COLUMNS].to_csv(r.файл, mode="a", header=False, index=False)
                    with open(r.журнал, "a", encoding="utf-8") as jf:
                        jf.write("".join(x + "\n" for x in sorted(days)))
    total = sum(len(v) for v in covered.values())
    print(f"  итого зачтено дней: {total} (~{total * MB_PER_DAY / 1024:.1f} ГБ качать не нужно)")
    return covered


def plan_days(C: pd.DataFrame, out_dir: str, covered: dict | None = None) -> tuple[dict, dict]:
    """Какие дни кому нужны и что уже собрано (по журналу рядом с каждым CSV)."""
    need, already = {}, {}
    for i, r in C.iterrows():
        done = set(covered.get(i, set())) if covered else set()
        if os.path.exists(r.журнал):
            done |= {ln.strip() for ln in open(r.журнал, encoding="utf-8") if ln.strip()}
        already[i] = done
        for day in pd.date_range(max(r.окно_с, DAILY_FROM), r.окно_по):
            ds = day.strftime("%Y%m%d")
            if ds not in done:
                need.setdefault(ds, []).append(i)
    return need, already


def download(C: pd.DataFrame, need: dict, workers: int, pause: float):
    """Качает каждый день один раз и раздаёт строки всем кейсам, которым он нужен."""
    pair_of = {i: (C.loc[i, "код_A"], C.loc[i, "код_B"]) for i in C.index}
    stats = {i: 0 for i in C.index}
    missing, failed = [], []
    days = sorted(need)
    t0 = time.time()

    def worker(ds: str):
        try:
            return ds, fetch_day(ds), None
        except Exception as e:
            return ds, None, e

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for k, (ds, raw, err) in enumerate(pool.map(worker, days), start=1):
            if err is not None:
                failed.append(ds)
            elif raw is None:
                missing.append(ds)
                for i in need[ds]:                      # файла нет на сервере — больше не пробуем
                    open(C.loc[i, "журнал"], "a", encoding="utf-8").write(ds + "\n")
            else:
                idxs = need[ds]
                try:
                    parts = split_by_pairs(raw, sorted({pair_of[i] for i in idxs}))
                except Exception:
                    failed.append(ds)
                    parts = {}
                for i in idxs:
                    part = parts.get(pair_of[i])
                    if part is None:
                        continue
                    if len(part):
                        part.to_csv(C.loc[i, "файл"], mode="a", header=False, index=False)
                        stats[i] += len(part)
                    # день записывается в журнал, даже если строк по паре не нашлось,
                    # иначе при следующем запуске он качался бы заново
                    open(C.loc[i, "журнал"], "a", encoding="utf-8").write(ds + "\n")
            speed = k / max(time.time() - t0, 1e-9)
            line = (f"  {ds}  [{k}/{len(days)}, {k/len(days)*100:5.1f} %]  "
                    f"осталось ~{(len(days)-k)/speed/60:.0f} мин")
            if sys.stdout.isatty():
                print(line, end="\r", flush=True)
            elif k % 10 == 0 or k == len(days):
                print(line, flush=True)
            time.sleep(pause)
    if sys.stdout.isatty():
        print()
    return stats, missing, failed


# --------------------------------------------------------------------------- #
#  Командная строка
# --------------------------------------------------------------------------- #
CASE_COLS = ["пара", "код_A", "код_B", "эпизод", "название", "группа", "источник", "день_X",
             "точность", "гос_против_гос", "окно_с", "окно_по", "метка",
             "дней_в_окне", "фон_загрязнён", "примечание"]


def show(C: pd.DataFrame):
    v = C.copy()
    for c in ("день_X", "окно_с", "окно_по"):
        v[c] = v[c].dt.date
    v["тип"] = v.метка.map({1: "война", 0: "контроль"})
    v["фон"] = v.фон_загрязнён.apply(lambda s: "загрязнён" if s else "")
    print(v[["пара", "день_X", "тип", "окно_с", "окно_по", "дней_в_окне", "фон",
             "источник", "название"]]
          .to_string(index=False))


def main():
    ap = argparse.ArgumentParser(
        description="Перечень межгосударственных эпизодов -> готовые выборки GDELT",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--out", default="cases", help="папка для результатов")
    ap.add_argument("--cases", default=None, help="свой CSV со списком кейсов вместо встроенного")
    ap.add_argument("--export", default=None, help="выгрузить список кейсов в CSV и выйти")
    ap.add_argument("--list", action="store_true", help="показать список кейсов и выйти")
    ap.add_argument("--window", type=int, default=WINDOW_DAYS, help="сколько дней до дня X собирать")
    ap.add_argument("--categories", default="ABC", help="категории интенсивности из документа")
    ap.add_argument("--precision", choices=["exact", "month", "all"], default="exact",
                    help="какой точности даты брать: exact — только известный день")
    ap.add_argument("--only-document", action="store_true",
                    help="только эпизоды из документа, без наших собственных кейсов")
    ap.add_argument("--include-nonstate", action="store_true",
                    help="брать и эпизоды против непризнанных государств и группировок")
    ap.add_argument("--min-gap", type=int, default=60,
                    help="минимум дней между эпизодами одной пары; из серии остаётся первый")
    ap.add_argument("--controls", type=int, default=0,
                    help="сколько контрольных окон (метка 0) добавить на каждый кейс")
    ap.add_argument("--only", default=None, help="одна пара, например RUS-UKR")
    ap.add_argument("--limit", type=int, default=None, help="взять только N первых кейсов")
    ap.add_argument("--workers", type=int, default=4, help="параллельных загрузок")
    ap.add_argument("--pause", type=float, default=0.2, help="пауза между запросами, секунд")
    ap.add_argument("--reuse", default=None,
                    help="папка с уже собранными CSV в схеме GDELT — их дни будут зачтены")
    ap.add_argument("--dry-run", action="store_true", help="показать план и выйти")
    args = ap.parse_args()

    # --- 1. список кейсов ---------------------------------------------------
    if args.cases:
        C = pd.read_csv(args.cases)
        for c in ("день_X", "окно_с", "окно_по"):
            C[c] = pd.to_datetime(C[c])
        C["фон_загрязнён"] = C.get("фон_загрязнён", "").fillna("")
        report = [f"список кейсов взят из файла {args.cases}: {len(C)} строк"]
    else:
        C, report = select(args)
    if not len(C):
        sys.exit("Ни одного кейса не осталось — ослабьте фильтры "
                 "(--precision month, --categories ABCD, --include-nonstate)")

    if args.list or args.export:
        print("\n".join(report) + "\n")
        show(C)
        if args.export:
            out = C.copy()
            for c in ("день_X", "окно_с", "окно_по"):
                out[c] = out[c].dt.date
            out[CASE_COLS].to_csv(args.export, index=False, encoding="utf-8-sig")
            print(f"\nСписок сохранён: {args.export} — можно править руками и подать через --cases")
        return

    os.makedirs(args.out, exist_ok=True)
    C["файл"] = [os.path.join(args.out, f"{r.пара}_{r.день_X.date()}"
                              f"{'' if r.метка == 1 else '_control'}.csv") for _, r in C.iterrows()]
    C["журнал"] = C.файл + ".days"
    exp = C.copy()
    for c in ("день_X", "окно_с", "окно_по"):
        exp[c] = exp[c].dt.date
    exp[CASE_COLS].to_csv(os.path.join(args.out, "case_list.csv"), index=False, encoding="utf-8-sig")

    # --- 2. план ------------------------------------------------------------
    print("\n".join(report))
    covered = adopt(C, args.reuse, args.dry_run)
    need, already = plan_days(C, args.out, covered)
    wars = int((C.метка == 1).sum())
    print(f"\nКейсов к сбору: {len(C)} (войн {wars}, контролей {len(C) - wars}) | "
          f"уникальных дней к скачиванию: {len(need)}")
    dup = sum(len(v) for v in need.values()) - len(need)
    print(f"Общие даты между кейсами: {dup} повторных загрузок не понадобится")
    print(f"Трафик примерно {len(need) * MB_PER_DAY / 1024:.1f} ГБ "
          f"(на диск ляжет только выборка, это десятки мегабайт)\n")
    show(C)
    if args.controls == 0 and not args.cases:
        print("\nПодсказка: без контрольных окон обучать модель не на чем — есть только "
              "примеры «перед войной». Добавьте --controls 1 (это примерно удвоит трафик).")
    if args.dry_run:
        print("\n--dry-run: ничего не скачивалось")
        return
    if not need:
        print("\nВсё уже собрано.")
        return

    # --- 3. скачивание ------------------------------------------------------
    for _, r in C.iterrows():
        if not os.path.exists(r.файл):
            pd.DataFrame(columns=GDELT_COLUMNS).to_csv(r.файл, index=False)
    stats, missing, failed = download(C, need, args.workers, args.pause)

    # --- 4. манифест --------------------------------------------------------
    man = []
    for i, r in C.iterrows():
        rows = 0
        if os.path.exists(r.файл):
            try:
                rows = sum(1 for _ in open(r.файл, encoding="utf-8")) - 1
            except Exception:
                rows = -1
        man.append(dict(пара=r.пара, день_X=r.день_X.date(), метка=r.метка,
                        название=r.название, окно_с=r.окно_с.date(), окно_по=r.окно_по.date(),
                        файл=os.path.basename(r.файл), строк_всего=rows,
                        строк_добавлено=stats.get(i, 0),
                        дней_собрано=(sum(1 for _ in open(r.журнал, encoding="utf-8"))
                                      if os.path.exists(r.журнал) else 0),
                        фон_загрязнён=r.фон_загрязнён))
    M = pd.DataFrame(man).sort_values(["метка", "день_X"], ascending=[False, False])
    M.to_csv(os.path.join(args.out, "manifest.csv"), index=False, encoding="utf-8-sig")

    print("\nГОТОВО")
    print(M[["пара", "день_X", "метка", "строк_всего", "файл"]].to_string(index=False))
    print(f"\nПапка: {args.out} | список кейсов: case_list.csv | статусы: manifest.csv")
    if missing:
        print(f"Дней без файла на сервере: {len(missing)} ({', '.join(missing[:5])}…)")
    if failed:
        print(f"Дней с ошибкой: {len(failed)} — перезапустите скрипт, они докачаются")
    thin = M[M.строк_всего < 200]
    if len(thin):
        print("\nМало данных (меньше 200 строк) — такие кейсы для анализа не годятся:")
        print(thin[["пара", "день_X", "строк_всего"]].to_string(index=False))


if __name__ == "__main__":
    main()
