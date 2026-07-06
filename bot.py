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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

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


# running conversation per chat, so it remembers the item + your whole back-and-forth
# until you start a new item (send new photos) or tap Done / send /new.
_convo = {}   # chat_id -> [ {role, content}, ... ]  (first user turn holds the item photos)


def _trim(convo):
    """Bound history/cost: keep the first turn (has the photos) + the last ~16 messages."""
    if len(convo) > 18:
        del convo[1:len(convo) - 16]


async def _complete(messages, *, search=True, think=True, max_tokens=4096):
    """Run one API turn over a COPY of messages; return assistant text (doesn't mutate input)."""
    if claude is None:
        return "⚠️ my brain isn't connected — ANTHROPIC_API_KEY isn't set in Railway."
    work = list(messages)
    kwargs = {"model": MODEL, "max_tokens": max_tokens, "system": SYSTEM, "messages": work}
    if think:
        kwargs["thinking"] = {"type": "adaptive"}
    if search:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search"}]
    resp = None
    for _ in range(6):   # allow a few pause_turn continuations while it searches
        resp = await claude.messages.create(**kwargs)
        if resp.stop_reason == "pause_turn":
            work.append({"role": "assistant", "content": resp.content})
            continue
        break
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return text or "hmm, I couldn't get a clear read on that — try another photo or a bit more detail? 🌷"


async def converse(cid, user_content, *, search=True, think=True, max_tokens=4096):
    """Add a turn to this chat's ongoing conversation and return the reply."""
    convo = _convo.setdefault(cid, [])
    convo.append({"role": "user", "content": user_content})
    text = await _complete(convo, search=search, think=think, max_tokens=max_tokens)
    convo.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    _trim(convo)
    return text


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


_has_item = set()   # chats that currently have an item in play (for the tap-buttons)

QUICK_PROMPT = (
    "Identify this item for resale. Give me, in ~4 short punchy lines: brand, model/line, era, key "
    "materials, and a ROUGH £ resale ballpark from your own knowledge (mark it as a rough guide). "
    "Do NOT write a listing, a long authenticity essay, or search the web — just the fast ID + ballpark.")

_ITEM_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("💷 Live price (sold comps)", callback_data="act:price"),
     InlineKeyboardButton("📝 Write the listing", callback_data="act:listing")],
    [InlineKeyboardButton("⚠️ Authenticity check", callback_data="act:auth"),
     InlineKeyboardButton("📖 Tell me more", callback_data="act:more")],
    [InlineKeyboardButton("✅ Done — new item", callback_data="act:done")],
])

_ACTIONS = {
    "price": ("💷 pricing it with live sold comps", True,
              "Price THIS exact item for UK resale. Use web search for comparable SOLD prices on "
              "eBay/Vinted/Depop. Give a realistic £ range and one suggested list price. Keep it tight."),
    "listing": ("📝 writing your listing", False,
                "Write a ready-to-post listing for THIS item: a punchy, keyword-rich TITLE (words buyers "
                "search), then an honest, appealing DESCRIPTION. Note it ships with the y2kbaddie "
                "thank-you card + collectable art print. Ready to paste — no preamble."),
    "auth": ("⚠️ running an authenticity check", False,
             "Give an honest authenticity checklist for THIS exact item — what to check (date code, heat "
             "stamp, stitching, hardware, lining) and any red/green flags visible in the photos. Be real "
             "about what photos alone can't confirm. Concise."),
    "more": ("📖 digging up more on it", True,
             "Tell me more about THIS item — the line's history, why buyers want it, how to style it, and "
             "what makes this one desirable. A few short punchy paragraphs."),
}


