import os
import json
import logging
import re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import anthropic
import httpx

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TU_CHAT_ID        = os.environ.get("TU_CHAT_ID", "")
GOOGLE_CLIENT_ID  = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_FILE = "google_token.json"

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CLIENTE ANTHROPIC ────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── GOOGLE CALENDAR: OBTENER SERVICIO ────────────────────────────────────────
def get_calendar_service():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds)

def save_credentials(creds):
    with open(TOKEN_FILE, "w") as f:
        json.dump({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
        }, f)

# ─── IA: INTERPRETAR MENSAJE DEL USUARIO ──────────────────────────────────────
def interpretar_mensaje(texto: str) -> dict:
    ahora = datetime.now()
    fecha_hoy = ahora.strftime("%Y-%m-%d")
    hora_ahora = ahora.strftime("%H:%M")
    dia_semana = ahora.strftime("%A")  # en inglés, lo convertimos abajo

    dias_es = {
        "Monday": "lunes", "Tuesday": "martes", "Wednesday": "miércoles",
        "Thursday": "jueves", "Friday": "viernes", "Saturday": "sábado", "Sunday": "domingo"
    }
    dia_hoy = dias_es.get(dia_semana, dia_semana)

    prompt = f"""Hoy es {dia_hoy} {fecha_hoy} y son las {hora_ahora} (hora de Lima, Perú).

Analiza este mensaje sobre Google Calendar y extrae la intención. Responde SOLO con JSON válido, sin backticks.

Mensaje: "{texto}"

Acciones posibles:
- "crear": crear un nuevo evento
- "listar": ver eventos (hoy, mañana, esta semana, etc.)
- "eliminar": borrar un evento
- "modificar": cambiar un evento existente
- "buscar": buscar un evento por nombre

Para "crear" devuelve:
{{"accion": "crear", "titulo": "título del evento", "fecha_inicio": "YYYY-MM-DDTHH:MM:SS", "fecha_fin": "YYYY-MM-DDTHH:MM:SS", "descripcion": "descripción opcional o vacío", "duracion_minutos": 60}}

Para "listar" devuelve:
{{"accion": "listar", "desde": "YYYY-MM-DD", "hasta": "YYYY-MM-DD", "descripcion_rango": "hoy / mañana / esta semana / etc."}}

Para "eliminar" devuelve:
{{"accion": "eliminar", "titulo_busqueda": "nombre aproximado del evento", "fecha_aproximada": "YYYY-MM-DD o vacío"}}

Para "modificar" devuelve:
{{"accion": "modificar", "titulo_busqueda": "nombre aproximado del evento", "cambios": {{"titulo": "nuevo título o vacío", "fecha_inicio": "nueva fecha o vacío", "fecha_fin": "nueva fecha o vacío"}}}}

Para "buscar" devuelve:
{{"accion": "buscar", "titulo_busqueda": "texto a buscar"}}

Reglas de fechas:
- "mañana" = {(ahora + timedelta(days=1)).strftime("%Y-%m-%d")}
- "hoy" = {fecha_hoy}
- Si no se especifica hora, usa 09:00:00 para inicio
- Si no se especifica duración, asume 1 hora
- "lunes que viene", "el viernes", etc.: calcula la fecha correcta relativa a hoy
- "esta semana" va desde hoy hasta el próximo domingo

Si el mensaje no tiene relación con el calendario, devuelve:
{{"accion": "desconocido"}}"""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

# ─── OPERACIONES EN GOOGLE CALENDAR ───────────────────────────────────────────
def crear_evento(service, datos: dict) -> dict:
    evento = {
        "summary": datos["titulo"],
        "description": datos.get("descripcion", ""),
        "start": {
            "dateTime": datos["fecha_inicio"],
            "timeZone": "America/Lima"
        },
        "end": {
            "dateTime": datos["fecha_fin"],
            "timeZone": "America/Lima"
        }
    }
    return service.events().insert(calendarId="primary", body=evento).execute()

def listar_eventos(service, desde: str, hasta: str) -> list:
    time_min = f"{desde}T00:00:00-05:00"
    time_max = f"{hasta}T23:59:59-05:00"
    result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
        maxResults=15
    ).execute()
    return result.get("items", [])

def buscar_eventos(service, texto: str, fecha: str = None) -> list:
    params = {
        "calendarId": "primary",
        "q": texto,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 5
    }
    if fecha:
        params["timeMin"] = f"{fecha}T00:00:00-05:00"
    else:
        params["timeMin"] = datetime.now().strftime("%Y-%m-%dT00:00:00-05:00")
    result = service.events().list(**params).execute()
    return result.get("items", [])

def eliminar_evento(service, event_id: str):
    service.events().delete(calendarId="primary", eventId=event_id).execute()

