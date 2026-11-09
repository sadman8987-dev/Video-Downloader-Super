"""محرك التنزيل والضغط. ينظف مجلد العمل دائماً بعد إرسال الملف أو فشل العملية."""
import asyncio
import logging
import subprocess
import tempfile
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

logger = logging.getLogger(__name__)

# نترك هامشاً أسفل سقف تيليجرام البالغ 50 MB.
MAX_UPLOAD_BYTES = 48 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 600
FFMPEG_TIMEOUT_SECONDS = 300


class DownloadError(Exception):
    """خطأ عام في التنزيل."""


class PrivateVideoError(DownloadError):
    """المحتوى خاص أو يتطلب تسجيل دخول."""


class DownloadTimeoutError(DownloadError):
    """انتهت مهلة التنزيل أو المعالجة."""


class FileTooLargeError(DownloadError):
    """تعذر جعل الملف ضمن حد الإرسال."""


def _looks_private(error: str) -> bool:
    text = error.lower()
    markers = (
        "private video", "login required", "sign in", "authentication",
        "cookies", "members-only", "video unavailable", "account",
    )
    return any(marker in text for marker in markers)


def _find_video_file(folder: Path) -> Path:
    candidates = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
    ]
    if not candidates:
        raise DownloadError("لم ينتج محرك التنزيل ملف فيديو")
    return max(candidates, key=lambda p: p.stat().st_size)


async def _download_async(url: str, folder: Path) -> Path:
    """تشغيل yt-dlp كعملية مستقلة يمكن إيقافها فعلياً عند انتهاء المهلة."""
    output_template = str(folder / "source.%(ext)s")
    command = [
        sys.executable, "-m", "yt_dlp",
        "--output", output_template,
        "--format", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--socket-timeout", "30",
        "--retries", "2",
        "--fragment-retries", "2",
        "--file-access-retries", "2",
        "--extractor-retries", "2",
        "--concurrent-fragments", "4",
        "--no-warnings",
        "--quiet",
        url,
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise DownloadTimeoutError("انتهت مهلة التنزيل") from exc

    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace")
        logger.warning("yt-dlp failed: %s", message[-1800:])
        if _looks_private(message):
            raise PrivateVideoError("المحتوى غير متاح للعامة")
        if any(s in message.lower() for s in ("timed out", "timeout", "temporary failure")):
            raise DownloadTimeoutError("انتهت مهلة الاتصال")
        raise DownloadError("فشل التنزيل")

    return _find_video_file(folder)


def _compress_sync(source: Path, folder: Path) -> Path:
    """ضغط تدريجي مع تقليل الدقة ومعدل البت حتى يصبح الملف أصغر من الحد."""
    output = folder / "compressed.mp4"
    attempts = [
        (720, "1100k", "128k"),
        (480, "750k", "96k"),
        (360, "450k", "64k"),
    ]
    for height, video_bitrate, audio_bitrate in attempts:
        command = [
            "ffmpeg", "-y", "-i", str(source),
            "-vf", f"scale=-2:'min({height},ih)'",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", video_bitrate, "-maxrate", video_bitrate,
            "-bufsize", str(int(video_bitrate[:-1]) * 2) + "k",
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-movflags", "+faststart",
            str(output),
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True,
                timeout=FFMPEG_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DownloadTimeoutError("انتهت مهلة ضغط الفيديو") from exc
        if result.returncode == 0 and output.exists() and output.stat().st_size <= MAX_UPLOAD_BYTES:
            return output
        if result.returncode != 0:
            logger.warning("FFmpeg compression attempt failed: %s", result.stderr[-1200:])
    raise FileTooLargeError("تعذر ضغط الفيديو إلى الحجم المطلوب")


@asynccontextmanager
async def download_video(url: str) -> AsyncIterator[Path]:
    """
    ينشئ مجلداً مؤقتاً، ينزّل الفيديو ويضغطه عند الحاجة.
    يظل الملف متاحاً أثناء إرسال تيليجرام، ثم يُحذف المجلد دائماً.
    """
    temp_dir = tempfile.TemporaryDirectory(prefix="video_bot_")
    folder = Path(temp_dir.name)
    try:
        source = await _download_async(url, folder)
        if source.stat().st_size <= MAX_UPLOAD_BYTES:
            yield source
            return
        compressed = await asyncio.to_thread(_compress_sync, source, folder)
        yield compressed
    finally:
        # التنظيف مضمون بعد الإرسال أو عند أي استثناء.
        temp_dir.cleanup()
