import os
import json
import re
import asyncio
import threading
import logging
from collections import defaultdict, deque
from typing import Dict, Any, Optional

import httpx
from flask import Flask, request

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    ForceReply,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from firestore_db import (
    load_commands  as db_load_commands,
    save_commands  as db_save_commands,
    load_admins    as db_load_admins,
    save_admins    as db_save_admins,
    load_persona   as db_load_persona,
    save_persona   as db_save_persona,
    load_stats     as db_load_stats,
    save_stats     as db_save_stats,
)

# ====================== LOGGING ======================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ====================== CONFIG ======================
OWNER_ID         = 6011835055
ADMIN_GROUP_ID   = -1003852446283
WORKING_GROUP_ID = -1002075852265
DEFAULT_ADMIN_IDS = [OWNER_ID]

TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN is missing")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL   = "llama-3.3-70b-versatile"
RENDER_HOST  = os.environ.get("WEBHOOK_HOST") or os.environ.get("RENDER_EXTERNAL_HOSTNAME")

# ====================== DEFAULT PERSONA ======================
DEFAULT_AI_INSTRUCTIONS = [
    "ענה בעברית ברוב המקרים.",
    "היה קצר, ברור, חכם, טכנולוגי ונעים לקריאה.",
    "השתמש באימוג'ים במינון יפה ולא מוגזם.",
    "אם חסר מידע, שאל שאלה קצרה אחת בלבד.",
    "אל תמציא עובדות.",
]

# ====================== GLOBAL STATE ======================
flask_app = Flask(__name__)
application = (
    Application.builder()
    .token(TOKEN)
    .concurrent_updates(8)
    .build()
)

bot_loop  = asyncio.new_event_loop()
bot_ready = threading.Event()
BOT_USERNAME: str = ""

COMMANDS: Dict[str, str] = {}
ADMIN_IDS = DEFAULT_ADMIN_IDS[:]

AI_PERSONA: Dict[str, Any] = {
    "instructions": DEFAULT_AI_INSTRUCTIONS[:],
    "version": 1,
    "updated_at": "",
}
SYSTEM_PROMPT_CACHE: str = ""
CHAT_MEMORY  = defaultdict(lambda: deque(maxlen=12))
USER_STATE   = defaultdict(lambda: {"private_ai_mode": False})
ADMIN_FLOWS: Dict[int, Dict[str, Any]] = {}
STATS: Dict[str, Any] = {
    "ai": 0,
    "buttons":  defaultdict(int),
    "commands": defaultdict(int),
}

# ====================== FILE HELPERS (Firestore) ======================

def ensure_data_files() -> None:
    pass  # Firestore מאתחל לבד


def load_commands() -> None:
    global COMMANDS
    COMMANDS = db_load_commands()


def save_commands() -> None:
    db_save_commands(COMMANDS)
    global SYSTEM_PROMPT_CACHE
    SYSTEM_PROMPT_CACHE = rebuild_system_prompt()


def load_admins() -> None:
    global ADMIN_IDS
    ADMIN_IDS = db_load_admins(OWNER_ID)


def save_admins() -> None:
    db_save_admins(ADMIN_IDS)


def load_persona() -> None:
    global AI_PERSONA
    AI_PERSONA = db_load_persona()


def save_persona() -> None:
    db_save_persona(AI_PERSONA)


def load_stats() -> None:
    global STATS
    data = db_load_stats()
    STATS["ai"]       = data["ai"]
    STATS["commands"] = defaultdict(int, data["commands"])
    STATS["buttons"]  = defaultdict(int, data["buttons"])


def save_stats() -> None:
    db_save_stats(STATS)


def rebuild_system_prompt() -> str:
    lines = list(AI_PERSONA["instructions"]) if AI_PERSONA["instructions"] else ["ענה בעברית בקצרה ובאופן ברור."]

    if COMMANDS:
        lines.append("")
        lines.append("📚 פקודות זמינות במערכת (ענה עליהן אם ישאלו):")
        for cmd, reply in COMMANDS.items():
            lines.append(f"• {cmd} → {reply}")
        lines.append(f"סה\"כ: {len(COMMANDS)} פקודות.")

    if STATS.get("commands"):
        top = sorted(STATS["commands"].items(), key=lambda x: x[1], reverse=True)[:5]
        lines.append("")
        lines.append("📊 פקודות הכי מבוקשות לאחרונה:")
        for cmd, count in top:
            lines.append(f"• {cmd} — {count} פעמים")

    if STATS.get("ai", 0) > 0:
        lines.append(f"סה\"כ שאילתות AI שנענו: {STATS['ai']}.")

    return "\n".join(lines)


def refresh_persona(reset_memory: bool = True) -> str:
    global SYSTEM_PROMPT_CACHE
    SYSTEM_PROMPT_CACHE = rebuild_system_prompt()
    AI_PERSONA["version"] += 1
    save_persona()
    if reset_memory:
        CHAT_MEMORY.clear()
    return SYSTEM_PROMPT_CACHE


