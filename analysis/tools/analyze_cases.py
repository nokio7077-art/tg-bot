#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обучение и честная проверка модели на собранных кейсах.

Берёт папку, которую сделал collect_cases.py, и доводит дело до ответа на вопрос
«есть ли в медиа-фоне сигнал, предупреждающий о войне, и какой он силы».

Что делает:
  1. читает CSV кейсов, сворачивает события в дневные ряды по паре государств;
  2. считает признаки на конец окна (последняя неделя против четырёх недель фона);
  3. учит логистическую регрессию с проверкой «одна пара государств — один фолд»,
     причём признаки отбираются ВНУТРИ фолда, а не на всех данных;
  4. сравнивает её с необучаемыми базлайнами (сырой z_tone, правило TONE-1σ);
  5. считает доверительные интервалы кластерным бутстрапом по парам;
  6. прогоняет плацебо-тест: перемешивает метки и проверяет, что модель разваливается;
  7. считает, за сколько дней до дня X впервые срабатывает сигнал (лид-тайм);
  8. пишет отчёт и три CSV: признаки, предсказания, итоговые метрики.

Нужны pandas, numpy, scipy:   pip install pandas numpy scipy

    python analyze_cases.py cases                 обычный запуск
    python analyze_cases.py cases --out отчёт     куда класть результаты
    python analyze_cases.py --self-test           проверка самой машинки на
                                                  синтетике с известным ответом
