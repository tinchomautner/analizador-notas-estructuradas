"""Genera un PDF (horizontal, estética MaximUs navy/dorado) del análisis de la
nota: portada con veredicto + resumen, desglose por pilar, cuantitativo e informe
ampliado opcional.

Arma un HTML autocontenido y lo renderiza con Chrome/Edge headless
(--print-to-pdf). Chrome ya está instalado en la máquina de Martín."""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

_CHROME_CANDIDATES = [
    os.getenv("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _find_chrome() -> str:
    for c in _CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    raise RuntimeError("No se encontró Chrome ni Edge para generar el PDF (configurá CHROME_PATH).")


# --------------------------------------------------------------------------- #
# Formato
# --------------------------------------------------------------------------- #
def _esc(x) -> str:
    s = "" if x is None else str(x)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pct(x, d=1):
    try:
        return f"{float(x):,.{d}f}%".replace(",", "·").replace(".", ",").replace("·", ".")
    except Exception:
        return "—"


def _num(x, d=2):
    try:
        return f"{float(x):,.{d}f}".replace(",", "·").replace(".", ",").replace("·", ".")
    except Exception:
        return "—"


def _bn(x):
    if not isinstance(x, (int, float)):
        return "—"
    a = abs(x)
    if a >= 1e12:
        return f"${x/1e12:.2f} bill."
    if a >= 1e9:
        return f"${x/1e9:.1f} mil M"
    if a >= 1e6:
        return f"${x/1e6:.0f} M"
    return f"${x:,.0f}"


def _color(score):
    if score is None:
        return "#8a96a4"
    return "#1f8a5b" if score >= 70 else "#c98a1c" if score >= 40 else "#c0413a"


def _gauge_svg(score):
    col = _color(score)
    r = 54
    c = 2 * math.pi * r
    off = c * (1 - (score or 0) / 100.0)
    return (
        f'<svg viewBox="0 0 130 130" width="118" height="118">'
        f'<circle cx="65" cy="65" r="{r}" fill="none" stroke="#1b2a48" stroke-width="12"/>'
        f'<circle cx="65" cy="65" r="{r}" fill="none" stroke="{col}" stroke-width="12" '
        f'stroke-dasharray="{c:.1f}" stroke-dashoffset="{off:.1f}" stroke-linecap="round" transform="rotate(-90 65 65)"/>'
        f'<text x="65" y="60" text-anchor="middle" font-size="36" font-weight="800" fill="{col}" font-family="Montserrat,sans-serif">{_esc(score)}</text>'
        f'<text x="65" y="82" text-anchor="middle" font-size="11" fill="#5f6c80">/ 100</text></svg>'
    )


def _bars(p):
    rows = ""
    for s in p.get("sub", []):
        sc = s.get("score")
        col = _color(sc)
        val = "n/d" if sc is None else f"{sc:.0f}"
        rows += (
            f'<div class="sub"><div class="sub-h"><span>{_esc(s["nombre"])} — {_esc(s["detalle"])}</span>'
            f'<b style="color:{col}">{val}</b></div>'
            f'<div class="track"><i style="width:{0 if sc is None else sc}%;background:{col}"></i></div></div>'
        )
    col = _color(p.get("score"))
    return (
        f'<div class="pilar"><div class="pilar-h"><span>{_esc(p["nombre"])} '
        f'<small>· peso {p.get("peso","")}%</small></span><b style="color:{col}">{_num(p.get("score"),0)}</b></div>'
        f'<div class="track big"><i style="width:{p.get("score",0)}%;background:{col}"></i></div>{rows}</div>'
    )


def _histogram_svg(g, barrier):
    if not g or not g.get("counts"):
        return ""
    counts, edges = g["counts"], g["edges"]
    mx = max(counts) or 1
    W, H, pad = 700, 150, 24
    bw = (W - pad * 2) / len(counts)
    bars = ""
    for i, c in enumerate(counts):
        h = (c / mx) * (H - pad - 14)
        x = pad + i * bw
        y = H - pad - h
        below = edges[i] < (barrier or 0)
        bars += f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(bw-1,1):.1f}" height="{h:.1f}" fill="{"#c0413a" if below else "#3f6fb0"}"/>'
    mn, mxe = edges[0], edges[-1]
    bx = pad + (barrier - mn) / (mxe - mn) * (W - pad * 2) if (mxe > mn and barrier is not None) else pad
    return (
        f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" style="width:100%;height:46mm;display:block">{bars}'
        f'<line x1="{bx:.1f}" y1="6" x2="{bx:.1f}" y2="{H-pad}" stroke="#b88a2f" stroke-width="1.5" stroke-dasharray="4 3"/>'
        f'<text x="{bx+4:.1f}" y="16" fill="#b88a2f" font-size="10" font-weight="700">Barrera {_num(barrier,0)}%</text>'
        f'<text x="{W/2}" y="{H-6}" fill="#5d6e80" font-size="9" text-anchor="middle">Worst-of al vencimiento</text></svg>'
    )


def _series_svg(serie, barrier, ticker):
    if not serie or not serie.get("niveles") or barrier is None:
        return ""
    n, f = serie["niveles"], serie["fechas"]
    W, H, pad = 820, 240, 32
    mx = max(max(n), barrier) * 1.06
    mn = min(min(n), barrier, 0) * 0.98
    def X(i):
        return pad + i / max(len(n) - 1, 1) * (W - pad * 2)
    def Y(v):
        return H - pad - (v - mn) / ((mx - mn) or 1) * (H - pad - 16)
    path = "".join(f'{"L" if i else "M"}{X(i):.1f},{Y(v):.1f}' for i, v in enumerate(n))
    by = Y(barrier)
    pts = "".join(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="1.4" fill="#c0413a"/>' for i, v in enumerate(n) if v < barrier)
    bajo = serie.get("pct_bajo_barrera", 0) or 0
    return (
        f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" style="width:100%;height:82mm;display:block">'
        f'<rect x="{pad}" y="{by:.1f}" width="{W-pad*2}" height="{H-pad-by:.1f}" fill="#c0413a" opacity="0.06"/>'
        f'<line x1="{pad}" y1="{by:.1f}" x2="{W-pad}" y2="{by:.1f}" stroke="#c0413a" stroke-width="1.2" stroke-dasharray="4 3"/>'
        f'<text x="{W-pad}" y="{by-4:.1f}" fill="#c0413a" font-size="9" text-anchor="end">Barrera {_num(barrier,0)}%</text>'
        f'<path d="{path}" fill="none" stroke="#3f6fb0" stroke-width="1.6"/>{pts}'
        f'<text x="{pad}" y="{H-8}" fill="#5d6e80" font-size="9">{_esc(f[0])}</text>'
        f'<text x="{W-pad}" y="{H-8}" fill="#5d6e80" font-size="9" text-anchor="end">{_esc(f[-1])}</text>'
        f'<text x="{pad}" y="13" fill="#11243f" font-size="10.5" font-weight="700">{_esc(ticker)} (% del strike)</text>'
        f'<text x="{W-pad}" y="13" fill="{"#c0413a" if bajo>0 else "#8a96a4"}" font-size="9.5" text-anchor="end">{_num(bajo,0)}% del tiempo bajo barrera</text></svg>'
    )


# --------------------------------------------------------------------------- #
# HTML (horizontal, dark navy + dorado)
# --------------------------------------------------------------------------- #
def build_html(terms: dict, quant: dict, verdict: dict, narrative_html: str = "", logo: str = "") -> str:
    meta = quant.get("meta", {}) if quant else {}
    subs = terms.get("subyacentes") or []
    nombres = ", ".join(s.get("nombre") or s.get("ticker_yf") or "" for s in subs)
    score = verdict.get("score")
    sem = verdict.get("semaforo", "")
    col = _color(score)
    fecha = meta.get("fecha_datos", "")
    mc = (quant.get("montecarlo") or {}).get("resumen") or {}

    kpis = [
        ("Prob. quiebre barrera", _pct(mc.get("prob_quiebre_barrera"))),
        ("Retorno total esperado", _pct(mc.get("retorno_total_mediano"))),
        ("Prob. de pérdida", _pct(mc.get("prob_perdida"))),
        ("Vida esperada", f'{_num(mc.get("vida_esperada_anios"))} años'),
    ]
    kpi_html = "".join(f'<div class="kpi"><span>{k}</span><b>{v}</b></div>' for k, v in kpis)

    rows = ""
    for t, s in (quant.get("subyacentes") or {}).items():
        rows += (f'<tr><td>{_esc(t)}</td><td>{_num(s.get("precio_actual"))}</td><td>{_pct(s.get("nivel_actual_pct"),0)}</td>'
                 f'<td>{_pct(s.get("vol_1y"))}</td><td>{_esc(s.get("beta_spx"))}</td>'
                 f'<td>{_pct(s.get("max_drawdown_3y"))}</td><td>{_pct(s.get("dist_a_barrera_capital_pct"))}</td></tr>')
    sub_tbl = ('<table class="data"><thead><tr><th>Ticker</th><th>Precio</th><th>Nivel</th><th>Vol 1a</th>'
               '<th>Beta</th><th>MaxDD 3a</th><th>Dist. barrera</th></tr></thead><tbody>' + rows + "</tbody></table>")

    prows = ""
    for r in (quant.get("tabla_payoff") or []):
        prows += (f'<tr><td>{_pct(r.get("worst_of_final_pct"),0)}</td><td>{_pct(r.get("retorno_directo_pct"))}</td>'
                  f'<td>{_pct(r.get("retorno_nota_pct"))}</td><td>{"✓" if r.get("nota_supera") else "—"}</td></tr>')
    payoff_tbl = ('<table class="data"><thead><tr><th>Worst-of final</th><th>Inv. directa</th>'
                  '<th>Nota</th><th>Supera</th></tr></thead><tbody>' + prows + "</tbody></table>")

    subs_q = quant.get("subyacentes") or {}
    fund_tbl = ""
    if any(any(v is not None for v in (s.get("fundamentals") or {}).values()) for s in subs_q.values()):
        tickers = list(subs_q.keys())
        rows_def = [
            ("Sector", "sector", lambda v: _esc(v)),
            ("Market cap", "market_cap", _bn),
            ("Ingresos (12m)", "revenue", _bn),
            ("P/E forward", "pe_forward", lambda v: _num(v, 1)),
            ("P/E trailing", "pe_trailing", lambda v: _num(v, 1)),
            ("EV/EBITDA", "ev_ebitda", lambda v: _num(v, 1)),
            ("PEG", "peg", lambda v: _num(v, 2)),
            ("Crec. ingresos", "crec_ingresos_pct", lambda v: _pct(v, 1)),
            ("Crec. ganancias", "crec_ganancias_pct", lambda v: _pct(v, 1)),
            ("Deuda/EBITDA", "deuda_ebitda", lambda v: _num(v, 2)),
            ("Div. yield", "dividend_yield_pct", lambda v: _pct(v, 2)),
        ]
        head = "<th>Métrica</th>" + "".join(f"<th>{_esc(t)}</th>" for t in tickers)
        body = ""
        for label, key, fn in rows_def:
            vals = [(subs_q[t].get("fundamentals") or {}).get(key) for t in tickers]
            if all(v is None for v in vals):
                continue
            cells = "".join(f"<td>{fn(v) if v is not None else '—'}</td>" for v in vals)
            body += f"<tr><td>{label}</td>{cells}</tr>"
        fund_tbl = ('<div class="subt">Fundamentales (Yahoo Finance)</div><table class="data"><thead><tr>'
                    + head + "</tr></thead><tbody>" + body + "</tbody></table>")

    erows = ""
    for e in (mc.get("escenarios") or []):
        erows += (f'<tr><td>{_esc(e["nombre"])}</td><td>{_pct(e["prob"],0)}</td>'
                  f'<td>{_pct(e.get("retorno_tipico_pct"))}</td><td>{_esc(e["barrera_tocada"])}</td></tr>')
    esc_tbl = ('<div class="subt">Escenarios posibles (Monte Carlo · suman 100%)</div>'
               '<table class="data"><thead><tr><th>Escenario</th><th>Probabilidad</th>'
               '<th>Retorno típico</th><th>Barrera</th></tr></thead><tbody>' + erows + "</tbody></table>") if erows else ""

    def lst(items, cls):
        if not items:
            return ""
        return f'<ul class="lst {cls}">' + "".join(f"<li>{_esc(x)}</li>" for x in items) + "</ul>"

    flags_html = ""
    for f in verdict.get("red_flags", []):
        c = "#ec5b54" if f["tipo"] == "descalificante" else "#e0b150"
        flags_html += (f'<div class="flag" style="border-color:{c}66;background:{c}14">'
                       f'<b style="color:{c}">{"⛔" if f["tipo"]=="descalificante" else "⚠"} {_esc(f["nombre"])} · '
                       f'{"alerta crítica" if f["tipo"]=="descalificante" else "alerta"}</b>'
                       f'<div>{_esc(f["detalle"])}</div></div>')

    encaje_html = ""
    for e in verdict.get("encaje_cliente", []):
        encaje_html += f'<div class="sub-h sub"><span>{_esc(e["texto"])}</span><b style="color:{_color(e["score"])}">{_num(e["score"],0)}</b></div>'

    pilares_html = "".join(_bars(p) for p in verdict.get("pilares", []))
    logo_img = f'<img class="client-logo" src="{logo}"/>' if logo else ""

    sh = quant.get("series_historicas") or {}
    barr_cap = ((quant.get("barreras", {}).get("capital", {})) or {}).get("nivel_pct")
    series_page = ""
    if sh:
        charts = "".join(f'<div class="serie">{_series_svg(s, barr_cap, t)}</div>' for t, s in sh.items())
        series_page = (
            '<div class="page"><div class="sec">Histórico de subyacentes vs. barrera (backtest de quiebres)</div>'
            + charts +
            '<div class="foot">Precio de cada subyacente como % de su strike inicial; la línea roja es la barrera de '
            'capital y los puntos rojos marcan cuándo estuvo por debajo (habría roto la barrera). Últimos 5 años.</div></div>'
        )
    seasoned = meta.get("seasoned")
    seasoned_note = (f'<div class="note">Nota ya emitida: se valúa la vida remanente de '
                     f'{_num(meta.get("vida_remanente_anios"))} años (vence {_esc(meta.get("fecha_vencimiento"))}), '
                     f'con los niveles actuales relativos al strike original.</div>' if seasoned else "")

    narrative_section = ""
    if narrative_html:
        narrative_section = (
            '<div class="page"><div class="sec">Informe ampliado</div>'
            '<div class="warn">Informe redactado por IA con búsqueda web. Verificá los datos antes de usarlo con un cliente.</div>'
            f'<div class="md">{narrative_html}</div></div>'
        )

    pp = verdict.get("config", {}).get("pesos_pilares", {})

    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Montserrat:wght@400;600;700&display=swap');
*{{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
@page{{size:A4 landscape;margin:0}}
:root{{--bg:#ffffff;--bg2:#f5f7fa;--panel:#f6f8fb;--bd:#e3e8ef;--bd2:#d4dce6;--gold:#b88a2f;--gold2:#9a7322;
 --fg:#13243d;--mut:#5d6e80;--sub:#8a96a4}}
body{{margin:0;background:#fff;color:var(--fg);font-family:'Inter',Arial,sans-serif;font-size:10.5pt;line-height:1.5}}
.page{{width:297mm;min-height:209.5mm;padding:13mm 15mm;background:#fff;page-break-after:always;position:relative}}
.brand{{font-family:'Montserrat',sans-serif;font-weight:600;letter-spacing:.05em;font-size:22pt;color:#fff}}
.brand span{{color:#d9b25f}}
/* cover (dark) */
.cover{{background:linear-gradient(150deg,#0a1424,#11243f 60%,#16314f)}}
.cover-top{{display:flex;justify-content:space-between;align-items:center}}
.client-logo{{max-height:18mm;max-width:60mm;object-fit:contain}}
.cols3{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;align-items:start;margin-bottom:6px}}
.cover-body{{display:flex;gap:14mm;margin-top:10mm;align-items:flex-start}}
.cover-left{{flex:0 0 auto;text-align:center;padding-top:4mm}}
.cover-left .sem{{font-family:'Montserrat',sans-serif;font-size:21pt;font-weight:700;letter-spacing:.04em;margin-top:3mm}}
.cover-left .cap{{color:#9fb0c6;font-size:9pt;margin-top:1mm}}
.cover-right{{flex:1}}
.cover-right h1{{font-family:'Montserrat',sans-serif;font-weight:300;font-size:26pt;margin:0 0 3mm;letter-spacing:.01em;color:#fff}}
.cover-right h1 b{{font-weight:700}}
.meta{{color:#c9d4e3;font-size:11pt;margin:1mm 0}} .meta.gold{{color:#d9b25f}}
.resumen-box{{margin-top:6mm;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.18);border-left:3px solid #d9b25f;
 border-radius:8px;padding:6mm 7mm}}
.resumen-box .rk{{font-family:'Montserrat',sans-serif;color:#d9b25f;text-transform:uppercase;letter-spacing:.14em;font-size:8.5pt;font-weight:600;margin-bottom:2mm}}
.resumen-box p{{margin:0;font-size:11pt;line-height:1.6;color:#eef2f7}}
.cover-foot{{position:absolute;bottom:9mm;left:15mm;right:15mm;color:#9fb0c6;font-size:8pt;border-top:1px solid rgba(255,255,255,.15);padding-top:3mm}}
/* sections (light) */
.sec{{font-family:'Montserrat',sans-serif;background:#11243f;color:#fff;font-weight:600;letter-spacing:.14em;text-transform:uppercase;
 font-size:10pt;padding:5px 12px;border-radius:5px;border-left:4px solid #b88a2f;margin:0 0 9px}}
.cols{{display:flex;gap:10mm}} .col{{flex:1;min-width:0}}
.pilar{{margin:0 0 7px;background:var(--bg2);border:1px solid var(--bd);border-radius:8px;padding:8px 11px}}
.pilar-h{{display:flex;justify-content:space-between;font-family:'Montserrat',sans-serif;font-size:11pt;font-weight:600;color:#13243d}}
.pilar-h small{{color:var(--sub);font-weight:400;font-family:'Inter'}}
.track{{height:6px;background:#e3e8ef;border-radius:5px;overflow:hidden;margin:4px 0 6px}}
.track.big{{height:8px}} .track i{{display:block;height:100%}}
.sub{{margin:4px 0;background:#fff;border:1px solid var(--bd);border-radius:6px;padding:5px 9px}}
.sub-h{{display:flex;justify-content:space-between;gap:10px;font-size:9pt;color:var(--fg)}}
.sub-h b{{flex:0 0 auto;font-variant-numeric:tabular-nums}}
.flag{{border:1px solid;border-radius:7px;padding:7px 10px;margin:0 0 6px;font-size:9pt;color:var(--fg)}}
.flag b{{display:block;margin-bottom:2px}}
.kpis{{display:flex;gap:9px;margin:0 0 9px}}
.kpi{{flex:1;background:var(--bg2);border:1px solid var(--bd);border-top:2px solid #b88a2f;border-radius:7px;padding:8px 11px}}
.kpi span{{display:block;font-size:8pt;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}}
.kpi b{{font-size:16pt;color:#11243f;font-family:'Montserrat',sans-serif}}
table.data{{width:100%;border-collapse:collapse;font-size:8.5pt;margin:4px 0 9px}}
table.data th{{color:var(--mut);text-transform:uppercase;font-size:7pt;letter-spacing:.05em;padding:5px 7px;text-align:right;border-bottom:1px solid var(--bd2)}}
table.data td{{padding:5px 7px;text-align:right;border-bottom:1px solid var(--bd);color:var(--fg)}}
table.data th:first-child,table.data td:first-child{{text-align:left}}
.subt{{font-family:'Montserrat',sans-serif;font-size:8.5pt;color:#b88a2f;text-transform:uppercase;letter-spacing:.12em;font-weight:600;margin:10px 0 5px}}
.lst{{margin:4px 0 9px;padding:0;list-style:none;font-size:9pt}}
.lst li{{margin:0 0 3px;padding-left:14px;position:relative;color:var(--fg)}}
.lst.good li:before{{content:"▲";color:#1f8a5b;font-size:7pt;position:absolute;left:0;top:2px}}
.lst.bad li:before{{content:"▼";color:#c0413a;font-size:7pt;position:absolute;left:0;top:2px}}
.lst.imp li:before{{content:"→";color:#b88a2f;position:absolute;left:0}}
.note,.warn{{font-size:8.5pt;border-radius:6px;padding:7px 10px;margin:6px 0;background:#fbf3df;
 border:1px solid #ecd6a6;color:#7a5a14}}
.foot{{margin-top:10px;border-top:1px solid var(--bd);padding-top:6px;color:var(--sub);font-size:8pt}}
.runfoot{{position:fixed;bottom:5mm;left:15mm;right:15mm;text-align:center;color:#9aa6b5;font-size:7pt;
 font-family:'Montserrat',sans-serif;letter-spacing:.1em;text-transform:uppercase}}
.md{{font-size:9.5pt;color:var(--fg)}}
.md h1,.md h2,.md h3{{font-family:'Montserrat',sans-serif;color:#11243f;font-size:11pt;border-bottom:1px solid var(--bd);padding-bottom:3px;margin-top:10px}}
.md strong{{color:#11243f}} .md table{{border-collapse:collapse;width:100%;font-size:8.5pt}}
.md th,.md td{{border:1px solid var(--bd);padding:4px 7px}} .md th{{color:#b88a2f}}
.md a{{color:#9a7322}}
.serie{{margin:0 0 8px;page-break-inside:avoid}}
/* saltos de página: cada fase del informe en página nueva; no cortar tablas/bloques */
.fase{{page-break-inside:auto}}
.fase + .fase{{page-break-before:always}}
.fase-title{{font-family:'Montserrat',sans-serif;color:#fff;background:#11243f;border-left:4px solid #b88a2f;
 padding:6px 12px;border-radius:5px;font-size:11pt;font-weight:600;margin:0 0 9px;letter-spacing:.06em}}
.md table,table.data,.pilar,.kpis,.cols3{{page-break-inside:avoid}}
.md h1,.md h2,.md h3{{page-break-after:avoid}}
</style></head><body>
<div class="runfoot">MaximUs · Análisis de Nota Estructurada · {_esc(nombres)}</div>

<div class="page cover">
  <div class="cover-top"><div class="brand">Maxim<span>U</span>s</div>{logo_img}</div>
  <div class="cover-body">
    <div class="cover-left">
      {_gauge_svg(score)}
      <div class="sem" style="color:{col}">{_esc(sem)}</div>
      <div class="cap">riesgo intrínseco: {_esc(verdict.get("nivel_riesgo_nota"))}</div>
    </div>
    <div class="cover-right">
      <h1>Análisis de <b>Nota Estructurada</b></h1>
      <div class="meta gold">{_esc(terms.get("tipo_nota"))}</div>
      <div class="meta">{_esc(nombres)}</div>
      <div class="meta">Emisor: {_esc(terms.get("emisor") or "—")} · {_esc(terms.get("moneda") or "")} · datos al {_esc(fecha)}{(" · vence " + _esc(meta.get("fecha_vencimiento"))) if meta.get("fecha_vencimiento") else ""}</div>
      <div class="resumen-box"><div class="rk">Resumen — por qué este veredicto</div><p>{_esc(verdict.get("resumen") or verdict.get("una_linea"))}</p></div>
    </div>
  </div>
  <div class="cover-foot">Herramienta de apoyo para asesores · El análisis es informativo y no constituye una recomendación ni asesoramiento de inversión.</div>
</div>

<div class="page">
  <div class="sec">Veredicto · desglose por pilar</div>
  <div class="cols3">{pilares_html}</div>
  <div class="cols">
    <div class="col">
      {('<div class="subt">Alertas</div>' + flags_html) if flags_html else ''}
      {('<div class="subt">Fortalezas de la nota</div>' + lst(verdict.get("fortalezas"), "good")) if verdict.get("fortalezas") else ''}
    </div>
    <div class="col">
      {('<div class="subt">Debilidades de la nota</div>' + lst(verdict.get("debilidades"), "bad")) if verdict.get("debilidades") else ''}
      {('<div class="subt">Qué mejoraría el veredicto</div>' + lst(verdict.get("que_mejoraria"), "imp")) if verdict.get("que_mejoraria") else ''}
    </div>
  </div>
</div>

<div class="page">
  <div class="sec">Análisis cuantitativo</div>
  {seasoned_note}
  <div class="kpis">{kpi_html}</div>
  <div class="cols">
    <div class="col">{esc_tbl}<div class="subt">Métricas por subyacente</div>{sub_tbl}</div>
    <div class="col">{fund_tbl}</div>
  </div>
  <div class="cols">
    <div class="col"><div class="subt">Distribución worst-of al vencimiento (Monte Carlo)</div>
      {_histogram_svg(quant.get("grafico_worst_of_vto"), (quant.get("barreras",{}).get("capital",{}) or {}).get("nivel_pct"))}</div>
    <div class="col"><div class="subt">Tabla de payoff a vencimiento</div>{payoff_tbl}</div>
  </div>
  <div class="foot">Cuantitativo con datos de Yahoo Finance ({_esc(fecha)}) · Monte Carlo {_esc(meta.get("mc_paths"))} trayectorias.
  Ponderación: Riesgo/Retorno {int(pp.get("riesgo_retorno",0)*100)}% · Valor relativo {int(pp.get("valor_relativo",0)*100)}% · Idoneidad {int(pp.get("suitability",0)*100)}%.</div>
</div>

{series_page}

{narrative_section}
</body></html>"""


# --------------------------------------------------------------------------- #
# Render con Chrome headless
# --------------------------------------------------------------------------- #
def render_pdf(html: str) -> bytes:
    chrome = _find_chrome()
    tmp = Path(tempfile.gettempdir()) / f"nota_{uuid.uuid4().hex}"
    html_path = tmp.with_suffix(".html")
    pdf_path = tmp.with_suffix(".pdf")
    html_path.write_text(html, encoding="utf-8")
    try:
        url = "file:///" + str(html_path).replace("\\", "/")
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
             "--run-all-compositor-stages-before-draw", "--virtual-time-budget=4000",
             f"--print-to-pdf={pdf_path}", url],
            check=True, timeout=90,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not pdf_path.exists():
            raise RuntimeError("Chrome no generó el PDF.")
        return pdf_path.read_bytes()
    finally:
        for p in (html_path, pdf_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


def build_pdf(terms, quant, verdict, narrative_html="", logo="") -> bytes:
    return render_pdf(build_html(terms, quant, verdict, narrative_html, logo))
