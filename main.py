"""بوت تيليجرام لتنزيل الفيديو والصوت MP3 للمحتوى المسموح به فقط."""
import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    CallbackQueryHandler, filters,
)

from downloader import (
    DownloadError, DownloadTimeoutError, FileTooLargeError,
    PrivateVideoError, PlatformVerificationRequired, download_video, download_audio,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("video_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    ALLOWED_USER_IDS = {
        int(item.strip())
        for item in os.getenv("ALLOWED_USER_IDS", "").split(",")
        if item.strip()
    }
except ValueError as exc:
    raise RuntimeError("ALLOWED_USER_IDS يجب أن يحتوي على أرقام مفصولة بفواصل") from exc

if not BOT_TOKEN:
    raise RuntimeError("متغير BOT_TOKEN غير مضبوط")
if not ALLOWED_USER_IDS:
    raise RuntimeError("يجب ضبط ALLOWED_USER_IDS بمُعرّف مستخدم واحد على الأقل")

MAX_REQUESTS_PER_MINUTE = 5
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)
requests_by_user = defaultdict(deque)

SUPPORTED_DOMAINS = {
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "facebook.com", "fb.watch", "x.com", "twitter.com",
}


def authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


def supported_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in {"http", "https"}
            and any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)
        )
    except ValueError:
        return False


def rate_allowed(user_id: int) -> bool:
    now = time.monotonic()
    queue = requests_by_user[user_id]
    while queue and now - queue[0] >= 60:
        queue.popleft()
    if len(queue) >= MAX_REQUESTS_PER_MINUTE:
        return False
    queue.append(now)
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    if not authorized(user.id):
        await msg.reply_text("⛔ عذراً، غير مسموح لك باستخدام هذا البوت.")
        return
    await msg.reply_text(
        "أهلاً بك في بوت تنزيل الوسائط 📥\n\n"
        "أرسل رابطاً عاماً من YouTube أو TikTok أو Instagram أو Facebook أو X، "
        "ثم اختر تنزيل فيديو أو صوت MP3.\n"
        "يُسمح فقط بالمحتوى الذي تملك حق تنزيله أو لديك إذن باستخدامه.\n\n"
        "استخدم /help لمعرفة التفاصيل."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    if not authorized(user.id):
        await msg.reply_text("⛔ غير مسموح لك باستخدام هذا البوت.")
        return
    await msg.reply_text(
        "طريقة الاستخدام:\n"
        "1) أرسل رابطاً عاماً من منصة مدعومة.\n"
        "2) اختر 🎬 تنزيل فيديو أو 🎵 تنزيل صوت MP3.\n"
        "3) انتظر إرسال الملف.\n\n"
        "الحد: 5 طلبات لكل دقيقة، وتنزيلان في الوقت نفسه. "
        "لا يدعم البوت المحتوى الخاص أو تجاوز تسجيل الدخول أو القيود."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, msg = update.effective_user, update.effective_message
    if not user or not msg or not msg.text:
        return
    if not authorized(user.id):
        await msg.reply_text("⛔ غير مسموح لك باستخدام هذا البوت.")
        return
    if not rate_allowed(user.id):
        await msg.reply_text("⏳ وصلت إلى 5 طلبات خلال دقيقة. انتظر قليلاً ثم حاول مجدداً.")
        return

    url = msg.text.strip()
    if not supported_url(url):
        await msg.reply_text("❌ الرابط غير صحيح أو المنصة غير مدعومة.")
        return

    context.user_data["pending_media_url"] = url
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 تنزيل فيديو", callback_data="media:video"),
        InlineKeyboardButton("🎵 تنزيل صوت MP3", callback_data="media:audio"),
    ]])
    await msg.reply_text("اختار نوع الملف المطلوب:", reply_markup=keyboard)


async def choose_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    if not authorized(query.from_user.id):
        await query.answer("⛔ غير مسموح لك باستخدام هذا البوت.", show_alert=True)
        return

    url = context.user_data.get("pending_media_url")
    if not url:
        await query.answer("انتهت صلاحية الرابط. أرسله مرة أخرى.", show_alert=True)
        return

    mode = (query.data or "").split(":", 1)[1]
    if mode not in {"video", "audio"}:
        await query.answer("اختيار غير صالح.", show_alert=True)
        return
    await query.answer("بدأ تجهيز الملف...")
    context.user_data.pop("pending_media_url", None)

    status = await query.message.reply_text(
        "⏳ جاري تجهيز الصوت MP3..." if mode == "audio" else "⏳ جاري تجهيز الفيديو..."
    )
    try:
        async with DOWNLOAD_SEMAPHORE:
            manager = download_audio(url) if mode == "audio" else download_video(url)
            async with manager as media_path:
                action = ChatAction.UPLOAD_AUDIO if mode == "audio" else ChatAction.UPLOAD_VIDEO
                await context.bot.send_chat_action(chat_id=query.message.chat_id, action=action)
                await status.edit_text("✅ اكتمل التنزيل، جاري إرسال الملف...")
                with media_path.open("rb") as stream:
                    if mode == "audio":
                        await query.message.reply_audio(
                            audio=stream,
                            filename=media_path.name,
                            title=media_path.stem[:64],
                            caption="🎵 تم تنزيل الصوت بصيغة MP3 — 192 kbps",
                            read_timeout=180,
                            write_timeout=180,
                            connect_timeout=30,
                            pool_timeout=30,
                        )
                    else:
                        await query.message.reply_video(
                            video=stream,
                            supports_streaming=True,
                            read_timeout=180,
                            write_timeout=180,
                            connect_timeout=30,
                            pool_timeout=30,
                        )
        await status.delete()
    except PlatformVerificationRequired:
        await status.edit_text(
            "⚠️ المنصة طلبت تحققاً لمكافحة التنزيل الآلي (مثل YouTube). "
            "هذا لا يعني أن الفيديو خاص. جرّب لاحقاً أو استخدم رابطاً عاماً آخر."
        )
    except PrivateVideoError:
        await status.edit_text(
            "🔒 المنصة أكدت أن المحتوى خاص أو يتطلب صلاحية وصول. "
            "استخدم رابطاً عاماً تملك حق الوصول إليه."
        )
    except DownloadTimeoutError:
        await status.edit_text("⏰ استغرق التنزيل وقتاً أطول من المسموح. حاول لاحقاً.")
    except FileTooLargeError:
        await status.edit_text("❌ حجم الملف أكبر من حد تيليجرام حتى بعد المعالجة.")
    except DownloadError as exc:
        logger.info("Download rejected/failed: %s", exc)
        await status.edit_text("❌ تعذر تنزيل الملف. تحقق من الرابط وإمكانية الوصول إليه.")
    except TelegramError:
        logger.exception("Telegram send error")
        try:
            await status.edit_text("⚠️ تعذر إرسال الملف عبر تيليجرام. حاول بملف أصغر.")
        except TelegramError:
            pass
    except Exception:
        logger.exception("Unexpected request failure")
        try:
            await status.edit_text("⚠️ حدث خطأ غير متوقع. حاول مرة أخرى لاحقاً.")
        except TelegramError:
            pass


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(180)
        .write_timeout(180)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(choose_media, pattern=r"^media:(video|audio)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    logger.info("بدأ تشغيل بوت الفيديو والصوت بنظام Polling")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
