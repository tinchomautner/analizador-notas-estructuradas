"""Cliente OpenAI (Chat Completions vía httpx): parseo del termsheet (JSON estricto)
y streaming de cada fase del análisis. Misma interfaz que claude_client para que el
dispatcher de main.py los pueda intercambiar (LLM_PROVIDER=openai|anthropic).

Nota: OpenAI no trae la herramienta de web_search nativa de Anthropic, así que las
fases corren sobre los datos calculados + conocimiento del modelo (sin búsqueda en
vivo). El parseo y el veredicto no la necesitan."""

from __future__ import annotations

import os
from typing import AsyncIterator

import httpx

from prompts import SYSTEM, PARSE
from claude_client import _extract_json, _build_user_message  # helpers compartidos

API_URL = "https://api.openai.com/v1/chat/completions"
RESPONSES_URL = "https://api.openai.com/v1/responses"
WEB_SEARCH_PHASES = {1, 2}  # fases que pueden buscar datos de mercado en vivo


def _model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def _headers() -> dict:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Falta OPENAI_API_KEY en el archivo .env del backend.")
    return {"Authorization": f"Bearer {key}", "content-type": "application/json"}


async def parse_termsheet(termsheet: str) -> dict:
    body = {
        "model": _model(),
        "temperature": 0,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": PARSE},
            {"role": "user", "content": f"Termsheet:\n\n{termsheet}"},
        ],
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(API_URL, headers=_headers(), json=body)
        if r.status_code != 200:
            raise RuntimeError(f"OpenAI HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
    text = data["choices"][0]["message"]["content"]
    return _extract_json(text)


async def stream_phase(
    phase: int, termsheet: str, terms: dict, quant: dict, prior: dict, verdict: dict | None = None
) -> AsyncIterator[tuple[str, str]]:
    """Genera tuplas (kind, data) con kind en {text, tool, error}.

    Usa la Responses API con la herramienta de búsqueda web (web_search) en las
    fases 1 y 2, para que los datos de mercado sean actuales y no inventados."""
    import json as _json

    instructions = SYSTEM[phase]
    body = {
        "model": _model(),
        "input": _build_user_message(termsheet, terms, quant, prior, verdict),
        "max_output_tokens": 6000,
        "stream": True,
    }
    nombres_sub = ", ".join(
        f'{(s.get("nombre") or s.get("ticker") or "")} ({s.get("ticker_yf") or s.get("ticker") or ""})'.strip()
        for s in (terms.get("subyacentes") or [])
    )
    if phase in WEB_SEARCH_PHASES:
        body["tools"] = [{"type": "web_search"}]
        body["tool_choice"] = "required"  # obligar a buscar antes de redactar
        instructions += (
            f"\n\nLos subyacentes de ESTA nota son EXACTAMENTE: {nombres_sub}. Analizá ÚNICAMENTE esos activos; "
            "NO menciones ni analices ningún otro activo, índice ni criptomoneda.\n"
            "OBLIGATORIO: usá la herramienta de búsqueda web para traer NOTICIAS y CATALIZADORES recientes de cada "
            "subyacente listado arriba. Usá SOLO fuentes financieras reputadas (Yahoo Finance, Investing.com, CNBC, "
            "TradingView, Bloomberg, Reuters, MarketWatch, WSJ, Morningstar); NUNCA cites Reddit, foros, redes sociales "
            "ni blogs. CADA dato o afirmación que traigas de la web debe ir con su fuente (nombre "
            "del sitio) y fecha entre paréntesis. Los precios, la capitalización y los fundamentals NO se buscan en "
            "la web: YA vienen de Yahoo Finance en los datos calculados (campo 'fundamentals') — usalos tal cual. NO "
            "uses tu conocimiento de entrenamiento para datos de mercado. Si algo no está disponible, OMITILO (no "
            "escribas 'no disponible'). Nunca pongas fechas de acceso viejas (tipo 2023).")
    body["instructions"] = instructions

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream("POST", RESPONSES_URL, headers=_headers(), json=body) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode("utf-8", "ignore")
                    yield ("error", f"HTTP {r.status_code}: {detail[:500]}")
                    return
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        ev = _json.loads(payload)
                    except ValueError:
                        continue
                    t = ev.get("type", "")
                    if t == "response.output_text.delta":
                        yield ("text", ev.get("delta", ""))
                    elif "web_search_call" in t and ("in_progress" in t or "searching" in t):
                        yield ("tool", "Buscando en la web…")
                    elif t in ("response.error", "error", "response.failed"):
                        yield ("error", _json.dumps(ev.get("error") or ev.get("response") or ev, ensure_ascii=False)[:500])
    except Exception as e:  # noqa: BLE001
        yield ("error", f"{type(e).__name__}: {e}")
