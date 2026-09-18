#!/usr/bin/env python3
"""Сканер пар государств по выгрузке GDELT.

Читает выгрузку (один CSV или папку с файлами), сворачивает её до уровня
«пара стран × день», считает признаки последнего 7-дневного окна относительно
фона предыдущих четырёх недель и ранжирует пары по баллу.

ВАЖНО: балл — это не вероятность войны. Это мера того, насколько последняя
неделя пары аномальна на фоне её собственных предыдущих недель. Калибровать
его в вероятность не на чем: размеченных кейсов слишком мало (см. аудит).

Примеры:
    python scan_dyads.py gdelt_50d.csv
    python scan_dyads.py ./daily_exports/ --min-events 300 --top 40
    python scan_dyads.py gdelt_50d.csv --no-header --out ranking.csv
"""
from __future__ import annotations
import argparse, sys, glob, os
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dyad_features import (GDELT_COLUMNS, USE_COLS, MIN_DAYS, aggregate_pair_days,
                           combine_chunks, pair_daily_frame, features_at)

CHUNK = 500_000


def iter_files(path: str) -> list[str]:
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.csv")) +
                       glob.glob(os.path.join(path, "*.CSV")) +
                       glob.glob(os.path.join(path, "*.tsv")))
        if not files:
            sys.exit(f"В папке {path} не найдено csv/tsv файлов")
        return files
    return [path]


