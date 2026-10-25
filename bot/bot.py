#!/usr/bin/env python3
"""KILLDPI Telegram bot — runs the KILLDPI server itself inside GitHub Actions.

Secrets come ONLY from environment variables (GitHub Actions secrets):
  BOT_TOKEN            Telegram bot token
  GH_PAT               PAT with repo + gist + workflow scopes (state, subscriptions, relaunch)
  KILLDPI_PRIVATE_KEY  server X25519 key (auto-generated and stored as a secret on first run)

Environment (non-secret):
  DATA_REPO    owner/repo that holds data/state.json (default: GITHUB_REPOSITORY)
  ADMIN_IDS    comma separated Telegram user ids; empty = everybody is admin
  RUN_SECONDS  how long this run lives (default 20400 = 5h40m; GitHub job hard-limit is 6h)
  LOCAL_STORAGE=1  keep state on disk, don't talk to GitHub (development)
  NO_SERVER=1      don't start xray/cloudflared (development without binaries)
"""
from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import re
import sys
import time
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import qrcode
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
                      Update)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

from killdpi_link import KillDPIConfig, LinkError
from profiles import PROFILES, PROFILE_ORDER, Profile
from server import KillDPIServer, find_bin
from storage import LocalStorage, Storage
from subscription import DEFAULT_CLEAN_IPS, build_subscription

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("killdpi-bot")

# ----------------------------------------------------------------- environment
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GH_PAT = os.environ.get("GH_PAT", "")
DATA_REPO = os.environ.get("DATA_REPO") or os.environ.get("GITHUB_REPOSITORY", "")
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "20400"))
RELAUNCH_LEAD = int(os.environ.get("RELAUNCH_LEAD", "600"))      # dispatch next run this many s before the end
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "bot.yml")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
RUN_ID = os.environ.get("GITHUB_RUN_ID", str(int(time.time())))
LOCAL = os.environ.get("LOCAL_STORAGE") == "1"
NO_SERVER = os.environ.get("NO_SERVER") == "1"
MAX_CONFIGS_PER_USER = int(os.environ.get("MAX_CONFIGS_PER_USER", "6"))

if not BOT_TOKEN:
    print("BOT_TOKEN is not set (configure it as a GitHub Actions secret).", file=sys.stderr)
    sys.exit(2)
if LOCAL:
    store: Storage = LocalStorage()
else:
    if not GH_PAT or not DATA_REPO:
        print("GH_PAT and DATA_REPO (or GITHUB_REPOSITORY) are required.", file=sys.stderr)
        sys.exit(2)
    store = Storage(GH_PAT, DATA_REPO)

STATE = store.load_state()
SETTINGS = STATE["settings"]
SETTINGS.setdefault("clean_ips", DEFAULT_CLEAN_IPS)
SETTINGS.setdefault("ws_path", "/killdpi")
SETTINGS.setdefault("max_ips", 4)
SETTINGS.setdefault("maintenance", False)
RUNTIME = STATE["runtime"]
DIRTY = False
HANDOVER = False   # set once the successor run was dispatched: this run stops writing state.json
STARTED = time.time()
DEADLINE = STARTED + RUN_SECONDS
SERVER: KillDPIServer | None = None
PBK = ""
TEHRAN = timezone(timedelta(hours=3, minutes=30))


# ----------------------------------------------------------------- helpers
def mark_dirty() -> None:
    global DIRTY
    DIRTY = True


def flush_state(reason: str = "bot: update state") -> None:
    global DIRTY
    if HANDOVER:
        return  # the next run owns data/state.json from now on
    if DIRTY:
        try:
            store.save_state(STATE, reason)
            DIRTY = False
        except Exception as e:  # noqa: BLE001
            log.error("save_state failed: %s", e)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_admin(uid: int) -> bool:
    return not ADMIN_IDS or uid in ADMIN_IDS


def get_user(uid: int, tg=None) -> dict:
    u = STATE["users"].setdefault(str(uid), {"first_seen": now_iso(), "configs": [], "sub_gist": None, "sub_file": None})
    if tg is not None:
        u["name"] = tg.full_name
        u["username"] = tg.username or ""
        u["last_seen"] = now_iso()
    return u


def remaining() -> float:
    return max(0.0, DEADLINE - time.time())


def fmt_remaining(seconds: float | None = None) -> str:
    s = int(remaining() if seconds is None else seconds)
    h, m = divmod(s // 60, 60)
    return f"{h} ساعت و {m} دقیقه"


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def email_of(token: str) -> str:
    return f"{token}@killdpi"


def qr_png(text: str, name: str = "killdpi.png") -> io.BytesIO:
    img = qrcode.make(text, border=2, box_size=6)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    bio.name = name
    return bio


def server_online() -> bool:
    return SERVER is not None and SERVER.healthy() and bool(RUNTIME.get("tunnel_host"))


def all_clients() -> list[dict]:
    out = []
    for token, c in STATE["configs"].items():
        p = PROFILES[c["profile"]]
        out.append({"id": c["uuid"], "email": email_of(token), "cipher": p.cipher, "padding": p.padding})
    return out


def primary_link(cfg: dict, name: str | None = None) -> str:
    p = PROFILES[cfg["profile"]]
    host = RUNTIME.get("tunnel_host", "")
    return KillDPIConfig(uuid=cfg["uuid"], host=host, port=443, pbk=PBK, cipher=p.cipher, network="ws", security="tls",
                         sni=host, ws_host=host, path=SETTINGS["ws_path"], fp="chrome", padding=p.padding,
                         keepalive=p.keepalive, name=name or f"{p.emoji} {p.title} | CF-Auto").to_link()


def republish_user(uid: int, user: dict) -> str:
    body, _ = build_subscription(user, STATE["configs"], RUNTIME, PBK, SETTINGS["clean_ips"], SETTINGS["ws_path"],
                                 max_ips=SETTINGS["max_ips"])
    url = store.publish_subscription(user, body)
    user["sub_url"] = url
    mark_dirty()
    return url


def republish_all(reason: str) -> int:
    n = 0
    for uid_s, user in STATE["users"].items():
        if user.get("configs") or user.get("sub_gist"):
            try:
                republish_user(int(uid_s), user)
                n += 1
            except Exception as e:  # noqa: BLE001
                log.error("republish %s: %s", uid_s, e)
    flush_state(reason)
    return n


# ----------------------------------------------------------------- keyboards
BTN_NEW, BTN_MINE, BTN_SUB, BTN_STATUS, BTN_HELP, BTN_ADMIN = ("➕ کانفیگ جدید", "📋 کانفیگ‌های من", "🔗 لینک ساب",
                                                                "📡 وضعیت سرور", "ℹ️ راهنما", "🛠 پنل ادمین")


def reply_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(BTN_NEW), KeyboardButton(BTN_MINE)],
            [KeyboardButton(BTN_SUB), KeyboardButton(BTN_STATUS)],
            [KeyboardButton(BTN_HELP)]]
    if is_admin(uid):
        rows[-1].append(KeyboardButton(BTN_ADMIN))
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="یک گزینه انتخاب کنید…")


