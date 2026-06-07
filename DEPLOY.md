# Cómo publicar la web para compartir

Hay dos caminos. Para **compartir y que lo revisen**, el más rápido y que funciona 100%
es el **Opción A (túnel)**. Para una URL permanente, la **Opción B (nube)**.

---

## Opción A — Túnel (rápido, todo funciona) ✅ recomendado

La app corre **en tu máquina** (donde Yahoo Finance y Chrome funcionan bien) y un túnel
le da una **URL pública** para compartir. Ideal para que alguien lo revise.

### Pasos (un solo doble clic)
1. Doble clic en **`Compartir Analizador.bat`**. Ese .bat solo:
   - descarga `cloudflared` la primera vez (no hay que instalar nada),
   - levanta el servidor,
   - abre el túnel y muestra el **link público** `https://....trycloudflare.com`.
2. Copiás ese link de la ventana y **lo compartís**. Mientras la ventana esté abierta,
   cualquiera entra desde ahí.

> El link cambia cada vez que reabrís el `.bat`, y tu PC tiene que estar prendida.

### La AI (OpenAI) — ya queda configurada
No hay que hacer nada extra: la app usa la `OPENAI_API_KEY` de tu `backend/.env` local.
Como corre en tu máquina, la IA funciona igual que cuando lo probás vos.

> Contras: tu PC tiene que estar encendida y la URL cambia cada vez que reabrís el túnel.

---

## Opción B — Nube (URL permanente, en Render)

URL fija 24/7, pero **ojo**: Yahoo Finance suele **bloquear IPs de servidores cloud**, así
que el cuantitativo puede fallar intermitentemente. Sirve para mostrar la herramienta;
para uso real conviene la Opción A o una fuente de datos paga.

### Pasos (1 clic con el botón)
1. Clic en el botón **Deploy to Render** del README (o pegá en el navegador:
   `https://render.com/deploy?repo=https://github.com/tinchomautner/analizador-notas-estructuradas`).
   Render lee el `render.yaml` y el `Dockerfile` (con Chromium para el PDF) solo.
2. Te pide la **`OPENAI_API_KEY`** → pegá tu key. Queda como **secreto en Render, NO en el repo**.
   (`OPENAI_MODEL=gpt-4o` y `LLM_PROVIDER=openai` ya vienen seteados por el blueprint.)
3. **Apply / Create** → buildea (~5 min la primera vez) y te da la URL pública
   `https://analizador-notas-estructuradas.onrender.com`.
4. **CHEQUEÁ YAHOO FINANCE** (lo que querés saber): abrí
   `https://<tu-app>.onrender.com/api/diag`:
   - `"yahoo_finance": {"anda": true, ...}` → el cuantitativo funciona en la nube. 🎉
   - `"anda": false` (Yahoo bloquea la IP del datacenter) → el cuantitativo fallará; usá la Opción A.

### Notas
- La key se carga **solo** en el panel de Render (variables de entorno), nunca en el repo.
- El plan free se "duerme" tras inactividad (la primera carga tarda ~30s en despertar).
- Si el cuantitativo falla por bloqueo de Yahoo, usá la Opción A.

---

## Importante: rotá tu API key
Tu `OPENAI_API_KEY` fue compartida en el chat de desarrollo. Por seguridad, **rotala**
en <https://platform.openai.com/api-keys> (revocar la vieja, crear una nueva) y actualizá
solo la línea `OPENAI_API_KEY=` de tu `.env` (Opción A) o la variable en Render (Opción B).
