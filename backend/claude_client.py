"""Cliente de la Anthropic Messages API: parseo del termsheet (no streaming) y
streaming de cada fase del análisis con web_search en las fases 1 y 2."""

from __future__ import annotations

import json
import os
from typing import AsyncIterator

import httpx

from prompts import SYSTEM, PARSE

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
WEB_SEARCH_PHASES = {1, 2}


def _model() -> str:
    return os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")


def _headers() -> dict:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en el archivo .env del backend.")
    return {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No se encontró JSON en la respuesta del parser.")
    return json.loads(text[start : end + 1])


async def parse_termsheet(termsheet: str) -> dict:
    body = {
        "model": _model(),
        "max_tokens": 2000,
        "system": PARSE,
        "messages": [{"role": "user", "content": f"Termsheet:\n\n{termsheet}"}],
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(API_URL, headers=_headers(), json=body)
        r.raise_for_status()
        data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return _extract_json(text)


def _strip_none(o):
    """Quita claves con valor None/null para que el LLM no las vea ni las comente."""
    if isinstance(o, dict):
        return {k: _strip_none(v) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        return [_strip_none(v) for v in o]
    return o


def _build_user_message(termsheet: str, terms: dict, quant: dict, prior: dict, verdict: dict | None = None) -> str:
    parts = [
        "## TERMSHEET (texto original)\n" + termsheet,
        "## TÉRMINOS EXTRAÍDOS (JSON)\n```json\n" + json.dumps(_strip_none(terms), ensure_ascii=False, indent=2) + "\n```",
        "## DATOS CALCULADOS POR EL SISTEMA (fuente de verdad, NO recalcular)\n```json\n"
        + json.dumps(_strip_none(quant), ensure_ascii=False, indent=2)
        + "\n```",
    ]
    if verdict:
        v = {k: verdict.get(k) for k in ("score", "semaforo", "una_linea", "nivel_riesgo_nota", "red_flags")}
        parts.append(
            "## VEREDICTO DEL SISTEMA (tu análisis NO debe contradecirlo)\n```json\n"
            + json.dumps(v, ensure_ascii=False, indent=2) + "\n```"
        )
    if prior:
        for ph in sorted(prior):
            parts.append(f"## RESULTADO FASE {ph} (contexto previo)\n{prior[ph]}")
    parts.append(
        "Generá el análisis de tu fase siguiendo tu rol. Reglas OBLIGATORIAS:\n"
        "(1) Los DATOS CALCULADOS y el VEREDICTO son la fuente de verdad: citá sus VALORES NUMÉRICOS EXACTOS, NO los "
        "recalcules ni los contradigas.\n"
        "(2) PROHIBIDO usar variables o placeholders (X, Y, Z, W, A%, B%, $X, $Z, etc.) o excusas de 'confidencialidad': "
        "el informe es interno y debe mostrar SIEMPRE los números reales.\n"
        "(3) Para datos de mercado actuales (noticias, catalizadores) usá la búsqueda web y CITÁ la fuente con su fecha; "
        "los precios y fundamentals YA vienen calculados (Yahoo Finance), usalos tal cual.\n"
        "(4) Si un dato no está disponible, OMITILO EN SILENCIO: NO escribas 'omitido', 'null', 'no disponible', 'N/A', "
        "'no especifica' ni menciones que falta; directamente no incluyas esa línea/campo/fila (nada de 'Market Cap: "
        "(omitido, null)' ni 'Otros: omitidos').\n"
        "(5) Usá el RETORNO TOTAL esperado, NO la TIR anualizada (se distorsiona en plazos cortos y genera dispersiones "
        "engañosas).\n"
        "(6) NÚMEROS: mostralos con 2 decimales como máximo (ej. 0.06%, NUNCA 0.0619290911%).\n"
        "(7) FUENTES: para datos de la web usá SOLO fuentes financieras reputadas (Yahoo Finance, Investing.com, CNBC, "
        "TradingView, Bloomberg, Reuters, MarketWatch, WSJ, Morningstar, Financial Times). NUNCA cites Reddit, foros, "
        "redes sociales ni blogs.\n"
        "(8) No repitas métricas idénticas: si dos filas dan el mismo valor (p. ej. prob. de quiebre de barrera = prob. "
        "de pérdida cuando la barrera es europea), mostrá UNA sola aclarando la equivalencia.")
    return "\n\n".join(parts)


async def stream_phase(
    phase: int, termsheet: str, terms: dict, quant: dict, prior: dict, verdict: dict | None = None
) -> AsyncIterator[tuple[str, str]]:
    """Genera tuplas (kind, data) con kind en {text, tool, error}."""
    body = {
        "model": _model(),
        "max_tokens": 8000,
        "system": SYSTEM[phase],
        "messages": [
            {"role": "user", "content": _build_user_message(termsheet, terms, quant, prior, verdict)}
        ],
        "stream": True,
    }
    if phase in WEB_SEARCH_PHASES:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}]

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream("POST", API_URL, headers=_headers(), json=body) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode("utf-8", "ignore")
                    yield ("error", f"HTTP {r.status_code}: {detail[:500]}")
                    return
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        ev = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("type")
                    if t == "content_block_start":
                        block = ev.get("content_block", {})
                        if block.get("type") == "server_tool_use" and block.get("name") == "web_search":
                            yield ("tool", "Buscando en la web…")
                    elif t == "content_block_delta":
                        delta = ev.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield ("text", delta.get("text", ""))
                    elif t == "error":
                        yield ("error", json.dumps(ev.get("error", {}), ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        yield ("error", f"{type(e).__name__}: {e}")