# ====================== HELPERS ======================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id == OWNER_ID


def format_instruction_preview(text: str, limit: int = 42) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_admin_zone(chat_id: int) -> bool:
    return chat_id > 0 or chat_id == ADMIN_GROUP_ID


def split_text_chunks(text: str, max_len: int = 3800) -> list:
    lines = text.split("\n")
    chunks = []
    current = ""
    for line in lines:
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > max_len:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


def build_home_text(user_id: int, chat_id: int) -> str:
    ai_state = "פעיל ✅" if USER_STATE[user_id]["private_ai_mode"] else "כבוי ❌"
    if is_admin(user_id):
        return (
            f"👋 שלום לך!\n\n"
            f"🤖 AI אישי: {ai_state}\n"
            f"🧠 אישיות AI: גרסה {AI_PERSONA['version']}\n"
            f"📚 פקודות שמורות: {len(COMMANDS)}\n\n"
            "הכל מסודר לפי תחומים:\n"
            "• 🤖 AI — תגובות חכמות דרך תיוג או מצב אישי\n"
            "• 📚 פקודות רגילות — הוספה, עריכה, מחיקה והצגה\n"
            "• 👮 מנהלים — ניהול מנהלים בזמן אמת\n"
            "• 📊 סטטיסטיקה — שימושים ולחיצות\n"
        )
    return (
        "👋 שלום!\n\n"
        "אני בוט חכם עם אישיות טכנולוגית ⚡\n"
        "בקבוצות — תייגו אותי כדי שאענה.\n"
        "בפרטי — אפשר לדבר איתי רגיל.\n"
    )


def build_home_keyboard(user_id: int, chat_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🤖 AI", callback_data="menu_ai_toggle"),
            InlineKeyboardButton("📚 פקודות רגילות", callback_data="menu_commands"),
        ],
        [
            InlineKeyboardButton("📊 סטטיסטיקה", callback_data="menu_stats"),
            InlineKeyboardButton("ℹ️ הסבר", callback_data="menu_help"),
        ],
    ]
    if is_admin(user_id) and is_admin_zone(chat_id):
        rows.extend([
            [
                InlineKeyboardButton("🧠 אישיות AI", callback_data="menu_ai_panel"),
                InlineKeyboardButton("👮 מנהלים", callback_data="menu_admins"),
            ],
            [
                InlineKeyboardButton("📘 מדריך לבעלים", callback_data="menu_owner_help"),
                InlineKeyboardButton("🏠 בית", callback_data="menu_home"),
            ],
        ])
    else:
        rows.append([InlineKeyboardButton("🤖 איך לדבר איתי?", callback_data="menu_ai_hint")])
    return InlineKeyboardMarkup(rows)


def build_help_text() -> str:
    return (
        "ℹ️ הסבר מהיר\n\n"
        "• הכפתורים הם הדרך הראשית לשלוט בבוט\n"
        "• AI בקבוצה עובד רק כשמתייגים את הבוט או עונים לו\n"
        "• מנהלים מנהלים הכל בפרטי או בקבוצת המנהלים\n"
        "• פקודות רגילות הן דברים שהבוט עונה להם לפי טקסט\n"
        "• ה-AI יודע על כל הפקודות ויכול לענות עליהן בשפה חופשית\n"
    )


def build_owner_help_text() -> str:
    return (
        "📘 מדריך לבעלים\n\n"
        "🤖 AI אישי — מצב אישי למנהל בפרטי (ענה לכל הודעה)\n"
        "📚 פקודות רגילות — ניהול פקודות שמורות\n"
        "🧠 אישיות AI — הוספה/עריכה/מחיקה של הוראות ה-AI\n"
        "👮 מנהלים — הוספה/הסרה/הצגה של מנהלים\n"
        "📊 סטטיסטיקה — לחיצות ופעילות (נשמר גם אחרי איפוס)\n\n"
        "💡 ניהול מתבצע בקבוצת המנהלים או בפרטי.\n"
        "💡 כל פקודה שמוסיפים — ה-AI מתעדכן אוטומטית.\n"
        "💡 כל הנתונים שמורים ב-Firestore — לא נמחקים לעולם!\n"
    )


def build_commands_panel_text() -> str:
    load_commands()
    return (
        f"📚 פקודות רגילות\n\n"
        f"פקודות שמורות כרגע: {len(COMMANDS)}\n\n"
        "כאן מנהלים פקודות טקסט רגילות שהבוט מזהה לפי ההודעה.\n"
        "ה-AI מתעדכן אוטומטית בכל שינוי!\n"
    )


