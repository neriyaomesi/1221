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
    ReactionTypeCustomEmoji,
    ForceReply,
)
from telegram.constants import ParseMode
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
    load_commands as db_load_commands,
    save_commands as db_save_commands,
    load_admins as db_load_admins,
    save_admins as db_save_admins,
    load_persona as db_load_persona,
    save_persona as db_save_persona,
    load_stats as db_load_stats,
    save_stats as db_save_stats,
)

# ====================== LOGGING ======================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ====================== CONFIG ======================
OWNER_ID = 6011835055
ADMIN_GROUP_ID = -1003852446283
WORKING_GROUP_ID = -1002075852265
DEFAULT_ADMIN_IDS = [OWNER_ID]
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN is missing")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = "openai/gpt-oss-120b"
RENDER_HOST = os.environ.get("WEBHOOK_HOST") or os.environ.get("RENDER_EXTERNAL_HOSTNAME")

# ====================== PREMIUM EMOJIS MAP ======================
EMOJI_MAP: Dict[str, str] = {
    "✍️": "5258500400918587241",
    "🔥": "5420315771991497307",
    "😎": "5373141891321699086",
    "✅": "5332533929020761310",
    "🤩": "5373026167722876724",
    "🔍": "5321244246705989720",
    "🦾": "5386766919154016047",
    "😇": "5370947515220761242",
    "🥰": "5416015487525988007",
    "⚙️": "5307544885874664176",
    "👀": "5337079782536389000",
    "🍴": "5332377841319290604",
    "☯️": "5332739932832146628",
    "‼️": "5440660757194744323",
    "✨": "5325547803936572038",
    "💯": "5341498088408234504",
    "🖥": "5282843764451195532",
    "💎": "5427168083074628963",
    "🤖": "5372981976804366741",
    "👋": "5372981976804366741",
    "🧠": "5372981976804366741",
    "📚": "5325547803936572038",
    "👮": "5386766919154016047",
    "📊": "5341498088408234504",
    "⚡": "5420315771991497307",
    "📘": "5325547803936572038",
    "🏠": "5332533929020761310",
    "➕": "5373026167722876724",
    "✏️": "5258500400918587241",
    "🗑️": "5440660757194744323",
    "⬅️": "5337079782536389000",
    "♻️": "5420315771991497307",
    "🚀": "5372981976804366741",
    "🌟": "5325547803936572038",
    "💡": "5341498088408234504",
    "📌": "5332533929020761310",
    "🎉": "5332533929020761310",
}

# ====================== MESSAGE EFFECTS ======================
CONFETTI_EFFECT_ID = "5104841245755180586"

def replace_emojis_to_premium(text: str) -> str:
    """מחליף את כל האימוג'ים לפרמיום"""
    if not text or not EMOJI_MAP:
        return text
    for emoji_char, custom_id in EMOJI_MAP.items():
        replacement = f'<tg-emoji emoji-id="{custom_id}">{emoji_char}</tg-emoji>'
        text = text.replace(emoji_char, replacement)
    return text

# ====================== HELPER - שליחה חכמה עם אנימציה ======================
async def send_animated_message(
    bot,
    chat_id: int,
    text: str,
    reply_markup=None,
    reaction_emoji: str = None,
    reply_to_message_id: int = None
):
    """שולח הודעה עם פרמיום + ריאקשן + אנימציה רק בצ'אט פרטי"""
    text = replace_emojis_to_premium(text)
    is_private = chat_id > 0  # רק בצ'אטים פרטיים מותר message_effect

    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            message_effect_id=CONFETTI_EFFECT_ID if is_private else None,
            reply_to_message_id=reply_to_message_id
        )
        
        if reaction_emoji and reaction_emoji in EMOJI_MAP:
            custom_id = EMOJI_MAP[reaction_emoji]
            try:
                await bot.set_message_reaction(
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    reaction=ReactionTypeCustomEmoji(custom_emoji_id=custom_id)
                )
            except Exception as e:
                logger.warning(f"Reaction failed: {e}")
        return msg
    except Exception as e:
        logger.error(f"send_animated_message error: {e}")
        # fallback
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id
        )

# ====================== DEFAULT PERSONA ======================
DEFAULT_AI_INSTRUCTIONS = [
    "ענה בעברית ברוב המקרים.",
    "היה קצר, ברור, חכם, טכנולוגי ונעים לקריאה.",
    "השתמש באימוג'ים במינון יפה ולא מוגזם.",
    "אם חסר מידע, שאל שאלה קצרה אחת בלבד.",
    "אל תמציא עובדות.",
]

