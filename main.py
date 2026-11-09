"""بوت تيليجرام لتنزيل المحتوى المسموح به فقط."""
import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from downloader import (
    DownloadError, DownloadTimeoutError, FileTooLargeError,
    PrivateVideoError, download_video,
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
        "أهلاً بك في بوت تنزيل الفيديو 📥\n\n"
        "أرسل رابطاً عاماً من YouTube أو TikTok أو Instagram أو Facebook أو X.\n"
        "يُسمح فقط بالمحتوى الذي تملك حق تنزيله أو لديك إذن باستخدامه.\n\n"
        "استخدم /help لمعرفة طريقة الاستخدام."
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
        "1) أرسل رابط فيديو عام من منصة مدعومة.\n"
        "2) انتظر انتهاء التجهيز.\n"
        "3) سيُرسل الفيديو في نفس المحادثة.\n\n"
        "الحد: 5 طلبات لكل دقيقة، وتنزيلان في الوقت نفسه.\n"
        "لا يدعم البوت الحسابات الخاصة أو تجاوز تسجيل الدخول أو القيود."
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

    status = await msg.reply_text("⏳ جاري فحص الرابط وتجهيز الفيديو...")
    try:
        # يسمح بتنزيلين وإرسالهما في الوقت نفسه كحد أقصى.
        async with DOWNLOAD_SEMAPHORE:
            async with download_video(url) as video_path:
                await context.bot.send_chat_action(
                    chat_id=msg.chat_id, action=ChatAction.UPLOAD_VIDEO
                )
                await status.edit_text("✅ اكتمل التنزيل، جاري إرسال الفيديو...")
                with video_path.open("rb") as stream:
                    await msg.reply_video(
                        video=stream,
                        supports_streaming=True,
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                        pool_timeout=30,
                    )
        await status.delete()
    except PrivateVideoError:
        await status.edit_text(
            "🔒 تعذر الوصول إلى الفيديو. قد يكون خاصاً أو محذوفاً أو يتطلب تسجيل دخول.\n"
            "لا يحاول البوت تجاوز إعدادات الخصوصية."
        )
    except DownloadTimeoutError:
        await status.edit_text("⏰ استغرق التنزيل وقتاً أطول من المسموح. حاول لاحقاً.")
    except FileTooLargeError:
        await status.edit_text("❌ حجم الفيديو أكبر من حد تيليجرام حتى بعد الضغط.")
    except DownloadError as exc:
        logger.info("Download rejected/failed: %s", exc)
        await status.edit_text("❌ تعذر تنزيل الفيديو. تحقق من الرابط وإمكانية الوصول إليه.")
    except TelegramError:
        logger.exception("Telegram send error")
        try:
            await status.edit_text("⚠️ تعذر إرسال الملف عبر تيليجرام. حاول بفيديو أصغر.")
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    logger.info("بدأ تشغيل البوت بنظام Polling")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