def build_commands_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 הצג כל הפקודות", callback_data="cmd_view"),
            InlineKeyboardButton("➕ הוסף פקודה",      callback_data="cmd_add"),
        ],
        [
            InlineKeyboardButton("✏️ ערוך פקודה",  callback_data="cmd_edit"),
            InlineKeyboardButton("🗑️ מחק פקודה", callback_data="cmd_remove"),
        ],
        [InlineKeyboardButton("⬅️ חזרה", callback_data="menu_home")],
    ])


def build_ai_panel_text() -> str:
    return (
        "🧠 אישיות ה-AI\n\n"
        f"• גרסה: {AI_PERSONA['version']}\n"
        f"• הוראות פעילות: {len(AI_PERSONA['instructions'])}\n"
        f"• פקודות שה-AI יודע עליהן: {len(COMMANDS)}\n\n"
        "כאן שולטים בהוראות של ה-AI.\n"
        "ה-AI מחובר אוטומטית לפקודות ולסטטיסטיקה."
    )


def build_ai_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ הוסף הוראה", callback_data="ai_add"),
            InlineKeyboardButton("✏️ ערוך הוראה", callback_data="ai_edit"),
        ],
        [
            InlineKeyboardButton("🗑️ מחק הוראה", callback_data="ai_remove"),
            InlineKeyboardButton("🔄 אתחל AI",    callback_data="ai_reload"),
        ],
        [
            InlineKeyboardButton("📜 הצג הוראות", callback_data="ai_view"),
            InlineKeyboardButton("⬅️ חזרה",       callback_data="menu_home"),
        ],
    ])


def build_instruction_picker(mode: str) -> InlineKeyboardMarkup:
    rows = []
    for idx, item in enumerate(AI_PERSONA["instructions"]):
        label = format_instruction_preview(item, 22)
        if mode == "edit":
            rows.append([InlineKeyboardButton(f"✏️ {idx + 1}. {label}", callback_data=f"ai_edit_pick:{idx}")])
        else:
            rows.append([InlineKeyboardButton(f"🗑️ {idx + 1}. {label}", callback_data=f"ai_remove_pick:{idx}")])
    rows.append([InlineKeyboardButton("⬅️ חזרה", callback_data="menu_ai_panel")])
    return InlineKeyboardMarkup(rows)


def build_confirm_remove_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ כן, למחוק", callback_data=f"ai_remove_yes:{index}"),
            InlineKeyboardButton("❌ לא",         callback_data="ai_remove_no"),
        ]
    ])


def build_admins_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 הצג מנהלים", callback_data="admin_view")],
        [InlineKeyboardButton("➕ הוסף מנהל",  callback_data="admin_add")],
        [InlineKeyboardButton("➖ הסר מנהל",   callback_data="admin_remove")],
        [InlineKeyboardButton("⬅️ חזרה",       callback_data="menu_home")],
    ])


# ====================== GROQ ======================

async def ask_groq(user_id: int, prompt: str) -> str:
    if not GROQ_API_KEY:
        return "❌ לא הוגדר GROQ_API_KEY."

    history = list(CHAT_MEMORY[user_id])
    system_content = SYSTEM_PROMPT_CACHE or rebuild_system_prompt()
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 700,
                },
            )
            res.raise_for_status()
            data = res.json()

        answer = data["choices"][0]["message"]["content"].strip()
        CHAT_MEMORY[user_id].append({"role": "user",      "content": prompt})
        CHAT_MEMORY[user_id].append({"role": "assistant", "content": answer})
        STATS["ai"] += 1
        save_stats()
        return answer
    except Exception as e:
        logger.exception("Groq error: %s", e)
        return f"⚠️ שגיאת AI: {type(e).__name__}: {str(e)[:150]}"


async def send_ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    if not update.message:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = await ask_groq(update.effective_user.id, prompt)
    await update.message.reply_text(f"🤖 {reply}")


# ====================== BOT ADDRESSING ======================

def mention_regex() -> Optional[re.Pattern]:
    if not BOT_USERNAME:
        return None
    return re.compile(rf"@{re.escape(BOT_USERNAME)}\b", re.IGNORECASE)


def is_bot_mentioned(message) -> bool:
    if not BOT_USERNAME:
        return False
    text = (message.text or message.caption or "").lower()
    return f"@{BOT_USERNAME.lower()}" in text


def strip_bot_mention(message_text: str) -> str:
    rx = mention_regex()
    if not rx:
        return message_text.strip()
    cleaned = rx.sub(" ", message_text)
    return re.sub(r"\s+", " ", cleaned).strip()


