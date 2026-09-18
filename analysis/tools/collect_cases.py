#!/usr/bin/env python3
"""Одна команда: из файла UCDP -> скачанные и разложенные по папкам данные GDELT.

Что делает:
  1. читает таблицу UCDP (диадную Dyadic_vXX.xlsx или конфликтную UcdpPrioConflict_vXX.xlsx);
  2. отбирает эпизоды нужного типа и строит список кейсов «пара государств + день X»;
  3. качает суточные выгрузки GDELT за окно [день X − N; день X − 1];
  4. оставляет только строки нужной пары (в обе стороны: A→B и B→A);
  5. складывает по файлу на кейс в указанную папку и пишет manifest.csv со статусами.

Каждый суточный файл скачивается один раз, даже если он нужен нескольким кейсам.
Скрипт можно прерывать: при перезапуске уже собранные дни пропускаются.

    python collect_cases.py Dyadic_v26_1.xlsx --out ../data/cases
    python collect_cases.py Dyadic_v26_1.xlsx --out ../data/cases --only RUS-UKR
    python collect_cases.py Dyadic_v26_1.xlsx --dry-run
"""
from __future__ import annotations
import argparse, io, os, sys, time, zipfile
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dyad_features import GDELT_COLUMNS
from build_case_list import build_cases

BASE_URL = "http://data.gdeltproject.org/events/{date}.export.CSV.zip"
DAILY_FROM = pd.Timestamp("2013-04-01")
CHUNK = 100_000
A1, A2 = "Actor1CountryCode", "Actor2CountryCode"


def fetch_day(date: str, retries: int = 3) -> bytes | None:
    """Качает суточный архив. None — файла за этот день нет."""
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


