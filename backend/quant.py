"""
Motor cuantitativo para análisis de notas estructuradas.

Todo lo computable se calcula acá (sin IA): descarga de datos de mercado vía
yfinance, volatilidad realizada, beta, correlaciones, drawdowns, probabilidad de
quiebre de barrera (ventanas históricas + Monte Carlo GBM), probabilidad de
autocall, vida esperada, retorno esperado (TIR) y tabla de payoff.

Lógica por default: worst-of. Barrera americana (continua) o europea (vencimiento).
Cupones fijos / condicionales / con memoria.
"""

from __future__ import annotations

import os
import math
import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

warnings.simplefilter("ignore")

PERIODS = {"mensual": 12, "trimestral": 4, "semestral": 2, "anual": 1}
TRADING_DAYS = 252


# --------------------------------------------------------------------------- #
# Descarga de datos
# --------------------------------------------------------------------------- #
def _download(tickers: list[str], period: str = "max") -> pd.DataFrame:
    import yfinance as yf

    data = yf.download(
        tickers,
        period=period,
        interval="1d",
        auto_adjust=True,  # ajustado por dividendos y splits ~ total return
        progress=False,
        group_by="column",
        threads=True,
    )
    if isinstance(data.columns, pd.MultiIndex):
        close = data["Close"].copy()
    else:
        close = data[["Close"]].copy()
        close.columns = [tickers[0]]
    close = close.dropna(how="all")
    return close


def _annualized_vol(daily_log: pd.Series, window: int) -> float:
    s = daily_log.dropna().tail(window)
    if len(s) < max(5, window // 2):
        return float("nan")
    return float(s.std(ddof=1) * math.sqrt(TRADING_DAYS))


def _max_drawdown(prices: pd.Series) -> float:
    p = prices.dropna()
    if p.empty:
        return float("nan")
    roll_max = p.cummax()
    dd = p / roll_max - 1.0
    return float(dd.min())


def _fundamentals(ticker: str) -> dict:
    """Datos fundamentales SOLO de Yahoo Finance (yfinance .info)."""
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:  # noqa: BLE001
        return {}

    def g(k):
        v = info.get(k)
        return float(v) if isinstance(v, (int, float)) else None

    total_debt, ebitda = g("totalDebt"), g("ebitda")
    deuda_ebitda = round(total_debt / ebitda, 2) if (total_debt and ebitda) else None
    rg, eg = g("revenueGrowth"), g("earningsGrowth")
    # dividend yield CONFIABLE: dividendo anual ($) / precio. El campo dividendYield de Yahoo
    # viene inconsistente (a veces fracción, a veces %), así que no lo usamos.
    price = g("regularMarketPrice") or g("regularMarketPreviousClose") or g("previousClose")
    dr = g("trailingAnnualDividendRate")
    dy_pct = round(dr / price * 100, 2) if (dr and price) else None
    return {
        "sector": info.get("sector") or info.get("category"),
        "market_cap": g("marketCap"),
        "revenue": g("totalRevenue"),
        "pe_forward": g("forwardPE"),
        "pe_trailing": g("trailingPE"),
        "ev_ebitda": g("enterpriseToEbitda"),
        "peg": g("trailingPegRatio") or g("pegRatio"),
        "crec_ingresos_pct": round(rg * 100, 1) if rg is not None else None,
        "crec_ganancias_pct": round(eg * 100, 1) if eg is not None else None,
        "deuda_ebitda": deuda_ebitda,
        "dividend_yield_pct": dy_pct,
    }


def _round_deep(o, nd=2):
    """Redondea recursivamente todos los floats (para que el LLM y la UI nunca
    vean 0.0619290911...% sino 0.06%)."""
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, dict):
        return {k: _round_deep(v, nd) for k, v in o.items()}
    if isinstance(o, list):
        return [_round_deep(v, nd) for v in o]
    return o


def _parse_date(s):
    """Parsea 'YYYY-MM-DD' a Timestamp; None si está vacío o es un placeholder."""
    if not s or not isinstance(s, str) or s.strip().upper().startswith("YYYY"):
        return None
    try:
        return pd.Timestamp(s)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Simulación de la vida de la nota sobre una matriz worst-of (paths x dias)