def modificar_evento(service, event_id: str, cambios: dict) -> dict:
    evento = service.events().get(calendarId="primary", eventId=event_id).execute()
    if cambios.get("titulo"):
        evento["summary"] = cambios["titulo"]
    if cambios.get("fecha_inicio"):
        evento["start"]["dateTime"] = cambios["fecha_inicio"]
    if cambios.get("fecha_fin"):
        evento["end"]["dateTime"] = cambios["fecha_fin"]
    return service.events().update(calendarId="primary", eventId=event_id, body=evento).execute()

# ─── FORMATEAR EVENTO PARA MENSAJE ────────────────────────────────────────────
def formatear_evento(e: dict) -> str:
    titulo = e.get("summary", "(sin título)")
    start = e.get("start", {})
    dt = start.get("dateTime") or start.get("date", "")
    if "T" in dt:
        d = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        fecha_str = d.strftime("%a %d/%m %H:%M")
    else:
        fecha_str = dt
    return f"📅 *{titulo}* — {fecha_str}"

# ─── HANDLERS DE TELEGRAM ─────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = get_calendar_service()
    if service:
        msg = (
            "👋 Hola Brian\\! Soy tu asistente de Google Calendar\\.\n\n"
            "✅ *Ya estás conectado a Google Calendar\\.*\n\n"
            "Puedes escribirme cosas como:\n"
            "• `Reunión con cliente mañana a las 10am`\n"
            "• `¿Qué tengo el viernes?`\n"
            "• `Cancela la reunión con el estudio`\n"
            "• `Mueve la llamada de mañana al lunes a las 3pm`\n\n"
            "📋 *Comandos:*\n"
            "/hoy \\- Ver eventos de hoy\n"
            "/semana \\- Ver eventos de esta semana\n"
            "/auth \\- Reconectar Google Calendar\n"
            "/ayuda \\- Ver esta ayuda"
        )
    else:
        msg = (
            "👋 Hola Brian\\! Soy tu asistente de Google Calendar\\.\n\n"
            "⚠️ *Primero necesitas conectar tu cuenta de Google\\.*\n\n"
            "Usa el comando /auth para comenzar\\."
        )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")