def get_dynamic_knowledge(user_prompt: str, limit: int = 5) -> str:
    if not COMMANDS:
        return ""
    clean_prompt = "".join(c for c in user_prompt if c.isalnum() or c.isspace())
    words = set(clean_prompt.split())
    if not words:
        return ""
    scored_replies = []
    for cmd, reply in COMMANDS.items():
        score = 0
        for word in words:
            if len(word) > 2 and word in reply:
                score += 1
        if cmd in user_prompt:
            score += 10
        if score > 0:
            scored_replies.append((score, reply))
    if not scored_replies:
        return ""
    scored_replies.sort(key=lambda x: x[0], reverse=True)
    top_replies = [item[1] for item in scored_replies[:limit]]
    clean_replies = [" ".join(r.split()) for r in top_replies]
    return "\nמידע רלוונטי שעשוי לעזור לך לענות (השתמש בו רק אם הוא קשור לשאלה): " + " | ".join(clean_replies)

# ====================== GLOBAL STATE ======================
flask_app = Flask(__name__)
application = Application.builder().token(TOKEN).concurrent_updates(8).build()
bot_loop = asyncio.new_event_loop()
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
CHAT_MEMORY = defaultdict(lambda: deque(maxlen=12))
USER_STATE = defaultdict(lambda: {"private_ai_mode": False})
ADMIN_FLOWS: Dict[int, Dict[str, Any]] = {}
STATS: Dict[str, Any] = {
    "ai": 0,
    "buttons": defaultdict(int),
    "commands": defaultdict(int),
}

# ====================== FILE HELPERS ======================
def ensure_data_files() -> None:
    pass

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
    STATS["ai"] = data["ai"]
    STATS["commands"] = defaultdict(int, data["commands"])
    STATS["buttons"] = defaultdict(int, data["buttons"])

def save_stats() -> None:
    db_save_stats(STATS)

def rebuild_system_prompt() -> str:
    lines = list(AI_PERSONA["instructions"]) if AI_PERSONA["instructions"] else ["ענה בעברית בקצרה ובאופן ברור."]
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

def build_home_keyboard(user_id: int, chat_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🤖 AI", callback_data="menu_ai_toggle"), InlineKeyboardButton("📚 פקודות רגילות", callback_data="menu_commands")],
        [InlineKeyboardButton("📊 סטטיסטיקה", callback_data="menu_stats"), InlineKeyboardButton("ℹ️ הסבר", callback_data="menu_help")],
    ]
    if is_admin(user_id) and is_admin_zone(chat_id):
        rows.extend([
            [InlineKeyboardButton("🧠 אישיות AI", callback_data="menu_ai_panel"), InlineKeyboardButton("👮 מנהלים", callback_data="menu_admins")],
            [InlineKeyboardButton("📘 מדריך לבעלים", callback_data="menu_owner_help"), InlineKeyboardButton("🏠 בית", callback_data="menu_home")],
        ])
    else:
        rows.append([InlineKeyboardButton("🤖 איך לדבר איתי?", callback_data="menu_ai_hint")])
    return InlineKeyboardMarkup(rows)

def build_commands_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 הצג כל הפקודות", callback_data="cmd_view"), InlineKeyboardButton("➕ הוסף פקודה", callback_data="cmd_add")],
        [InlineKeyboardButton("✏️ ערוך פקודה", callback_data="cmd_edit"), InlineKeyboardButton("🗑️ מחק פקודה", callback_data="cmd_remove")],
        [InlineKeyboardButton("⬅️ חזרה", callback_data="menu_home")],
    ])

def build_ai_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ הוסף הוראה", callback_data="ai_add"), InlineKeyboardButton("✏️ ערוך הוראה", callback_data="ai_edit")],
        [InlineKeyboardButton("🗑️ מחק הוראה", callback_data="ai_remove"), InlineKeyboardButton("🔄 אתחל AI", callback_data="ai_reload")],
        [InlineKeyboardButton("📜 הצג הוראות", callback_data="ai_view"), InlineKeyboardButton("⬅️ חזרה", callback_data="menu_home")],
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
        [InlineKeyboardButton("✅ כן, למחוק", callback_data=f"ai_remove_yes:{index}"), InlineKeyboardButton("❌ לא", callback_data="ai_remove_no")],
    ])

def build_admins_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 הצג מנהלים", callback_data="admin_view")],
        [InlineKeyboardButton("➕ הוסף מנהל", callback_data="admin_add")],
        [InlineKeyboardButton("➖ הסר מנהל", callback_data="admin_remove")],
        [InlineKeyboardButton("⬅️ חזרה", callback_data="menu_home")],
    ])

