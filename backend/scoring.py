"""
Motor de veredicto determinístico para notas estructuradas.

A diferencia de las fases LLM (narrativa, cualitativas), este módulo produce un
veredicto REPRODUCIBLE y EXPLICABLE — el mismo input da siempre el mismo score —
combinando los términos de la nota, el resultado del cuantitativo (quant.py) y el
perfil del cliente. NO usa IA ni la API paga.

Modelo de 3 pilares (pesos en CONFIG["pesos_pilares"]):
  1. Riesgo / Retorno (50%) — colchón y estructura de barrera, prob. de quiebre y
     pérdida esperada, crédito del emisor, cupón vs. riesgo.
  2. Valor relativo (25%) — cupón vs. tasa libre de riesgo, costo embebido (proxy),
     liquidez secundaria.
  3. Idoneidad / suitability (25%) — encaje con el perfil de riesgo, horizonte vs.
     plazo, concentración y necesidad de liquidez del cliente.

Sobre el score ponderado se aplican REGLAS DURAS (red flags): las "descalificantes"
ponen un techo al score (fuerzan NEUTRAL o EVITAR) por más que los pilares den bien;
las "alertas" se listan pero no topean.

Todos los umbrales viven en CONFIG para que se calibren sin tocar la lógica.
Todos los textos en español.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# Calibración — todo lo ajustable vive acá.
# --------------------------------------------------------------------------- #
CONFIG: dict[str, Any] = {
    "pesos_pilares": {"riesgo_retorno": 0.50, "valor_relativo": 0.25, "suitability": 0.25},
    # Pesos internos de cada pilar (suman 1 dentro del pilar).
    "pesos_rr": {"colchon": 0.30, "prob_perdida": 0.30, "credito": 0.25, "cupon_riesgo": 0.15},
    "pesos_vr": {"retorno_vs_rf": 0.45, "captura_cupon": 0.30, "liquidez": 0.25},
    "pesos_su": {"perfil": 0.40, "horizonte": 0.30, "concentracion": 0.30},
    # Mapeo score -> semáforo.
    "umbral_invertir": 70,
    "umbral_neutral": 40,
    # Breakpoints (bueno -> 100, malo -> 0) de cada métrica.
    "bp_barrera_nivel": (50.0, 80.0),        # nivel barrera capital en %, más bajo = más colchón
    "bp_buffer_actual": (45.0, 5.0),         # distancia actual a barrera en pp
    "penalty_americana": 0.70,               # factor sobre el colchón si la barrera es continua
    "bp_prob_quiebre": (2.0, 35.0),          # prob. quiebre barrera capital en %
    "bp_prob_perdida": (5.0, 45.0),          # prob. de terminar con pérdida en %
    "bp_spread_cupon": (6.0, 0.0),           # cupón anual - tasa libre de riesgo, en pp
    "bp_excess_total": (10.0, -10.0),        # retorno total esperado - tasa libre compuesta del horizonte, en pp
    "liquidez_score": {"cotiza": 72.0, "iliquida": 35.0},
    "bp_concentracion": (5.0, 25.0),         # % de cartera en la nota
    "rf_fallback": 4.5,                      # tasa libre de riesgo si el quant no la trae
    # Crédito del emisor: notch S&P (mayor = mejor) -> score.
    "bp_rating_notch": (17, 10),             # A -> 100, BB- -> 0
    "bp_cds": (40.0, 300.0),                 # CDS en bps, menor = mejor
    "ig_floor_notch": 13,                    # BBB-
    "credito_neutral": 55.0,                 # si no hay dato de crédito
    # Red flags (descalificantes -> techo de score).
    "cap_prob_quiebre": 40.0,                # prob. quiebre > esto => techo EVITAR
    "techo_evitar": 38,
    "cap_cds": 300.0,
    "cap_spread_minimo": 1.5,                # cupón - rf por debajo => "no pagan el riesgo"
    "techo_neutral": 55,
    "triple_corr": 0.35,                     # corr. promedio worst-of por debajo
    "triple_buffer": 20.0,                   # colchón actual mínimo en pp
    "alerta_concentracion": 20.0,            # % de cartera
}

RATING_NOTCH = {
    "AAA": 22, "AA+": 21, "AA": 20, "AA-": 19, "A+": 18, "A": 17, "A-": 16,
    "BBB+": 15, "BBB": 14, "BBB-": 13, "BB+": 12, "BB": 11, "BB-": 10,
    "B+": 9, "B": 8, "B-": 7, "CCC+": 6, "CCC": 5, "CCC-": 4, "CC": 3, "C": 2, "D": 1,
    # equivalencias Moody's frecuentes
    "AA1": 21, "AA2": 20, "AA3": 19, "A1": 18, "A2": 17, "A3": 16,
    "BAA1": 15, "BAA2": 14, "BAA3": 13, "BA1": 12, "BA2": 11, "BA3": 10,
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _lin(x: float | None, x_good: float, x_bad: float) -> float | None:
    """Interpola 0–100: x_good -> 100, x_bad -> 0, lineal y clampeado.

    Soporta métricas crecientes (x_good > x_bad) y decrecientes (x_good < x_bad).
    Devuelve None si x es None (métrica faltante).
    """
    if x is None:
        return None
    if x_good == x_bad:
        return 50.0
    return _clamp((x - x_bad) / (x_good - x_bad) * 100.0)


def _wavg(pairs: list[tuple[float | None, float]]) -> tuple[float, dict]:
    """Promedio ponderado ignorando componentes None y renormalizando sus pesos."""
    usable = [(v, w) for v, w in pairs if v is not None]
    if not usable:
        return 50.0, {}
    tot_w = sum(w for _, w in usable)
    score = sum(v * w for v, w in usable) / tot_w
    return score, {}


def _rating_notch(rating: str | None) -> int | None:
    if not rating:
        return None
    return RATING_NOTCH.get(rating.strip().upper().replace(" ", ""))


def _avg_corr(corr: dict | None) -> float | None:
    if not corr:
        return None
    keys = list(corr)
    vals = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            v = corr.get(a, {}).get(b)
            if v is not None:
                vals.append(float(v))
    return sum(vals) / len(vals) if vals else None


def _min_buffer(subs: dict) -> float | None:
    """Menor distancia actual a la barrera de capital entre los subyacentes (pp)."""
    vals = [s.get("dist_a_barrera_capital_pct") for s in subs.values()
            if s.get("dist_a_barrera_capital_pct") is not None]
    return min(vals) if vals else None


def _sub(nombre: str, score: float | None, detalle: str, explicacion: str = "") -> dict:
    return {"nombre": nombre, "score": None if score is None else round(score, 1),
            "detalle": detalle, "explicacion": explicacion}


# --------------------------------------------------------------------------- #
# Pilar 1 — Riesgo / Retorno
# --------------------------------------------------------------------------- #
def _pilar_riesgo_retorno(terms, quant, credito, ctx) -> dict:
    mc = (quant.get("montecarlo") or {}).get("resumen") or {}
    barreras = quant.get("barreras") or {}
    cap = barreras.get("capital") or {}
    subs = quant.get("subyacentes") or {}
    cfg = CONFIG
    subscores = []

    # a) Colchón y estructura de barrera
    nivel = cap.get("nivel_pct")
    tipo = (cap.get("tipo") or "europea").lower()
    s_nivel = _lin(nivel, *cfg["bp_barrera_nivel"])
    s_buffer = _lin(ctx["min_buffer"], *cfg["bp_buffer_actual"])
    base = _wavg([(s_nivel, 0.5), (s_buffer, 0.5)])[0]
    colchon = base * (cfg["penalty_americana"] if tipo == "americana" else 1.0)
    det_colchon = (f"Barrera de capital {nivel:.0f}% ({tipo})"
                   + (f", colchón actual mínimo {ctx['min_buffer']:.0f}pp" if ctx["min_buffer"] is not None else "")
                   + (" — barrera continua penaliza" if tipo == "americana" else ""))
    exp_colchon = (
        f"Mide cuánto margen hay antes de perder capital y la calidad de la barrera. La barrera de capital está "
        f"en {nivel:.0f}% del nivel inicial y se observa "
        + ("solo al vencimiento (europea, más benigna). " if tipo != "americana"
           else "de forma continua (americana, más exigente). ")
        + (f"Hoy el subyacente más cercano está a {ctx['min_buffer']:.0f} puntos de tocarla. " if ctx["min_buffer"] is not None else "")
        + "Cuanto más bajo el nivel, más continua la observación y menos colchón actual, peor el puntaje.")
    subscores.append(_sub("Colchón y estructura de barrera", colchon, det_colchon, exp_colchon))

    # b) Probabilidad de quiebre + pérdida esperada
    pq = mc.get("prob_quiebre_barrera")
    pp = mc.get("prob_perdida")
    s_quiebre = _lin(pq, *cfg["bp_prob_quiebre"])
    s_perdida = _lin(pp, *cfg["bp_prob_perdida"])
    prob = _wavg([(s_quiebre, 0.55), (s_perdida, 0.45)])[0]
    iguales = pq is not None and pp is not None and abs(pq - pp) < 0.5
    if iguales:
        det_prob = f"{pq:.1f}% de probabilidad de quiebre de barrera (= probabilidad de pérdida)"
        exp_extra = "En esta nota romper la barrera de capital es la única vía de pérdida, por eso ambas cifras coinciden. "
    else:
        det_prob = f"Prob. quiebre barrera {pq:.1f}% · prob. pérdida {pp:.1f}%"
        exp_extra = "La prob. de pérdida puede diferir de la de quiebre si hay tramos de capital protegido. "
    exp_prob = (
        "Probabilidad, estimada con simulación Monte Carlo (10.000 trayectorias de los subyacentes), de que el peor "
        "de ellos termine por debajo de la barrera de capital al vencimiento. " + exp_extra
        + "Cuanto mayor la probabilidad, peor el puntaje.")
    subscores.append(_sub("Probabilidad de quiebre y pérdida", prob, det_prob, exp_prob))

    # c) Crédito del emisor
    s_credito, det_credito = _credito_score(credito, ctx)
    exp_credito = (
        "La nota es deuda senior del emisor/garante: si el emisor incumple, se pierde aunque los subyacentes estén bien. "
        "El puntaje sale del rating crediticio cargado (a mejor rating, mejor puntaje). Sin rating se asume neutral.")
    subscores.append(_sub("Crédito del emisor", s_credito, det_credito, exp_credito))

    # d) Cupón vs. riesgo (contra la referencia: bono IG si se cargó YTW, si no T-bill)
    s_cupon = _lin(ctx["spread_cupon"], *cfg["bp_spread_cupon"])
    det_cupon = (f"Cupón {ctx['cupon_pct']:.2f}% vs. {ctx['ref_label']} {ctx['tasa_comp']:.2f}% "
                 f"(spread {ctx['spread_cupon']:+.2f}pp)")
    if ctx["usa_ref"]:
        exp_cupon = (
            f"Compara el cupón anual de la nota ({ctx['cupon_pct']:.2f}%) con el rendimiento de un bono investment grade "
            f"de referencia (YTW {ctx['tasa_comp']:.2f}%, p. ej. LQD) —la alternativa real del cliente—. El spread "
            f"({ctx['spread_cupon']:+.2f} puntos) es lo que paga de más la nota por su riesgo. Más spread, mejor puntaje.")
    else:
        exp_cupon = (
            f"Compara el cupón anual de la nota ({ctx['cupon_pct']:.2f}%) con la tasa libre de riesgo —letra del Tesoro de "
            f"EE.UU. a 3 meses (^IRX), {ctx['tasa_comp']:.2f}%—. El spread ({ctx['spread_cupon']:+.2f} puntos) es lo que paga "
            "de más por el riesgo. Tip: cargá el YTW de un bono IG (LQD) para comparar contra la alternativa real del cliente.")
    subscores.append(_sub("Cupón vs. riesgo", s_cupon, det_cupon, exp_cupon))

    score, _ = _wavg([
        (colchon, cfg["pesos_rr"]["colchon"]),
        (prob, cfg["pesos_rr"]["prob_perdida"]),
        (s_credito, cfg["pesos_rr"]["credito"]),
        (s_cupon, cfg["pesos_rr"]["cupon_riesgo"]),
    ])
    return {"key": "riesgo_retorno", "nombre": "Riesgo / Retorno",
            "peso": int(cfg["pesos_pilares"]["riesgo_retorno"] * 100),
            "score": round(score, 1), "sub": subscores}


def _credito_score(credito, ctx) -> tuple[float, str]:
    cfg = CONFIG
    notch = ctx["notch"]
    cds = credito.get("cds_bps") if credito else None
    s_rating = _lin(notch, *cfg["bp_rating_notch"]) if notch is not None else None
    s_cds = _lin(cds, *cfg["bp_cds"]) if cds is not None else None
    if s_rating is None and s_cds is None:
        return cfg["credito_neutral"], "Sin dato de crédito del emisor (supuesto neutral — cargar rating/CDS)"
    sc = _wavg([(s_rating, 0.6), (s_cds, 0.4)])[0]
    parts = []
    if notch is not None:
        parts.append(f"rating {credito.get('rating')}")
    if cds is not None:
        parts.append(f"CDS {cds:.0f} bps")
    return sc, "Emisor: " + ", ".join(parts)


# --------------------------------------------------------------------------- #
# Pilar 2 — Valor relativo
# --------------------------------------------------------------------------- #
def _pilar_valor_relativo(terms, quant, credito, ctx) -> dict:
    mc = (quant.get("montecarlo") or {}).get("resumen") or {}
    cfg = CONFIG
    subscores = []

    # a) Retorno total esperado vs. la referencia del horizonte (sin anualizar)
    rt = mc.get("retorno_total_mediano")
    excess = (rt - ctx["comp_total"]) if rt is not None else None
    s_ret = _lin(excess, *cfg["bp_excess_total"])
    nombre_ret = "Retorno esperado vs. " + ("bono IG" if ctx["usa_ref"] else "sin riesgo")
    det_ret = (f"Retorno total esperado {rt:.1f}% vs. {ctx['ref_label']} {ctx['comp_total']:.1f}% "
               f"en {ctx['t_rem']:.2f}a (exceso {excess:+.1f}pp)"
               if excess is not None else "Retorno esperado no disponible")
    exp_ret = (
        "Retorno TOTAL esperado de la nota: mediana de 10.000 simulaciones Monte Carlo, donde en cada escenario se suman "
        "los cupones efectivamente cobrados más la redención final (capital devuelto o pérdida) menos lo invertido. "
        f"Se compara con poner el capital en {ctx['ref_label']} durante el mismo período ({ctx['comp_total']:.1f}% en "
        f"{ctx['t_rem']:.2f} años). Usamos el retorno total y no la TIR anualizada porque en notas de pocos meses la "
        "anualización exagera las cifras (una pérdida moderada se ve como un -88% anual).")
    subscores.append(_sub(nombre_ret, s_ret, det_ret, exp_ret))

    # b) Captura del cupón esperada (qué fracción de los cupones se espera cobrar)
    cap = mc.get("captura_cupon_pct")
    exp_cap = (
        "Los cupones son condicionales: solo se pagan si los subyacentes están por encima de la barrera de cupón en cada "
        f"fecha de observación. Esta cifra ({cap:.0f}%) es la fracción de los cupones que, en promedio entre las simulaciones, "
        "se espera cobrar. Más alta, mejor.") if cap is not None else "Sin datos de cupón."
    subscores.append(_sub("Captura del cupón esperada", cap,
                          f"Se espera cobrar ~{cap:.0f}% de los cupones del período" if cap is not None else "n/d", exp_cap))

    # c) Liquidez secundaria
    cotiza = bool(credito.get("cotiza_secundario")) if credito else False
    s_liq = cfg["liquidez_score"]["cotiza" if cotiza else "iliquida"]
    exp_liq = (
        "Las notas estructuradas suelen tener mercado secundario limitado: si el cliente quiere salir antes del vencimiento, "
        "puede no haber comprador o el precio estar castigado. Se asume iliquidez salvo que se marque que cotiza en secundario.")
    subscores.append(_sub("Liquidez secundaria", s_liq,
                          "Cotiza en secundario" if cotiza else "Iliquidez típica de nota (mantener a vencimiento)", exp_liq))

    score, _ = _wavg([
        (s_ret, cfg["pesos_vr"]["retorno_vs_rf"]),
        (cap, cfg["pesos_vr"]["captura_cupon"]),
        (s_liq, cfg["pesos_vr"]["liquidez"]),
    ])
    return {"key": "valor_relativo", "nombre": "Valor relativo",
            "peso": int(cfg["pesos_pilares"]["valor_relativo"] * 100),
            "score": round(score, 1), "sub": subscores}


# --------------------------------------------------------------------------- #
# Pilar 3 — Idoneidad / suitability
# --------------------------------------------------------------------------- #
PERFIL_MATRIX = {
    ("conservador", "bajo"): 85, ("conservador", "medio"): 45, ("conservador", "alto"): 12,
    ("moderado", "bajo"): 78, ("moderado", "medio"): 76, ("moderado", "alto"): 42,
    ("agresivo", "bajo"): 62, ("agresivo", "medio"): 80, ("agresivo", "alto"): 86,
}


def _nivel_riesgo_nota(quant, ctx) -> tuple[str, float]:
    """Deriva el nivel de riesgo intrínseco de la nota (bajo/medio/alto)."""
    mc = (quant.get("montecarlo") or {}).get("resumen") or {}
    cap = (quant.get("barreras") or {}).get("capital") or {}
    pts = 0.0
    pp = mc.get("prob_perdida")
    if pp is not None:
        pts += 2 if pp > 25 else 1 if pp > 12 else 0
    if (cap.get("tipo") or "").lower() == "americana":
        pts += 1
    n = ctx["n_sub"]
    pts += 1 if n >= 3 else 0.5 if n == 2 else 0
    if ctx["avg_corr"] is not None and n > 1 and ctx["avg_corr"] < 0.5:
        pts += 1
    if not ctx["tiene_proteccion"]:
        pts += 0.5
    nivel = "alto" if pts > 3 else "medio" if pts >= 1.5 else "bajo"
    return nivel, pts


def _pilar_suitability(terms, quant, profile, ctx) -> dict:
    cfg = CONFIG
    subscores = []
    nivel, _ = _nivel_riesgo_nota(quant, ctx)
    perfil = (profile.get("perfil_riesgo") or "moderado").lower()

    # a) Encaje con el perfil de riesgo
    s_perfil = PERFIL_MATRIX.get((perfil, nivel), 50)
    exp_perfil = (
        f"Cruza el riesgo intrínseco de la nota (clasificado como {nivel}) con el perfil del cliente ({perfil}). "
        "Una nota de riesgo alto encaja con un perfil agresivo, pero no con uno conservador. El riesgo de la nota se "
        "deriva de la prob. de pérdida, el tipo de barrera, la cantidad de subyacentes y su correlación.")
    subscores.append(_sub("Encaje con el perfil", s_perfil,
                          f"Nota de riesgo {nivel} para perfil {perfil}", exp_perfil))

    # b) Horizonte vs. plazo
    plazo = ctx["plazo"]
    horizonte = profile.get("horizonte_anios")
    if horizonte:
        ratio = horizonte / plazo if plazo else 1.0
        s_hor = 100.0 if ratio >= 1 else _lin(ratio, 1.0, 0.5)
        det_hor = f"Horizonte {horizonte:g}a vs. plazo {plazo:g}a (vida esperada {ctx['vida_esp']:.1f}a)"
    else:
        s_hor = None
        det_hor = "Horizonte del cliente no informado"
    exp_hor = (
        "Compara el horizonte de inversión del cliente con el plazo (o vida remanente) de la nota. Si el horizonte es "
        "igual o mayor, encaja; si es menor, hay riesgo de que el cliente necesite la plata antes del vencimiento, "
        "cuando la nota puede estar ilíquida o en pérdida.")
    subscores.append(_sub("Horizonte vs. plazo", s_hor, det_hor, exp_hor))

    # c) Concentración y liquidez
    pct = profile.get("pct_cartera")
    s_conc = _lin(pct, *cfg["bp_concentracion"]) if pct is not None else None
    if s_conc is not None and profile.get("necesita_liquidez") and not ctx["cotiza"]:
        s_conc *= 0.6
    det_conc = (f"{pct:g}% de la cartera" if pct is not None else "Asignación no informada")
    if profile.get("necesita_liquidez") and not ctx["cotiza"]:
        det_conc += " · cliente necesita liquidez y la nota es ilíquida"
    exp_conc = (
        "Qué porción de la cartera del cliente se asigna a esta única nota. Asignaciones altas concentran el riesgo en un "
        "solo producto; penaliza más si además el cliente marcó que necesita liquidez y la nota es ilíquida. "
        "Como referencia, en general se sugiere no superar el 10% en una sola nota estructurada.")
    subscores.append(_sub("Concentración y liquidez", s_conc, det_conc, exp_conc))

    score, _ = _wavg([
        (s_perfil, cfg["pesos_su"]["perfil"]),
        (s_hor, cfg["pesos_su"]["horizonte"]),
        (s_conc, cfg["pesos_su"]["concentracion"]),
    ])
    return {"key": "suitability", "nombre": "Idoneidad (cliente)",
            "peso": int(cfg["pesos_pilares"]["suitability"] * 100),
            "score": round(score, 1), "sub": subscores, "_nivel_riesgo": nivel}


# --------------------------------------------------------------------------- #
# Red flags
# --------------------------------------------------------------------------- #
def _red_flags(terms, quant, profile, credito, ctx) -> tuple[list[dict], int]:
    """Devuelve (lista de flags, techo de score). El techo es el menor de los
    impuestos por las flags descalificantes (100 si no hay)."""
    cfg = CONFIG
    mc = (quant.get("montecarlo") or {}).get("resumen") or {}
    cap = (quant.get("barreras") or {}).get("capital") or {}
    flags: list[dict] = []
    techo = 100

    # Crédito sub-investment grade o CDS alto
    notch = ctx["notch"]
    cds = credito.get("cds_bps") if credito else None
    if notch is not None and notch < cfg["ig_floor_notch"]:
        flags.append(_flag("Emisor sub-investment grade", "alta", "descalificante",
                           f"Rating {credito.get('rating')} por debajo de BBB-: riesgo de crédito domina la nota."))
        techo = min(techo, cfg["techo_evitar"])
    if cds is not None and cds > cfg["cap_cds"]:
        flags.append(_flag("CDS del emisor elevado", "alta", "descalificante",
                           f"CDS {cds:.0f} bps > {cfg['cap_cds']:.0f}: el mercado descuenta estrés de crédito."))
        techo = min(techo, cfg["techo_evitar"])

    # Probabilidad de quiebre de capital alta
    pq = mc.get("prob_quiebre_barrera")
    if pq is not None and pq > cfg["cap_prob_quiebre"]:
        flags.append(_flag(
            "Prob. de quiebre de barrera alta", "alta", "descalificante",
            f"{pq:.1f}% de probabilidad de tocar la barrera de capital (umbral crítico: {cfg['cap_prob_quiebre']:.0f}%).",
            explicacion=(
                "Es la misma probabilidad que muestra el ítem 'Probabilidad de quiebre y pérdida' del pilar Riesgo/Retorno, "
                f"pero acá funciona como ALERTA CRÍTICA: al superar el {cfg['cap_prob_quiebre']:.0f}% consideramos el riesgo "
                "de pérdida demasiado alto y la calificación final se limita a zona desfavorable, sin importar cómo den los "
                "demás pilares.")))
        techo = min(techo, cfg["techo_evitar"])

    # Triple amenaza worst-of
    if (ctx["n_sub"] > 1 and ctx["avg_corr"] is not None and ctx["avg_corr"] < cfg["triple_corr"]
            and (cap.get("tipo") or "").lower() == "americana"
            and ctx["min_buffer"] is not None and ctx["min_buffer"] < cfg["triple_buffer"]):
        flags.append(_flag("Worst-of de baja correlación + barrera continua + colchón fino", "alta", "descalificante",
                           f"Correlación media {ctx['avg_corr']:.2f}, barrera americana y colchón {ctx['min_buffer']:.0f}pp: "
                           "combinación de máximo riesgo de quiebre."))
        techo = min(techo, cfg["techo_neutral"])

    # Cupón no compensa el riesgo
    if ctx["spread_cupon"] is not None and ctx["spread_cupon"] < cfg["cap_spread_minimo"]:
        flags.append(_flag("El cupón no paga el riesgo", "alta", "descalificante",
                           f"Cupón {ctx['cupon_pct']:.2f}% apenas supera la {ctx['ref_label']} {ctx['tasa_comp']:.2f}% "
                           f"(spread {ctx['spread_cupon']:+.2f}pp)."))
        techo = min(techo, cfg["techo_neutral"])

    # Retorno esperado por debajo de la referencia (alerta, no topea)
    rt = mc.get("retorno_total_mediano")
    if rt is not None and rt < ctx["comp_total"]:
        flags.append(_flag("Retorno esperado bajo", "media", "alerta",
                           f"El retorno total esperado ({rt:.1f}%) no supera el rendimiento de la {ctx['ref_label']} "
                           f"del período ({ctx['comp_total']:.1f}%)."))

    # Concentración excesiva (alerta)
    pct = (profile or {}).get("pct_cartera")
    if pct is not None and pct > cfg["alerta_concentracion"]:
        flags.append(_flag("Concentración excesiva", "media", "alerta",
                           f"{pct:g}% de la cartera en una sola nota (> {cfg['alerta_concentracion']:.0f}%)."))

    # Historia insuficiente (alerta)
    hist = (quant.get("meta") or {}).get("historia_anios")
    if hist is not None and hist < 2 * ctx["plazo"]:
        flags.append(_flag("Historia de mercado limitada", "media", "alerta",
                           f"Solo {hist:.1f} años de historia para un plazo de {ctx['plazo']:g}a: probabilidades menos robustas."))

    return flags, techo


def _flag(nombre, severidad, tipo, detalle, explicacion=None) -> dict:
    if explicacion is None:
        explicacion = ("Alerta crítica: por su severidad limita la calificación final, por más que otros aspectos den bien."
                       if tipo == "descalificante"
                       else "Alerta: se informa para tenerla presente, pero no limita por sí sola la calificación.")
    return {"nombre": nombre, "severidad": severidad, "tipo": tipo, "detalle": detalle, "explicacion": explicacion}


# --------------------------------------------------------------------------- #
# Narrativa: fortalezas, debilidades, qué mejoraría
# --------------------------------------------------------------------------- #
def _narrativa(pilares, flags, terms, quant, profile, ctx) -> dict:
    # Fortalezas/debilidades = atributos de la NOTA (pilares de riesgo/retorno y valor relativo).
    # Encaje con el cliente = pilar de idoneidad, en su propia sección.
    fortalezas, debilidades, encaje = [], [], []
    for p in pilares:
        for s in p["sub"]:
            if s["score"] is None:
                continue
            line = f"{s['nombre']}: {s['detalle']}"
            if p["key"] == "suitability":
                tag = "bueno" if s["score"] >= 72 else "malo" if s["score"] <= 40 else "regular"
                encaje.append({"texto": line, "score": s["score"], "tag": tag})
            elif s["score"] >= 72:
                fortalezas.append(line)
            elif s["score"] <= 40:
                debilidades.append(line)
    # flags descalificantes son siempre debilidades de primer orden
    for f in flags:
        if f["tipo"] == "descalificante":
            debilidades.insert(0, f"{f['nombre']}: {f['detalle']}")

    que_mejoraria = []
    cap = (quant.get("barreras") or {}).get("capital") or {}
    cfg = CONFIG
    if (cap.get("tipo") or "").lower() == "americana":
        que_mejoraria.append("Una barrera europea (observación solo al vencimiento) en lugar de continua reduciría el riesgo de quiebre.")
    if cap.get("nivel_pct") and cap["nivel_pct"] > 60:
        que_mejoraria.append(f"Una barrera de capital más baja que {cap['nivel_pct']:.0f}% daría más colchón.")
    if ctx["spread_cupon"] is not None and ctx["spread_cupon"] < 3:
        que_mejoraria.append("Un cupón más alto (mayor spread sobre la tasa libre de riesgo) mejoraría el valor relativo.")
    if ctx["avg_corr"] is not None and ctx["n_sub"] > 1 and ctx["avg_corr"] < 0.5:
        que_mejoraria.append("Subyacentes más correlacionados (o un único subyacente) reducirían el riesgo del worst-of.")
    if ctx["notch"] is None:
        que_mejoraria.append("Cargar el rating/CDS del emisor permitiría afinar el riesgo de crédito (hoy es supuesto neutral).")
    pct = (profile or {}).get("pct_cartera")
    if pct is not None and pct > 10:
        que_mejoraria.append(f"Bajar la asignación de {pct:g}% a ≤10% de la cartera mejoraría la idoneidad.")

    return {"fortalezas": fortalezas[:5], "debilidades": debilidades[:5],
            "que_mejoraria": que_mejoraria[:5], "encaje_cliente": encaje}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_verdict(terms: dict, quant: dict, profile: dict | None = None,
                credito: dict | None = None) -> dict:
    """Calcula el veredicto determinístico. `profile` y `credito` son opcionales
    (si faltan, los pilares afectados usan supuestos neutrales y se listan en
    'supuestos')."""
    profile = profile or {}
    credito = credito or {}
    cfg = CONFIG

    mc = (quant.get("montecarlo") or {}).get("resumen") or {}
    meta = quant.get("meta") or {}
    subs = quant.get("subyacentes") or {}

    rf = meta.get("tasa_libre_riesgo_3m_pct")
    rf = float(rf) if rf is not None else cfg["rf_fallback"]
    cupon_pct = float((terms.get("cupon") or {}).get("tasa_anual") or 0.0) * 100.0
    t_rem = float(meta.get("vida_remanente_anios") or meta.get("plazo_anios") or terms.get("plazo_anios") or 1.0)
    rf_total = ((1.0 + rf / 100.0) ** t_rem - 1.0) * 100.0  # tasa libre compuesta sobre el horizonte

    # Referencia de comparación: si se carga un YTW (p. ej. de LQD), se usa como
    # alternativa del cliente; si no, se cae a la tasa libre de riesgo (T-bill 3m).
    yref = credito.get("yield_ref_pct")
    usa_ref = yref not in (None, "", 0)
    tasa_comp = float(yref) if usa_ref else rf
    comp_total = ((1.0 + tasa_comp / 100.0) ** t_rem - 1.0) * 100.0
    ref_label = "bono IG de referencia (p. ej. LQD)" if usa_ref else "tasa libre de riesgo (Tesoro 3m)"

    ctx = {
        "rf": rf,
        "prob_quiebre": mc.get("prob_quiebre_barrera"),
        "retorno_total": mc.get("retorno_total_mediano"),
        "rf_total": rf_total,
        "tasa_comp": tasa_comp,
        "comp_total": comp_total,
        "usa_ref": usa_ref,
        "ref_label": ref_label,
        "t_rem": t_rem,
        "cupon_pct": cupon_pct,
        "spread_cupon": cupon_pct - tasa_comp,
        "n_sub": len(subs),
        "avg_corr": _avg_corr(quant.get("correlaciones")),
        "min_buffer": _min_buffer(subs),
        "notch": _rating_notch(credito.get("rating")),
        "plazo": float(meta.get("plazo_anios") or terms.get("plazo_anios") or 1.0),
        "vida_esp": float(mc.get("vida_esperada_anios") or 0.0),
        "tiene_proteccion": terms.get("capital_protegido_pct") not in (None, "", 0),
        "cotiza": bool(credito.get("cotiza_secundario")),
    }

    p1 = _pilar_riesgo_retorno(terms, quant, credito, ctx)
    p2 = _pilar_valor_relativo(terms, quant, credito, ctx)
    p3 = _pilar_suitability(terms, quant, profile, ctx)
    nivel_riesgo = p3.pop("_nivel_riesgo", None)
    pilares = [p1, p2, p3]

    pesos = cfg["pesos_pilares"]
    score_raw = (p1["score"] * pesos["riesgo_retorno"]
                 + p2["score"] * pesos["valor_relativo"]
                 + p3["score"] * pesos["suitability"])

    flags, techo = _red_flags(terms, quant, profile, credito, ctx)
    score = int(round(min(score_raw, techo)))

    if score >= cfg["umbral_invertir"]:
        semaforo = "FAVORABLE"
    elif score >= cfg["umbral_neutral"]:
        semaforo = "NEUTRAL"
    else:
        semaforo = "DESFAVORABLE"

    narr = _narrativa(pilares, flags, terms, quant, profile, ctx)

    supuestos = []
    if not credito.get("rating") and not credito.get("cds_bps"):
        supuestos.append("Sin rating/CDS del emisor: el crédito se asumió neutral.")
    if meta.get("tasa_libre_riesgo_3m_pct") is None:
        supuestos.append(f"Tasa libre de riesgo asumida en {rf:.1f}% (no provista por el cuantitativo).")
    if not profile:
        supuestos.append("Sin perfil de cliente: el pilar de idoneidad usa supuestos neutrales.")
    if techo < 100:
        topador = min(flags, key=lambda f: cfg["techo_evitar"] if f["tipo"] == "descalificante" else 99,
                      default=None)
        if score_raw > techo:
            supuestos.append(f"El score ponderado ({score_raw:.0f}) fue topeado a {techo} por una red flag descalificante.")

    una_linea = _una_linea(semaforo, score, nivel_riesgo, flags)
    resumen = _resumen(semaforo, score, nivel_riesgo, flags, narr, ctx)

    return {
        "score": score,
        "score_ponderado": round(score_raw, 1),
        "techo_aplicado": techo if techo < 100 else None,
        "semaforo": semaforo,
        "una_linea": una_linea,
        "resumen": resumen,
        "nivel_riesgo_nota": nivel_riesgo,
        "pilares": pilares,
        "red_flags": flags,
        "fortalezas": narr["fortalezas"],
        "debilidades": narr["debilidades"],
        "encaje_cliente": narr["encaje_cliente"],
        "que_mejoraria": narr["que_mejoraria"],
        "supuestos": supuestos,
        "config": {"pesos_pilares": pesos, "umbrales": {"invertir": cfg["umbral_invertir"], "neutral": cfg["umbral_neutral"]}},
    }


def _resumen(semaforo, score, nivel, flags, narr, ctx) -> str:
    """Párrafo ejecutivo que explica POR QUÉ se llega al veredicto."""
    verbo = {"FAVORABLE": "es favorable", "NEUTRAL": "es neutral", "DESFAVORABLE": "es desfavorable"}.get(semaforo, "")
    partes = [f"El veredicto {verbo} (score {score}/100; riesgo intrínseco de la nota: {nivel})."]
    pq = ctx.get("prob_quiebre")
    if pq is not None:
        partes.append(f"La probabilidad estimada de romper la barrera de capital (perder capital) es {pq:.0f}%.")
    rt, ct, tr = ctx.get("retorno_total"), ctx.get("comp_total"), ctx.get("t_rem")
    if rt is not None and ct is not None:
        rel = "supera a" if rt >= ct else "queda por debajo de"
        anual = ((1 + rt / 100.0) ** (1.0 / tr) - 1.0) * 100.0 if (tr and tr > 0 and rt >= 0) else None
        anual_txt = f"; ≈{anual:.1f}% anualizado" if anual is not None else ""
        partes.append(
            f"El retorno total esperado ({rt:.1f}% en {tr:.2f} años{anual_txt}) {rel} la tasa libre de riesgo "
            f"del mismo período ({ct:.1f}%).")
    desc = [f for f in flags if f["tipo"] == "descalificante"]
    if desc:
        partes.append("Lo decisivo es " + desc[0]["nombre"].lower() + ".")
    nom = lambda s: s.split(":")[0].strip().lower()
    forts = [nom(f) for f in (narr.get("fortalezas") or [])][:2]
    debs = [nom(d) for d in (narr.get("debilidades") or []) if not desc or desc[0]["nombre"] not in d][:2]
    if forts:
        partes.append("A favor: " + ", ".join(forts) + ".")
    if debs:
        partes.append("A vigilar: " + ", ".join(debs) + ".")
    if semaforo == "NEUTRAL":
        partes.append("Conviene negociar términos (más colchón, mejor cupón o emisor) antes de avanzar.")
    return " ".join(partes)


def _una_linea(semaforo, score, nivel, flags) -> str:
    desc = next((f for f in flags if f["tipo"] == "descalificante"), None)
    if semaforo == "FAVORABLE":
        return f"Perfil riesgo-retorno atractivo (score {score})."
    if semaforo == "DESFAVORABLE":
        motivo = desc["nombre"].lower() if desc else f"riesgo {nivel or 'elevado'}"
        return f"Desfavorable (score {score}): {motivo}."
    motivo = (desc["nombre"].lower() if desc else "el retorno no compensa claramente el riesgo")
    return f"Neutral (score {score}): {motivo}; conviene revisar términos."
