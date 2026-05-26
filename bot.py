"""
Telegram Subscription Bot
- Shows plans, handles UPI payments, admin approval, channel invites, auto-expiry
- Uses a tiny Flask web server so it runs on Render's free Web Service plan
"""

import os
import time
import threading
from datetime import datetime, timedelta

import telebot
from telebot import types
from pymongo import MongoClient
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

# ─── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["BOT_TOKEN"]
ADMIN_ID   = int(os.environ["ADMIN_ID"])
CHANNEL_ID = int(os.environ["CHANNEL_ID"])   # negative number, e.g. -1001234567890
UPI_ID     = os.environ["UPI_ID"]
MONGO_URI  = os.environ["MONGO_URI"]

bot = telebot.TeleBot(BOT_TOKEN)

# ─── Flask (keeps Render Web Service alive) ────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!", 200

@app.route("/health")
def health():
    return "OK", 200

# ─── MongoDB ───────────────────────────────────────────────────────────────────
client  = MongoClient(MONGO_URI)
db      = client["subscription_bot"]
users   = db["users"]    # stores subscription info
pending = db["pending"]  # stores pending payment requests

# ─── Plans ─────────────────────────────────────────────────────────────────────
PLANS = {
    "1week":  {"label": "1 Week",  "price": 89,  "days": 7},
    "1month": {"label": "1 Month", "price": 199, "days": 30},
}

# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_plan_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("📅 1 Week  — ₹89",  callback_data="plan_1week"),
        types.InlineKeyboardButton("📆 1 Month — ₹199", callback_data="plan_1month"),
    )
    return kb


def get_paid_keyboard(plan_key):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ I Have Paid", callback_data=f"paid_{plan_key}"))
    kb.add(types.InlineKeyboardButton("🔙 Change Plan",  callback_data="back_to_plans"))
    return kb


def get_admin_keyboard(user_id, plan_key):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}_{plan_key}"),
        types.InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{user_id}_{plan_key}"),
    )
    return kb


def upi_payment_link(amount):
    """Simple UPI deep-link (works in most UPI apps)."""
    return f"upi://pay?pa={UPI_ID}&am={amount}&cu=INR"


def add_subscription(user_id, plan_key):
    """Save / extend subscription in MongoDB."""
    days = PLANS[plan_key]["days"]
    now  = datetime.utcnow()
    rec  = users.find_one({"user_id": user_id})

    if rec and rec.get("expiry") and rec["expiry"] > now:
        new_expiry = rec["expiry"] + timedelta(days=days)  # extend existing
    else:
        new_expiry = now + timedelta(days=days)

    users.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "plan": plan_key, "expiry": new_expiry, "active": True}},
        upsert=True,
    )
    return new_expiry


# ─── /start ────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message):
    name = message.from_user.first_name or "there"
    bot.send_message(
        message.chat.id,
        f"👋 Hi *{name}*! Welcome to the Subscription Bot.\n\n"
        "Choose a plan to get access to our *private channel*:",
        parse_mode="Markdown",
        reply_markup=get_plan_keyboard(),
    )


# ─── Plan selection ────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("plan_"))
def cb_plan(call):
    plan_key = call.data[5:]
    plan     = PLANS[plan_key]
    amount   = plan["price"]
    link     = upi_payment_link(amount)

    text = (
        f"💳 *Payment Details*\n\n"
        f"Plan   : {plan['label']}\n"
        f"Amount : ₹{amount}\n\n"
        f"UPI ID : `{UPI_ID}`\n\n"
        f"📲 [Tap to Pay via UPI App]({link})\n\n"
        "_After paying, tap the button below._"
    )

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=get_paid_keyboard(plan_key),
    )


@bot.callback_query_handler(func=lambda c: c.data == "back_to_plans")
def cb_back(call):
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Choose a plan to get access to our *private channel*:",
        parse_mode="Markdown",
        reply_markup=get_plan_keyboard(),
    )


# ─── "I Have Paid" ─────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("paid_"))
def cb_paid(call):
    plan_key = call.data[5:]
    user     = call.from_user
    plan     = PLANS[plan_key]

    # Save pending request
    pending.update_one(
        {"user_id": user.id},
        {"$set": {
            "user_id":   user.id,
            "username":  user.username or "",
            "full_name": user.full_name or "",
            "plan":      plan_key,
            "chat_id":   call.message.chat.id,
        }},
        upsert=True,
    )

    # Notify user
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="⏳ *Payment submitted!*\n\nOur admin will verify and approve within a few minutes. Please wait…",
        parse_mode="Markdown",
    )

    # Notify admin
    username_str = f"@{user.username}" if user.username else "(no username)"
    admin_text = (
        f"🔔 *New Payment Request*\n\n"
        f"👤 Name : {user.full_name}\n"
        f"🆔 ID   : `{user.id}`\n"
        f"📛 User : {username_str}\n"
        f"📦 Plan : {plan['label']} — ₹{plan['price']}\n\n"
        "Approve or Reject below:"
    )
    bot.send_message(
        ADMIN_ID,
        admin_text,
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard(user.id, plan_key),
    )