def main_menu(uid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ ساخت کانفیگ جدید", callback_data="new")],
            [InlineKeyboardButton("📋 کانفیگ‌های من", callback_data="mine"),
             InlineKeyboardButton("🔗 لینک Subscription", callback_data="sub")],
            [InlineKeyboardButton("📡 وضعیت سرور", callback_data="status"),
             InlineKeyboardButton("🧪 تست اتصال", callback_data="ping")],
            [InlineKeyboardButton("ℹ️ راهنما", callback_data="help"),
             InlineKeyboardButton("📱 کلاینت‌ها", callback_data="apps")]]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠 پنل ادمین", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def profiles_menu() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{PROFILES[k].emoji} {PROFILES[k].title} — {PROFILES[k].summary}", callback_data=f"prof:{k}")]
            for k in PROFILE_ORDER]
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def back_kb(target: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ منو", callback_data=target)]])


async def edit_or_send(q, text: str, kb=None, parse_mode=ParseMode.HTML) -> None:
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode, disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await q.message.reply_text(text, reply_markup=kb, parse_mode=parse_mode, disable_web_page_preview=True)


# ----------------------------------------------------------------- texts
HELP_TEXT = (
    "<b>KILLDPI</b> یک پروتکل رمزنگاری‌شده روی Xray-core است.\n\n"
    "🔹 سرور همین‌جا داخل GitHub Actions اجرا می‌شود و از طریق تونل Cloudflare در دسترس است.\n"
    "🔹 هر ران حداکثر ~۶ ساعت عمر دارد؛ ربات قبل از پایان، ران بعدی را خودش می‌زند و آدرس جدید را در ساب می‌گذارد.\n"
    "🔹 <b>لینک Subscription شما ثابت است</b>؛ فقط کافی است در کلاینت «بروزرسانی» بزنید.\n"
    "🔹 اولین آیتم ساب نشان می‌دهد چند ساعت و دقیقه تا بروزرسانی بعدی مانده است.\n"
    "🔹 هر کانفیگ با چند آدرس (تونل + IP تمیز Cloudflare) در ساب قرار می‌گیرد؛ اگر یکی کند بود دیگری را امتحان کنید.\n\n"
    "دستورات:\n"
    "/new – کانفیگ جدید\n/mine – کانفیگ‌های من\n/sub – لینک ساب\n/status – وضعیت سرور\n"
    "/ping – تست اتصال سرور\n/apps – کلاینت‌های سازگار\n/help – همین راهنما\n\n"
    "⚠️ هیچ پروتکلی شناسایی‌ناپذیریِ کامل را تضمین نمی‌کند."
)

APPS_TEXT = (
    "<b>کلاینت‌های سازگار با KILLDPI</b>\n\n"
    "📱 <b>Android:</b> نسخهٔ پچ‌شدهٔ v2rayNG از ریلیزهای ریپوی KILLDPI (android-build)\n"
    "💻 <b>Windows / Linux:</b> هستهٔ xray (نسخهٔ KILLDPI) از ریلیز core-build + فایل JSON هر کانفیگ (دکمهٔ 📄)\n\n"
    "روش استفاده با هسته: <code>xray run -c config.json</code> سپس SOCKS5 روی 127.0.0.1:10808"
)


# ----------------------------------------------------------------- status text
def status_text() -> str:
    online = server_online()
    lines = [f"<b>📡 وضعیت سرور KILLDPI</b>\n",
             f"وضعیت: {'🟢 آنلاین' if online else '🔴 آفلاین'}"]
    if online:
        lines.append(f"تونل: <code>{RUNTIME.get('tunnel_host')}</code>")
    lines.append(f"⏳ باقی‌مانده تا ران بعدی: <b>{fmt_remaining()}</b>")
    lines.append(f"شروع این ران: {datetime.fromtimestamp(STARTED, TEHRAN):%H:%M} (تهران)")
    lines.append(f"کانفیگ‌های فعال: {len(STATE['configs'])} | کاربران: {len(STATE['users'])}")
    if SERVER and online:
        try:
            up, down = SERVER.total_traffic()
            lines.append(f"ترافیک این ران: ⬆️ {human_bytes(up)} ⬇️ {human_bytes(down)}")
        except Exception:  # noqa: BLE001
            pass
    lines.append(f"ران شماره: <code>{RUN_ID}</code>")
    if SETTINGS.get("maintenance"):
        lines.append("\n🛠 <b>حالت تعمیرات فعال است</b> — ساخت کانفیگ موقتاً بسته است.")
    return "\n".join(lines)


# ----------------------------------------------------------------- handlers: basic
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    get_user(u.id, u)
    mark_dirty()
    await update.effective_message.reply_text(
        f"سلام {html.escape(u.first_name)}! 👋\nبه ربات <b>KILLDPI</b> خوش آمدید.\n"
        f"سرور: {'🟢 آنلاین' if server_online() else '🔴 آفلاین'} | ⏳ {fmt_remaining()} تا ران بعدی",
        parse_mode=ParseMode.HTML, reply_markup=reply_kb(u.id))
    await update.effective_message.reply_text("یک گزینه انتخاب کنید:", reply_markup=main_menu(u.id))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=back_kb(), disable_web_page_preview=True)


