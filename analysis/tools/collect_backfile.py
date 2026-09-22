#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сбор GDELT за период до апреля 2013: месячные и годовые файлы.

Зачем отдельный скрипт. До 01.04.2013 GDELT устроен иначе, и различий три:

  1. один файл покрывает целый месяц (2006 - март 2013) или целый год
     (1979-2005), а не сутки. Это резко дешевле: весь период 2006-2013 —
     это 87 файлов, и скачав их однажды, можно нарезать сколько угодно кейсов;
  2. в старых файлах 57 колонок, а не 58: нет SOURCEURL. Он добавлен только
     в суточных файлах с апреля 2013;
  3. файлы разложены по дате события (SQLDATE), а не по дате появления
     в новостях (DATEADDED), поэтому фильтр по окну идёт по SQLDATE.

Проверено на шести настоящих выгрузках после 2013 года, где есть оба поля:
они совпадают у 96-98% строк, корреляция дневных объёмов 0.993-0.999. То есть
подмена поля допустима, но помните, что для старых данных «день» означает
день события, а не день публикации.

    python collect_backfile.py mid_cases.csv --out data/mid --dry-run
    python collect_backfile.py mid_cases.csv --out data/mid

Нужен только pandas:   pip install pandas
"""
from __future__ import annotations
import argparse, io, os, sys, time, zipfile
import urllib.request, urllib.error
import pandas as pd

ВЕРСИЯ = "collect_backfile.py v1 — месячные и годовые файлы GDELT"
БАЗА = "http://data.gdeltproject.org/events/"
МЕСЯЧНЫЕ_С = pd.Timestamp("2006-01-01")
ГОДОВЫЕ_С   = pd.Timestamp("1979-01-01")
ДНЕВНЫЕ_С   = pd.Timestamp("2013-04-01")
CHUNK = 200_000

# 57 колонок старого формата: то же, что в суточных файлах, но без SOURCEURL
КОЛОНКИ_57 = [
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
    "DATEADDED",
]
A1, A2 = "Actor1CountryCode", "Actor2CountryCode"


def куски(день: pd.Timestamp) -> tuple[str, str]:
    """Какой файл покрывает эту дату: ('месяц','201203') или ('год','1999')."""
    if день >= ДНЕВНЫЕ_С:
        return "сутки", день.strftime("%Y%m%d")
    if день >= МЕСЯЧНЫЕ_С:
        return "месяц", день.strftime("%Y%m")
    if день >= ГОДОВЫЕ_С:
        return "год", день.strftime("%Y")
    return "вне покрытия", ""


def варианты_url(вид: str, ключ: str) -> list[str]:
    """Имена файлов на сервере отличаются между периодами, пробуем по очереди."""
    if вид == "сутки":
        return [f"{БАЗА}{ключ}.export.CSV.zip"]
    return [f"{БАЗА}{ключ}.zip", f"{БАЗА}{ключ}.export.CSV.zip", f"{БАЗА}{ключ}.csv.zip"]


def скачать(вид: str, ключ: str, retries: int = 3) -> tuple[bytes | None, str | None]:
    """Возвращает (содержимое, сработавший url). None — файла нет ни по одному имени."""
    последняя = None
    for url in варианты_url(вид, ключ):
        for попытка in range(1, retries + 1):
            try:
                with urllib.request.urlopen(url, timeout=600) as r:
                    return r.read(), url
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    break                      # этого имени нет, пробуем следующее
                последняя = e
                if попытка == retries:
                    break
            except Exception as e:
                последняя = e
                if попытка == retries:
                    break
            time.sleep(2.0 * попытка)
    if последняя is not None:
        print(f"    не скачалось: {последняя}")
    return None, None


def нарезать(raw: bytes, задания: list[dict]) -> dict[int, pd.DataFrame]:
    """Один проход по архиву: каждому кейсу свои строки.

    Задание — это пара стран плюс границы окна. Строка попадает в кейс, если
    совпала пара (в любом порядке) и SQLDATE лежит внутри окна.
    """
    собрано = {z["i"]: [] for z in задания}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        имя = [n for n in zf.namelist() if not n.endswith("/")][0]
        with zf.open(имя) as fh:
            первая = True
            for chunk in pd.read_csv(fh, sep="\t", header=None, dtype=str,
                                     chunksize=CHUNK, on_bad_lines="skip",
                                     encoding="utf-8", encoding_errors="replace"):
                if первая:
                    if chunk.shape[1] == len(КОЛОНКИ_57):
                        cols = КОЛОНКИ_57
                    elif chunk.shape[1] == len(КОЛОНКИ_57) + 1:
                        cols = КОЛОНКИ_57 + ["SOURCEURL"]
                    else:
                        print(f"    неожиданное число колонок: {chunk.shape[1]}, пропуск файла")
                        return {}
                    первая = False
                chunk.columns = cols[:chunk.shape[1]]
                д = pd.to_datetime(chunk.SQLDATE.astype(str).str[:8],
                                   format="%Y%m%d", errors="coerce")
                a1, a2 = chunk[A1], chunk[A2]
                for z in задания:
                    ca, cb = z["код_A"], z["код_B"]
                    m = (((a1 == ca) & (a2 == cb)) | ((a1 == cb) & (a2 == ca))) \
                        & (д >= z["окно_с"]) & (д <= z["окно_по"])
                    if m.any():
                        собрано[z["i"]].append(chunk[m])
    return {i: (pd.concat(v, ignore_index=True) if v else pd.DataFrame())
            for i, v in собрано.items()}


def main():
    ap = argparse.ArgumentParser(description="GDELT до апреля 2013: месячные и годовые файлы")
    ap.add_argument("cases", help="CSV со списком кейсов (от build_mid_cases.py)")
    ap.add_argument("--out", default="cases_backfile")
    ap.add_argument("--only-monthly", action="store_true",
                    help="только период месячных файлов (2006 - март 2013)")
    ap.add_argument("--pause", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(ВЕРСИЯ + "\n")

    C = pd.read_csv(args.cases)
    for c in ("день_X", "окно_с", "окно_по"):
        C[c] = pd.to_datetime(C[c])
    C = C.reset_index(drop=True)

    # --- какой файл нужен какому кейсу ---------------------------------------
    нужно: dict[tuple[str, str], list[dict]] = {}
    вне = 0
    for i, r in C.iterrows():
        ключи = set()
        for d in pd.date_range(r.окно_с, r.окно_по, freq="D"):
            вид, k = куски(d)
            if вид in ("сутки", "вне покрытия"):
                continue
            if args.only_monthly and вид != "месяц":
                continue
            ключи.add((вид, k))
        if not ключи:
            вне += 1
            continue
        for кл in ключи:
            нужно.setdefault(кл, []).append(dict(i=i, код_A=r.код_A, код_B=r.код_B,
                                                 окно_с=r.окно_с, окно_по=r.окно_по))

    мес = sorted(k for k in нужно if k[0] == "месяц")
    год = sorted(k for k in нужно if k[0] == "год")
    print(f"Кейсов в файле: {len(C)} | из них вне периода этого скрипта: {вне}")
    print(f"Нужно скачать: месячных файлов {len(мес)}, годовых {len(год)}")
    print(f"Один файл обслуживает в среднем "
          f"{sum(len(v) for v in нужно.values())/max(len(нужно),1):.1f} кейсов — "
          f"в этом и смысл: качаем период, а не кейсы\n")
    if args.dry_run:
        print("Месяцы:", ", ".join(k for _, k in мес[:12]), "..." if len(мес) > 12 else "")
        print("Годы:  ", ", ".join(k for _, k in год))
        print("\n--dry-run: ничего не скачивалось")
        return

    os.makedirs(args.out, exist_ok=True)
    C["файл"] = [os.path.join(args.out, f"{r.пара}_{r.день_X.date()}"
                              + ("" if int(r.метка) == 1 else "_control") + ".csv")
                 for _, r in C.iterrows()]
    C["журнал"] = C.файл + ".parts"
    сделано = {}
    for i, r in C.iterrows():
        сделано[i] = set()
        if os.path.exists(r.журнал):
            сделано[i] = {ln.strip() for ln in open(r.журнал, encoding="utf-8") if ln.strip()}

    порядок = мес + год
    строк_всего, нет_на_сервере, ошибки = 0, [], []
    t0 = time.time()
    for n, (вид, ключ) in enumerate(порядок, start=1):
        задания = [z for z in нужно[(вид, ключ)] if ключ not in сделано[z["i"]]]
        if not задания:
            continue
        raw, url = скачать(вид, ключ)
        if raw is None:
            нет_на_сервере.append(ключ)
            continue
        print(f"  [{n}/{len(порядок)}] {ключ} ({len(raw)/1048576:.0f} МБ, "
              f"кейсов {len(задания)})", flush=True)
        try:
            части = нарезать(raw, задания)
        except Exception as e:
            ошибки.append(ключ); print(f"    ошибка разбора: {e}"); continue
        for z in задания:
            часть = части.get(z["i"])
            путь = C.loc[z["i"], "файл"]
            if часть is not None and len(часть):
                часть.to_csv(путь, mode="a", header=not os.path.exists(путь), index=False)
                строк_всего += len(часть)
            with open(C.loc[z["i"], "журнал"], "a", encoding="utf-8") as jf:
                jf.write(ключ + "\n")
        time.sleep(args.pause)

    # --- манифест ------------------------------------------------------------
    ман = []
    for i, r in C.iterrows():
        строк = 0
        if os.path.exists(r.файл):
            try:
                строк = sum(1 for _ in open(r.файл, encoding="utf-8")) - 1
            except Exception:
                строк = -1
        ман.append(dict(пара=r.пара, день_X=r.день_X.date(), метка=r.метка,
                        название=r.get("название", ""), файл=os.path.basename(r.файл),
                        строк=строк))
    M = pd.DataFrame(ман).sort_values(["метка", "день_X"], ascending=[False, False])
    M.to_csv(os.path.join(args.out, "manifest.csv"), index=False, encoding="utf-8-sig")
    C.drop(columns=["файл", "журнал"]).to_csv(
        os.path.join(args.out, "case_list.csv"), index=False, encoding="utf-8-sig")

    print(f"\nГОТОВО за {(time.time()-t0)/60:.0f} мин | строк собрано {строк_всего:,}"
          .replace(",", " "))
    мало = M[M.строк < 200]
    print(f"Кейсов с данными: {(M.строк >= 200).sum()} из {len(M)} "
          f"(меньше 200 строк — для анализа не годятся)")
    if нет_на_сервере:
        print(f"Файлов не нашлось на сервере: {len(нет_на_сервере)} ({нет_на_сервере[:6]})")
    if ошибки:
        print(f"Файлов с ошибкой разбора: {len(ошибки)} — перезапустите, они докачаются")
    print(f"\nПапка: {args.out} | список: case_list.csv | статусы: manifest.csv")


if __name__ == "__main__":
    main()
