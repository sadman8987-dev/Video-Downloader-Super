"""محرك تنزيل الفيديو والصوت MP3 مع تنظيف الملفات المؤقتة."""
import asyncio
import logging
import subprocess
import tempfile
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 48 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 600
FFMPEG_TIMEOUT_SECONDS = 300


class DownloadError(Exception):
    """خطأ عام في التنزيل."""


class PrivateVideoError(DownloadError):
    """المحتوى خاص فعلاً أو يتطلب صلاحية وصول."""


class PlatformVerificationRequired(DownloadError):
    """المنصة تطلب تحققاً بشرياً أو تسجيل دخول لمكافحة البوتات."""


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


def _find_file(folder: Path, extensions: set[str]) -> Path:
    candidates = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    ]
    if not candidates:
        raise DownloadError("لم ينتج محرك التنزيل ملفاً صالحاً")
    return max(candidates, key=lambda p: p.stat().st_size)


async def _run_ytdlp(url: str, folder: Path, mode: str) -> Path:
    output_template = str(folder / "source.%(ext)s")
    command = [
        sys.executable, "-m", "yt_dlp",
        "--output", output_template,
        "--no-playlist",
        "--socket-timeout", "30",
        "--retries", "2",
        "--fragment-retries", "2",
        "--file-access-retries", "2",
        "--extractor-retries", "2",
        "--no-warnings",
        "--quiet",
    ]
    if mode == "audio":
        command += [
            "--format", "bestaudio/best",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "192K",
        ]
    else:
        command += [
            "--format", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "--concurrent-fragments", "4",
        ]
    command.append(url)

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=DOWNLOAD_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise DownloadTimeoutError("انتهت مهلة التنزيل") from exc

    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace")
        logger.warning("yt-dlp failed: %s", message[-1800:])
        lowered = message.lower()
        if any(marker in lowered for marker in (
            "sign in to confirm", "confirm you're not a bot",
            "confirm you’re not a bot", "verify you are human",
            "captcha", "automated requests",
        )):
            raise PlatformVerificationRequired(
                "المنصة طلبت تحققاً لمكافحة البوتات؛ لم يتم اعتبار الرابط خاصاً."
            )
        if _looks_private(message):
            raise PrivateVideoError("المحتوى غير متاح للعامة")
        if any(s in lowered for s in ("timed out", "timeout", "temporary failure")):
            raise DownloadTimeoutError("انتهت مهلة الاتصال")
        raise DownloadError("فشل التنزيل")

    extensions = {".mp3"} if mode == "audio" else {".mp4", ".mkv", ".webm", ".mov"}
    result = _find_file(folder, extensions)
    # الصوت لا يمكن إرساله إذا تجاوز الحد؛ الفيديو يمر إلى مرحلة الضغط أدناه.
    if mode == "audio" and result.stat().st_size > MAX_UPLOAD_BYTES:
        raise FileTooLargeError("ملف الصوت أكبر من حد الإرسال المسموح")
    return result


def _compress_sync(source: Path, folder: Path) -> Path:
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
            "-movflags", "+faststart", str(output),
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
            logger.warning("FFmpeg compression failed: %s", result.stderr[-1200:])
    raise FileTooLargeError("تعذر ضغط الفيديو إلى الحجم المطلوب")


@asynccontextmanager
async def download_video(url: str) -> AsyncIterator[Path]:
    """تنزيل الفيديو وضغطه عند الحاجة ثم تنظيف المجلد المؤقت."""
    temp_dir = tempfile.TemporaryDirectory(prefix="video_bot_")
    folder = Path(temp_dir.name)
    try:
        source = await _run_ytdlp(url, folder, "video")
        if source.stat().st_size <= MAX_UPLOAD_BYTES:
            yield source
        else:
            yield await asyncio.to_thread(_compress_sync, source, folder)
    finally:
        temp_dir.cleanup()


@asynccontextmanager
async def download_audio(url: str) -> AsyncIterator[Path]:
    """استخراج الصوت وتحويله إلى MP3 بمعدل 192 kbps."""
    temp_dir = tempfile.TemporaryDirectory(prefix="audio_bot_")
    folder = Path(temp_dir.name)
    try:
        audio_file = await _run_ytdlp(url, folder, "audio")
        if audio_file.stat().st_size > MAX_UPLOAD_BYTES:
            raise FileTooLargeError("ملف MP3 أكبر من حد تيليجرام. جرّب مقطعاً أقصر.")
        yield audio_file
    finally:
        temp_dir.cleanup()