async def cmd_apps(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(APPS_TEXT, parse_mode=ParseMode.HTML, reply_markup=back_kb())


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(status_text(), parse_mode=ParseMode.HTML, reply_markup=back_kb())


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("پروفایل مورد نظر را انتخاب کنید:", reply_markup=profiles_menu())


async def cmd_mine(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user(update.effective_user.id, update.effective_user)
    text, kb = mine_view(user)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    user = get_user(uid, update.effective_user)
    text, kb, url = sub_view(uid, user)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
    if url:
        await update.effective_message.reply_photo(qr_png(url, "sub.png"), caption="QR لینک Subscription")


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.effective_message.reply_text("⏳ در حال تست مسیر Cloudflare → سرور…")
    await msg.edit_text(await ping_text())


async def ping_text() -> str:
    if not server_online():
        return "🔴 سرور آفلاین است."
    t = time.time()
    ok = await asyncio.get_running_loop().run_in_executor(None, SERVER.probe)
    ms = int((time.time() - t) * 1000)
    return (f"✅ مسیر Cloudflare → سرور سالم است ({ms} ms)\nتونل: <code>{RUNTIME.get('tunnel_host')}</code>"
            if ok else "⚠️ تونل جواب نداد؛ ربات به‌زودی آن را بازسازی می‌کند. چند دقیقهٔ دیگر ساب را بروزرسانی کنید.")


# ----------------------------------------------------------------- views
def mine_view(user: dict) -> tuple[str, InlineKeyboardMarkup]:
    tokens = [t for t in user["configs"] if t in STATE["configs"]]
    if not tokens:
        return "هنوز کانفیگی ندارید.", InlineKeyboardMarkup([[InlineKeyboardButton("➕ ساخت کانفیگ", callback_data="new")],
                                                           [InlineKeyboardButton("⬅️ منو", callback_data="menu")]])
    rows = []
    lines = ["<b>کانفیگ‌های شما</b>  (📷 QR | 📄 JSON | 📊 آمار | 🗑 حذف)\n"]
    for t in tokens:
        c = STATE["configs"][t]
        p = PROFILES[c["profile"]]
        lines.append(f"{p.emoji} <b>{p.title}</b> — ساخته‌شده {c['created'][:10]}")
        rows.append([InlineKeyboardButton(f"{p.emoji} {p.title}", callback_data=f"show:{t}"),
                     InlineKeyboardButton("📷", callback_data=f"qr:{t}"),
                     InlineKeyboardButton("📄", callback_data=f"json:{t}"),
                     InlineKeyboardButton("📊", callback_data=f"stat:{t}"),
                     InlineKeyboardButton("🗑", callback_data=f"del:{t}")])
    rows.append([InlineKeyboardButton("➕ کانفیگ جدید", callback_data="new"), InlineKeyboardButton("⬅️ منو", callback_data="menu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def sub_view(uid: int, user: dict) -> tuple[str, InlineKeyboardMarkup, str]:
    if not user["configs"]:
        return "ابتدا یک کانفیگ بسازید.", InlineKeyboardMarkup([[InlineKeyboardButton("➕ ساخت کانفیگ", callback_data="new")],
                                                                [InlineKeyboardButton("⬅️ منو", callback_data="menu")]]), ""
    url = republish_user(uid, user)
    flush_state("bot: refresh subscription")
    text = (f"🔗 <b>Subscription شما (ثابت):</b>\n<code>{url}</code>\n\n"
            f"⏳ تا بروزرسانی بعدی: <b>{fmt_remaining()}</b>\n"
            "این لینک را در کلاینت وارد کنید؛ هر بار که سرور عوض شود فقط «بروزرسانی ساب» بزنید.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 بازسازی ساب", callback_data="sub"),
                                InlineKeyboardButton("📋 کانفیگ‌ها", callback_data="mine")],
                               [InlineKeyboardButton("⬅️ منو", callback_data="menu")]])
    return text, kb, url


def config_view(token: str, cfg: dict) -> tuple[str, InlineKeyboardMarkup]:
    p = PROFILES[cfg["profile"]]
    if server_online():
        link = primary_link(cfg)
        body = f"<b>لینک (تونل فعلی):</b>\n<code>{html.escape(link)}</code>\n\n"
    else:
        body = "🔴 سرور فعلاً آفلاین است؛ لینک مستقیم بعد از بالا آمدن سرور در ساب قرار می‌گیرد.\n\n"
    text = (f"{p.emoji} <b>{p.title}</b>\n<i>{p.summary}</i>\n\n{body}"
            f"<code>cipher={p.cipher} padding={p.padding} keepalive={p.keepalive}s</code>\n"
            f"UUID: <code>{cfg['uuid']}</code>")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 QR", callback_data=f"qr:{token}"), InlineKeyboardButton("📄 JSON", callback_data=f"json:{token}"),
         InlineKeyboardButton("📊 آمار", callback_data=f"stat:{token}")],
        [InlineKeyboardButton("🔗 لینک ساب", callback_data="sub"), InlineKeyboardButton("🗑 حذف", callback_data=f"del:{token}")],
        [InlineKeyboardButton("⬅️ کانفیگ‌ها", callback_data="mine")]])
    return text, kb


