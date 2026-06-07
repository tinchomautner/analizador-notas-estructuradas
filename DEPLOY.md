# Cómo publicar la web para compartir

Hay dos caminos. Para **compartir y que lo revisen**, el más rápido y que funciona 100%
es el **Opción A (túnel)**. Para una URL permanente, la **Opción B (nube)**.

---

## Opción A — Túnel (rápido, todo funciona) ✅ recomendado

La app corre **en tu máquina** (donde Yahoo Finance y Chrome funcionan bien) y un túnel
le da una **URL pública** para compartir. Ideal para que alguien lo revise.

### Pasos
1. **Levantá la app** local: doble clic en `Iniciar Analizador.bat` (queda en
   `http://127.0.0.1:8742`).
2. **Instalá cloudflared** (una vez): bajalo de
   <https://github.com/cloudflare/cloudflared/releases> (`cloudflared-windows-amd64.exe`),
   renombralo `cloudflared.exe`.
3. **Abrí el túnel** (en otra terminal):
   ```
   cloudflared tunnel --url http://127.0.0.1:8742
   ```
4. Te imprime una URL tipo `https://algo-al-azar.trycloudflare.com` → **esa la compartís**.
   Mientras tu PC y el túnel estén prendidos, cualquiera entra desde ahí.

### La AI (OpenAI) — ya queda configurada
No hay que hacer nada extra: la app usa la `OPENAI_API_KEY` de tu `backend/.env` local.
Como corre en tu máquina, la IA funciona igual que cuando lo probás vos.

> Contras: tu PC tiene que estar encendida y la URL cambia cada vez que reabrís el túnel.

---

## Opción B — Nube (URL permanente, en Render)

URL fija 24/7, pero **ojo**: Yahoo Finance suele **bloquear IPs de servidores cloud**, así
que el cuantitativo puede fallar intermitentemente. Sirve para mostrar la herramienta;
para uso real conviene la Opción A o una fuente de datos paga.

### Pasos
1. El repo ya tiene un **`Dockerfile`** (con Chromium incluido para el PDF). Ya está en
   GitHub.
2. Entrá a <https://render.com> → creá cuenta (gratis) → **New + → Web Service**.
3. **Conectá** el repo `tinchomautner/analizador-notas-estructuradas`.
4. Render detecta el `Dockerfile` solo. Runtime: **Docker**. Plan: **Free**.
5. **Configurá la AI (clave):** en **Environment → Add Environment Variable**:
   - `OPENAI_API_KEY` = `sk-...` (tu key — se carga acá, NO en el código)
   - `OPENAI_MODEL` = `gpt-4o`
   - `LLM_PROVIDER` = `openai`
6. **Create Web Service** → Render buildea y te da una URL pública
   (`https://analizador-notas-estructuradas.onrender.com`).

### Notas
- La key se carga **solo** en el panel de Render (variables de entorno), nunca en el repo.
- El plan free se "duerme" tras inactividad (la primera carga tarda ~30s en despertar).
- Si el cuantitativo falla por bloqueo de Yahoo, usá la Opción A.

---

## Importante: rotá tu API key
Tu `OPENAI_API_KEY` fue compartida en el chat de desarrollo. Por seguridad, **rotala**
en <https://platform.openai.com/api-keys> (revocar la vieja, crear una nueva) y actualizá
solo la línea `OPENAI_API_KEY=` de tu `.env` (Opción A) o la variable en Render (Opción B).