# --------------------------------------------------------------------------- #
def _simulate(
    worst_of: np.ndarray,          # (N, dias+1), normalizado, columna 0 = 1.0
    obs_idx: list[int],            # índices de columna de cada fecha de observación (la última = vencimiento)
    ppy: int,                      # periodos por año
    per_coupon: float,             # cupón por periodo (fracción del nominal)
    coupon_type: str,              # fijo | condicional | memoria
    coupon_barrier: float,         # barrera de cupón (fracción)
    autocall_level: np.ndarray,    # (K,) nivel de autocall por observación (inf si no aplica)
    barrier_level: float,          # barrera de capital (fracción)
    barrier_type: str,             # americana | europea
    capital_protect: float | None, # piso de capital (fracción) o None
) -> dict[str, np.ndarray]:
    N = worst_of.shape[0]
    K = len(obs_idx)
    cf = np.zeros((N, K))
    alive = np.ones(N, dtype=bool)
    autocalled = np.zeros(N, dtype=bool)
    autocall_period = np.full(N, -1, dtype=int)
    mem = np.zeros(N)
    coupons_paid = np.zeros(N, dtype=int)
    coupons_possible = np.zeros(N, dtype=int)
    breached = np.zeros(N, dtype=bool)

    for i, di in enumerate(obs_idx):
        wo = worst_of[:, di]
        is_maturity = i == K - 1
        coupons_possible += alive.astype(int)

        # --- cupón ---
        if coupon_type == "fijo":
            amt = np.where(alive, per_coupon, 0.0)
            coupons_paid += alive.astype(int)
        else:
            cond = wo >= coupon_barrier
            pay_now = alive & cond
            if coupon_type == "memoria":
                amt = np.where(pay_now, per_coupon + mem, 0.0)
                mem = np.where(alive & ~cond, mem + per_coupon, mem)
                mem = np.where(pay_now, 0.0, mem)
            else:  # condicional
                amt = np.where(pay_now, per_coupon, 0.0)
            coupons_paid += pay_now.astype(int)
        cf[:, i] += amt

        if not is_maturity:
            # --- autocall ---
            ac = alive & (wo >= autocall_level[i])
            cf[ac, i] += 1.0
            autocall_period[ac] = i
            autocalled[ac] = True
            alive[ac] = False
        else:
            # --- redención a vencimiento ---
            mat = alive.copy()
            if barrier_type == "europea":
                b = wo < barrier_level
            else:  # americana: tocó la barrera en cualquier momento hasta el vto
                b = worst_of[:, : di + 1].min(axis=1) < barrier_level
            breached = b & mat
            loss_cap = np.minimum(wo, 1.0)
            if capital_protect is not None:
                loss_cap = np.maximum(loss_cap, capital_protect)
            cap = np.where(b, loss_cap, 1.0)
            cf[mat, i] += cap[mat]
            alive[mat] = False

    times = (np.arange(K) + 1) / ppy
    irr = _irr_vec(cf, times)
    total_return = cf.sum(axis=1) - 1.0
    life = np.where(
        autocalled,
        (autocall_period + 1) / ppy,
        K / ppy,
    )
    return {
        "irr": irr,
        "total_return": total_return,
        "autocalled": autocalled,
        "autocall_period": autocall_period,
        "breached": breached,
        "coupons_paid": coupons_paid,
        "coupons_possible": coupons_possible,
        "life": life,
        "final_wo": worst_of[:, obs_idx[-1]],
        "min_wo": worst_of[:, : obs_idx[-1] + 1].min(axis=1),
    }


def _irr_vec(cf: np.ndarray, times: np.ndarray, iters: int = 80) -> np.ndarray:
    """TIR (money-weighted) por path vía Newton vectorizado. Inversión = 1 en t=0."""
    r = np.full(cf.shape[0], 0.08)
    t = times[None, :]
    for _ in range(iters):
        base = 1.0 + r[:, None]
        base = np.clip(base, 1e-6, None)
        disc = base ** (-t)
        npv = -1.0 + (cf * disc).sum(axis=1)
        dnpv = (cf * (-t) * base ** (-t - 1)).sum(axis=1)
        dnpv = np.where(np.abs(dnpv) < 1e-9, -1e-9, dnpv)
        r_new = r - npv / dnpv
        r = np.clip(r_new, -0.9999, 10.0)
    return r