def admin_view() -> tuple[str, InlineKeyboardMarkup]:
    users = STATE["users"]
    active = sum(1 for u in users.values() if u.get("configs"))
    text = (f"<b>🛠 پنل ادمین</b>\n\n"
            f"کاربران: {len(users)} (فعال: {active}) | کانفیگ‌ها: {len(STATE['configs'])}\n"
            f"IPهای تمیز: {', '.join(SETTINGS['clean_ips'][:SETTINGS['max_ips']])}\n"
            f"مسیر WS: <code>{SETTINGS['ws_path']}</code> | تعمیرات: {'✅' if SETTINGS.get('maintenance') else '❌'}\n\n"
            "دستورات:\n<code>/cleanips 1.2.3.4 5.6.7.8 …</code> – تنظیم IP تمیز\n"
            "<code>/maxips 4</code> – تعداد IP در ساب\n<code>/broadcast متن</code> – پیام همگانی\n"
            "<code>/users</code> – لیست کاربران\n<code>/relaunch</code> – ران جدید همین حالا\n"
            "<code>/maintenance on|off</code>\n<code>/export</code> – خروجی state")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 بازنشر همهٔ ساب‌ها", callback_data="adm:republish"),
         InlineKeyboardButton("🌐 بازسازی تونل", callback_data="adm:tunnel")],
        [InlineKeyboardButton("👥 کاربران", callback_data="adm:users"),
         InlineKeyboardButton("📈 آمار ترافیک", callback_data="adm:traffic")],
        [InlineKeyboardButton("🚀 ران جدید", callback_data="adm:relaunch"),
         InlineKeyboardButton("🛠 تعمیرات", callback_data="adm:maint")],
        [InlineKeyboardButton("⬅️ منو", callback_data="menu")]])
    return text, kb


# ----------------------------------------------------------------- config lifecycle
async def make_config(q, user: dict, uid: int, profile: Profile) -> None:
    if HANDOVER:
        await edit_or_send(q, "🔁 سرور در حال جابه‌جایی به ران جدید است؛ لطفاً ۱–۲ دقیقهٔ دیگر دوباره بزنید.", main_menu(uid))
        return
    if SETTINGS.get("maintenance") and not is_admin(uid):
        await edit_or_send(q, "🛠 ربات در حالت تعمیرات است؛ کمی بعد دوباره تلاش کنید.", main_menu(uid))
        return
    live = [t for t in user["configs"] if t in STATE["configs"]]
    if len(live) >= MAX_CONFIGS_PER_USER and not is_admin(uid):
        await edit_or_send(q, f"❌ حداکثر {MAX_CONFIGS_PER_USER} کانفیگ می‌توانید داشته باشید. یکی را حذف کنید.", main_menu(uid))
        return
    client_uuid = str(_uuid.uuid4())
    token = store.new_token()
    STATE["configs"][token] = {"owner": uid, "profile": profile.key, "uuid": client_uuid, "created": now_iso(), "up": 0, "down": 0}
    user["configs"].append(token)
    mark_dirty()

    # 1) activate on the live server (this is the "action" the button triggers)
    activated = False
    if server_online():
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, SERVER.add_client, client_uuid, email_of(token), profile.cipher, profile.padding)
            activated = True
        except Exception as e:  # noqa: BLE001
            log.error("add_client failed: %s", e)
    # 2) publish the subscription (stable URL) with tunnel + clean IPs
    sub_url = republish_user(uid, user)
    flush_state(f"bot: new config ({profile.key})")

    if activated:
        link = primary_link(STATE["configs"][token])
        text = (f"✅ کانفیگ <b>{profile.title}</b> ساخته و روی سرور فعال شد.\n\n"
                f"<b>لینک مستقیم (تونل فعلی):</b>\n<code>{html.escape(link)}</code>\n\n"
                f"<b>Subscription (ثابت — این را ذخیره کنید):</b>\n<code>{sub_url}</code>\n\n"
                f"⏳ تا بروزرسانی بعدی: <b>{fmt_remaining()}</b>\n"
                "در ساب، همین کانفیگ با چند IP تمیز Cloudflare هم هست.")
    else:
        text = (f"✅ کانفیگ <b>{profile.title}</b> ذخیره شد، ولی سرور فعلاً آفلاین است.\n\n"
                f"<b>Subscription (ثابت):</b>\n<code>{sub_url}</code>\n\n"
                "به‌محض بالا آمدن سرور، لینک‌ها خودکار داخل ساب قرار می‌گیرند؛ فقط «بروزرسانی ساب» بزنید.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 QR لینک", callback_data=f"qr:{token}"),
         InlineKeyboardButton("📄 فایل JSON", callback_data=f"json:{token}")],
        [InlineKeyboardButton("📷 QR ساب", callback_data="subqr"), InlineKeyboardButton("📋 کانفیگ‌های من", callback_data="mine")],
        [InlineKeyboardButton("⬅️ منو", callback_data="menu")]])
    await edit_or_send(q, text, kb)


def owned(uid: int, token: str) -> dict | None:
    cfg = STATE["configs"].get(token)
    if not cfg or (cfg["owner"] != uid and not is_admin(uid)):
        return None
    return cfg


async def delete_config(q, uid: int, user: dict, token: str) -> None:
    if HANDOVER:
        await edit_or_send(q, "🔁 سرور در حال جابه‌جایی است؛ لطفاً ۱–۲ دقیقهٔ دیگر دوباره بزنید.", main_menu(uid))
        return
    cfg = STATE["configs"].pop(token, None)
    if cfg is None:
        return
    owner = STATE["users"].get(str(cfg["owner"]), user)
    owner["configs"] = [t for t in owner.get("configs", []) if t != token]
    if server_online():
        await asyncio.get_running_loop().run_in_executor(None, SERVER.remove_client, email_of(token))
    mark_dirty()
    republish_user(cfg["owner"], owner)
    flush_state(f"bot: delete config {token}")
    text, kb = mine_view(owner)
    await edit_or_send(q, "🗑 کانفیگ حذف شد و ساب به‌روز شد.\n\n" + text, kb)


def stat_text(token: str, cfg: dict) -> str:
    p = PROFILES[cfg["profile"]]
    up, down = cfg.get("up", 0), cfg.get("down", 0)
    online = 0
    if server_online():
        try:
            u2, d2 = SERVER.traffic(email_of(token))
            up, down = up + u2, down + d2
            online = SERVER.online(email_of(token))
        except Exception:  # noqa: BLE001
            pass
    return (f"📊 <b>آمار {p.emoji} {p.title}</b>\n\n⬆️ آپلود: {human_bytes(up)}\n⬇️ دانلود: {human_bytes(down)}\n"
            f"🔌 اتصال فعال: {online}\n🗓 ساخته‌شده: {cfg['created'][:16].replace('T', ' ')} UTC")


