"""y2kbaddie reseller bot 🛍️ — send it photos of an item and it identifies it, prices it
for the UK resale market (Vinted/Depop/eBay) using live web search, and writes a ready-to-post
listing. Also answers reselling questions. Powered by Claude (Anthropic API).

Env: TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, OWNER_ID (comma-separated user ids to lock it).
Stateless, Railway-ready. Phase 2 (auto-posting to eBay) comes later.
"""
import base64
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
from telegram import Update
from telegram.error import Conflict
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

logging.basicConfig(level=logging.WARNING)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OWNER_IDS = {x.strip() for x in (os.environ.get("OWNER_ID") or "").split(",") if x.strip()}
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")   # override to claude-sonnet-4-6 to save
MAX_IMAGES = 6

try:
    claude = anthropic.AsyncAnthropic()   # reads ANTHROPIC_API_KEY from the environment
except Exception:
    claude = None

SYSTEM = (
    "You are the research assistant for 'y2kbaddie', a UK vintage & designer reselling brand run by Lace. "
    "You help her identify items, price them, and write listings.\n\n"
    "When she sends PHOTOS of an item:\n"
    "1. IDENTIFY it — brand, model/line, era, materials. Use web search to confirm, don't guess.\n"
    "2. AUTHENTICITY — be honest: you usually can't fully authenticate from photos. Say what looks "
    "right/off and what she should check (date code, heat stamp, stitching). Never invent a model or "
    "declare it genuine from photos alone.\n"
    "3. PRICE — a realistic UK resale range for Vinted/Depop/eBay using web search for comparable SOLD "
    "prices, plus one suggested list price.\n"
    "4. LISTING — a punchy title (with the words buyers actually search) and an honest, appealing "
    "description. Mention it ships with the y2kbaddie branded thank-you card + collectable art print.\n\n"
    "When she asks a QUESTION, answer helpfully and use web search for current prices/facts.\n\n"
    "Style: real, warm, a little baddie — no corporate fluff. British English, £. Be honest about "
    "uncertainty. Keep item replies in clear labelled sections."
)


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if OWNER_IDS and (not u or str(u.id) not in OWNER_IDS):
            if update.message:
                await update.message.reply_text("this is a private bot 🩷")
            return
        return await func(update, context)
    return wrapper


async def ask_claude(user_content):
    """One research turn with live web search. Returns the text answer."""
    if claude is None:
        return "⚠️ my brain isn't connected — ANTHROPIC_API_KEY isn't set in Railway."
    messages = [{"role": "user", "content": user_content}]
    resp = None
    for _ in range(6):   # allow a few pause_turn continuations while it searches
        resp = await claude.messages.create(
            model=MODEL,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=SYSTEM,
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            messages=messages,
        )
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return text or "hmm, I couldn't get a clear read on that — try another photo or a bit more detail? 🌷"


def _split(text, limit=3800):
    """Split a long reply into Telegram-safe chunks on paragraph breaks."""
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > limit:
            if cur:
                chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks or [text]


