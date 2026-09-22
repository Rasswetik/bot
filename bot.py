"""
Standalone Telegram bot for RasswetGifts.

Architecture:
    Telegram -> bot.py -> PostgreSQL
    Render -> app.py -> the same PostgreSQL

The website no longer receives Telegram webhooks. This bot uses long polling,
so a slow Render request cannot block /start or freebet deep links.

Required environment variables:
    TELEGRAM_BOT_TOKEN=...
    DATABASE_URL=postgresql://...
    WEBSITE_URL=https://rasswetgifts.onrender.com

Optional:
    POLL_TIMEOUT=30
    LOG_LEVEL=INFO
"""

import os
import time
import logging
import random
import string
from urllib.parse import quote_plus

import requests
import psycopg2
from psycopg2 import OperationalError, InterfaceError
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("freebet-bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBSITE_URL = os.getenv("WEBSITE_URL", "https://rasswetgifts.onrender.com").strip().rstrip("/")
POLL_TIMEOUT = max(1, int(os.getenv("POLL_TIMEOUT", "30")))
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")


def db_connect():
    """Open a fresh PostgreSQL connection. No Flask/Render dependency."""
    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10,
        application_name="rasswetgifts-telegram-bot",
    )


def tg_api(method, **kwargs):
    try:
        response = requests.post(
            f"{TG_API}/{method}",
            json=kwargs,
            timeout=35 if method == "getUpdates" else 15,
        )
        data = response.json()
        if not data.get("ok"):
            logger.warning("Telegram %s error: %s", method, data)
        return data
    except Exception as exc:
        logger.error("Telegram %s exception: %s", method, exc)
        return {"ok": False, "description": str(exc)}


def tg_send(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", **payload)


def make_start_menu():
    return {
        "inline_keyboard": [
            [{"text": "🎮 Играть", "web_app": {"url": WEBSITE_URL}}],
            [{"text": "📢 Канал", "url": "https://t.me/goshangifts"}],
            [{"text": "🆘 Поддержка", "url": "https://t.me/Goshangifts_sup_bot"}],
        ]
    }


def create_referral_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def ensure_user(user_id, first_name, username):
    """
    Keep the same users-table behaviour as the old /start handler:
    create the Telegram user if missing, otherwise update name/username.
    """
    conn = None
    try:
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            if cur.fetchone() is None:
                cur.execute(
                    """
                    INSERT INTO users
                        (id, first_name, username, balance_stars,
                         balance_tickets, referral_code, created_at)
                    VALUES
                        (%s, %s, %s, 0, 0, %s, CURRENT_TIMESTAMP)
                    """,
                    (user_id, first_name, username, create_referral_code()),
                )
            else:
                cur.execute(
                    "UPDATE users SET first_name = %s, username = %s WHERE id = %s",
                    (first_name, username, user_id),
                )
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def process_referral(referred_user_id, referral_code):
    """Small standalone version of the existing /start referral handling."""
    if not referral_code:
        return

    conn = None
    try:
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE referral_code = %s LIMIT 1",
                (referral_code,),
            )
            referrer = cur.fetchone()
            if not referrer:
                return

            referrer_id = referrer[0]
            if int(referrer_id) == int(referred_user_id):
                return

            cur.execute(
                "SELECT id FROM referrals WHERE referred_id = %s LIMIT 1",
                (referred_user_id,),
            )
            if cur.fetchone():
                return

            cur.execute(
                "INSERT INTO referrals (referrer_id, referred_id) VALUES (%s, %s)",
                (referrer_id, referred_user_id),
            )
            cur.execute(
                "UPDATE users SET referral_count = referral_count + 1 WHERE id = %s",
                (referrer_id,),
            )
            cur.execute(
                """
                UPDATE users
                SET balance_tickets = balance_tickets + 1,
                    total_earned_tickets = total_earned_tickets + 1
                WHERE id = %s
                """,
                (referrer_id,),
            )
        conn.commit()
        logger.info("Referral: %s -> %s", referred_user_id, referral_code)
    except Exception as exc:
        if conn:
            conn.rollback()
        logger.warning("Referral processing failed: %s", exc)
    finally:
        if conn:
            conn.close()


