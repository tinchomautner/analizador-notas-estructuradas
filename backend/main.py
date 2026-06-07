"""Analizador de Notas Estructuradas — backend FastAPI.

Sirve el frontend (React por CDN), parsea el termsheet, corre el cuantitativo y
hace streaming de las 4 fases de análisis contra la Anthropic API.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

import quant  # noqa: E402
import scoring  # noqa: E402
import claude_client  # noqa: E402
import openai_client  # noqa: E402


# --------------------------------------------------------------------------- #
# Proveedor de LLM (configurado a nivel backend vía .env, no por usuario)
# --------------------------------------------------------------------------- #
def _provider():
    return claude_client if os.getenv("LLM_PROVIDER", "openai").lower() == "anthropic" else openai_client


def _llm_ready() -> bool:
    if os.getenv("LLM_PROVIDER", "openai").lower() == "anthropic":
        return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    return bool(os.getenv("OPENAI_API_KEY", "").strip())

app = FastAPI(title="Analizador de Notas Estructuradas")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND = BASE.parent / "frontend"


# --------------------------------------------------------------------------- #
# Modelos de request
# --------------------------------------------------------------------------- #
class ParseReq(BaseModel):
    termsheet: str


class QuantReq(BaseModel):
    terms: dict


class PhaseReq(BaseModel):
    phase: int
    termsheet: str
    terms: dict
    quant: dict
    prior: dict = {}
    verdict: dict = {}


class VerdictReq(BaseModel):
    terms: dict
    quant: dict
    profile: dict = {}
    credito: dict = {}




# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #
@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "provider": os.getenv("LLM_PROVIDER", "openai").lower(),
        "llm_ready": _llm_ready(),
    }


@app.get("/api/diag")
async def diag():
    """Diagnóstico: ¿anda Yahoo Finance desde este servidor? (clave para saber si el
    cuantitativo funciona en un host cloud). Devuelve la IP saliente + un test de descarga."""
    import time
    import urllib.request

    out = {"provider": os.getenv("LLM_PROVIDER", "openai").lower(), "llm_ready": _llm_ready()}
    try:
        out["ip_publica"] = urllib.request.urlopen("https://api.ipify.org", timeout=6).read().decode()
    except Exception as e:  # noqa: BLE001
        out["ip_publica"] = f"n/d ({type(e).__name__})"
    try:
        import yfinance as yf
        t0 = time.time()
        df = yf.download("AAPL", period="5d", progress=False)
        ok = df is not None and len(df) > 0
        out["yahoo_finance"] = {
            "anda": bool(ok),
            "filas": int(len(df)) if ok else 0,
            "ultima_fecha": str(df.index[-1].date()) if ok else None,
            "segundos": round(time.time() - t0, 1),
        }
    except Exception as e:  # noqa: BLE001
        out["yahoo_finance"] = {"anda": False, "error": f"{type(e).__name__}: {e}"[:300]}
    return out


# --------------------------------------------------------------------------- #
# Parseo del termsheet
# --------------------------------------------------------------------------- #
@app.post("/api/pdf-text")
async def pdf_text(file: UploadFile = File(...)):
    from pypdf import PdfReader

    raw = await file.read()
    try:
        reader = PdfReader(io.BytesIO(raw))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"No se pudo leer el PDF: {e}")
    if not text.strip():
        raise HTTPException(400, "El PDF no tiene texto extraíble (¿es un escaneo/imagen? Pegá el texto a mano).")
    return {"text": text}


@app.post("/api/parse")
async def parse(req: ParseReq):
    if not req.termsheet.strip():
        raise HTTPException(400, "Termsheet vacío.")
    try:
        terms = await _provider().parse_termsheet(req.termsheet)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Error parseando el termsheet: {e}")
    return terms


# --------------------------------------------------------------------------- #
# Cuantitativo
# --------------------------------------------------------------------------- #
@app.post("/api/quant")
async def run_quant(req: QuantReq):
    try:
        result = quant.run_quant(req.terms)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Error en el cuantitativo: {e}")
    return JSONResponse(result)


# --------------------------------------------------------------------------- #
# Veredicto determinístico (sin IA)
# --------------------------------------------------------------------------- #
@app.post("/api/verdict")
async def run_verdict(req: VerdictReq):
    try:
        result = scoring.run_verdict(req.terms, req.quant, req.profile, req.credito)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Error calculando el veredicto: {e}")
    return JSONResponse(result)


# --------------------------------------------------------------------------- #
# Export PDF (estilo MaximUs, vía Chrome headless)
# --------------------------------------------------------------------------- #
class ReportReq(BaseModel):
    terms: dict
    quant: dict
    verdict: dict
    narrative_html: str = ""
    logo: str = ""


@app.post("/api/report")
async def report(req: ReportReq):
    import pdf_report
    try:
        pdf = pdf_report.build_pdf(req.terms, req.quant, req.verdict, req.narrative_html, req.logo)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"No se pudo generar el PDF: {e}")
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="analisis_nota.pdf"'},
    )


# --------------------------------------------------------------------------- #
# Streaming de fases
# --------------------------------------------------------------------------- #
@app.post("/api/phase")
async def phase(req: PhaseReq):
    if req.phase not in (1, 2, 3, 4):
        raise HTTPException(400, "Fase inválida.")

    async def gen():
        try:
            async for kind, data in _provider().stream_phase(
                req.phase, req.termsheet, req.terms, req.quant, req.prior, req.verdict
            ):
                yield f"data: {json.dumps({'kind': kind, 'data': data}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'kind': 'error', 'data': str(e)})}\n\n"
        yield f"data: {json.dumps({'kind': 'done', 'data': ''})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