def sniff(path: str) -> tuple[str, bool]:
    """Определяет разделитель и наличие заголовка."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
    sep = "\t" if first.count("\t") > first.count(",") else ","
    has_header = "GLOBALEVENTID" in first.upper()
    return sep, has_header


def load_aggregate(paths: list[str], force_no_header: bool = False,
                   dedup_global: bool = False) -> pd.DataFrame:
    """Читает файлы кусками и сворачивает их до агрегата «пара × день».

    dedup_global=True держит в памяти множество уже виденных GLOBALEVENTID —
    нужно, если файлы перекрываются (повторные выгрузки за одни и те же дни).
    """
    parts = []
    seen: set[int] = set()
    dates_by_file: dict[str, set] = {}
    for path in paths:
        sep, has_header = sniff(path)
        if force_no_header:
            has_header = False
        reader_kw = dict(sep=sep, chunksize=CHUNK, dtype=str, low_memory=False,
                         on_bad_lines="skip", encoding="utf-8", encoding_errors="replace")
        if has_header:
            reader = pd.read_csv(path, **reader_kw)
        else:
            reader = pd.read_csv(path, names=GDELT_COLUMNS, header=None, **reader_kw)
        n_rows = n_dup = 0
        file_dates: set = set()
        for chunk in reader:
            if "GLOBALEVENTID" in chunk.columns:
                chunk = chunk[chunk.GLOBALEVENTID != "GLOBALEVENTID"]
                chunk = chunk.drop_duplicates(subset="GLOBALEVENTID")
                if dedup_global:
                    ids = pd.to_numeric(chunk.GLOBALEVENTID, errors="coerce")
                    fresh = ~ids.isin(seen)
                    n_dup += int((~fresh).sum())
                    chunk = chunk[fresh.values]
                    seen.update(ids[fresh].dropna().astype("int64").tolist())
            missing = [c for c in USE_COLS if c not in chunk.columns]
            if missing:
                sys.exit(f"{os.path.basename(path)}: нет колонок {missing}. "
                         f"Если файл без заголовка, добавьте --no-header")
            if len(chunk):
                file_dates.update(chunk.DATEADDED.astype(str).str[:8].unique())
                parts.append(aggregate_pair_days(chunk[USE_COLS]))
            n_rows += len(chunk)
        dates_by_file[os.path.basename(path)] = file_dates
        tail = f", повторов отброшено {n_dup:,}".replace(",", " ") if n_dup else ""
        print(f"  {os.path.basename(path)}: {n_rows:,} строк".replace(",", " ") + tail)

    if len(paths) > 1 and not dedup_global:
        counts: dict = {}
        for fdates in dates_by_file.values():
            for dt in fdates:
                counts[dt] = counts.get(dt, 0) + 1
        overlap = sorted(d for d, c in counts.items() if c > 1)
        if overlap:
            print(f"\n  ВНИМАНИЕ: {len(overlap)} дат встречаются больше чем в одном файле "
                  f"(например {', '.join(overlap[:3])}).")
            print("  События за эти дни посчитаны дважды. Перезапустите с --dedup-global.")
    return combine_chunks(parts)


def main():
    ap = argparse.ArgumentParser(description="Ранжирование пар государств по аномальности последней недели")
    ap.add_argument("path", help="CSV-файл выгрузки или папка с файлами")
    ap.add_argument("--min-events", type=int, default=200,
                    help="минимум событий у пары за всё окно (по умолчанию 200)")
    ap.add_argument("--min-week", type=int, default=20,
                    help="минимум событий у пары в последнюю неделю (по умолчанию 20)")
    ap.add_argument("--top", type=int, default=25, help="сколько строк показать")
    ap.add_argument("--out", default="dyad_ranking.csv", help="куда сохранить полный рейтинг")
    ap.add_argument("--no-header", action="store_true", help="файлы без строки заголовка")
    ap.add_argument("--dedup-global", action="store_true",
                    help="убирать повторы GLOBALEVENTID между файлами (нужно, если выгрузки "
                         "перекрываются; держит id в памяти)")
    args = ap.parse_args()

    files = iter_files(args.path)
    print(f"Файлов к обработке: {len(files)}")
    agg = load_aggregate(files, args.no_header, args.dedup_global)
    if agg.empty:
        sys.exit("Не удалось собрать ни одной пары — проверьте формат файла")

    span = (agg.date.max() - agg.date.min()).days + 1
    print(f"\nОкно данных: {agg.date.min().date()} — {agg.date.max().date()} ({span} дней)")
    print(f"Пар с событиями: {agg.pair.nunique():,}".replace(",", " "))
    if span < MIN_DAYS:
        sys.exit(f"Нужно минимум {MIN_DAYS} дней истории, в выгрузке {span}")

    totals = agg.groupby("pair").n.sum()
    candidates = totals[totals >= args.min_events].index
    print(f"Пар с числом событий >= {args.min_events}: {len(candidates):,}".replace(",", " "))

    rows = []
    for pair in candidates:
        p = pair_daily_frame(agg, pair)
        if len(p) < MIN_DAYS:
            continue
        f = features_at(p)
        if f is None or f["events_week"] < args.min_week:
            continue
        f["pair"] = pair
        rows.append(f)

    if not rows:
        sys.exit("Ни одна пара не прошла фильтры — ослабьте --min-events / --min-week")

    R = pd.DataFrame(rows).set_index("pair")
    R["балл"] = R.z_tone
    R = R.sort_values("балл", ascending=False)
    R["тревога_1s"] = np.where(R.z_tone >= 1.0, "да", "")

    cols = ["балл", "тревога_1s", "z_tone", "z_vol", "z_hard", "z_mat",
            "lvl_tone", "events_week", "events_total"]
    R[cols].to_csv(args.out, float_format="%.3f")

    print(f"\nПар в рейтинге: {len(R)} | полный результат: {args.out}")
    print(f"Сработало правило TONE-1σ (z >= 1): {int((R.z_tone >= 1).sum())} пар "
          f"({(R.z_tone >= 1).mean()*100:.1f} %)\n")
    show = R[cols].head(args.top).copy()
    show.columns = ["балл", "тревога", "z тона", "z объёма", "z жёстк.", "z матер.",
                    "тон", "событий/нед", "событий всего"]
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(show.round(2).to_string())

    print("\n" + "=" * 78)
    print("КАК ЧИТАТЬ РЕЗУЛЬТАТ")
    print("=" * 78)
    print("Балл = насколько тон последней недели хуже фона предыдущих 4 недель (в сигмах).")
    print("Это НЕ вероятность войны. По аудиту на шести размеченных кейсах:")
    print("  • правило срабатывает примерно в 27 % обычных недель;")
    print("  • при войне раз в два года на пару доля верных тревог около 3 %.")
    print("Практический смысл: список для ручного просмотра, а не прогноз.")
    print("Пары с малым числом событий шумят сильнее — поднимайте --min-events.")


if __name__ == "__main__":
    main()