async def _reply_long(context, chat_id, text):
    for chunk in _split(text):
        try:
            await context.bot.send_message(chat_id, chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await context.bot.send_message(chat_id, chunk)   # fall back to plain if markdown trips


# ── photo batching (albums arrive as separate updates — debounce ~2.5s) ────────
_buffers = {}   # chat_id -> {"items": [(file_id, media_type), ...], "note": str}
SUPPORTED = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _schedule(update, context):
    cid = update.effective_chat.id
    if update.message.caption:
        _buffers[cid]["note"] = update.message.caption
    for job in context.job_queue.get_jobs_by_name(f"proc_{cid}"):
        job.schedule_removal()
    context.job_queue.run_once(_process_batch, when=2.5, name=f"proc_{cid}", data=cid, chat_id=cid)


@owner_only
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    buf = _buffers.setdefault(cid, {"items": [], "note": ""})
    buf["items"].append((update.message.photo[-1].file_id, "image/jpeg"))   # photos are jpeg
    _schedule(update, context)


@owner_only
async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Images sent 'as a file' (PNG screenshots etc.) come through as documents."""
    doc = update.message.document
    mime = (doc.mime_type or "").lower()
    if not mime.startswith("image/"):
        await update.message.reply_text(
            "that's not an image I can read 🌷 send me a photo of the item (png or jpg is perfect)")
        return
    media_type = mime if mime in SUPPORTED else "image/jpeg"
    cid = update.effective_chat.id
    buf = _buffers.setdefault(cid, {"items": [], "note": ""})
    buf["items"].append((doc.file_id, media_type))
    _schedule(update, context)


async def _process_batch(context: ContextTypes.DEFAULT_TYPE):
    cid = context.job.data
    buf = _buffers.pop(cid, None)
    if not buf or not buf["items"]:
        return
    await context.bot.send_message(cid, "on it — identifying & pricing your item 🔎🩷 (~30s)")
    await context.bot.send_chat_action(cid, ChatAction.TYPING)
    content = []
    for fid, media_type in buf["items"][:MAX_IMAGES]:
        try:
            f = await context.bot.get_file(fid)
            data = bytes(await f.download_as_bytearray())
            if len(data) > 4_500_000:   # Anthropic caps images ~5MB after base64
                await context.bot.send_message(
                    cid, "one of those images is too big — send it as a Photo (it compresses) 🌷")
                continue
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": media_type,
                "data": base64.standard_b64encode(data).decode()}})
        except Exception as e:
            logging.warning("image download failed: %r", e)
    if not content:
        await context.bot.send_message(
            cid, "hmm, none of the images came through 🌷 try sending them again as Photos "
            "(tap the 📎 → Photo, not File).")
        return
    content.append({"type": "text", "text":
                    "Here's an item for y2kbaddie. Identify it, flag authenticity honestly, price it "
                    "for the UK resale market, and write me a ready-to-post listing. "
                    f"My notes: {buf['note'] or '(none)'}"})
    try:
        answer = await ask_claude(content)
    except Exception as e:
        await context.bot.send_message(cid, f"something went wrong reaching my brain 🌷 ({e}) — try again?")
        return
    await _reply_long(context, cid, answer)


@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        answer = await ask_claude([{"type": "text", "text": update.message.text}])
    except Exception as e:
        await update.message.reply_text(f"something went wrong 🌷 ({e}) — try again?")
        return
    await _reply_long(context, update.effective_chat.id, answer)


@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "hey boss 🛍️🩷 I'm your y2kbaddie reseller assistant\n\n"
        "📸 *send me photos of an item* (a few angles + any markings) and I'll:\n"
        "• identify it (brand, model, era)\n"
        "• flag anything authenticity-wise, honestly\n"
        "• price it for Vinted / Depop / eBay\n"
        "• write you a ready-to-post listing\n\n"
        "💬 or just *ask me anything* — pricing, what a piece is, how to word something.\n\n"
        "add a caption with your photos for extra context (e.g. \"tan leather, no date code\").",
        parse_mode=ParseMode.MARKDOWN,
    )


@owner_only
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"your user id: {update.effective_user.id}\n\n"
        "add it to OWNER_ID in Railway to lock the bot to you 🩷"
    )


_conflict_logged = False


async def on_error(update, context):
    """Log errors as a one-line warning instead of a scary red traceback.
    Telegram 'Conflict' (two instances) self-heals once the duplicate stops —
    log it only once so it doesn't spam."""
    global _conflict_logged
    err = context.error
    if isinstance(err, Conflict):
        if not _conflict_logged:
            _conflict_logged = True
            logging.warning("Telegram Conflict — another instance is polling this token. "
                            "Ensure only ONE Railway deploy is active. Silencing further repeats.")
    else:
        logging.warning("handler error: %r", err)


def _start_health_server():
    port = os.environ.get("PORT")
    if not port:
        return
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a):
            pass
    try:
        srv = HTTPServer(("0.0.0.0", int(port)), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    except Exception:
        pass


def main():
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (from @BotFather).")
    if claude is None:
        print("⚠️ ANTHROPIC_API_KEY not set — the bot runs but can't think until you add it.")
    _start_health_server()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", whoami))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    print("🛍️ y2kbaddie reseller bot live — polling")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