def _summary(sim: dict[str, np.ndarray], ppy: int, K: int) -> dict[str, Any]:
    irr = sim["irr"]
    tr = sim["total_return"]
    autocalled = sim["autocalled"]
    breached = sim["breached"]
    full_coupons = sim["coupons_paid"] == sim["coupons_possible"]
    cp = sim["coupons_possible"].astype(float)
    capture = np.where(cp > 0, sim["coupons_paid"] / cp, 0.0)

    def pct(x):
        return float(np.mean(x) * 100.0)

    q = lambda a, p: float(np.percentile(a, p))

    # Escenarios reales (buckets mutuamente excluyentes, suman 100%) calculados del MC
    maturity = ~autocalled
    masks = [
        (autocalled, "Autocall (cancelación anticipada)", "No",
         "Los subyacentes se mantienen sobre el nivel de autocall en una observación; se devuelve el capital más los cupones acumulados."),
        (maturity & ~breached, "A vencimiento sin tocar la barrera", "No",
         "No hubo autocall y el peor subyacente termina sobre la barrera de capital: capital íntegro más los cupones cobrados."),
        (maturity & breached, "A vencimiento rompiendo la barrera", "Sí",
         "El peor subyacente termina por debajo de la barrera de capital: hay pérdida de capital (se recibe el activo)."),
    ]
    escenarios = []
    for mask, nombre, barr, desc in masks:
        n = int(mask.sum())
        escenarios.append({
            "nombre": nombre,
            "prob": float(np.mean(mask) * 100.0),
            "retorno_tipico_pct": (float(np.median(tr[mask]) * 100.0) if n > 0 else None),
            "barrera_tocada": barr,
            "descripcion": desc,
        })

    out = {
        "escenarios": escenarios,
        "prob_quiebre_barrera": pct(breached),
        "prob_autocall_total": pct(autocalled),
        "prob_perdida": pct(tr < 0),
        "prob_cupon_completo": pct(full_coupons),
        "vida_esperada_anios": float(np.mean(sim["life"])),
        "tir_media": float(np.mean(irr)) * 100.0,
        "tir_mediana": float(np.median(irr)) * 100.0,
        "tir_p5": q(irr, 5) * 100.0,
        "tir_p25": q(irr, 25) * 100.0,
        "tir_p75": q(irr, 75) * 100.0,
        "tir_p95": q(irr, 95) * 100.0,
        "retorno_total_mediano": float(np.median(tr)) * 100.0,
        "retorno_total_media": float(np.mean(tr)) * 100.0,
        "retorno_total_p5": q(tr, 5) * 100.0,
        "retorno_total_p25": q(tr, 25) * 100.0,
        "retorno_total_p75": q(tr, 75) * 100.0,
        "retorno_total_p95": q(tr, 95) * 100.0,
        "captura_cupon_pct": float(np.mean(capture)) * 100.0,
        "worst_of_vto": {
            "min": q(sim["final_wo"], 0) * 100.0,
            "p5": q(sim["final_wo"], 5) * 100.0,
            "p25": q(sim["final_wo"], 25) * 100.0,
            "p50": q(sim["final_wo"], 50) * 100.0,
            "p75": q(sim["final_wo"], 75) * 100.0,
            "max": q(sim["final_wo"], 100) * 100.0,
        },
        "drawdown_peor_p5": q(sim["min_wo"], 5) * 100.0 - 100.0,
        "drawdown_peor_p50": q(sim["min_wo"], 50) * 100.0 - 100.0,
    }
    return _round_deep(out, 2)


def _autocall_by_period(sim: dict[str, np.ndarray], obs_dates: list[str]) -> list[dict]:
    ap = sim["autocall_period"]
    n = len(ap)
    out = []
    cum = 0.0
    for i, d in enumerate(obs_dates[:-1]):  # la última es vencimiento, no autocall
        p = float(np.mean(ap == i)) * 100.0
        cum += p
        out.append({"fecha": d, "prob_autocall": round(p, 2), "prob_acumulada": round(cum, 2)})
    return out