def main():
    ap = argparse.ArgumentParser(description="UCDP -> готовые выборки GDELT по кейсам")
    ap.add_argument("ucdp", help="Dyadic_vXX.xlsx или UcdpPrioConflict_vXX.xlsx")
    ap.add_argument("--out", default="cases", help="папка для результатов")
    ap.add_argument("--types", default="2", help="типы конфликта (2 = межгосударственный)")
    ap.add_argument("--min-year", type=int, default=1979)
    ap.add_argument("--window", type=int, default=60, help="сколько дней до дня X собирать")
    ap.add_argument("--all-precisions", action="store_true",
                    help="брать и эпизоды с неточной датой начала (по умолчанию только точные)")
    ap.add_argument("--only", default=None, help="одна пара, например RUS-UKR")
    ap.add_argument("--limit", type=int, default=None, help="взять только N первых кейсов")
    ap.add_argument("--workers", type=int, default=4, help="параллельных загрузок")
    ap.add_argument("--pause", type=float, default=0.2, help="пауза между запросами, секунд")
    ap.add_argument("--dry-run", action="store_true", help="показать план и выйти")
    args = ap.parse_args()

    # --- 1. список кейсов ----------------------------------------------------
    df = pd.read_excel(args.ucdp)
    C, unmapped = build_cases(df, [int(t) for t in args.types.split(",")],
                              args.min_year, not args.all_precisions, args.window)
    if not len(C):
        sys.exit("Ни одного кейса не получилось — ослабьте фильтры")
    ready = C[C.покрытие_GDELT == "дневные файлы"].copy()
    if args.only:
        ready = ready[ready.пара == args.only]
    if args.limit:
        ready = ready.head(args.limit)
    if not len(ready):
        sys.exit("Нет кейсов с дневным покрытием GDELT (файлы есть только с 2013-04-01)")

    os.makedirs(args.out, exist_ok=True)
    C.to_csv(os.path.join(args.out, "case_list.csv"), index=False, encoding="utf-8-sig")

    # --- 2. какие дни нужны и кому ------------------------------------------
    ready["файл"] = [os.path.join(args.out, f"{r.пара}_{r.день_X}.csv") for _, r in ready.iterrows()]
    ready["журнал"] = [f + ".days" for f in ready.файл]
    need: dict[str, list[int]] = {}
    already: dict[int, set] = {}
    for i, r in ready.iterrows():
        done = set()
        # журнал обработанных дней: нужен потому, что день без единой строки по паре
        # ничего не пишет в CSV, и без журнала качался бы заново при каждом запуске
        if os.path.exists(r.журнал):
            done = {ln.strip() for ln in open(r.журнал, encoding="utf-8") if ln.strip()}
        elif os.path.exists(r.файл):
            try:
                prev = pd.read_csv(r.файл, dtype=str, usecols=["DATEADDED"])
                done = set(prev.DATEADDED.astype(str).str[:8])
            except Exception:
                done = set()
        already[i] = done
        for day in pd.date_range(r.окно_с, r.окно_по):
            if day < DAILY_FROM:
                continue
            ds = day.strftime("%Y%m%d")
            if ds in done:
                continue
            need.setdefault(ds, []).append(i)

    print(f"Кейсов к сбору: {len(ready)} | уникальных дней к скачиванию: {len(need)}")
    print(f"Экономия за счёт общих дат: "
          f"{sum(len(v) for v in need.values()) - len(need)} повторных загрузок не понадобится")
    print(f"Трафик примерно {len(need) * 20 / 1024:.1f} ГБ, на диск ляжет только выборка\n")
    for _, r in ready.iterrows():
        print(f"  {r.пара:<9} день X {r.день_X} | окно {r.окно_с} — {r.окно_по}"
              f" | уже собрано дней {len(already[_])}")
    if unmapped:
        print("\nБез кода GDELT (пропущены):", ", ".join(f"{c}—{n}" for c, n in sorted(unmapped)))
    if args.dry_run:
        print("\n--dry-run: ничего не скачивалось")
        return
    if not need:
        print("\nВсё уже собрано.")
        return

    # --- 3. подготовка файлов ------------------------------------------------
    for i, r in ready.iterrows():
        if not os.path.exists(r.файл):
            pd.DataFrame(columns=GDELT_COLUMNS).to_csv(r.файл, index=False)

    pair_of = {i: (ready.loc[i, "код_A"], ready.loc[i, "код_B"]) for i in ready.index}
    stats = {i: dict(дней=0, строк=0) for i in ready.index}
    missing_days, failed_days = [], []

    def worker(ds: str):
        try:
            return ds, fetch_day(ds), None
        except Exception as e:
            return ds, None, e

    # --- 4. скачивание и раскладка ------------------------------------------
    days = sorted(need)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for k, (ds, raw, err) in enumerate(pool.map(worker, days), start=1):
            if err is not None:
                failed_days.append(ds)
            elif raw is None:
                missing_days.append(ds)
                for i in need[ds]:                   # файла нет на сервере — больше не пробуем
                    with open(ready.loc[i, "журнал"], "a", encoding="utf-8") as jf:
                        jf.write(ds + "\n")
            else:
                idxs = need[ds]
                pairs = sorted({pair_of[i] for i in idxs})
                try:
                    parts = split_by_pairs(raw, pairs)
                except Exception as e:
                    failed_days.append(ds)
                    parts = {}
                for i in idxs:
                    part = parts.get(pair_of[i])
                    if part is None:
                        continue
                    if len(part):
                        part.to_csv(ready.loc[i, "файл"], mode="a", header=False, index=False)
                        stats[i]["строк"] += len(part)
                    with open(ready.loc[i, "журнал"], "a", encoding="utf-8") as jf:
                        jf.write(ds + "\n")          # день обработан, даже если строк не было
                    stats[i]["дней"] += 1
            speed = k / max(time.time() - t0, 1e-9)
            eta = (len(days) - k) / speed if speed else 0
            line = (f"  {ds}  [{k}/{len(days)}, {k/len(days)*100:5.1f} %]  "
                    f"осталось ~{eta/60:.0f} мин")
            if sys.stdout.isatty():
                print(line, end="\r", flush=True)
            elif k % 10 == 0 or k == len(days):
                print(line, flush=True)          # в лог пишем каждый десятый день
            time.sleep(args.pause)
    if sys.stdout.isatty():
        print()

    # --- 5. манифест ---------------------------------------------------------
    man = []
    for i, r in ready.iterrows():
        rows = 0
        if os.path.exists(r.файл):
            try:
                rows = sum(1 for _ in open(r.файл, encoding="utf-8")) - 1
            except Exception:
                rows = -1
        man.append(dict(пара=r.пара, день_X=r.день_X, окно_с=r.окно_с, окно_по=r.окно_по,
                        файл=os.path.basename(r.файл), строк_всего=rows,
                        дней_скачано=stats[i]["дней"] + len(already[i]),
                        страна_A=r.страна_A, страна_B=r.страна_B,
                        интенсивность=r.интенсивность))
    M = pd.DataFrame(man).sort_values("день_X", ascending=False)
    M.to_csv(os.path.join(args.out, "manifest.csv"), index=False, encoding="utf-8-sig")

    print("\nГОТОВО")
    print(M.to_string(index=False))
    print(f"\nПапка: {args.out} | список кейсов: case_list.csv | статусы: manifest.csv")
    if missing_days:
        print(f"Дней без файла на сервере: {len(missing_days)} ({', '.join(missing_days[:5])}…)")
    if failed_days:
        print(f"Дней с ошибкой: {len(failed_days)} — перезапустите скрипт, они докачаются")
    thin = M[M.строк_всего < 200]
    if len(thin):
        print("\nМало данных (меньше 200 строк) — такие кейсы для анализа не годятся:")
        print(thin[["пара", "день_X", "строк_всего"]].to_string(index=False))


if __name__ == "__main__":
    main()
