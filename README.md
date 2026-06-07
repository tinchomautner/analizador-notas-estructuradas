# Analizador de Notas Estructuradas

Herramienta interna de **LATAM ConsultUs** para evaluar notas estructuradas
(autocallables, phoenix, reverse convertibles, capital protegido) a partir de su
termsheet y emitir un **veredicto claro**: ¿conviene o no?

> El análisis es informativo y **no constituye una recomendación ni asesoramiento de
> inversión**. La decisión final es del asesor y su cliente.

## Qué hace

Del termsheet a un veredicto en minutos:

1. **Lectura del termsheet** (PDF/texto) → extrae los términos con un LLM (OpenAI),
   o se cargan a mano.
2. **Cuantitativo** (Python, datos en vivo de Yahoo Finance vía `yfinance`):
   - Probabilidad de quiebre de barrera y de pérdida — **Monte Carlo (10.000
     trayectorias, deriva neutral al riesgo)** + ventanas históricas.
   - Métricas por subyacente, correlaciones (5a), fundamentales (P/E, EV/EBITDA,
     crecimiento, etc.), distribución worst-of, tabla de payoff, **backtest de
     quiebres de barrera** y drawdown.
3. **Veredicto determinístico** (`scoring.py`) — score 0-100 → semáforo
   **FAVORABLE / NEUTRAL / DESFAVORABLE** sobre 3 pilares (Riesgo/Retorno 50% ·
   Valor relativo 25% · Idoneidad 25%) + alertas (red flags) + resumen del porqué.
   No usa IA: es reproducible.
4. **Informe ampliado (opcional, IA)** — narrativa cualitativa con búsqueda web en
   vivo (solo fuentes financieras reputadas), alimentada con el veredicto y los
   datos calculados para que no se contradiga ni invente.
5. **Export PDF** estilo MaximUs (carátula navy/dorado, páginas internas en blanco
   para imprimir, horizontal).

## Stack

- **Backend:** FastAPI + `quant.py` (numpy/pandas/yfinance) + `scoring.py`
  (determinístico) + `openai_client.py` (Chat Completions + Responses API con
  `web_search`). PDF vía Chrome headless (`--print-to-pdf`).
- **Frontend:** una sola página (React por CDN), estética MaximUs.

## Setup

Requiere Python 3.12+ y Google Chrome (para el PDF).

```bash
cd backend
python -m venv ../.venv
../.venv/Scripts/pip install -r requirements.txt   # Windows
# (Linux/Mac: ../.venv/bin/pip install -r requirements.txt)

cp .env.example .env          # y completá las claves (ver abajo)
../.venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 8742
```

Abrir <http://127.0.0.1:8742>. En Windows alcanza con doble clic en
`Iniciar Analizador.bat`.

### Configuración del LLM (`backend/.env`)

```
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...        # tu key de OpenAI
OPENAI_MODEL=gpt-4o          # recomendado (gpt-4o-mini es más barato pero menos fiable)
```

- El **veredicto y el cuantitativo NO usan IA** — funcionan sin API key.
- La API key sólo se usa para **leer el termsheet** y el **informe ampliado**.
- `LLM_PROVIDER=anthropic` para usar Claude (completar `ANTHROPIC_API_KEY`).
- **`.env` está en `.gitignore` — nunca se commitea la API key.**

## Estructura

```
backend/
  main.py            # FastAPI: /api/parse, /api/quant, /api/verdict, /api/phase, /api/report
  quant.py           # Monte Carlo + histórico + fundamentales (Yahoo Finance)
  scoring.py         # veredicto determinístico (3 pilares + red flags)
  openai_client.py   # LLM OpenAI (parseo + narrativa con web search)
  claude_client.py   # LLM Anthropic + prompt builder compartido
  prompts.py         # system prompts por fase
  pdf_report.py      # genera el PDF (HTML → Chrome headless)
frontend/index.html  # UI
```