"""
from __future__ import annotations
import argparse, os, sys, json
import numpy as np
import pandas as pd
from scipy import stats

WINDOW_DAYS = 7          # окно оценки
BASE_WEEKS  = 4          # фон: столько недель перед окном
MIN_DAYS    = WINDOW_DAYS + BASE_WEEKS * 7
RIDGE       = 1.0        # регуляризация логрегрессии
N_FEAT      = 3          # столько признаков берём в модель (37 войн -> больше нельзя)
B_BOOT      = 2000       # повторов бутстрапа
B_PERM      = 1000       # повторов плацебо-теста
RNG         = np.random.default_rng(20260919)
ВЕРСИЯ      = "analyze_cases.py v3 — парный анализ, лид-тайм по нормированным признакам"

CAND = ["z_tone", "z_gold", "z_mat", "z_hard", "z_force", "z_vol",
        "vol_ratio", "slope21_tone", "log_vol"]


# --------------------------------------------------------------------------- #
#  1. Данные -> дневной ряд -> признаки
# --------------------------------------------------------------------------- #
def daily_from_case(path: str) -> pd.DataFrame:
    """Сворачивает файл кейса в дневной ряд с заполненным календарём."""
    d = pd.read_csv(path, dtype=str, low_memory=False)
    if not len(d):
        return pd.DataFrame()
    for c in ("AvgTone", "GoldsteinScale", "QuadClass", "EventRootCode", "EventCode"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["date"] = pd.to_datetime(d.DATEADDED.astype(str).str[:8], format="%Y%m%d", errors="coerce")
    d = d[d.date.notna()]
    if not len(d):
        return pd.DataFrame()
    d["is_mat"]   = (d.QuadClass == 4).astype("int8")
    d["is_hard"]  = d.EventRootCode.isin([18, 19, 20]).astype("int8")
    d["is_force"] = d.EventCode.isin([190, 193, 195]).astype("int8")
    g = d.groupby("date", sort=True).agg(
        n=("AvgTone", "size"), tone=("AvgTone", "mean"), gold=("GoldsteinScale", "mean"),
        mat_s=("is_mat", "mean"), hard_s=("is_hard", "mean"), force_s=("is_force", "mean"))
    idx = pd.date_range(g.index.min(), g.index.max())
    g = g.reindex(idx)
    g["n"] = g.n.fillna(0)
    for c in ("tone", "gold", "mat_s", "hard_s", "force_s"):
        g[c] = g[c].interpolate().ffill().bfill()
    return g


def features_at(p: pd.DataFrame, end: int | None = None) -> dict | None:
    """Признаки окна, заканчивающегося на позиции end. None — мало истории."""
    end = len(p) if end is None else end
    if end < MIN_DAYS:
        return None
    w = p.iloc[end - WINDOW_DAYS:end]
    b = p.iloc[end - MIN_DAYS:end - WINDOW_DAYS]
    blocks = [b.iloc[i:i + 7] for i in range(0, len(b) - 6, 7)]
    if len(blocks) < BASE_WEEKS:
        return None
    f = {}
    # знак выставлен так, чтобы «больше» везде значило «тревожнее»
    for col, sign, name in [("tone", -1, "tone"), ("gold", -1, "gold"), ("mat_s", +1, "mat"),
                            ("hard_s", +1, "hard"), ("force_s", +1, "force")]:
        bv = np.array([x[col].mean() for x in blocks])
        sd = bv.std(ddof=1)
        f[f"z_{name}"] = float(sign * (w[col].mean() - bv.mean()) / sd) if sd > 0 else 0.0
    bn = np.log(np.maximum([x.n.mean() for x in blocks], 1))
    sdn = bn.std(ddof=1)
    f["z_vol"] = float((np.log(max(w.n.mean(), 1)) - bn.mean()) / sdn) if sdn > 0 else 0.0
    f["vol_ratio"] = float(w.n.mean() / max(b.n.mean(), 1))
    f["log_vol"] = float(np.log(max(w.n.mean(), 1)))
    y = p.tone.iloc[max(0, end - 21):end].values
    f["slope21_tone"] = float(-np.polyfit(np.arange(len(y)), y, 1)[0]) if len(y) >= 10 else 0.0
    f["events_week"] = float(w.n.sum())
    return f


def build_table(cases_dir: str) -> pd.DataFrame:
    """Одна строка на кейс: признаки на конец окна + метка."""
    cl = os.path.join(cases_dir, "case_list.csv")
    if not os.path.exists(cl):
        sys.exit(f"Нет файла {cl} — укажите папку, которую сделал collect_cases.py")
    C = pd.read_csv(cl)
    rows, skipped = [], []
    for _, r in C.iterrows():
        name = f"{r.пара}_{r.день_X}" + ("" if int(r.метка) == 1 else "_control") + ".csv"
        path = os.path.join(cases_dir, name)
        if not os.path.exists(path):
            skipped.append((name, "файла нет")); continue
        p = daily_from_case(path)
        if len(p) < MIN_DAYS:
            skipped.append((name, f"дней всего {len(p)}, нужно {MIN_DAYS}")); continue
        f = features_at(p)
        if f is None:
            skipped.append((name, "не хватило истории для признаков")); continue
        f.update(пара=r.пара, день_X=r.день_X, метка=int(r.метка),
                 название=r.get("название", ""), файл=name, дней=len(p))
        rows.append(f)
    if skipped:
        print("Пропущено кейсов:", len(skipped))
        for n, why in skipped[:12]:
            print(f"   {n} — {why}")
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
#  2. Логистическая регрессия (без sklearn, чтобы не тянуть зависимость)
# --------------------------------------------------------------------------- #
def fit_logit(X: np.ndarray, y: np.ndarray, ridge: float = RIDGE, iters: int = 200):
    """Логрегрессия с L2, метод Ньютона. Первый столбец X — константа."""
    n, k = X.shape
    w = np.zeros(k)
    pen = np.eye(k) * ridge
    pen[0, 0] = 0.0
    for _ in range(iters):
        z = np.clip(X @ w, -30, 30)
        p = 1 / (1 + np.exp(-z))
        g = X.T @ (p - y) + pen @ w
        W = np.clip(p * (1 - p), 1e-6, None)
        H = X.T @ (X * W[:, None]) + pen
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def predict_logit(w, X):
    return 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))


def design(df: pd.DataFrame, feats: list[str], mu=None, sd=None):
    """Матрица плана со стандартизацией; mu/sd берутся из обучающей части."""
    V = df[feats].to_numpy(float)
    if mu is None:
        mu, sd = V.mean(0), V.std(0, ddof=1)
        sd = np.where(sd > 0, sd, 1.0)
    V = (V - mu) / sd
    return np.column_stack([np.ones(len(V)), V]), mu, sd


# --------------------------------------------------------------------------- #
#  3. Метрики
# --------------------------------------------------------------------------- #
def auc(y: np.ndarray, s: np.ndarray) -> float:
    """Площадь под ROC = вероятность, что война получит балл выше контроля."""
    pos, neg = s[y == 1], s[y == 0]
    if not len(pos) or not len(neg):
        return np.nan
    r = stats.rankdata(np.concatenate([pos, neg]))
    return (r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def cluster_bootstrap_auc(y, s, groups, B=B_BOOT):
    """Доверительный интервал AUC с пересэмплированием ПАР, а не наблюдений."""
    uniq = np.unique(groups)
    out = []
    for _ in range(B):
        take = RNG.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([np.where(groups == g)[0] for g in take])
        a = auc(y[idx], s[idx])
        if not np.isnan(a):
            out.append(a)
    return (np.percentile(out, 2.5), np.percentile(out, 97.5)) if out else (np.nan, np.nan)


def brier(y, p):
    return float(np.mean((p - y) ** 2))


def precision_at_k(y, s, frac=0.2):
    """Какая доля тревог настоящая, если тревожимся по верхним frac баллов."""
    k = max(1, int(round(len(s) * frac)))
    top = np.argsort(-s)[:k]
    return float(y[top].mean()), k


# --------------------------------------------------------------------------- #
#  4. Честная кросс-валидация: один фолд — одна пара государств
# --------------------------------------------------------------------------- #
def logo_scores(T: pd.DataFrame, cand: list[str], n_feat: int = N_FEAT):
    """Оставляем пару целиком, признаки отбираем только на обучающей части."""
    y = T.метка.to_numpy(int)
    groups = T.пара.to_numpy()
    scores = np.full(len(T), np.nan)
    chosen = []
    for g in np.unique(groups):
        te = groups == g
        tr = ~te
        if len(np.unique(y[tr])) < 2:
            continue
        # отбор признаков внутри фолда: по одномерному AUC на обучающей части
        rank = sorted(cand, key=lambda c: -abs(auc(y[tr], T[c].to_numpy(float)[tr]) - 0.5))
        feats = rank[:n_feat]
        chosen.append(feats)
        Xtr, mu, sd = design(T[tr], feats)
        w = fit_logit(Xtr, y[tr])
        Xte, _, _ = design(T[te], feats, mu, sd)
        scores[te] = predict_logit(w, Xte)
    return scores, chosen


def permutation_test(T, cand, observed, B=B_PERM):
    """Плацебо: перемешиваем метки между парами и смотрим, какой AUC даёт шум."""
    y = T.метка.to_numpy(int)
    null = []
    S = T.copy()
    for _ in range(B):
        S["метка"] = RNG.permutation(y)
        s, _ = logo_scores(S, cand)
        ok = ~np.isnan(s)
        a = auc(S.метка.to_numpy(int)[ok], s[ok])
        if not np.isnan(a):
            null.append(a)
    null = np.array(null)
    p = float((np.sum(null >= observed) + 1) / (len(null) + 1))
    return null, p


# Порог имеет смысл только для признаков, нормированных на собственный фон пары.
# log_vol и events_week — абсолютные уровни, у них «порог 1.0» срабатывает всегда.
# по два порога на признак: мягкий часто срабатывает от шума, строгий — реже,
# но его срабатывание что-то значит
LEAD_SPECS = [("z_tone", 1.0), ("z_tone", 2.0),
              ("z_vol", 1.0), ("z_vol", 2.0),
              ("vol_ratio", 1.5), ("vol_ratio", 2.5)]
LEAD_FALLBACK = {"log_vol": "z_vol", "events_week": "z_vol"}
LEAD_CEILING = 60 - MIN_DAYS + 1      # сколько дней окна вообще доступно для наблюдения


def lead_times(cases_dir: str, T: pd.DataFrame,
               specs: list[tuple[str, float]] | None = None) -> pd.DataFrame:
    """За сколько дней до дня X признак впервые перешёл порог.

    Считается сразу по нескольким нормированным признакам, чтобы не гонять
    чтение всех файлов повторно. Значение упирается в потолок: при окне 60 дней
    и 35 днях истории, нужных на расчёт фона, раньше чем за 26 дней сигнал
    увидеть физически нельзя. Такие наблюдения помечаются как цензурированные —
    настоящий лид-тайм у них не меньше указанного, но насколько, мы не знаем.
    """
    specs = specs or LEAD_SPECS
    out = []
    for _, r in T[T.метка == 1].iterrows():
        p = daily_from_case(os.path.join(cases_dir, r.файл))
        if len(p) < MIN_DAYS:
            continue
        ends = list(range(MIN_DAYS, len(p) + 1))
        vals = {e: features_at(p, e) for e in ends}
        for feat, thr in specs:
            first = None
            for e in ends:
                f = vals[e]
                if f and f.get(feat) is not None and f[feat] >= thr:
                    first = len(p) - e + 1
                    break
            out.append(dict(пара=r.пара, день_X=r.день_X, название=r.название,
                            признак=feat, порог=thr, лид_тайм_дней=first,
                            цензурировано=(first is not None and first >= len(ends))))
    return pd.DataFrame(out)


def lead_report(L: pd.DataFrame):
    """Печатает распределение лид-таймов с честной пометкой про цензурирование."""
    n_wars = L[["пара", "день_X"]].drop_duplicates().shape[0]
    line(f"{'признак':<14}{'порог':>7}{'сработал':>11}{'медиана':>10}"
         f"{'квартили':>14}{'уткнулись в потолок':>22}")
    for (feat, thr), grp in L.groupby(["признак", "порог"], sort=False):
        got = grp.лид_тайм_дней.dropna()
        cens = int(grp.цензурировано.sum())
        if not len(got):
            line(f"{feat:<14}{thr:>7.1f}{'0':>11}{'—':>10}{'—':>14}{'—':>22}")
            continue
        line(f"{feat:<14}{thr:>7.1f}{len(got):>6}/{n_wars:<4}{got.median():>10.0f}"
             f"{f'{got.quantile(.25):.0f}-{got.quantile(.75):.0f}':>14}"
             f"{f'{cens} из {len(got)}':>22}")
    worst = L.groupby(["признак", "порог"]).цензурировано.mean().max()
    if worst > 0.6:
        line("\nВНИМАНИЕ: больше половины срабатываний уткнулись в потолок наблюдения.")
        line("Это значит, что порог слишком мягкий либо окно в 60 дней слишком короткое:")
        line("сигнал уже горел на самом раннем дне, который мы в принципе можем посчитать.")
        line("Чтобы увидеть настоящий лид-тайм, нужен сбор с --window 120.")



# --------------------------------------------------------------------------- #
#  4б. Парный анализ: каждый контроль строился под конкретную войну
# --------------------------------------------------------------------------- #
def match_pairs(T: pd.DataFrame) -> list[tuple]:
    """Сопоставляет контроль его войне по названию «контроль к «...»»."""
    W, C = T[T.метка == 1], T[T.метка == 0].copy()
    C["к_войне"] = C.название.str.extract(r"контроль к «(.+?)»\s*\(−\d")[0]
    out = []
    for _, c in C.iterrows():
        m = W[(W.пара == c.пара) & (W.название == c.к_войне)]
        if len(m) == 1:
            out.append((c.пара, m.iloc[0], c))
    return out


def paired_report(T: pd.DataFrame, cand: list[str]):
    """Сравнение «война против своего же контроля» — так дизайн и задуман.

    Парный тест сильнее непарного: он убирает различия между парами государств
    (Иран-США всегда освещают больше, чем Киргизию-Таджикистан).
    """
    mp = match_pairs(T)
    if len(mp) < 6:
        line("Сматчено слишком мало пар «война-контроль», парный анализ пропущен")
        return None, mp
    line(f"Сматчено пар «война-контроль»: {len(mp)}")
    line(f"{'признак':<14}{'выше у войны':>14}{'медиана разницы':>18}{'Вилкоксон p':>14}{'d':>7}")
    rows = []
    for c in cand:
        d = np.array([w[c] - ct[c] for _, w, ct in mp], float)
        st = stats.wilcoxon(d)
        rows.append(dict(признак=c, выше=int((d > 0).sum()), всего=len(d),
                         медиана=float(np.median(d)), p=float(st.pvalue),
                         d=float(d.mean() / d.std(ddof=1))))
        line(f"{c:<14}{rows[-1]['выше']:>7}/{len(d):<6}{np.median(d):>18.3f}"
             f"{st.pvalue:>14.4f}{rows[-1]['d']:>7.2f}")
    P = pd.DataFrame(rows)
    thr = 0.05 / len(cand)
    line(f"\nПоправка Бонферрони на {len(cand)} признаков: значимо при p < {thr:.5f}")
    surv = P[P.p < thr]
    line("Проходят поправку: " + (", ".join(surv.признак) if len(surv) else "ни один"))
    return P, mp


def fixed_effects(T: pd.DataFrame, target: str):
    """Регрессия с фиксированными эффектами пары и календарным годом.

    Отвечает на вопрос «это правда предвоенный всплеск или просто со временем
    новостей становится больше»: год входит отдельным регрессором.
    """
    df = T.copy()
    df["год"] = pd.to_datetime(df.день_X).dt.year
    D = pd.get_dummies(df.пара, drop_first=True).astype(float)
    X = np.column_stack([np.ones(len(df)), df.метка.values.astype(float),
                         (df.год.values - df.год.mean()).astype(float), D.values])
    yv = df[target].values.astype(float)
    b, *_ = np.linalg.lstsq(X, yv, rcond=None)
    resid = yv - X @ b
    dof = len(df) - X.shape[1]
    if dof <= 0:
        return None
    cov = (resid @ resid / dof) * np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    out = {}
    for i, nm in [(1, "война"), (2, "год")]:
        tst = b[i] / se[i]
        out[nm] = (float(b[i]), float(se[i]), float(2 * (1 - stats.t.cdf(abs(tst), dof))))
    return out


def paired_and_fe_block(T: pd.DataFrame):
    line()
    line("=" * 78)
    line("ПАРНЫЙ АНАЛИЗ: война против своего же контроля")
    line("=" * 78)
    P, mp = paired_report(T, CAND)
    if P is None:
        return None, None
    best = P.sort_values("p").iloc[0].признак
    line()
    line("=" * 78)
    line(f"ЭТО НЕ АРТЕФАКТ? Фикс. эффекты пары + календарный год, признак {best}")
    line("=" * 78)
    for feat in dict.fromkeys([best, "z_tone", "vol_ratio"]):
        fe = fixed_effects(T, feat)
        if fe is None:
            continue
        line(f"{feat:<14} война: {fe['война'][0]:+.3f} (p={fe['война'][2]:.4f})   "
             f"год: {fe['год'][0]:+.3f} (p={fe['год'][2]:.4f})")
    line("\nЕсли «год» значим и того же знака, что «война», — эффект может быть")
    line("просто ростом корпуса GDELT со временем, а не предвоенным всплеском.")
    return P, best


# --------------------------------------------------------------------------- #
#  5. Отчёт
# --------------------------------------------------------------------------- #
def line(t=""):
    print(t)


def report(T: pd.DataFrame, cases_dir: str | None, out_dir: str, do_perm=True, do_lead=True):
    y = T.метка.to_numpy(int)
    groups = T.пара.to_numpy()
    line("=" * 78)
    line("ЧТО В ВЫБОРКЕ")
    line("=" * 78)
    line(f"Кейсов: {len(T)} | войн: {int(y.sum())} | контролей: {int((y == 0).sum())} "
         f"| пар государств: {T.пара.nunique()}")
    if len(np.unique(groups)) < 3 or y.sum() < 3:
        sys.exit("Слишком мало данных для честной проверки — нужно минимум 3 пары и 3 войны")

    line()
    line("=" * 78)
    line("ПРИЗНАКИ ПО ОТДЕЛЬНОСТИ (без обучения)")
    line("=" * 78)
    line(f"{'признак':<15}{'война':>9}{'контроль':>10}{'разница d':>11}{'AUC':>8}{'p':>10}")
    singles = []
    for c in CAND:
        v = T[c].to_numpy(float)
        a, b = v[y == 1], v[y == 0]
        sp = np.sqrt(((len(a)-1)*a.var(ddof=1) + (len(b)-1)*b.var(ddof=1)) / (len(a)+len(b)-2))
        d = (a.mean() - b.mean()) / sp if sp > 0 else 0.0
        u = stats.mannwhitneyu(a, b, alternative="two-sided")
        au = auc(y, v)
        singles.append(dict(признак=c, d=d, AUC=au, p=u.pvalue))
        line(f"{c:<15}{a.mean():>9.2f}{b.mean():>10.2f}{d:>11.2f}{au:>8.3f}{u.pvalue:>10.4f}")
    S = pd.DataFrame(singles)
    m = len(CAND)
    line(f"\nПоправка Бонферрони на {m} признаков: значимым считается p < {0.05/m:.4f}")
    surv = S[S.p < 0.05/m]
    line("Проходят поправку: " + (", ".join(surv.признак) if len(surv) else "ни один"))

    line()
    line("=" * 78)
    line("ПРОВЕРКА НА ОТЛОЖЕННЫХ ПАРАХ (одна пара — один фолд)")
    line("=" * 78)
    res = {}
    # базлайны: ничего не учим, просто берём признак как балл
    for name, s in [("сырой z_tone", T.z_tone.to_numpy(float)),
                    ("сырой z_vol", T.z_vol.to_numpy(float))]:
        a = auc(y, s)
        lo, hi = cluster_bootstrap_auc(y, s, groups)
        res[name] = (a, lo, hi)
        line(f"{name:<28} AUC = {a:.3f}  [{lo:.3f}; {hi:.3f}]")
    # правило TONE-1σ: порог, а не ранжирование
    alarm = (T.z_tone.to_numpy(float) >= 1.0).astype(int)
    tpr = alarm[y == 1].mean(); fpr = alarm[y == 0].mean()
    line(f"{'правило TONE-1σ':<28} ловит войн {tpr:.0%}, ложных тревог {fpr:.0%}")
    # обученная модель
    s_model, chosen = logo_scores(T, CAND)
    ok = ~np.isnan(s_model)
    a = auc(y[ok], s_model[ok])
    lo, hi = cluster_bootstrap_auc(y[ok], s_model[ok], groups[ok])
    res["модель"] = (a, lo, hi)
    line(f"{'обученная модель':<28} AUC = {a:.3f}  [{lo:.3f}; {hi:.3f}]  "
         f"(предсказаний {ok.sum()} из {len(T)})")
    line(f"{'':<28} Brier = {brier(y[ok], s_model[ok]):.3f} "
         f"(чем меньше, тем лучше калибровка)")
    p20, k = precision_at_k(y[ok], s_model[ok], 0.2)
    line(f"{'':<28} из верхних {k} тревог настоящих {p20:.0%}")
    cnt = pd.Series([c for f in chosen for c in f]).value_counts()
    line(f"\nКакие признаки модель выбирала по фолдам (из {len(chosen)}):")
    for c, v in cnt.items():
        line(f"   {c:<15} {v:>3} раз")

    best = max(res, key=lambda k_: res[k_][0] if not np.isnan(res[k_][0]) else -1)
    line(f"\nЛучший вариант: {best} (AUC {res[best][0]:.3f})")
    if best != "модель":
        line("Обучение не дало выигрыша — значит хватает одного признака без модели.")

    P_paired, best_paired = paired_and_fe_block(T)

    perm_p = None
    if do_perm:
        line()
        line("=" * 78)
        line(f"ПЛАЦЕБО-ТЕСТ ДЛЯ ЛУЧШЕГО ВАРИАНТА ({best}): метки перемешаны")
        line("=" * 78)
        a_best = res[best][0]
        if best == "модель":
            null, perm_p = permutation_test(T, CAND, a_best, B=200)
        else:
            src = T.z_tone if "tone" in best else T.z_vol
            v = src.to_numpy(float)
            null = np.array([auc(RNG.permutation(y), v) for _ in range(2000)])
            perm_p = float((np.sum(null >= a_best) + 1) / (len(null) + 1))
        line(f"AUC на перемешанных метках: медиана {np.median(null):.3f}, "
             f"95-й процентиль {np.percentile(null, 95):.3f}")
        line(f"Наш AUC = {a_best:.3f}  ->  p = {perm_p:.4f}")
        line("Вывод: " + ("шумом такой результат не объясняется"
                          if perm_p < 0.05 else
                          "результат неотличим от случайного"))

    line()
    line("=" * 78)
    line("УСТОЙЧИВОСТЬ: что будет, если убрать одну пару целиком")
    line("=" * 78)
    drops = []
    for g in np.unique(groups):
        keep = groups != g
        if len(np.unique(y[keep])) < 2:
            continue
        drops.append(dict(без_пары=g, AUC=auc(y[keep], T.z_tone.to_numpy(float)[keep])))
    D = pd.DataFrame(drops).sort_values("AUC")
    full = auc(y, T.z_tone.to_numpy(float))
    line(f"AUC сырого z_tone на всех парах: {full:.3f}")
    line(f"Если убирать пары по одной, гуляет от {D.AUC.min():.3f} до {D.AUC.max():.3f}")
    if full > 0.6:
        line("Без этих пар просаживается сильнее всего: " + ", ".join(D.head(3).без_пары))
        if D.AUC.min() < 0.55:
            line("ВНИМАНИЕ: убрав одну пару, мы теряем результат — он держится на ней одной")
        else:
            line("Результат не держится на одной паре — это хороший признак")

    L = None
    if do_lead and cases_dir:
        line()
        line("=" * 78)
        line("ЛИД-ТАЙМ: за сколько дней до дня X сигнал сработал впервые")
        line("=" * 78)
        specs = list(LEAD_SPECS)
        bp = LEAD_FALLBACK.get(best_paired, best_paired)
        if bp and bp not in [s[0] for s in specs]:
            specs.append((bp, 1.0))
        L = lead_times(cases_dir, T, specs)
        if len(L):
            lead_report(L)
            line("\nЭто и есть честный ответ про «примерные даты»: не дата, а окно,")
            line("и часть наблюдений — «не меньше чем», а не точное число.")
        else:
            line("Не удалось посчитать ни одного лид-тайма")

    line()
    line("=" * 78)
    line("ИТОГ")
    line("=" * 78)
    a_best, lo_best, hi_best = res[best]
    # какой эффект мы вообще были в состоянии заметить на такой выборке
    n1, n2 = int(y.sum()), int((y == 0).sum())
    def _power(d):
        nc = d * np.sqrt(n1 * n2 / (n1 + n2))
        crit = stats.t.ppf(0.975, n1 + n2 - 2)
        return 1 - stats.nct.cdf(crit, n1 + n2 - 2, nc) + stats.nct.cdf(-crit, n1 + n2 - 2, nc)
    grid = np.arange(0.05, 4.0, 0.01)
    d_min = next((d for d in grid if _power(d) >= 0.8), None)

    # парный тест — основной для этого дизайна, непарный AUC его недооценивает
    if P_paired is not None:
        row = P_paired.sort_values("p").iloc[0]
        thr = 0.05 / len(P_paired)
        fe = fixed_effects(T, row.признак)
        артефакт = fe is not None and fe["год"][2] < 0.05 and \
                   np.sign(fe["год"][0]) == np.sign(fe["война"][0])
        line(f"ПАРНОЕ СРАВНЕНИЕ (основное для этого дизайна): сильнее всего "
             f"отличается {row.признак}.")
        line(f"   у {int(row.выше)} войн из {int(row.всего)} он выше, чем у своего "
             f"контроля, p = {row.p:.5f}, размер эффекта d = {row.d:.2f}")
        if row.p < thr and not артефакт:
            line("   поправку Бонферрони проходит, календарным трендом не объясняется")
        elif артефакт:
            line("   ОСТОРОЖНО: эффект может быть календарным трендом, а не войной")
        else:
            line("   поправку на множественные сравнения не проходит")
        line()

    есть_сигнал = (lo_best > 0.5) and (perm_p is None or perm_p < 0.05)
    if есть_сигнал:
        line(f"СИГНАЛ ЕСТЬ. Лучший вариант — {best}, AUC = {a_best:.3f} "
             f"[{lo_best:.3f}; {hi_best:.3f}].")
        line(f"Это значит: взяв наугад одно предвоенное окно и одно спокойное, мы правильно")
        line(f"угадаем, какое из них предвоенное, в {a_best*100:.0f}% случаев.")
        line("Нижняя граница интервала выше 0.5, плацебо-тест пройден.")
        for base, txt in [(1/50, "1 эскалация на 50 пара-недель"), (1/20, "1 на 20")]:
            mu = stats.norm.ppf(min(a_best, 0.999)) * np.sqrt(2)
            thr = mu - stats.norm.ppf(0.85)
            fpr = stats.norm.cdf(-thr)
            prec = 0.85 * base / (0.85 * base + fpr * (1 - base))
            line(f"   при частоте «{txt}»: ловим 85% войн, из 100 тревог настоящих "
                 f"{prec*100:.0f} (обычный уровень — {base*100:.0f})")
    else:
        line(f"СИГНАЛ НЕ ПОДТВЕРЖДЁН. Лучший вариант — {best}, AUC = {a_best:.3f} "
             f"[{lo_best:.3f}; {hi_best:.3f}].")
        if lo_best <= 0.5:
            line("Доверительный интервал накрывает 0.5 — результат не отличается от")
            line("подбрасывания монетки. Чаще всего это значит, что кейсов слишком мало.")
        elif perm_p is not None and perm_p >= 0.05:
            line("Интервал выше 0.5, но плацебо-тест не пройден: такой же результат")
            line("получается и на перемешанных метках. Доверять ему нельзя.")
        else:
            line("Плацебо-тест не проводился — запустите без --no-permutation.")
    if d_min is not None:
        line(f"\nНа выборке {n1} войн и {n2} контролей мы способны заметить эффект "
             f"от d = {d_min:.2f} и выше.")
        line("Более слабый сигнал эта выборка не различит — и это ограничение данных,")
        line("а не метода.")

    os.makedirs(out_dir, exist_ok=True)
    T.to_csv(os.path.join(out_dir, "features.csv"), index=False, encoding="utf-8-sig")
    P = T[["пара", "день_X", "метка", "название"]].copy()
    P["балл_модели"] = s_model
    P["z_tone"] = T.z_tone.values
    P.sort_values("балл_модели", ascending=False).to_csv(
        os.path.join(out_dir, "predictions.csv"), index=False, encoding="utf-8-sig")
    summary = dict(кейсов=len(T), войн=int(y.sum()), пар=int(T.пара.nunique()),
                   AUC_модели=float(a), AUC_ci=[float(lo), float(hi)],
                   AUC_z_tone=float(res["сырой z_tone"][0]),
                   плацебо_p=perm_p, лучший=best)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    if L is not None:
        L.to_csv(os.path.join(out_dir, "lead_times.csv"), index=False, encoding="utf-8-sig")
    line(f"\nФайлы: {out_dir}/features.csv, predictions.csv, summary.json")
    return summary


# --------------------------------------------------------------------------- #
#  6. Самопроверка: синтетика с заранее известным ответом
# --------------------------------------------------------------------------- #
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


def make_fake_case(path, day_x, war, effect, rng, days=60):
    """Синтетический кейс: у «войны» последние 7 дней тон хуже на effect сигм."""
    dates = pd.date_range(pd.Timestamp(day_x) - pd.Timedelta(days=days),
                          pd.Timestamp(day_x) - pd.Timedelta(days=1))
    lam = rng.uniform(60, 400)          # своя интенсивность новостей у каждой пары
    base = rng.uniform(-6, -2)          # свой обычный тон
    day_sd = 0.8                        # разброс дневных средних (это и есть масштаб z)
    rows = []
    for i, dt in enumerate(dates):
        n = max(5, int(rng.poisson(lam)))
        shift = effect * day_sd if (war and i >= len(dates) - 7) else 0.0
        tone = rng.normal(base - shift, day_sd) + rng.normal(0, 8, n)
        gold = rng.normal(-2, 4, n)
        quad = rng.choice([1, 2, 3, 4], n, p=[.35, .25, .25, .15])
        root = rng.choice([4, 5, 17, 18, 19], n, p=[.3, .3, .2, .1, .1])
        for j in range(n):
            r = [""] * 58
            r[0] = str(rng.integers(1, 10**9)); r[1] = dt.strftime("%Y%m%d")
            r[7] = "AAA"; r[17] = "BBB"
            r[26] = "190"; r[28] = str(root[j]); r[29] = str(quad[j])
            r[30] = f"{gold[j]:.1f}"; r[33] = "3"; r[34] = f"{tone[j]:.3f}"
            r[56] = dt.strftime("%Y%m%d") + "000000"; r[57] = f"http://x/{i}_{j}"
            rows.append(r)
    pd.DataFrame(rows, columns=GDELT_COLUMNS).to_csv(path, index=False)


def self_test(tmp: str, effect: float, n_pairs=20, seed=7):
    """Строит синтетическую папку кейсов с известным эффектом и гоняет весь путь."""
    rng = np.random.default_rng(seed)
    os.makedirs(tmp, exist_ok=True)
    rows = []
    for i in range(n_pairs):
        pair = f"P{i:02d}-Q{i:02d}"
        for war, day_x in [(1, f"2024-0{(i % 9) + 1}-15"), (0, f"2022-0{(i % 9) + 1}-15")]:
            name = f"{pair}_{day_x}" + ("" if war else "_control") + ".csv"
            make_fake_case(os.path.join(tmp, name), day_x, bool(war), effect, rng)
            rows.append(dict(пара=pair, день_X=day_x, метка=war,
                             название="синтетика", файл=name))
    pd.DataFrame(rows).to_csv(os.path.join(tmp, "case_list.csv"),
                              index=False, encoding="utf-8-sig")
    T = build_table(tmp)
    return T


def main():
    ap = argparse.ArgumentParser(description="Обучение и честная проверка на кейсах")
    ap.add_argument("cases", nargs="?", help="папка, сделанная collect_cases.py")
    ap.add_argument("--out", default="report", help="куда класть результаты")
    ap.add_argument("--self-test", action="store_true",
                    help="прогнать машинку на синтетике с известным ответом")
    ap.add_argument("--effect", type=float, default=1.0,
                    help="для самопроверки: заложенный сдвиг тона в сигмах")
    ap.add_argument("--no-permutation", action="store_true", help="пропустить плацебо-тест")
    ap.add_argument("--no-lead", action="store_true", help="пропустить расчёт лид-тайма")
    args = ap.parse_args()
    print(ВЕРСИЯ + "\n")

    if args.self_test:
        import tempfile, shutil
        tmp = tempfile.mkdtemp(prefix="selftest_")
        try:
            for eff, ожидание in [(args.effect, "сигнал ДОЛЖЕН найтись"),
                                  (0.0, "сигнала НЕТ, машинка должна это показать")]:
                line("\n" + "#" * 78)
                line(f"# САМОПРОВЕРКА: заложенный сдвиг тона = {eff} сигмы — {ожидание}")
                line("#" * 78)
                d = os.path.join(tmp, f"eff{eff}")
                T = self_test(d, eff)
                report(T, d, os.path.join(args.out, f"selftest_{eff}"),
                       do_perm=not args.no_permutation, do_lead=False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return

    if not args.cases:
        sys.exit("Укажите папку с кейсами или запустите с --self-test")
    T = build_table(args.cases)
    if not len(T):
        sys.exit("Не удалось собрать ни одного кейса с признаками")
    report(T, args.cases, args.out,
           do_perm=not args.no_permutation, do_lead=not args.no_lead)


if __name__ == "__main__":
    main()