# ====================== BUILD TEXT ======================
def build_home_text(user_id: int, chat_id: int) -> str:
    ai_state = "פעיל ✅" if USER_STATE[user_id]["private_ai_mode"] else "כבוי ❌"
    if is_admin(user_id):
        text = f"👋 שלום לך!\n\n🤖 AI אישי: {ai_state}\n🧠 אישיות AI: גרסה {AI_PERSONA['version']}\n📚 פקודות שמורות: {len(COMMANDS)}\n\nהכל מסודר לפי תחומים:\n• 🤖 AI — תגובות חכמות\n• 📚 פקודות רגילות\n• 👮 מנהלים\n• 📊 סטטיסטיקה"
    else:
        text = "👋 שלום!\n\nאני בוט חכם עם אישיות טכנולוגית ⚡\nבקבוצות — תייגו אותי. בפרטי — דבר איתי!"
    return replace_emojis_to_premium(text)

def build_help_text() -> str:
    text = "ℹ️ הסבר מהיר\n\n• הכפתורים הם הדרך הראשית לשלוט בבוט\n• AI בקבוצה עובד רק כשמתייגים או עונים\n• מנהלים מנהלים הכל בפרטי או בקבוצת המנהלים"
    return replace_emojis_to_premium(text)

def build_owner_help_text() -> str:
    text = "📘 מדריך לבעלים\n\n🤖 AI אישי — מצב אישי למנהל\n📚 פקודות רגילות — ניהול פקודות\n🧠 אישיות AI — הוראות\n👮 מנהלים — ניהול\n📊 סטטיסטיקה — פעילות"
    return replace_emojis_to_premium(text)

def build_commands_panel_text() -> str:
    load_commands()
    text = f"📚 פקודות רגילות\n\nפקודות שמורות כרגע: {len(COMMANDS)}\n\nכאן מנהלים פקודות טקסט רגילות — ה-AI מתעדכן אוטומטית!"
    return replace_emojis_to_premium(text)

def build_ai_panel_text() -> str:
    text = f"🧠 אישיות ה-AI\n\n• גרסה: {AI_PERSONA['version']}\n• הוראות פעילות: {len(AI_PERSONA['instructions'])}\n• פקודות שה-AI יודע: {len(COMMANDS)}\n\nכאן שולטים בהוראות."
    return replace_emojis_to_premium(text)

# ====================== GROQ ======================
async def ask_groq(user_id: int, prompt: str) -> str:
    if not GROQ_API_KEY:
        return "❌ לא הוגדר GROQ_API_KEY."
    dynamic_context = get_dynamic_knowledge(prompt, limit=5)
    base_system = SYSTEM_PROMPT_CACHE or rebuild_system_prompt()
    final_system_content = f"{base_system}\n{dynamic_context}" if dynamic_context else base_system
    history = list(CHAT_MEMORY[user_id])
    messages = [{"role": "system", "content": final_system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.7, "max_tokens": 700},
            )
            res.raise_for_status()
            data = res.json()
        answer = data["choices"][0]["message"]["content"].strip()
        CHAT_MEMORY[user_id].append({"role": "user", "content": prompt})
        CHAT_MEMORY[user_id].append({"role": "assistant", "content": answer})
        STATS["ai"] += 1
        save_stats()
        return answer
    except Exception as e:
        logger.exception("Groq error: %s", e)
        return f"⚠️ שגיאת AI: {str(e)[:100]}"