async def delete_message_later(bot, chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest:
        pass
    except Exception as e:
        logger.exception("delete_message_later error: %s", e)


# ====================== ADMIN FLOWS ======================

async def apply_admin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str) -> bool:
    if not update.message:
        return False
    user_id = update.effective_user.id
    flow    = ADMIN_FLOWS.get(user_id)
    if not flow:
        return False
    step = flow.get("step")

    if step == "cmd_add":
        ADMIN_FLOWS[user_id] = {"step": "cmd_add_reply", "command": message_text.strip()}
        await update.message.reply_text("✍️ מעולה. עכשיו שלח את התגובה של הפקודה הזו.", reply_markup=ForceReply(selective=True))
        return True

    if step == "cmd_add_reply":
        command = flow.get("command", "").strip()
        COMMANDS[command] = message_text.strip()
        save_commands()
        ADMIN_FLOWS.pop(user_id, None)
        await update.message.reply_text("✅ הפקודה נשמרה — ה-AI עודכן אוטומטית! 🤖")
        return True

    if step == "cmd_edit_select":
        command = message_text.strip()
        if command in COMMANDS:
            ADMIN_FLOWS[user_id] = {"step": "cmd_edit_command", "old_command": command}
            await update.message.reply_text("✏️ עכשיו שלח את השם החדש של הפקודה.", reply_markup=ForceReply(selective=True))
        else:
            ADMIN_FLOWS.pop(user_id, None)
            await update.message.reply_text("⚠️ הפקודה לא נמצאה.")
        return True

    if step == "cmd_edit_command":
        old_command = flow.get("old_command", "")
        if old_command in COMMANDS:
            ADMIN_FLOWS[user_id] = {"step": "cmd_edit_reply", "old_command": old_command, "new_command": message_text.strip()}
            await update.message.reply_text("📝 עכשיו שלח את התגובה החדשה של הפקודה.", reply_markup=ForceReply(selective=True))
        else:
            ADMIN_FLOWS.pop(user_id, None)
            await update.message.reply_text("⚠️ הפקודה הישנה לא נמצאה.")
        return True

    if step == "cmd_edit_reply":
        old_command = flow.get("old_command", "")
        new_command = flow.get("new_command", "")
        if old_command in COMMANDS:
            del COMMANDS[old_command]
        COMMANDS[new_command] = message_text.strip()
        save_commands()
        ADMIN_FLOWS.pop(user_id, None)
        await update.message.reply_text("✅ הפקודה עודכנה — ה-AI עודכן אוטומטית! 🤖")
        return True

    if step == "cmd_remove_select":
        command = message_text.strip()
        if command in COMMANDS:
            del COMMANDS[command]
            save_commands()
            ADMIN_FLOWS.pop(user_id, None)
            await update.message.reply_text("🗑️ הפקודה נמחקה — ה-AI עודכן אוטומטית! 🤖")
        else:
            ADMIN_FLOWS.pop(user_id, None)
            await update.message.reply_text("⚠️ הפקודה לא נמצאה.")
        return True

    if step == "ai_add":
        AI_PERSONA["instructions"].append(message_text.strip())
        refresh_persona(reset_memory=True)
        ADMIN_FLOWS.pop(user_id, None)
        await update.message.reply_text("✨ ההוראה נוספה, ה-AI אותחל ומוכן.")
        return True

    if step == "ai_edit_select":
        try:
            index = int(message_text.strip()) - 1
        except ValueError:
            ADMIN_FLOWS.pop(user_id, None)
            await update.message.reply_text("⚠️ צריך לשלוח מספר הוראה.")
            return True
        if 0 <= index < len(AI_PERSONA["instructions"]):
            ADMIN_FLOWS[user_id] = {"step": "ai_edit_new", "index": index}
            await update.message.reply_text("✏️ עכשיו שלח את הטקסט החדש של ההוראה הזו.", reply_markup=ForceReply(selective=True))
        else:
            ADMIN_FLOWS.pop(user_id, None)
            await update.message.reply_text("⚠️ ההוראה לא נמצאה.")
        return True

    if step == "ai_edit_new":
        index = int(flow["index"])
        if 0 <= index < len(AI_PERSONA["instructions"]):
            AI_PERSONA["instructions"][index] = message_text.strip()
            refresh_persona(reset_memory=True)
            ADMIN_FLOWS.pop(user_id, None)
            await update.message.reply_text("✅ ההוראה עודכנה, ה-AI אותחל ומוכן.")
        else:
            ADMIN_FLOWS.pop(user_id, None)
            await update.message.reply_text("⚠️ ההוראה לא נמצאה.")
        return True

    if step == "admin_add":
        try:
            new_admin = int(message_text.strip())
        except ValueError:
            ADMIN_FLOWS.pop(user_id, None)
            await update.message.reply_text("⚠️ ה-ID חייב להיות מספר.")
            return True
        if new_admin not in ADMIN_IDS:
            ADMIN_IDS.append(new_admin)
            save_admins()
            await update.message.reply_text(f"👮 מנהל {new_admin} נוסף בהצלחה.")
        else:
            await update.message.reply_text("המנהל הזה כבר קיים.")
        ADMIN_FLOWS.pop(user_id, None)
        return True

    if step == "admin_remove":
        try:
            remove_admin = int(message_text.strip())
        except ValueError:
            ADMIN_FLOWS.pop(user_id, None)
            await update.message.reply_text("⚠️ ה-ID חייב להיות מספר.")
            return True
        if remove_admin in ADMIN_IDS and remove_admin != OWNER_ID:
            ADMIN_IDS.remove(remove_admin)
            save_admins()
            await update.message.reply_text(f"👮 מנהל {remove_admin} הוסר בהצלחה.")
        else:
            await update.message.reply_text("לא ניתן להסיר את המנהל הזה.")
        ADMIN_FLOWS.pop(user_id, None)
        return True

    return False