async def _process_batch(context: ContextTypes.DEFAULT_TYPE):
    cid = context.job.data
    buf = _buffers.pop(cid, None)
    if not buf or not buf["items"]:
        return
    await context.bot.send_chat_action(cid, ChatAction.TYPING)
    images = []
    for fid, media_type in buf["items"][:MAX_IMAGES]:
        try:
            f = await context.bot.get_file(fid)
            data = bytes(await f.download_as_bytearray())
            if len(data) > 4_500_000:   # Anthropic caps images ~5MB after base64
                await context.bot.send_message(
                    cid, "one of those images is too big — send it as a Photo (it compresses) 🌷")
                continue
            images.append({"type": "image", "source": {
                "type": "base64", "media_type": media_type,
                "data": base64.standard_b64encode(data).decode()}})
        except Exception as e:
            logging.warning("image download failed: %r", e)
    if not images:
        await context.bot.send_message(
            cid, "hmm, none of the images came through 🌷 try sending them again as Photos "
            "(tap the 📎 → Photo, not File).")
        return
    _convo[cid] = []          # new item = fresh conversation (photos become turn 1)
    _has_item.add(cid)
    note = f"\n\nMy notes: {buf['note']}" if buf["note"] else ""
    try:
        answer = await converse(cid, images + [{"type": "text", "text": QUICK_PROMPT + note}],
                                search=False, think=True, max_tokens=2000)
    except Exception as e:
        await context.bot.send_message(cid, f"something went wrong reaching my brain 🌷 ({e}) — try again?")
        return
    for chunk in _split(answer):
        try:
            await context.bot.send_message(cid, chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await context.bot.send_message(cid, chunk)
    await context.bot.send_message(cid, "tap for more 👇", reply_markup=_ITEM_KB)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if OWNER_IDS and str(q.from_user.id) not in OWNER_IDS:
        return
    action = q.data.split(":", 1)[1]
    cid = q.message.chat.id
    if action == "done":
        _convo.pop(cid, None)
        _has_item.discard(cid)
        await context.bot.send_message(cid, "done with that one ✅ send the next item's photos 🩷")
        return
    if cid not in _has_item or action not in _ACTIONS:
        await q.message.reply_text("send me the item photos again 🌷 (I lost track of that one)")
        return
    label, search, prompt = _ACTIONS[action]
    await context.bot.send_message(cid, f"{label}… 🔎")
    await context.bot.send_chat_action(cid, ChatAction.TYPING)
    try:
        answer = await converse(cid, [{"type": "text", "text": prompt}], search=search, think=True)
    except Exception as e:
        await context.bot.send_message(cid, f"something went wrong 🌷 ({e}) — try again?")
        return
    await _reply_long(context, cid, answer)
    await context.bot.send_message(cid, "anything else? 👇", reply_markup=_ITEM_KB)


@owner_only
async def new_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    _convo.pop(cid, None)
    _has_item.discard(cid)
    await update.message.reply_text("fresh start ✨ send the next item's photos 🩷")


@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        answer = await converse(cid, [{"type": "text", "text": update.message.text}])
    except Exception as e:
        await update.message.reply_text(f"something went wrong 🌷 ({e}) — try again?")
        return
    await _reply_long(context, update.effective_chat.id, answer)


@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "hey boss 🛍️🩷 I'm your y2kbaddie reseller assistant\n\n"
        "📸 *send me photos of an item* (a few angles + any markings). I'll give you a *quick ID + "
        "ballpark price* in seconds, then buttons to tap for more:\n"
        "• 💷 live price (real sold comps)\n"
        "• 📝 write the listing\n"
        "• ⚠️ authenticity check\n"
        "• 📖 tell me more\n\n"
        "💬 then *keep chatting* — I remember the item, so ask follow-ups freely "
        "(\"can you make the title punchier?\", \"is the price fair?\").\n"
        "✅ when you're onto the next piece, tap *Done* or send /new.\n\n"
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
    app.add_handler(CommandHandler("new", new_item))
    app.add_handler(CommandHandler("id", whoami))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_document))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^act:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    print("🛍️ y2kbaddie reseller bot live — polling")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