def get_freebet(code):
    """
    Read-only freebet check. The actual reward is still claimed by the
    website's /api/freebet/claim endpoint against the same PostgreSQL DB.
    """
    conn = None
    try:
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT code, is_active, max_uses, used_count
                FROM freebets
                WHERE UPPER(code) = UPPER(%s)
                LIMIT 1
                """,
                (code,),
            )
            return cur.fetchone()
    finally:
        if conn:
            conn.close()


def handle_start(message):
    user = message.get("from") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    user_id = user.get("id")

    if not chat_id or not user_id:
        return

    first_name = user.get("first_name", "")
    username = user.get("username", "")
    text = message.get("text", "")

    try:
        ensure_user(user_id, first_name, username)
    except Exception as exc:
        logger.error("User registration failed for %s: %s", user_id, exc)
        tg_send(chat_id, "⚠️ Не удалось подключиться к базе данных. Попробуйте ещё раз через несколько секунд.")
        return

    parts = text.split(maxsplit=1)
    param = parts[1].strip() if len(parts) > 1 else ""

    if param.lower().startswith("ref_"):
        process_referral(user_id, param[4:].strip())
        # After processing referral, show the normal start screen.
    elif param.lower().startswith("freebet_"):
        free_code = param[len("freebet_"):].strip().upper()
        if not free_code:
            tg_send(chat_id, "❌ <b>Фрибет не найден.</b>\n\nПроверьте ссылку или попросите новую.")
            return

        try:
            fr = get_freebet(free_code)
        except (OperationalError, InterfaceError) as exc:
            logger.warning("Freebet DB read failed for %s: %s", free_code, exc)
            tg_send(
                chat_id,
                "⚠️ <b>Временная ошибка.</b>\n\n"
                "Не удалось проверить фрибет. Попробуйте открыть ссылку ещё раз через несколько секунд.",
            )
            return
        except Exception as exc:
            logger.exception("Unexpected freebet DB error for %s", free_code)
            tg_send(
                chat_id,
                "⚠️ <b>Временная ошибка.</b>\n\n"
                "Не удалось проверить фрибет. Попробуйте ещё раз через несколько секунд.",
            )
            return

        if fr and bool(fr[1]) and (
            int(fr[2] or 1) <= 0 or int(fr[3] or 0) < int(fr[2] or 1)
        ):
            tg_send(
                chat_id,
                "🎁 <b>Фрибет получен!</b>\n\n"
                "🔥 Награда уже готова к активации.\n"
                "👇 Нажми кнопку ниже, чтобы забрать её.",
                reply_markup={
                    "inline_keyboard": [[
                        {
                            "text": "👇 Активировать фрибет",
                            "web_app": {
                                "url": f"{WEBSITE_URL}/games?freebet={quote_plus(free_code)}"
                            },
                        }
                    ]]
                },
            )
        elif fr:
            tg_send(
                chat_id,
                "❌ <b>Фрибет уже закончен.</b>\n\n"
                "Лимит активаций исчерпан или фрибет отключён.",
            )
        else:
            tg_send(
                chat_id,
                "❌ <b>Фрибет не найден.</b>\n\n"
                "Проверьте ссылку или попросите новую.",
            )
        return

    welcome_text = (
        "🎉 Привет, на связи команда GOSHANGIFTS и теперь ты в нашей большой семье! 🎁\n\n"
        "Открывай кейсы и выигрывай лучшие NFT гифты!\n\n"
        "💰 Делись своей реферальной ссылкой с друзьями – и за каждого приведённого друга который сделает депозит ты получишь 10% от суммы их пополнений!\n"
        "Заинтересовало?\n\n"
        "🎁 Хочешь попробовать?\n"
        "Жми «Открыть кейс» и забирай свой приз!"
    )
    tg_send(chat_id, welcome_text, reply_markup=make_start_menu())


def handle_message(message):
    """
    The old bot replied to ordinary messages with the play button.
    Keep that behaviour without bringing the old admin/payment/business-bot
    code back into Render.
    """
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id:
        tg_send(
            chat_id,
            "Нажми кнопку чтобы начать:",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "🎮 ИГРАТЬ", "web_app": {"url": WEBSITE_URL}}
                ]]
            },
        )


def handle_update(update):
    if not isinstance(update, dict):
        return

    message = update.get("message")
    if not message:
        return

    text = message.get("text", "")
    if text.startswith("/start"):
        handle_start(message)
    else:
        handle_message(message)


def prepare_bot():
    # The bot is now independent from Render, so remove any old webhook.
    result = tg_api("deleteWebhook", drop_pending_updates=False)
    if not result.get("ok"):
        logger.warning("deleteWebhook failed: %s", result)

    me = tg_api("getMe")
    if not me.get("ok"):
        raise RuntimeError(f"Telegram token check failed: {me}")

    username = (me.get("result") or {}).get("username", "")
    logger.info("Bot connected: @%s", username)

    commands = [
        {"command": "start", "description": "Запустить бота"},
    ]
    tg_api("setMyCommands", commands=commands)


def run():
    prepare_bot()
    offset = None
    logger.info("Telegram long polling started.")

    while True:
        try:
            payload = {
                "timeout": POLL_TIMEOUT,
                "allowed_updates": ["message"],
            }
            if offset is not None:
                payload["offset"] = offset

            data = tg_api("getUpdates", **payload)
            if not data.get("ok"):
                time.sleep(3)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception:
                    logger.exception("Update handling failed: %s", update.get("update_id"))

        except KeyboardInterrupt:
            logger.info("Bot stopped.")
            break
        except Exception as exc:
            logger.exception("Polling loop error: %s", exc)
            time.sleep(3)


if __name__ == "__main__":
    run()
