#!/usr/bin/env python3
"""Скачивает дневные выгрузки GDELT и собирает данные по каждому кейсу из case_list.csv.

Для каждого кейса берёт дни окна [окно_с; окно_по], качает суточный файл GDELT,
оставляет только строки нужной пары государств и дописывает их в CSV кейса.
Сырые суточные файлы не сохраняются — на диске остаются только выборки.

    python fetch_gdelt.py ../data/case_list.csv --out ../data/cases
    python fetch_gdelt.py ../data/case_list.csv --only RUS-UKR --out ../data/cases
    python fetch_gdelt.py ../data/case_list.csv --dry-run

Скрипт можно прерывать и запускать заново: уже скачанные дни пропускаются.
Работает только для дней с 2013-04-01 (раньше GDELT отдаёт месячные и годовые файлы).
"""
from __future__ import annotations
import argparse, io, os, sys, time, zipfile
import urllib.request, urllib.error
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dyad_features import GDELT_COLUMNS

BASE_URL = "http://data.gdeltproject.org/events/{date}.export.CSV.zip"
DAILY_FROM = pd.Timestamp("2013-04-01")
A1, A2 = "Actor1CountryCode", "Actor2CountryCode"


def parse_day(raw: bytes, code_a: str, code_b: str) -> pd.DataFrame:
    """Распаковывает суточный архив и оставляет строки нужной пары."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            df = pd.read_csv(fh, sep="\t", header=None, names=GDELT_COLUMNS,
                             dtype=str, on_bad_lines="skip",
                             encoding="utf-8", encoding_errors="replace")
    mask = (((df[A1] == code_a) & (df[A2] == code_b)) |
            ((df[A1] == code_b) & (df[A2] == code_a)))
    return df[mask]


def fetch_day(date: str, retries: int = 3, pause: float = 2.0) -> bytes | None:
    url = BASE_URL.format(date=date)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                      # файла за этот день нет
            if attempt == retries:
                raise
        except Exception:
            if attempt == retries:
                raise
        time.sleep(pause * attempt)
    return None


def main():
    ap = argparse.ArgumentParser(description="Сбор данных GDELT по списку кейсов")
    ap.add_argument("case_list", help="CSV, собранный build_case_list.py")
    ap.add_argument("--out", default="cases", help="папка для выборок по кейсам")
    ap.add_argument("--only", default=None, help="обработать только одну пару, например RUS-UKR")
    ap.add_argument("--dry-run", action="store_true", help="показать план, ничего не качать")
    ap.add_argument("--pause", type=float, default=0.5, help="пауза между файлами, секунд")
    args = ap.parse_args()

    C = pd.read_csv(args.case_list)
    C = C[C.покрытие_GDELT == "дневные файлы"]
    if args.only:
        C = C[C.пара == args.only]
    if C.empty:
        sys.exit("Нет кейсов с дневным покрытием — проверьте фильтры")
    os.makedirs(args.out, exist_ok=True)

    total_days = 0
    for _, case in C.iterrows():
        days = pd.date_range(case.окно_с, case.окно_по)
        days = days[days >= DAILY_FROM]
        total_days += len(days)
    print(f"Кейсов: {len(C)} | суточных файлов к скачиванию: {total_days}")
    print(f"Ориентировочный объём трафика: {total_days * 20 / 1024:.1f} ГБ "
          f"(на диск попадут только выборки, это единицы мегабайт)\n")
    if args.dry_run:
        for _, case in C.iterrows():
            print(f"  {case.пара} | день X {case.день_X} | окно {case.окно_с} — {case.окно_по}")
        return

    for _, case in C.iterrows():
        pair, code_a, code_b = case.пара, case.код_A, case.код_B
        out_path = os.path.join(args.out, f"{pair}_{case.день_X}.csv")
        done: set[str] = set()
        if os.path.exists(out_path):
            prev = pd.read_csv(out_path, dtype=str, usecols=["DATEADDED"])
            done = set(prev.DATEADDED.astype(str).str[:8])
            print(f"{pair} ({case.день_X}): продолжаем, уже собрано дней {len(done)}")
        else:
            pd.DataFrame(columns=GDELT_COLUMNS).to_csv(out_path, index=False)
            print(f"{pair} ({case.день_X}): начинаем")

        kept = 0
        for day in pd.date_range(case.окно_с, case.окно_по):
            if day < DAILY_FROM:
                continue
            ds = day.strftime("%Y%m%d")
            if ds in done:
                continue
            try:
                raw = fetch_day(ds)
            except Exception as e:
                print(f"    {ds}: ошибка ({e}) — пропущено, перезапустите позже")
                continue
            if raw is None:
                print(f"    {ds}: файла нет")
                continue
            part = parse_day(raw, code_a, code_b)
            if len(part):
                part.to_csv(out_path, mode="a", header=False, index=False)
                kept += len(part)
            print(f"    {ds}: строк по паре {len(part)}", end="\r")
            time.sleep(args.pause)
        print(f"\n  готово: {out_path}, строк добавлено {kept}")

    print("\nДальше: python scan_dyads.py <папка кейса> или обучение в ml_pipeline.ipynb")


if __name__ == "__main__":
    main()