# ─── Admin Approve / Reject ────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("approve_"))
def cb_approve(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Not authorised.")
        return

    _, user_id_str, plan_key = call.data.split("_", 2)
    user_id = int(user_id_str)
    plan    = PLANS[plan_key]

    # Save subscription
    expiry = add_subscription(user_id, plan_key)

    # Create single-use invite link that expires with the subscription
    try:
        link_obj = bot.create_chat_invite_link(
            CHANNEL_ID,
            member_limit=1,
            expire_date=int(expiry.timestamp()),
        )
        invite_link = link_obj.invite_link
    except Exception as e:
        invite_link = None
        print(f"[WARN] Could not create invite link: {e}")

    # Notify user
    rec     = pending.find_one({"user_id": user_id})
    chat_id = rec["chat_id"] if rec else user_id

    if invite_link:
        msg = (
            f"🎉 *Payment Approved!*\n\n"
            f"Plan    : {plan['label']}\n"
            f"Expires : {expiry.strftime('%d %b %Y')}\n\n"
            f"👉 [Join the Private Channel]({invite_link})\n\n"
            "_This link is valid for you only. Join before it expires!_"
        )
    else:
        msg = (
            f"🎉 *Payment Approved!*\n\n"
            f"Plan    : {plan['label']}\n"
            f"Expires : {expiry.strftime('%d %b %Y')}\n\n"
            "⚠️ Could not auto-generate invite link. Admin will send it shortly."
        )

    bot.send_message(chat_id, msg, parse_mode="Markdown")
    pending.delete_one({"user_id": user_id})

    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.answer_callback_query(call.id, "✅ Approved!")
    bot.send_message(ADMIN_ID, f"✅ Approved user `{user_id}` for *{plan['label']}*.", parse_mode="Markdown")


@bot.callback_query_handler(func=lambda c: c.data.startswith("reject_"))
def cb_reject(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Not authorised.")
        return

    _, user_id_str, plan_key = call.data.split("_", 2)
    user_id = int(user_id_str)

    rec     = pending.find_one({"user_id": user_id})
    chat_id = rec["chat_id"] if rec else user_id

    bot.send_message(
        chat_id,
        "❌ *Payment Rejected*\n\nYour payment could not be verified.\n"
        "Please contact admin or try again with /start.",
        parse_mode="Markdown",
    )
    pending.delete_one({"user_id": user_id})

    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.answer_callback_query(call.id, "❌ Rejected.")
    bot.send_message(ADMIN_ID, f"❌ Rejected user `{user_id}`.", parse_mode="Markdown")


# ─── Background: expiry checker ───────────────────────────────────────────────

def check_expired_subscriptions():
    """Runs every 30 minutes. Removes expired users and sends renewal message."""
    while True:
        time.sleep(30 * 60)
        now = datetime.utcnow()

        for rec in users.find({"expiry": {"$lt": now}, "active": True}):
            user_id = rec["user_id"]

            # Kick then immediately unban so user can rejoin after renewal
            try:
                bot.ban_chat_member(CHANNEL_ID, user_id)
                time.sleep(1)
                bot.unban_chat_member(CHANNEL_ID, user_id)
            except Exception as e:
                print(f"[WARN] Could not remove user {user_id}: {e}")

            users.update_one({"user_id": user_id}, {"$set": {"active": False}})

            try:
                bot.send_message(
                    user_id,
                    "⏰ *Your subscription has expired!*\n\n"
                    "You have been removed from the private channel.\n\n"
                    "🔄 Tap /start to renew and get access again.",
                    parse_mode="Markdown",
                )
            except Exception as e:
                print(f"[WARN] Could not message user {user_id}: {e}")


# ─── Background: bot polling ───────────────────────────────────────────────────

def run_bot():
    print("✅ Bot polling started.")
    bot.infinity_polling()


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🤖 Bot is starting…")

    # Thread 1: Telegram bot polling
    threading.Thread(target=run_bot, daemon=True).start()

    # Thread 2: Subscription expiry checker
    threading.Thread(target=check_expired_subscriptions, daemon=True).start()

    # Main thread: Flask web server (required by Render Web Service)
    port = int(os.environ.get("PORT", 8080))
    print(f"🌐 Web server on port {port}")
    app.run(host="0.0.0.0", port=port)