# ====================== HANDLERS ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await update.message.reply_text(build_home_text(user_id, chat_id), reply_markup=build_home_keyboard(user_id, chat_id))


async def owner_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(build_owner_help_text(), reply_markup=build_home_keyboard(update.effective_user.id, update.effective_chat.id))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = ["📊 סטטיסטיקה", "", f"🤖 שימוש ב-AI: {STATS['ai']}", ""]
    if STATS["commands"]:
        text.append("🔹 פקודות פעילות:")
        for cmd, count in sorted(STATS["commands"].items(), key=lambda x: x[1], reverse=True)[:10]:
            text.append(f"• {cmd} — {count}")
        text.append("")
    if STATS["buttons"]:
        text.append("🧩 כפתורים פעילים:")
        for key, count in sorted(STATS["buttons"].items(), key=lambda x: x[1], reverse=True)[:10]:
            text.append(f"• {key} — {count}")
    await update.message.reply_text("\n".join(text), reply_markup=build_home_keyboard(user_id, update.effective_chat.id))


async def handle_commands_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    load_commands()
    if text in COMMANDS:
        STATS["commands"][text] += 1
        save_stats()
        await update.message.reply_text(COMMANDS[text])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.message.text:
            return
        user_id   = update.effective_user.id
        chat_type = update.effective_chat.type
        text      = update.message.text.strip()

        if user_id in ADMIN_FLOWS:
            if await apply_admin_flow(update, context, text):
                return

        if chat_type == "private" and is_admin(user_id) and USER_STATE[user_id]["private_ai_mode"]:
            await send_ai_reply(update, context, text)
            return

        if chat_type in ("group", "supergroup"):
            if is_bot_mentioned(update.message):
                prompt = strip_bot_mention(text) or "ענה בקצרה ובכיף."
                await send_ai_reply(update, context, prompt)
                return
            await handle_commands_text(update, context)
            return

        await handle_commands_text(update, context)

    except Exception as e:
        logger.exception("Error in handle_message: %s", e)
        if update.message:
            await update.message.reply_text("⚠️ אירעה שגיאה פנימית.")


async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        new_member = update.message.new_chat_members[0]
        chat_id    = update.effective_chat.id
        welcome_msg = await update.message.reply_text(f"🎉 ברוך הבא {new_member.full_name}!\nנעים להכיר 😄")
        asyncio.create_task(delete_message_later(context.bot, chat_id, update.message.message_id, 2))
        asyncio.create_task(delete_message_later(context.bot, chat_id, welcome_msg.message_id, 58))
    except Exception as e:
        logger.exception("Error in welcome: %s", e)


async def goodbye(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        asyncio.create_task(delete_message_later(context.bot, update.effective_chat.id, update.message.message_id, 3))
    except Exception as e:
        logger.exception("Error in goodbye: %s", e)


async def react_to_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message.reply_to_message or update.message
    if message:
        try:
            await context.bot.set_message_reaction(
                chat_id=update.effective_chat.id,
                message_id=message.message_id,
                reaction=ReactionTypeEmoji(emoji="👍"),
            )
        except Exception as e:
            logger.exception("Error in react: %s", e)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        build_home_text(update.effective_user.id, update.effective_chat.id),
        reply_markup=build_home_keyboard(update.effective_user.id, update.effective_chat.id),
    )