# --------------------------------------------------------------------------- #
# Construcción del calendario de observaciones
# --------------------------------------------------------------------------- #
def _obs_schedule(plazo: float, ppy: int) -> list[float]:
    """Fechas de observación en años (incluye el vencimiento como última)."""
    k = max(1, round(plazo * ppy))
    return [round((i + 1) / ppy, 4) for i in range(k)]


def _obs_indices(obs_years: list[float], plazo: float, dias: int) -> list[int]:
    return [min(dias, int(round(y / plazo * dias))) for y in obs_years]


_MESES_ES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _obs_calendar(as_of, obs_years: list[float]) -> list[str]:
    """Convierte observaciones (años desde hoy) en etiquetas de fecha 'mmm-aa'."""
    out = []
    for y in obs_years:
        d = as_of + pd.Timedelta(days=y * 365.25)
        out.append(f"{_MESES_ES[d.month - 1]}-{str(d.year)[2:]}")
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_quant(terms: dict, mc_paths: int | None = None, seed: int | None = None) -> dict:
    mc_paths = mc_paths or int(os.getenv("MC_PATHS", "10000"))
    seed = seed if seed is not None else int(os.getenv("MC_SEED", "42"))
    warns: list[str] = []

    plazo = float(terms.get("plazo_anios") or 1.0)
    moneda = terms.get("moneda", "USD")
    subs = terms.get("subyacentes") or []
    tickers = [s.get("ticker_yf") or s.get("ticker") for s in subs if (s.get("ticker_yf") or s.get("ticker"))]
    tickers = [t for t in tickers if t]
    if not tickers:
        raise ValueError("No hay tickers válidos en el termsheet.")

    cup = terms.get("cupon") or {}
    ppy = PERIODS.get((cup.get("frecuencia") or "trimestral").lower(), 4)
    tasa = float(cup.get("tasa_anual") or 0.0)
    per_coupon = tasa / ppy
    coupon_type = (cup.get("tipo") or "condicional").lower()
    coupon_barrier = float(cup.get("barrera_cupon_pct") or 0.0)

    bar = terms.get("barrera_capital") or {}
    barrier_level = float(bar.get("nivel_pct") or 0.6)
    barrier_type = (bar.get("tipo") or "europea").lower()

    ac = terms.get("autocall") or {}
    has_ac = bool(ac.get("tiene"))
    ac_level0 = float(ac.get("nivel_autocall_pct") or 1.0)
    step = ac.get("step_down")
    step = float(step) if step not in (None, "", 0) else 0.0

    cap_prot = terms.get("capital_protegido_pct")
    cap_prot = float(cap_prot) if cap_prot not in (None, "") else None

    logica = (terms.get("logica_basket") or "worst-of").lower()
    if "worst" not in logica and len(tickers) > 1:
        warns.append(f"Lógica de basket '{logica}' no es worst-of; el modelo usa worst-of. Revisá manualmente.")

    # --- datos ---
    period_req = "max"
    close = _download(tickers + ["^GSPC"], period=period_req)
    # Un ticker inválido puede venir como columna toda-NaN: lo tratamos como faltante.
    have = [t for t in tickers if t in close.columns and close[t].notna().any()]
    missing = [t for t in tickers if t not in have]
    benchmark_ok = "^GSPC" in close.columns and close["^GSPC"].notna().any()
    if missing:
        if not benchmark_ok:
            # Ni el índice de referencia bajó → no es el ticker, es Yahoo.
            raise ValueError(
                "No se pudo conectar con Yahoo Finance desde este servidor (no bajó ni el índice de "
                "referencia). Suele pasar en hosting cloud (Render y similares), donde Yahoo bloquea o "
                "limita las IP de datacenter. Corré la app en local / por el link del túnel, donde sí funciona."
            )
        raise ValueError(
            f"No se pudieron bajar datos de mercado de: {', '.join(missing)}. "
            "Revisá el ticker de Yahoo Finance en el paso de revisión "
            "(p. ej. Tempus AI es 'TEM', no 'TEMPUS')."
        )

    px = close[have].dropna()
    spx = close["^GSPC"].reindex(px.index).ffill() if "^GSPC" in close.columns else None
    log_ret = np.log(px / px.shift(1)).dropna()

    n_years_hist = (px.index[-1] - px.index[0]).days / 365.25

    # --- métricas por subyacente ---
    per_asset = {}
    for t in have:
        lr = log_ret[t]
        beta = float("nan")
        if spx is not None:
            spx_ret = np.log(spx / spx.shift(1)).reindex(lr.index).dropna()
            common = lr.reindex(spx_ret.index).dropna()
            sp = spx_ret.reindex(common.index)
            if len(common) > 30 and sp.var() > 0:
                beta = float(np.cov(common.tail(TRADING_DAYS), sp.tail(TRADING_DAYS))[0, 1] / sp.tail(TRADING_DAYS).var(ddof=1))
        cur = float(px[t].iloc[-1])
        sub_meta = next((s for s in subs if (s.get("ticker_yf") or s.get("ticker")) == t), {})
        strike = sub_meta.get("strike_inicial")
        strike = float(strike) if strike not in (None, "", 0) else cur
        per_asset[t] = {
            "precio_actual": cur,
            "strike_inicial": strike,
            "nivel_actual_pct": round(cur / strike * 100.0, 2),
            "vol_30d": round(_annualized_vol(lr, 30) * 100.0, 2),
            "vol_90d": round(_annualized_vol(lr, 90) * 100.0, 2),
            "vol_1y": round(_annualized_vol(lr, TRADING_DAYS) * 100.0, 2),
            "beta_spx": round(beta, 2) if not math.isnan(beta) else None,
            "max_drawdown_3y": round(_max_drawdown(px[t].tail(TRADING_DAYS * 3)) * 100.0, 2),
            "dist_a_barrera_capital_pct": round((cur / strike - barrier_level) * 100.0, 2),
            "dist_a_barrera_cupon_pct": round((cur / strike - coupon_barrier) * 100.0, 2) if coupon_barrier else None,
            "fundamentals": _fundamentals(t),
        }

    # --- series históricas normalizadas al strike (backtest de quiebres de barrera) ---
    series = {}
    barr_pct = barrier_level * 100.0
    for t in have:
        s = (px[t].dropna() / per_asset[t]["strike_inicial"] * 100.0).tail(TRADING_DAYS * 5)
        stepn = max(1, len(s) // 140)
        s = s.iloc[::stepn]
        series[t] = {
            "fechas": [d.strftime("%b-%y") for d in s.index],
            "niveles": [round(float(v), 1) for v in s.to_numpy()],
            "pct_bajo_barrera": round(float((s < barr_pct).mean() * 100.0), 1),
        }

    # --- correlaciones (5 años) ---
    corr = None
    if len(have) > 1:
        c = log_ret[have].tail(TRADING_DAYS * 5).corr()
        corr = {a: {b: round(float(c.loc[a, b]), 2) for b in have} for a in have}

    # --- horizonte: nota fresca vs. ya emitida (seasoned) ---
    as_of = px.index[-1]
    mat = _parse_date(terms.get("fecha_vencimiento"))
    strike_dt = _parse_date(terms.get("fecha_strike")) or _parse_date(terms.get("fecha_emision"))
    if mat is not None:
        t_rem = (mat - as_of).days / 365.25
        t_rem = min(max(t_rem, 1.0 / 365.25), plazo + 1e-6)
    else:
        t_rem = plazo
    seasoned = mat is not None and t_rem < plazo - 0.02
    if seasoned:
        warns.append(
            f"Nota ya emitida: se valúa la vida remanente ({t_rem:.2f} años, vence {mat.date()}) "
            "con los niveles actuales relativos al strike original."
        )
    # períodos ya transcurridos (para step-down y período de no-call)
    if seasoned and strike_dt is not None:
        elapsed = int(round((as_of - strike_dt).days / 365.25 * ppy))
    else:
        elapsed = 0
    # nivel actual de cada subyacente relativo a su strike (1.0 = fresca / en el strike)
    level0 = np.array([per_asset[t]["nivel_actual_pct"] / 100.0 for t in have])
    if n_years_hist < 2 * t_rem:
        warns.append(
            f"Los subyacentes tienen poca historia de mercado ({n_years_hist:.1f} años de cotización disponible). "
            "Por eso el método de ventanas históricas es menos representativo; el Monte Carlo no se ve afectado."
        )

    # --- calendario (sobre la vida remanente) ---
    obs_years = _obs_schedule(t_rem, ppy)
    obs_cal = _obs_calendar(as_of, obs_years)
    K = len(obs_years)
    # primera observación de autocall como índice global (período de no-call)
    first_call_global = 0
    fobs = _parse_date((terms.get("autocall") or {}).get("primera_fecha_obs"))
    if has_ac and fobs is not None and strike_dt is not None:
        first_call_global = max(0, int(round((fobs - strike_dt).days / 365.25 * ppy)) - 1)
    autocall_level = np.array([
        (ac_level0 - (elapsed + i) * step) if (has_ac and (elapsed + i) >= first_call_global) else np.inf
        for i in range(K)
    ])

    # --- tasa libre de riesgo (T-bill 3m) — se usa como deriva neutral del Monte Carlo ---
    rf = None
    try:
        irx = _download(["^IRX"], period="1mo")
        rf = round(float(irx.iloc[-1, 0]), 2)  # ^IRX ya viene en %
    except Exception:  # noqa: BLE001
        warns.append("No se pudo obtener la tasa libre de riesgo (^IRX); se usó deriva 0 en el Monte Carlo.")

    # ===================================================================== #
    # MÉTODO 1: ventanas históricas
    # ===================================================================== #
    hist_summary, hist_autocall = None, None
    win_days = int(round(t_rem * TRADING_DAYS))
    if len(px) > win_days + 30:
        norm_assets = []
        for i, t in enumerate(have):
            arr = px[t].to_numpy()
            sw = np.lib.stride_tricks.sliding_window_view(arr, win_days + 1)
            sw = sw / sw[:, [0]] * level0[i]   # relativo al strike: arranca en el nivel actual
            norm_assets.append(sw)
        wo_hist = np.min(np.stack(norm_assets, axis=0), axis=0)  # (n_win, win_days+1)
        obs_idx = _obs_indices(obs_years, t_rem, win_days)
        sim_h = _simulate(
            wo_hist, obs_idx, ppy, per_coupon, coupon_type, coupon_barrier,
            autocall_level, barrier_level, barrier_type, cap_prot,
        )
        hist_summary = _summary(sim_h, ppy, K)
        hist_summary["n_ventanas"] = int(wo_hist.shape[0])
        hist_autocall = _autocall_by_period(sim_h, obs_cal)
    else:
        warns.append("Historia insuficiente para el método de ventanas móviles.")

    # ===================================================================== #
    # MÉTODO 2: Monte Carlo GBM multivariado
    # ===================================================================== #
    lr_mat = log_ret[have].to_numpy()
    # winsorizar outliers extremos
    lo, hi = np.percentile(lr_mat, [0.25, 99.75], axis=0)
    lr_mat = np.clip(lr_mat, lo, hi)
    # Deriva NEUTRAL AL RIESGO: usamos la tasa libre, NO la media histórica reciente
    # (un subyacente que viene cayendo no debe asumirse que sigue cayendo).
    rf_daily = (rf / 100.0) / TRADING_DAYS if rf is not None else 0.0
    mu_daily = np.full(lr_mat.shape[1], rf_daily)
    cov_daily = np.cov(lr_mat, rowvar=False)
    if cov_daily.ndim == 0:
        cov_daily = np.array([[float(cov_daily)]])
    try:
        chol = np.linalg.cholesky(cov_daily)
    except np.linalg.LinAlgError:
        cov_daily = cov_daily + np.eye(len(have)) * 1e-10
        chol = np.linalg.cholesky(cov_daily)

    rng = np.random.default_rng(seed)
    days = int(round(t_rem * TRADING_DAYS))
    obs_idx_mc = _obs_indices(obs_years, t_rem, days)
    n_assets = len(have)

    chunk = 2500
    parts: list[dict] = []
    done = 0
    while done < mc_paths:
        m = min(chunk, mc_paths - done)
        z = rng.standard_normal((m, days, n_assets))
        shocks = z @ chol.T  # correlacionar
        drift = (mu_daily - 0.5 * np.diag(cov_daily))[None, None, :]
        log_paths = np.cumsum(drift + shocks, axis=1)
        # arranca en el nivel actual relativo al strike (level0); fresca => level0=1
        prices = level0[None, None, :] * np.exp(log_paths)
        start = np.broadcast_to(level0, (m, 1, n_assets))
        prices = np.concatenate([start, prices], axis=1)
        wo_mc = prices.min(axis=2)  # (m, days+1)
        sim = _simulate(
            wo_mc, obs_idx_mc, ppy, per_coupon, coupon_type, coupon_barrier,
            autocall_level, barrier_level, barrier_type, cap_prot,
        )
        parts.append(sim)
        done += m

    sim_mc = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    mc_summary = _summary(sim_mc, ppy, K)
    mc_summary["n_paths"] = int(mc_paths)
    mc_autocall = _autocall_by_period(sim_mc, obs_cal)

    # histograma del worst-of al vencimiento (MC) para graficar
    hist_counts, hist_edges = np.histogram(sim_mc["final_wo"] * 100.0, bins=40)
    wo_hist_chart = {
        "counts": hist_counts.tolist(),
        "edges": [round(float(e), 1) for e in hist_edges],
    }
    # muestra de paths para graficar
    sample_idx = rng.choice(len(sim_mc["final_wo"]), size=min(40, len(sim_mc["final_wo"])), replace=False)

    # ===================================================================== #
    # Tabla de payoff a vencimiento (nota vs inversión directa worst-of)
    # ===================================================================== #
    grid = [0.40, 0.20, 0.10, 0.00, -0.10, -0.30, -0.50, -0.70]
    payoff = []
    total_coupons_if_held = per_coupon * K  # si paga todos (aprox techo)
    for g in grid:
        final = 1.0 + g
        if final >= barrier_level:
            nota_cap = 1.0
        else:
            nota_cap = max(final, cap_prot) if cap_prot is not None else final
        # cupones: si worst-of final >= barrera cupón asumimos cobro pleno (aprox)
        cupones = total_coupons_if_held if (coupon_type == "fijo" or final >= coupon_barrier) else 0.0
        nota_ret = (nota_cap + cupones - 1.0) * 100.0
        directo_ret = g * 100.0
        payoff.append({
            "worst_of_final_pct": round(final * 100, 1),
            "retorno_directo_pct": round(directo_ret, 1),
            "retorno_nota_pct": round(nota_ret, 1),
            "nota_supera": nota_ret > directo_ret,
        })

    return {
        "meta": {
            "tickers": have,
            "moneda": moneda,
            "plazo_anios": plazo,
            "vida_remanente_anios": round(t_rem, 2),
            "seasoned": bool(seasoned),
            "fecha_vencimiento": str(mat.date()) if mat is not None else None,
            "periodos_por_anio": ppy,
            "n_observaciones": K,
            "historia_anios": round(n_years_hist, 1),
            "tasa_libre_riesgo_3m_pct": rf,
            "mc_paths": mc_paths,
            "seed": seed,
            "fecha_datos": str(px.index[-1].date()),
        },
        "subyacentes": per_asset,
        "series_historicas": series,
        "correlaciones": corr,
        "barreras": {
            "capital": {"nivel_pct": barrier_level * 100, "tipo": barrier_type},
            "cupon": {"nivel_pct": coupon_barrier * 100 if coupon_barrier else None, "tipo": coupon_type},
            "autocall": {"nivel_pct": ac_level0 * 100 if has_ac else None, "step_down": step if step else None},
        },
        "historico": {"resumen": hist_summary, "autocall_por_fecha": hist_autocall},
        "montecarlo": {"resumen": mc_summary, "autocall_por_fecha": mc_autocall},
        "grafico_worst_of_vto": wo_hist_chart,
        "tabla_payoff": payoff,
        "warnings": warns,
    }