# ----------------------------------------------------------------- callback router
async def cb_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id
    user = get_user(uid, q.from_user)
    try:
        await q.answer()
    except BadRequest:
        pass

    if data == "menu":
        await edit_or_send(q, "منوی اصلی:", main_menu(uid))
    elif data == "help":
        await edit_or_send(q, HELP_TEXT, back_kb())
    elif data == "apps":
        await edit_or_send(q, APPS_TEXT, back_kb())
    elif data == "status":
        await edit_or_send(q, status_text(), InlineKeyboardMarkup([[InlineKeyboardButton("🔄", callback_data="status"),
                                                                     InlineKeyboardButton("🧪 تست اتصال", callback_data="ping")],
                                                                    [InlineKeyboardButton("⬅️ منو", callback_data="menu")]]))
    elif data == "ping":
        await edit_or_send(q, await ping_text(), back_kb("status"))
    elif data == "new":
        await edit_or_send(q, "پروفایل مورد نظر را انتخاب کنید:", profiles_menu())
    elif data.startswith("prof:"):
        p = PROFILES.get(data.split(":", 1)[1])
        if not p:
            return
        text = (f"{p.emoji} <b>{p.title}</b>\n\n{p.description}\n\n<b>محدودیت‌ها:</b>\n{p.limitations}\n\n"
                f"<code>cipher={p.cipher} padding={p.padding} keepalive={p.keepalive}</code>")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ بساز و روی سرور فعال کن", callback_data=f"make:{p.key}")],
                                   [InlineKeyboardButton("⬅️ پروفایل‌ها", callback_data="new")]])
        await edit_or_send(q, text, kb)
    elif data.startswith("make:"):
        p = PROFILES.get(data.split(":", 1)[1])
        if p:
            await edit_or_send(q, "⏳ در حال ساخت و فعال‌سازی روی سرور…", None)
            await make_config(q, user, uid, p)
    elif data == "mine":
        text, kb = mine_view(user)
        await edit_or_send(q, text, kb)
    elif data == "sub":
        text, kb, url = sub_view(uid, user)
        await edit_or_send(q, text, kb)
    elif data == "subqr":
        if user.get("sub_url"):
            await q.message.reply_photo(qr_png(user["sub_url"], "sub.png"), caption="QR لینک Subscription (ثابت)")
    elif data == "admin":
        if is_admin(uid):
            text, kb = admin_view()
            await edit_or_send(q, text, kb)
    elif data.startswith("adm:"):
        if is_admin(uid):
            await admin_action(q, data.split(":", 1)[1])
    elif data.startswith(("show:", "qr:", "json:", "del:", "stat:", "delok:")):
        action, token = data.split(":", 1)
        cfg = owned(uid, token)
        if not cfg:
            await q.answer("پیدا نشد", show_alert=True)
            return
        if action == "show":
            text, kb = config_view(token, cfg)
            await edit_or_send(q, text, kb)
        elif action == "qr":
            if not server_online():
                await q.answer("سرور آفلاین است؛ QR ساب را استفاده کنید.", show_alert=True)
                return
            link = primary_link(cfg)
            await q.message.reply_photo(qr_png(link), caption=f"QR — {PROFILES[cfg['profile']].title} (تونل فعلی)")
        elif action == "json":
            if not server_online():
                await q.answer("سرور آفلاین است.", show_alert=True)
                return
            js = KillDPIConfig.from_link(primary_link(cfg)).to_client_json()
            bio = io.BytesIO(js.encode()); bio.name = f"killdpi-{cfg['profile']}.json"
            await q.message.reply_document(bio, caption="کانفیگ JSON برای Xray-core (نسخهٔ KILLDPI) — آدرس این ران")
        elif action == "stat":
            await edit_or_send(q, stat_text(token, cfg), back_kb("mine"))
        elif action == "del":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"delok:{token}"),
                                        InlineKeyboardButton("❌ انصراف", callback_data="mine")]])
            await edit_or_send(q, f"کانفیگ <b>{PROFILES[cfg['profile']].title}</b> حذف شود؟", kb)
        elif action == "delok":
            await delete_config(q, uid, user, token)


async def admin_action(q, action: str) -> None:
    if action == "republish":
        n = republish_all("bot: admin republish")
        await edit_or_send(q, f"🔄 {n} ساب بازنشر شد.", back_kb("admin"))
    elif action == "tunnel":
        if not SERVER:
            await edit_or_send(q, "سرور در این ران اجرا نشده.", back_kb("admin"))
            return
        await edit_or_send(q, "⏳ بازسازی تونل…", None)
        try:
            host = await asyncio.get_running_loop().run_in_executor(None, SERVER.restart_tunnel)
            RUNTIME["tunnel_host"] = host
            mark_dirty()
            n = republish_all("bot: tunnel restarted")
            await edit_or_send(q, f"🌐 تونل جدید: <code>{host}</code>\n{n} ساب بازنشر شد.", back_kb("admin"))
        except Exception as e:  # noqa: BLE001
            await edit_or_send(q, f"❌ خطا: {html.escape(str(e))[:300]}", back_kb("admin"))
    elif action == "users":
        await edit_or_send(q, users_text(), back_kb("admin"))
    elif action == "traffic":
        await edit_or_send(q, traffic_text(), back_kb("admin"))
    elif action == "relaunch":
        ok = store.dispatch_workflow(WORKFLOW_FILE, {"reason": "admin"})
        await edit_or_send(q, "🚀 ران جدید درخواست شد؛ این ران تا بالا آمدن ران بعدی ادامه می‌دهد." if ok else "❌ dispatch ناموفق",
                           back_kb("admin"))
    elif action == "maint":
        SETTINGS["maintenance"] = not SETTINGS.get("maintenance")
        mark_dirty(); flush_state("bot: toggle maintenance")
        text, kb = admin_view()
        await edit_or_send(q, text, kb)