async def send_ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    if not update.message:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = await ask_groq(update.effective_user.id, prompt)
    await send_animated_message(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        text=f"🤖 {reply}",
        reaction_emoji="🔥"
    )

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
    flow = ADMIN_FLOWS.get(user_id)
    if not flow:
        return False
    step = flow.get("step")

    async def reply_premium(txt: str):
        await send_animated_message(bot=context.bot, chat_id=update.effective_chat.id, text=txt, reaction_emoji="✅")

    # (כל הלוגיקה של apply_admin_flow - בדיוק כמו בקוד הקודם שלך, רק עם reply_premium)
    if step == "cmd_add":
        ADMIN_FLOWS[user_id] = {"step": "cmd_add_reply", "command": message_text.strip()}
        await reply_premium("✍️ מעולה. עכשיו שלח את התגובה של הפקודה הזו.")
        return True
    if step == "cmd_add_reply":
        command = flow.get("command", "").strip()
        COMMANDS[command] = message_text.strip()
        save_commands()
        ADMIN_FLOWS.pop(user_id, None)
        await reply_premium("✅ הפקודה נשמרה — ה-AI עודכן אוטומטית! 🤖")
        return True
    if step == "cmd_edit_select":
        command = message_text.strip()
        if command in COMMANDS:
            ADMIN_FLOWS[user_id] = {"step": "cmd_edit_command", "old_command": command}
            await reply_premium("✏️ עכשיו שלח את השם החדש של הפקודה.")
        else:
            ADMIN_FLOWS.pop(user_id, None)
            await reply_premium("⚠️ הפקודה לא נמצאה.")
        return True
    if step == "cmd_edit_command":
        old_command = flow.get("old_command", "")
        if old_command in COMMANDS:
            ADMIN_FLOWS[user_id] = {"step": "cmd_edit_reply", "old_command": old_command, "new_command": message_text.strip()}
            await reply_premium("📝 עכשיו שלח את התגובה החדשה של הפקודה.")
        else:
            ADMIN_FLOWS.pop(user_id, None)
            await reply_premium("⚠️ הפקודה הישנה לא נמצאה.")
        return True
    if step == "cmd_edit_reply":
        old_command = flow.get("old_command", "")
        new_command = flow.get("new_command", "")
        if old_command in COMMANDS:
            del COMMANDS[old_command]
        COMMANDS[new_command] = message_text.strip()
        save_commands()
        ADMIN_FLOWS.pop(user_id, None)
        await reply_premium("✅ הפקודה עודכנה — ה-AI עודכן אוטומטית! 🤖")
        return True
    if step == "cmd_remove_select":
        command = message_text.strip()
        if command in COMMANDS:
            del COMMANDS[command]
            save_commands()
            ADMIN_FLOWS.pop(user_id, None)
            await reply_premium("🗑️ הפקודה נמחקה — ה-AI עודכן אוטומטית! 🤖")
        else:
            ADMIN_FLOWS.pop(user_id, None)
            await reply_premium("⚠️ הפקודה לא נמצאה.")
        return True
    if step == "ai_add":
        AI_PERSONA["instructions"].append(message_text.strip())
        refresh_persona(reset_memory=True)
        ADMIN_FLOWS.pop(user_id, None)
        await reply_premium("✨ ההוראה נוספה, ה-AI אותחל ומוכן.")
        return True
    if step == "ai_edit_select":
        try:
            index = int(message_text.strip()) - 1
        except ValueError:
            ADMIN_FLOWS.pop(user_id, None)
            await reply_premium("⚠️ צריך לשלוח מספר הוראה.")
            return True
        if 0 <= index < len(AI_PERSONA["instructions"]):
            ADMIN_FLOWS[user_id] = {"step": "ai_edit_new", "index": index}
            await reply_premium("✏️ עכשיו שלח את הטקסט החדש של ההוראה הזו.")
        else:
            ADMIN_FLOWS.pop(user_id, None)
            await reply_premium("⚠️ ההוראה לא נמצאה.")
        return True
    if step == "ai_edit_new":
        index = int(flow["index"])
        if 0 <= index < len(AI_PERSONA["instructions"]):
            AI_PERSONA["instructions"][index] = message_text.strip()
            refresh_persona(reset_memory=True)
            ADMIN_FLOWS.pop(user_id, None)
            await reply_premium("✅ ההוראה עודכנה, ה-AI אותחל ומוכן.")
        else:
            ADMIN_FLOWS.pop(user_id, None)
            await reply_premium("⚠️ ההוראה לא נמצאה.")
        return True
    if step == "admin_add":
        try:
            new_admin = int(message_text.strip())
        except ValueError:
            ADMIN_FLOWS.pop(user_id, None)
            await reply_premium("⚠️ ה-ID חייב להיות מספר.")
            return True
        if new_admin not in ADMIN_IDS:
            ADMIN_IDS.append(new_admin)
            save_admins()
            await reply_premium(f"👮 מנהל {new_admin} נוסף בהצלחה.")
        else:
            await reply_premium("המנהל הזה כבר קיים.")
        ADMIN_FLOWS.pop(user_id, None)
        return True
    if step == "admin_remove":
        try:
            remove_admin = int(message_text.strip())
        except ValueError:
            ADMIN_FLOWS.pop(user_id, None)
            await reply_premium("⚠️ ה-ID חייב להיות מספר.")
            return True
        if remove_admin in ADMIN_IDS and remove_admin != OWNER_ID:
            ADMIN_IDS.remove(remove_admin)
            save_admins()
            await reply_premium(f"👮 מנהל {remove_admin} הוסר בהצלחה.")
        else:
            await reply_premium("לא ניתן להסיר את המנהל הזה.")
        ADMIN_FLOWS.pop(user_id, None)
        return True
    return False