async def auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"]
            }
        },
        scopes=SCOPES
    )
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent"
    )
    context.user_data["flow"] = flow

    msg = (
        f"🔐 *Conectar Google Calendar*\n\n"
        f"1\\. Abre este enlace en tu navegador:\n{auth_url}\n\n"
        f"2\\. Inicia sesión con tu cuenta de Google\n"
        f"3\\. Copia el código que aparece\n"
        f"4\\. Pégalo aquí en este chat"
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2", disable_web_page_preview=True)

async def hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = get_calendar_service()
    if not service:
        await update.message.reply_text("⚠️ Primero conecta tu Google Calendar con /auth")
        return
    hoy_str = datetime.now().strftime("%Y-%m-%d")
    eventos = listar_eventos(service, hoy_str, hoy_str)
    if not eventos:
        await update.message.reply_text("📅 No tienes eventos hoy.")
        return
    lineas = ["📅 *Eventos de hoy:*\n"]
    for e in eventos:
        lineas.append(formatear_evento(e))
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

async def semana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = get_calendar_service()
    if not service:
        await update.message.reply_text("⚠️ Primero conecta tu Google Calendar con /auth")
        return
    hoy_dt = datetime.now()
    fin_dt = hoy_dt + timedelta(days=7)
    eventos = listar_eventos(service, hoy_dt.strftime("%Y-%m-%d"), fin_dt.strftime("%Y-%m-%d"))
    if not eventos:
        await update.message.reply_text("📅 No tienes eventos esta semana.")
        return
    lineas = ["📅 *Próximos 7 días:*\n"]
    for e in eventos:
        lineas.append(formatear_evento(e))
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if TU_CHAT_ID and str(update.effective_user.id) != TU_CHAT_ID:
        await update.message.reply_text("⛔ No estás autorizado.")
        return

    texto = update.message.text.strip()
    if not texto:
        return

    # ¿Es un código de autorización OAuth?
    if "flow" in context.user_data and len(texto) > 20 and " " not in texto:
        flow = context.user_data["flow"]
        try:
            flow.fetch_token(code=texto)
            save_credentials(flow.credentials)
            del context.user_data["flow"]
            await update.message.reply_text(
                "✅ *¡Google Calendar conectado exitosamente!*\n\n"
                "Ya puedes escribirme eventos en lenguaje natural.\n"
                "Ejemplo: `Reunión con cliente mañana a las 10am`",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error OAuth: {e}")
            await update.message.reply_text("❌ Código inválido. Usa /auth para intentar de nuevo.")
        return

    service = get_calendar_service()
    if not service:
        await update.message.reply_text(
            "⚠️ Primero necesitas conectar tu Google Calendar.\nUsa el comando /auth"
        )
        return

    await update.message.reply_text("⏳ Procesando...")

    try:
        datos = interpretar_mensaje(texto)
        accion = datos.get("accion", "desconocido")

        # ── CREAR ──────────────────────────────────────────────────────────────
        if accion == "crear":
            evento = crear_evento(service, datos)
            link = evento.get("htmlLink", "")
            msg = (
                f"✅ *Evento creado*\n\n"
                f"📌 {datos['titulo']}\n"
                f"🕐 {datos['fecha_inicio'][11:16]} — {datos['fecha_fin'][11:16]}\n"
                f"📆 {datos['fecha_inicio'][:10]}"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")

        # ── LISTAR ─────────────────────────────────────────────────────────────
        elif accion == "listar":
            eventos = listar_eventos(service, datos["desde"], datos["hasta"])
            rango = datos.get("descripcion_rango", "el período solicitado")
            if not eventos:
                await update.message.reply_text(f"📅 No tienes eventos para {rango}.")
                return
            lineas = [f"📅 *Eventos — {rango}:*\n"]
            for e in eventos:
                lineas.append(formatear_evento(e))
            await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

        # ── BUSCAR ─────────────────────────────────────────────────────────────
        elif accion == "buscar":
            eventos = buscar_eventos(service, datos["titulo_busqueda"])
            if not eventos:
                await update.message.reply_text(f"🔍 No encontré eventos con ese nombre.")
                return
            lineas = [f"🔍 *Resultados:*\n"]
            for e in eventos:
                lineas.append(formatear_evento(e))
            await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

        # ── ELIMINAR ───────────────────────────────────────────────────────────
        elif accion == "eliminar":
            eventos = buscar_eventos(service, datos["titulo_busqueda"], datos.get("fecha_aproximada"))
            if not eventos:
                await update.message.reply_text(f"🔍 No encontré el evento '{datos['titulo_busqueda']}'.")
                return
            if len(eventos) == 1:
                # Guardar en contexto y pedir confirmación
                context.user_data["eliminar_evento"] = eventos[0]
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Sí, eliminar", callback_data=f"del_{eventos[0]['id']}"),
                        InlineKeyboardButton("❌ Cancelar", callback_data="cancel")
                    ]
                ]
                await update.message.reply_text(
                    f"¿Confirmas eliminar este evento?\n\n{formatear_evento(eventos[0])}",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
            else:
                lineas = ["Encontré varios eventos. ¿Cuál quieres eliminar?\n"]
                keyboard = []
                for e in eventos[:5]:
                    titulo = e.get("summary", "(sin título)")
                    lineas.append(formatear_evento(e))
                    keyboard.append([InlineKeyboardButton(f"❌ {titulo[:30]}", callback_data=f"del_{e['id']}")])
                keyboard.append([InlineKeyboardButton("Cancelar", callback_data="cancel")])
                await update.message.reply_text(
                    "\n".join(lineas),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )

        # ── MODIFICAR ──────────────────────────────────────────────────────────
        elif accion == "modificar":
            eventos = buscar_eventos(service, datos["titulo_busqueda"], None)
            if not eventos:
                await update.message.reply_text(f"🔍 No encontré el evento '{datos['titulo_busqueda']}'.")
                return
            evento = eventos[0]
            modificar_evento(service, evento["id"], datos["cambios"])
            await update.message.reply_text(
                f"✅ *Evento actualizado*\n\n{formatear_evento(evento)}",
                parse_mode="Markdown"
            )

        else:
            await update.message.reply_text(
                "🤔 No entendí eso como una acción de calendario. Intenta con algo como:\n"
                "• `Reunión mañana a las 3pm`\n"
                "• `¿Qué tengo hoy?`\n"
                "• `Cancela la llamada del viernes`"
            )

    except json.JSONDecodeError:
        await update.message.reply_text("❌ No pude interpretar el mensaje. Sé más específico.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Ocurrió un error: {str(e)[:100]}")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        await query.edit_message_text("❌ Acción cancelada.")
        return

    if data.startswith("del_"):
        event_id = data[4:]
        service = get_calendar_service()
        try:
            eliminar_evento(service, event_id)
            await query.edit_message_text("🗑️ Evento eliminado correctamente.")
        except Exception as e:
            await query.edit_message_text(f"❌ Error al eliminar: {str(e)[:100]}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", start))
    app.add_handler(CommandHandler("auth", auth))
    app.add_handler(CommandHandler("hoy", hoy))
    app.add_handler(CommandHandler("semana", semana))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje))

    logger.info("Bot Calendar iniciado...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