def users_text() -> str:
    lines = ["<b>👥 کاربران</b>\n"]
    for uid_s, u in sorted(STATE["users"].items(), key=lambda kv: kv[1].get("last_seen", ""), reverse=True)[:40]:
        n = len([t for t in u.get("configs", []) if t in STATE["configs"]])
        tag = f"@{u['username']}" if u.get("username") else ""
        lines.append(f"• <code>{uid_s}</code> {html.escape(u.get('name', ''))} {tag} — {n} کانفیگ — {u.get('last_seen', '')[:10]}")
    return "\n".join(lines)


def traffic_text() -> str:
    rows = []
    for token, c in STATE["configs"].items():
        up, down = c.get("up", 0), c.get("down", 0)
        if server_online():
            try:
                u2, d2 = SERVER.traffic(email_of(token)); up += u2; down += d2
            except Exception:  # noqa: BLE001
                pass
        rows.append((up + down, token, c, up, down))
    rows.sort(reverse=True)
    lines = ["<b>📈 ترافیک (مجموع همهٔ ران‌ها)</b>\n"]
    for total, token, c, up, down in rows[:30]:
        lines.append(f"• {PROFILES[c['profile']].emoji} <code>{c['owner']}</code> ⬆️{human_bytes(up)} ⬇️{human_bytes(down)}")
    if SERVER and server_online():
        try:
            u, d = SERVER.total_traffic()
            lines.append(f"\nاین ران: ⬆️ {human_bytes(u)} ⬇️ {human_bytes(d)}")
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines) if len(lines) > 1 else "هنوز ترافیکی ثبت نشده."