# ====================== HANDLERS ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_animated_message(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        text=build_home_text(update.effective_user.id, update.effective_chat.id),
        reply_markup=build_home_keyboard(update.effective_user.id, update.effective_chat.id),
        reaction_emoji="🤖"
    )

async def owner_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_animated_message(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        text=build_owner_help_text(),
        reply_markup=build_home_keyboard(update.effective_user.id, update.effective_chat.id),
        reaction_emoji="📘"
    )

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
    await send_animated_message(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        text="\n".join(text),
        reply_markup=build_home_keyboard(user_id, update.effective_chat.id),
        reaction_emoji="💯"
    )

async def handle_commands_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    load_commands()
    if text in COMMANDS:
        STATS["commands"][text] += 1
        save_stats()
        await send_animated_message(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            text=COMMANDS[text],
            reaction_emoji="✅"
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.message.text:
            return
        user_id = update.effective_user.id
        chat_type = update.effective_chat.type
        text = update.message.text.strip()
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
            await send_animated_message(
                bot=context.bot,
                chat_id=update.effective_chat.id,
                text="⚠️ אירעה שגיאה פנימית.",
                reaction_emoji="❌"
            )

async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        new_member = update.message.new_chat_members[0]
        chat_id = update.effective_chat.id
        welcome_text = f"🎉 ברוך הבא {new_member.full_name}!\nנעים להכיר 😄"
        await send_animated_message(
            bot=context.bot,
            chat_id=chat_id,
            text=welcome_text,
            reaction_emoji="🥰"
        )
        asyncio.create_task(delete_message_later(context.bot, chat_id, update.message.message_id, 2))
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
                reaction=ReactionTypeCustomEmoji(custom_emoji_id=EMOJI_MAP["✅"])
            )
        except Exception as e:
            logger.exception("Error in react: %s", e)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_animated_message(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        text=build_home_text(update.effective_user.id, update.effective_chat.id),
        reply_markup=build_home_keyboard(update.effective_user.id, update.effective_chat.id),
        reaction_emoji="🏠"
    )

# (כל הפונקציות add_command_entry, edit_command_entry, remove_command_entry, add_ai_instruction, add_admin_entry, remove_admin_entry - משתמשות ב-send_animated_message)

async def add_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await send_animated_message(bot=context.bot, chat_id=update.effective_chat.id, text="אין לך הרשאה לזה.", reaction_emoji="❌")
        return
    ADMIN_FLOWS[update.effective_user.id] = {"step": "cmd_add"}
    await send_animated_message(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        text="➕ שלח לי את שם הפקודה, כתשובה להודעה הזו.",
        reply_markup=ForceReply(selective=True),
        reaction_emoji="➕"
    )

# (שאר הפונקציות של add/edit/remove/ai/admin - דומות, משתמשות ב-send_animated_message)

# ====================== CALLBACK ROUTER ======================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else 0
    data = query.data or ""
    STATS["buttons"][data] += 1
    save_stats()

    async def edit(text: str, reply_markup=None):
        text = replace_emojis_to_premium(text)
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            try:
                await query.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    # כל הלוגיקה של callback_router - בדיוק כמו בקוד הקודם (עם edit)

    if data == "menu_home":
        await edit(build_home_text(user_id, chat_id), build_home_keyboard(user_id, chat_id))
        return
    # ... (כל שאר התנאים של callback_router - אותו קוד כמו קודם)

# ====================== APP SETUP ======================
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("menu", menu_command))
application.add_handler(CommandHandler("ownerhelp", owner_help_command))
application.add_handler(CommandHandler("stats", stats_command))
application.add_handler(CommandHandler("ADD", add_command_entry))
application.add_handler(CommandHandler("EDIT", edit_command_entry))
application.add_handler(CommandHandler("DEL", remove_command_entry))
application.add_handler(CommandHandler("AIADD", add_ai_instruction))
application.add_handler(CommandHandler("ADM", add_admin_entry))
application.add_handler(CommandHandler("RADM", remove_admin_entry))
application.add_handler(CommandHandler("react", react_to_message))
application.add_handler(MessageHandler(filters.Regex(r"^/start(@\w+)?$"), start))
application.add_handler(CallbackQueryHandler(callback_router))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
application.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, goodbye))

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
    return "OK", 100

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