async def add_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("אין לך הרשאה לזה.")
        return
    ADMIN_FLOWS[update.effective_user.id] = {"step": "cmd_add"}
    await update.message.reply_text("➕ שלח לי את שם הפקודה, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))


async def edit_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("אין לך הרשאה לזה.")
        return
    load_commands()
    if not COMMANDS:
        await update.message.reply_text("אין עדיין פקודות לעריכה.")
        return
    ADMIN_FLOWS[update.effective_user.id] = {"step": "cmd_edit_select"}
    await update.message.reply_text("✏️ שלח לי את שם הפקודה שתרצה לערוך, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))


async def remove_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("אין לך הרשאה לזה.")
        return
    load_commands()
    if not COMMANDS:
        await update.message.reply_text("אין עדיין פקודות למחיקה.")
        return
    ADMIN_FLOWS[update.effective_user.id] = {"step": "cmd_remove_select"}
    await update.message.reply_text("🗑️ שלח לי את שם הפקודה שתרצה למחוק, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))


async def add_ai_instruction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("אין לך הרשאה לזה.")
        return
    ADMIN_FLOWS[update.effective_user.id] = {"step": "ai_add"}
    await update.message.reply_text("➕ שלח לי את ההוראה החדשה של ה-AI, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))


async def add_admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("רק הבעלים יכול לעשות את זה.")
        return
    ADMIN_FLOWS[update.effective_user.id] = {"step": "admin_add"}
    await update.message.reply_text("➕ שלח לי את ה-ID של המנהל החדש, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))


async def remove_admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("רק הבעלים יכול לעשות את זה.")
        return
    ADMIN_FLOWS[update.effective_user.id] = {"step": "admin_remove"}
    await update.message.reply_text("➖ שלח לי את ה-ID של המנהל להסרה, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))


# ====================== CALLBACK ROUTER ======================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else 0
    data    = query.data or ""
    STATS["buttons"][data] += 1
    save_stats()

    async def edit(text: str, reply_markup=None):
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup)
        except Exception:
            try:
                await query.message.reply_text(text=text, reply_markup=reply_markup)
            except Exception:
                pass

    if data == "menu_home":
        await edit(build_home_text(user_id, chat_id), build_home_keyboard(user_id, chat_id))
        return

    if data == "menu_help":
        await edit(build_help_text(), build_home_keyboard(user_id, chat_id))
        return

    if data == "menu_owner_help":
        if not is_admin(user_id):
            await edit("אין לך גישה למדריך הזה.", build_home_keyboard(user_id, chat_id))
            return
        await edit(build_owner_help_text(), build_home_keyboard(user_id, chat_id))
        return

    if data == "menu_stats":
        lines = ["📊 סטטיסטיקה", "", f"🤖 שימוש ב-AI: {STATS['ai']}", ""]
        if STATS["commands"]:
            lines.append("🔹 פקודות פעילות:")
            for cmd, count in sorted(STATS["commands"].items(), key=lambda x: x[1], reverse=True)[:10]:
                lines.append(f"• {cmd} — {count}")
            lines.append("")
        if STATS["buttons"]:
            lines.append("🧩 כפתורים פעילים:")
            for key, count in sorted(STATS["buttons"].items(), key=lambda x: x[1], reverse=True)[:10]:
                lines.append(f"• {key} — {count}")
        await edit("\n".join(lines), build_home_keyboard(user_id, chat_id))
        return

    if data == "menu_commands":
        await edit(build_commands_panel_text(), build_commands_panel_keyboard())
        return

    if data == "cmd_view":
        load_commands()
        if not COMMANDS:
            await edit("📋 עדיין אין פקודות שמורות.", build_commands_panel_keyboard())
            return
        lines  = [f"• {cmd}\n  ↳ {reply}" for cmd, reply in COMMANDS.items()]
        body   = "\n\n".join(lines)
        header = f"📋 כל הפקודות ({len(COMMANDS)} סה\"כ):\n\n"
        if len(header + body) <= 4000:
            await edit(header + body, build_commands_panel_keyboard())
        else:
            chunks = split_text_chunks(body, max_len=3800)
            await edit(f"📋 {len(COMMANDS)} פקודות — שולח ב-{len(chunks)} הודעות ⬇️", build_commands_panel_keyboard())
            for i, chunk in enumerate(chunks, 1):
                prefix = f"📋 פקודות ({i}/{len(chunks)}):\n\n" if len(chunks) > 1 else "📋 פקודות:\n\n"
                await query.message.reply_text(prefix + chunk)
        return

    if data == "cmd_add":
        if not is_admin(user_id):
            await edit("אין לך הרשאה לזה.", build_commands_panel_keyboard())
            return
        ADMIN_FLOWS[user_id] = {"step": "cmd_add"}
        await edit("➕ הוספת פקודה רגילה נפתחה. המשך בהודעה הבאה.", build_commands_panel_keyboard())
        await query.message.reply_text("➕ שלח לי את שם הפקודה החדשה, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))
        return

    if data == "cmd_edit":
        if not is_admin(user_id):
            await edit("אין לך הרשאה לזה.", build_commands_panel_keyboard())
            return
        load_commands()
        if not COMMANDS:
            await edit("אין עדיין פקודות לעריכה.", build_commands_panel_keyboard())
            return
        ADMIN_FLOWS[user_id] = {"step": "cmd_edit_select"}
        await edit("✏️ שלח לי את שם הפקודה שתרצה לערוך.", build_commands_panel_keyboard())
        await query.message.reply_text("✏️ כתוב את שם הפקודה שתרצה לערוך, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))
        return

    if data == "cmd_remove":
        if not is_admin(user_id):
            await edit("אין לך הרשאה לזה.", build_commands_panel_keyboard())
            return
        load_commands()
        if not COMMANDS:
            await edit("אין עדיין פקודות למחיקה.", build_commands_panel_keyboard())
            return
        ADMIN_FLOWS[user_id] = {"step": "cmd_remove_select"}
        await edit("🗑️ שלח לי את שם הפקודה שתרצה למחוק.", build_commands_panel_keyboard())
        await query.message.reply_text("🗑️ כתוב את שם הפקודה למחיקה, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))
        return

    if data == "menu_ai_toggle":
        if not is_admin(user_id):
            await edit("אין לך הרשאה למצב AI אישי.", build_home_keyboard(user_id, chat_id))
            return
        USER_STATE[user_id]["private_ai_mode"] = not USER_STATE[user_id]["private_ai_mode"]
        state = "פעיל ✅" if USER_STATE[user_id]["private_ai_mode"] else "כבוי ❌"
        await edit(f"🤖 מצב AI אישי עכשיו: {state}", build_home_keyboard(user_id, chat_id))
        return

    if data == "menu_ai_hint":
        await edit("🤖 בקבוצה: תייגו אותי או ענו לי ואגיב.\nבפרטי: מנהלים יכולים להפעיל AI אישי שעונה על הכל.\nה-AI יודע על כל הפקודות!", build_home_keyboard(user_id, chat_id))
        return

    if data == "menu_ai_panel":
        if not is_admin(user_id):
            await edit("אין לך גישה לאישיות ה-AI.", build_home_keyboard(user_id, chat_id))
            return
        await edit(build_ai_panel_text(), build_ai_panel_keyboard())
        return

    if data == "ai_view":
        if not is_admin(user_id):
            return
        if AI_PERSONA["instructions"]:
            lines = ["📜 ההוראות הפעילות:\n"]
            for idx, instr in enumerate(AI_PERSONA["instructions"], start=1):
                lines.append(f"{idx}. {instr}")
            text = "\n".join(lines)
        else:
            text = "📜 עדיין אין הוראות פעילות."
        await edit(text, build_ai_panel_keyboard())
        return

    if data == "ai_add":
        if not is_admin(user_id):
            return
        ADMIN_FLOWS[user_id] = {"step": "ai_add"}
        await edit("➕ הוספת הוראת AI נפתחה. המשך בהודעה הבאה.", build_ai_panel_keyboard())
        await query.message.reply_text("➕ שלח לי את ההוראה החדשה, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))
        return

    if data == "ai_edit":
        if not is_admin(user_id):
            return
        if not AI_PERSONA["instructions"]:
            await edit("אין עדיין הוראות לערוך.", build_ai_panel_keyboard())
            return
        ADMIN_FLOWS[user_id] = {"step": "ai_edit_select"}
        await edit("✏️ שלח את מספר ההוראה שתרצה לערוך.", build_ai_panel_keyboard())
        await query.message.reply_text("✏️ כתוב את מספר ההוראה שתרצה לערוך, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))
        return

    if data.startswith("ai_edit_pick:"):
        return

    if data == "ai_remove":
        if not is_admin(user_id):
            return
        if not AI_PERSONA["instructions"]:
            await edit("אין עדיין הוראות למחוק.", build_ai_panel_keyboard())
            return
        await edit("🗑️ בחר את ההוראה שתרצה למחוק:", build_instruction_picker("remove"))
        return

    if data.startswith("ai_remove_pick:"):
        if not is_admin(user_id):
            return
        try:
            index = int(data.split(":", 1)[1])
        except ValueError:
            return
        if not (0 <= index < len(AI_PERSONA["instructions"])):
            await edit("ההוראה לא נמצאה.", build_ai_panel_keyboard())
            return
        instr = AI_PERSONA["instructions"][index]
        await edit(f"האם למחוק את ההוראה הזו?\n\n{instr}", build_confirm_remove_keyboard(index))
        return

    if data.startswith("ai_remove_yes:"):
        if not is_admin(user_id):
            return
        try:
            index = int(data.split(":", 1)[1])
        except ValueError:
            return
        if 0 <= index < len(AI_PERSONA["instructions"]):
            del AI_PERSONA["instructions"][index]
            refresh_persona(reset_memory=True)
            ADMIN_FLOWS.pop(user_id, None)
            await edit("🗑️ ההוראה נמחקה, ה-AI אותחל ומוכן.", build_ai_panel_keyboard())
        return

    if data == "ai_remove_no":
        ADMIN_FLOWS.pop(user_id, None)
        await edit("המחיקה בוטלה.", build_ai_panel_keyboard())
        return

    if data == "ai_reload":
        if not is_admin(user_id):
            return
        refresh_persona(reset_memory=True)
        await edit("♻️ ה-AI אותחל מחדש והוא מוכן לעבודה.", build_ai_panel_keyboard())
        return

    if data == "menu_admins":
        if user_id != OWNER_ID:
            await edit("רק הבעלים יכול לראות את זה.", build_home_keyboard(user_id, chat_id))
            return
        await edit("👮 ניהול מנהלים\n\nכאן מוסיפים, מסירים ורואים מנהלים בזמן אמת.", build_admins_keyboard())
        return

    if data == "admin_view":
        if user_id != OWNER_ID:
            await edit("רק הבעלים יכול לראות את זה.", build_home_keyboard(user_id, chat_id))
            return
        await edit("👮 מנהלים פעילים:\n\n" + "\n".join(f"• {x}" for x in ADMIN_IDS), build_admins_keyboard())
        return

    if data == "admin_add":
        if user_id != OWNER_ID:
            return
        ADMIN_FLOWS[user_id] = {"step": "admin_add"}
        await edit("➕ הוספת מנהל נפתחה. המשך בהודעה הבאה.", build_admins_keyboard())
        await query.message.reply_text("➕ שלח לי את ה-ID של המנהל החדש, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))
        return

    if data == "admin_remove":
        if user_id != OWNER_ID:
            return
        ADMIN_FLOWS[user_id] = {"step": "admin_remove"}
        await edit("➖ הסרת מנהל נפתחה. המשך בהודעה הבאה.", build_admins_keyboard())
        await query.message.reply_text("➖ שלח לי את ה-ID של המנהל להסרה, כתשובה להודעה הזו.", reply_markup=ForceReply(selective=True))
        return


# ====================== APP SETUP ======================

application.add_handler(CommandHandler("start",     start))
application.add_handler(CommandHandler("menu",      menu_command))
application.add_handler(CommandHandler("ownerhelp", owner_help_command))
application.add_handler(CommandHandler("stats",     stats_command))
application.add_handler(CommandHandler("ADD",       add_command_entry))
application.add_handler(CommandHandler("EDIT",      edit_command_entry))
application.add_handler(CommandHandler("DEL",       remove_command_entry))
application.add_handler(CommandHandler("AIADD",     add_ai_instruction))
application.add_handler(CommandHandler("ADM",       add_admin_entry))
application.add_handler(CommandHandler("RADM",      remove_admin_entry))
application.add_handler(CommandHandler("react",     react_to_message))
application.add_handler(MessageHandler(filters.Regex(r"^/start(@\w+)?$"), start))
application.add_handler(CallbackQueryHandler(callback_router))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
application.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER,  goodbye))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("PTB error: %s", context.error)


application.add_error_handler(error_handler)


# ====================== STARTUP ======================

async def startup_async() -> None:
    global BOT_USERNAME, SYSTEM_PROMPT_CACHE

    ensure_data_files()
    load_commands()
    load_admins()
    load_persona()
    load_stats()

    SYSTEM_PROMPT_CACHE = rebuild_system_prompt()

    await application.initialize()
    await application.start()

    me = await application.bot.get_me()
    BOT_USERNAME = me.username or ""
    logger.info("Bot username: %s", BOT_USERNAME)

    webhook_url = f"https://{RENDER_HOST}/telegram/{TOKEN}" if RENDER_HOST else None
    if webhook_url:
        await application.bot.set_webhook(
            url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Webhook set to: %s", webhook_url)

    bot_ready.set()


def bot_thread_target():
    asyncio.set_event_loop(bot_loop)
    bot_loop.run_until_complete(startup_async())
    bot_loop.run_forever()


threading.Thread(target=bot_thread_target, daemon=True).start()


# ====================== FLASK ROUTES ======================

@flask_app.route("/")
def index():
    return "✅ הבוט פעיל.", 200


@flask_app.route("/ping")
def ping():
    return "OK", 200


@flask_app.route("/setwebhook")
def set_webhook_route():
    if not RENDER_HOST:
        return "WEBHOOK_HOST חסר", 500
    if not bot_ready.is_set():
        return "הבוט עדיין לא מוכן", 503
    webhook_url = f"https://{RENDER_HOST}/telegram/{TOKEN}"
    try:
        future = asyncio.run_coroutine_threadsafe(
            application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True),
            bot_loop,
        )
        future.result(timeout=20)
        return f"✅ Webhook set to: {webhook_url}", 200
    except Exception as e:
        logger.exception("setwebhook error: %s", e)
        return f"ERROR: {e}", 500


@flask_app.route(f"/telegram/{TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        if not bot_ready.is_set():
            return "Bot not ready", 503
        data = request.get_json(silent=True)
        if not data:
            return "No data", 400
        update = Update.de_json(data, application.bot)
        bot_loop.call_soon_threadsafe(application.update_queue.put_nowait, update)
        return "OK", 200
    except Exception as e:
        logger.exception("Webhook error: %s", e)
        return "ERROR", 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    try:
        from waitress import serve
        logger.info("Starting with waitress on port %d", port)
        serve(flask_app, host="0.0.0.0", port=port, threads=4)
    except ImportError:
        flask_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)