# ----------------------------------------------------------------- admin commands
def admin_only(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_admin(update.effective_user.id):
            await update.effective_message.reply_text("⛔ فقط ادمین.")
            return
        await fn(update, ctx)
    return wrapper


@admin_only
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = admin_view()
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@admin_only
async def cmd_cleanips(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ips = [a for a in (ctx.args or []) if re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}|[a-z0-9.-]+\.[a-z]{2,}", a)]
    if not ips:
        await update.effective_message.reply_text(
            f"فعلی: {', '.join(SETTINGS['clean_ips'])}\n\nفرمت: /cleanips 188.114.96.1 162.159.192.1 …\n/cleanips reset → پیش‌فرض")
        return
    SETTINGS["clean_ips"] = DEFAULT_CLEAN_IPS if ctx.args[0] == "reset" else ips
    mark_dirty()
    n = republish_all("bot: clean ips changed")
    await update.effective_message.reply_text(f"✅ IPهای تمیز ذخیره شد و {n} ساب بازنشر شد.")


@admin_only
async def cmd_maxips(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args or not ctx.args[0].isdigit():
        await update.effective_message.reply_text(f"فعلی: {SETTINGS['max_ips']}\nفرمت: /maxips 4")
        return
    SETTINGS["max_ips"] = max(0, min(8, int(ctx.args[0])))
    mark_dirty()
    n = republish_all("bot: max ips changed")
    await update.effective_message.reply_text(f"✅ هر کانفیگ با {SETTINGS['max_ips']} IP تمیز در ساب؛ {n} ساب بازنشر شد.")


@admin_only
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(users_text(), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text.partition(" ")[2].strip()
    if not text:
        await update.effective_message.reply_text("فرمت: /broadcast متن پیام")
        return
    sent = 0
    for uid_s in list(STATE["users"]):
        try:
            await ctx.bot.send_message(int(uid_s), f"📢 {text}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:  # noqa: BLE001
            pass
    await update.effective_message.reply_text(f"✅ به {sent} کاربر ارسال شد.")


@admin_only
async def cmd_relaunch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ok = store.dispatch_workflow(WORKFLOW_FILE, {"reason": "admin"})
    await update.effective_message.reply_text("🚀 ران جدید درخواست شد." if ok else "❌ dispatch ناموفق (GH_PAT باید scope workflow داشته باشد).")


@admin_only
async def cmd_maintenance(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (ctx.args or ["toggle"])[0].lower()
    SETTINGS["maintenance"] = (arg == "on") if arg in ("on", "off") else not SETTINGS.get("maintenance")
    mark_dirty(); flush_state("bot: maintenance")
    await update.effective_message.reply_text(f"🛠 تعمیرات: {'روشن' if SETTINGS['maintenance'] else 'خاموش'}")


@admin_only
async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    bio = io.BytesIO(json.dumps(STATE, indent=2, ensure_ascii=False).encode()); bio.name = "state.json"
    await update.effective_message.reply_document(bio, caption="state.json")


@admin_only
async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    out = []
    for name in ("xray.log", "cloudflared.log"):
        p = os.path.join(".runtime", name)
        if os.path.exists(p):
            tail = open(p, errors="ignore").read()[-1500:]
            out.append(f"<b>{name}</b>\n<pre>{html.escape(tail)}</pre>")
    await update.effective_message.reply_text("\n\n".join(out) or "لاگی نیست.", parse_mode=ParseMode.HTML)


# ----------------------------------------------------------------- text (reply keyboard + pasted links)
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    uid = update.effective_user.id
    get_user(uid, update.effective_user)
    if text == BTN_NEW:
        await cmd_new(update, ctx)
    elif text == BTN_MINE:
        await cmd_mine(update, ctx)
    elif text == BTN_SUB:
        await cmd_sub(update, ctx)
    elif text == BTN_STATUS:
        await cmd_status(update, ctx)
    elif text == BTN_HELP:
        await cmd_help(update, ctx)
    elif text == BTN_ADMIN:
        await cmd_admin(update, ctx)
    else:
        m = re.search(r"killdpi://\S+", text)
        if not m:
            return
        try:
            cfg = KillDPIConfig.from_link(m.group(0))
        except LinkError as e:
            await update.effective_message.reply_text(f"❌ لینک نامعتبر: {e}")
            return
        await update.effective_message.reply_photo(
            qr_png(cfg.to_link()),
            caption=f"✅ لینک معتبر — {cfg.name}\n{cfg.host}:{cfg.port} {cfg.network}/{cfg.security} {cfg.cipher}")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=ctx.error)


# ----------------------------------------------------------------- server bootstrap
def ensure_private_key() -> str:
    """Use KILLDPI_PRIVATE_KEY; otherwise generate one and save it as a repo secret so the
    public key (and therefore every published config) stays stable across runs."""
    key = os.environ.get("KILLDPI_PRIVATE_KEY", "").strip()
    if key:
        return key
    if STATE["settings"].get("private_key"):  # legacy/local fallback
        return STATE["settings"]["private_key"]
    xray = find_bin("xray")
    priv, pub = KillDPIServer.keygen(xray)
    if store.set_secret("KILLDPI_PRIVATE_KEY", priv):
        log.warning("generated KILLDPI_PRIVATE_KEY and stored it as a repository secret (pub=%s)", pub)
    else:
        STATE["settings"]["private_key"] = priv  # LocalStorage / no permission: keep in state
        mark_dirty()
        log.warning("stored private key in state (could not create secret)")
    return priv


def start_server() -> None:
    global SERVER, PBK
    priv = ensure_private_key()
    PBK = KillDPIServer.public_key_of(priv)
    if NO_SERVER:
        log.warning("NO_SERVER=1 -> not starting xray/cloudflared")
        RUNTIME.update({"tunnel_host": "", "run_id": RUN_ID})
        return
    SERVER = KillDPIServer(find_bin("xray"), find_bin("cloudflared"), priv, ws_path=SETTINGS["ws_path"])
    host = SERVER.start(all_clients())
    RUNTIME.update({"tunnel_host": host, "run_id": RUN_ID, "started_ts": STARTED, "deadline_ts": DEADLINE,
                    "pbk": PBK, "clients": len(STATE["configs"])})
    mark_dirty()


def accumulate_traffic() -> None:
    """Fold this run's per-user counters into persistent totals (called before exit)."""
    if not server_online():
        return
    for token, c in STATE["configs"].items():
        try:
            up, down = SERVER.traffic(email_of(token))
            if up or down:
                c["up"] = c.get("up", 0) + up
                c["down"] = c.get("down", 0) + down
                mark_dirty()
        except Exception:  # noqa: BLE001
            pass
    # reset so the next fold does not double count
    try:
        SERVER.api.query_stats("user>>>", reset=True)
    except Exception:  # noqa: BLE001
        pass


async def notify_admins(app: Application, text: str) -> None:
    for uid in ADMIN_IDS:
        try:
            await app.bot.send_message(uid, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            pass


COMMANDS = [
    BotCommand("start", "شروع / منوی اصلی"), BotCommand("new", "ساخت کانفیگ جدید"),
    BotCommand("mine", "کانفیگ‌های من"), BotCommand("sub", "لینک Subscription ثابت"),
    BotCommand("status", "وضعیت سرور و زمان باقی‌مانده"), BotCommand("ping", "تست اتصال سرور"),
    BotCommand("apps", "کلاینت‌های سازگار"), BotCommand("help", "راهنما"),
]
ADMIN_COMMANDS = [BotCommand("admin", "پنل ادمین"), BotCommand("cleanips", "تنظیم IP تمیز"),
                  BotCommand("maxips", "تعداد IP در ساب"), BotCommand("users", "کاربران"),
                  BotCommand("broadcast", "پیام همگانی"), BotCommand("relaunch", "ران جدید"),
                  BotCommand("maintenance", "حالت تعمیرات"), BotCommand("export", "خروجی state"),
                  BotCommand("log", "لاگ سرور")]


def other_run_active() -> dict | None:
    """Fresh state from GitHub; returns runtime if a *different* run owns the server right now."""
    try:
        fresh = store.load_state()
    except Exception as e:  # noqa: BLE001
        log.warning("load_state: %s", e)
        return None
    rt = fresh.get("runtime", {})
    if rt.get("run_id") and str(rt["run_id"]) != RUN_ID and rt.get("tunnel_host"):
        return rt
    return None


def redundant_start() -> bool:
    """Another run is healthy and has plenty of time left -> this (cron/manual) run is not needed."""
    rt = other_run_active()
    if not rt:
        return False
    left = rt.get("deadline_ts", 0) - time.time()
    if left > RELAUNCH_LEAD + 180:
        live = [r for r in store.list_runs(WORKFLOW_FILE, "in_progress") if str(r.get("id")) == str(rt["run_id"])]
        if live:
            log.warning("run %s is active for another %ds -> exiting as redundant", rt["run_id"], left)
            return True
    return False


def merge_traffic_into_fresh_state() -> None:
    """At exit: fold this run's counters into whatever state is current on GitHub (another run may own it)."""
    if not server_online():
        return
    deltas: dict[str, tuple[int, int]] = {}
    for token in list(STATE["configs"]):
        try:
            up, down = SERVER.traffic(email_of(token))
            if up or down:
                deltas[token] = (up, down)
        except Exception:  # noqa: BLE001
            pass
    if not deltas:
        return
    try:
        fresh = store.load_state()
        for token, (up, down) in deltas.items():
            c = fresh["configs"].get(token)
            if c is not None:
                c["up"] = c.get("up", 0) + up
                c["down"] = c.get("down", 0) + down
        store.save_state(fresh, f"bot: run {RUN_ID} traffic totals")
    except Exception as e:  # noqa: BLE001
        log.error("merge traffic: %s", e)


# ----------------------------------------------------------------- main loop
async def run() -> None:
    global HANDOVER
    app = Application.builder().token(BOT_TOKEN).build()
    for name, fn in (("start", cmd_start), ("menu", cmd_start), ("help", cmd_help), ("new", cmd_new), ("mine", cmd_mine),
                     ("sub", cmd_sub), ("status", cmd_status), ("ping", cmd_ping), ("apps", cmd_apps),
                     ("admin", cmd_admin), ("cleanips", cmd_cleanips), ("maxips", cmd_maxips), ("users", cmd_users),
                     ("broadcast", cmd_broadcast), ("relaunch", cmd_relaunch), ("maintenance", cmd_maintenance),
                     ("export", cmd_export), ("log", cmd_log)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    if RUN_SECONDS and not LOCAL and redundant_start():
        return
    overlapping = bool(other_run_active())

    # --- server first, so the very first user sees a live tunnel
    try:
        start_server()
    except Exception as e:  # noqa: BLE001
        log.exception("server start failed")
        RUNTIME.update({"tunnel_host": "", "run_id": RUN_ID, "error": str(e)[:300]})
        mark_dirty()
    n = republish_all(f"bot: run {RUN_ID} started, tunnel={RUNTIME.get('tunnel_host') or 'none'}")
    log.info("republished %d subscriptions", n)

    await app.initialize()
    await app.bot.set_my_commands(COMMANDS)
    for uid in ADMIN_IDS:
        try:
            from telegram import BotCommandScopeChat
            await app.bot.set_my_commands(COMMANDS + ADMIN_COMMANDS, scope=BotCommandScopeChat(uid))
        except Exception:  # noqa: BLE001
            pass
    await app.start()
    if overlapping:
        log.info("previous run still alive -> waiting 25 s for it to release long-polling")
        await asyncio.sleep(25)
    await app.updater.start_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
    log.info("polling started (RUN_SECONDS=%s, storage=%s, tunnel=%s)", RUN_SECONDS, type(store).__name__, RUNTIME.get("tunnel_host"))
    if RUNTIME.get("tunnel_host"):
        await notify_admins(app, f"🟢 ران <code>{RUN_ID}</code> بالا آمد\nتونل: <code>{RUNTIME['tunnel_host']}</code>\n"
                                 f"کانفیگ‌ها: {len(STATE['configs'])} | ساب‌های بازنشرشده: {n}\n⏳ عمر ران: {fmt_remaining()}")
    else:
        await notify_admins(app, f"🔴 ران <code>{RUN_ID}</code> بدون سرور بالا آمد: {html.escape(RUNTIME.get('error', ''))}")

    relaunched = False
    handed_over = False
    last_handover_check = time.time()
    last_sub_refresh = time.time()
    last_flush = time.time()
    last_probe = time.time()
    try:
        while RUN_SECONDS == 0 or time.time() < DEADLINE:
            await asyncio.sleep(5)
            now = time.time()
            # a) persist dirty state quickly (every 20 s at most)
            if DIRTY and now - last_flush > 20:
                flush_state(); last_flush = now
            # b) refresh the countdown name inside every subscription (every 10 min)
            if now - last_sub_refresh > 600 and not HANDOVER:
                last_sub_refresh = now
                await asyncio.get_running_loop().run_in_executor(None, republish_all, "bot: countdown refresh")
            # c) watchdog: tunnel/xray died -> rebuild
            if SERVER and now - last_probe > 120:
                last_probe = now
                if not SERVER.healthy():
                    log.error("server unhealthy -> restarting tunnel")
                    try:
                        if not SERVER.tunnel_alive():
                            RUNTIME["tunnel_host"] = await asyncio.get_running_loop().run_in_executor(None, SERVER.restart_tunnel)
                            mark_dirty()
                            await asyncio.get_running_loop().run_in_executor(None, republish_all, "bot: tunnel rebuilt")
                            await notify_admins(app, f"♻️ تونل بازسازی شد: <code>{RUNTIME['tunnel_host']}</code>")
                    except Exception as e:  # noqa: BLE001
                        log.error("tunnel restart failed: %s", e)
            # d) hand-over: dispatch the next run shortly before this one must end
            if RUN_SECONDS and not relaunched and DEADLINE - now <= RELAUNCH_LEAD:
                relaunched = True
                others = [r for r in store.list_runs(WORKFLOW_FILE, "in_progress") if str(r.get("id")) != RUN_ID]
                queued = store.list_runs(WORKFLOW_FILE, "queued")
                if others or queued:
                    log.info("next run already exists (%d in progress, %d queued)", len(others), len(queued))
                elif store.dispatch_workflow(WORKFLOW_FILE, {"reason": "handover"}):
                    log.info("dispatched next run")
                    flush_state("bot: pre-handover flush")
                    HANDOVER = True
                    await notify_admins(app, f"🔁 ران بعدی درخواست شد؛ این ران تا {fmt_remaining()} دیگر فعال است.")
                last_handover_check = now
            # e) after dispatch: the moment the successor publishes its tunnel, stop (one poller at a time)
            if relaunched and now - last_handover_check > 15:
                last_handover_check = now
                rt = await asyncio.get_running_loop().run_in_executor(None, other_run_active)
                if rt:
                    log.info("successor run %s is live (%s) -> handing over", rt["run_id"], rt["tunnel_host"])
                    handed_over = True
                    break
    finally:
        if handed_over:
            # successor owns data/state.json now: only add our traffic, never touch runtime
            merge_traffic_into_fresh_state()
            await notify_admins(app, f"🔁 ران <code>{RUN_ID}</code> کار را به ران بعدی سپرد و خاموش شد.")
        elif HANDOVER:
            # successor never showed up in time: give it our traffic but leave runtime alone
            merge_traffic_into_fresh_state()
        else:
            accumulate_traffic()
            RUNTIME.update({"tunnel_host": "", "ended_ts": time.time(), "deadline_ts": 0})
            mark_dirty()
            republish_all("bot: run ended")
            await notify_admins(app, f"⚫️ ران <code>{RUN_ID}</code> تمام شد؛ ساب‌ها با ران بعدی بروزرسانی می‌شوند.")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        if SERVER:
            SERVER.stop()
        log.info("stopped cleanly")


if __name__ == "__main__":
    asyncio.run(run())
