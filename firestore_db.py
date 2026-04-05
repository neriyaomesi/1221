import os
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

import firebase_admin
from firebase_admin import credentials, firestore

logger = logging.getLogger(__name__)

# ====================== INIT ======================

def _init_firebase():
    if firebase_admin._apps:
        return firestore.client()

    key_json = os.environ.get("FIREBASE_KEY_JSON")
    key_path = os.environ.get("FIREBASE_KEY_PATH", "firebase_key.json")

    if key_json:
        cred = credentials.Certificate(json.loads(key_json))
    elif os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        raise RuntimeError("Firebase credentials not found. Set FIREBASE_KEY_JSON or place firebase_key.json in the project folder.")

    firebase_admin.initialize_app(cred)
    return firestore.client()

db = _init_firebase()

# ====================== COMMANDS ======================

def load_commands() -> Dict[str, str]:
    try:
        doc = db.collection("bot_data").document("commands").get()
        return doc.to_dict() or {} if doc.exists else {}
    except Exception as e:
        logger.exception("load_commands error: %s", e)
        return {}

def save_commands(commands: Dict[str, str]) -> None:
    try:
        db.collection("bot_data").document("commands").set(commands)
    except Exception as e:
        logger.exception("save_commands error: %s", e)

# ====================== ADMINS ======================

def load_admins(owner_id: int) -> List[int]:
    try:
        doc = db.collection("bot_data").document("admins").get()
        if doc.exists:
            data = doc.to_dict() or {}
            ids = [int(x) for x in data.get("ids", [])]
            if owner_id not in ids:
                ids.insert(0, owner_id)
            return ids
        return [owner_id]
    except Exception as e:
        logger.exception("load_admins error: %s", e)
        return [owner_id]

def save_admins(admin_ids: List[int]) -> None:
    try:
        db.collection("bot_data").document("admins").set({"ids": admin_ids})
    except Exception as e:
        logger.exception("save_admins error: %s", e)

# ====================== PERSONA ======================

DEFAULT_INSTRUCTIONS = [
    "ענה בעברית ברוב המקרים.",
    "היה קצר, ברור, חכם, טכנולוגי ונעים לקריאה.",
    "השתמש באימוג'ים במינון יפה ולא מוגזם.",
    "אם חסר מידע, שאל שאלה קצרה אחת בלבד.",
    "אל תמציא עובדות.",
]

def load_persona() -> Dict[str, Any]:
    try:
        doc = db.collection("bot_data").document("persona").get()
        if doc.exists:
            data = doc.to_dict() or {}
            instructions = data.get("instructions", DEFAULT_INSTRUCTIONS)
            if not isinstance(instructions, list) or not instructions:
                instructions = DEFAULT_INSTRUCTIONS[:]
            return {
                "instructions": [str(x).strip() for x in instructions if str(x).strip()],
                "version": int(data.get("version", 1)),
                "updated_at": str(data.get("updated_at", "")),
            }
    except Exception as e:
        logger.exception("load_persona error: %s", e)
    return {"instructions": DEFAULT_INSTRUCTIONS[:], "version": 1, "updated_at": ""}

def save_persona(persona: Dict[str, Any]) -> None:
    try:
        persona["updated_at"] = datetime.now(timezone.utc).isoformat()
        db.collection("bot_data").document("persona").set(persona)
    except Exception as e:
        logger.exception("save_persona error: %s", e)

# ====================== STATS ======================

def load_stats() -> Dict[str, Any]:
    try:
        doc = db.collection("bot_data").document("stats").get()
        if doc.exists:
            data = doc.to_dict() or {}
            return {
                "ai": int(data.get("ai", 0)),
                "commands": dict(data.get("commands", {})),
                "buttons": dict(data.get("buttons", {})),
            }
    except Exception as e:
        logger.exception("load_stats error: %s", e)
    return {"ai": 0, "commands": {}, "buttons": {}}

def save_stats(stats: Dict[str, Any]) -> None:
    try:
        db.collection("bot_data").document("stats").set({
            "ai": stats.get("ai", 0),
            "commands": {k: v for k, v in stats.get("commands", {}).items() if v > 0},
            "buttons":  {k: v for k, v in stats.get("buttons", {}).items()  if v > 0},
        })
    except Exception as e:
        logger.exception("save_stats error: %s", e)