# Video Downloader Super Bot

بوت تيليجرام شخصي لتنزيل الفيديوهات العامة التي يملك المستخدم حق تنزيلها أو إذناً بذلك.

## المنصات
YouTube وTikTok وInstagram وFacebook وX/Twitter، حسب دعم yt-dlp وإتاحة المحتوى للعامة. لا يتجاوز البوت تسجيل الدخول أو الخصوصية أو أنظمة الحماية.

## المتطلبات
- Python 3.11+
- FFmpeg
- Bot Token من BotFather
- Telegram user ID واحد على الأقل

## تشغيل محلي
1. ثبّت Python وFFmpeg.
2. أنشئ بيئة افتراضية: `python -m venv .venv`
3. فعّلها: Windows `.venv\\Scripts\\activate` أو Linux/macOS `source .venv/bin/activate`.
4. ثبّت المكتبات: `python -m pip install -r requirements.txt`
5. انسخ `.env.example` إلى `.env` واضبط `BOT_TOKEN` و`ALLOWED_USER_IDS`.
6. حمّل متغيرات البيئة ثم شغّل `python main.py`.

Linux/macOS:
`set -a; source .env; set +a; python main.py`

PowerShell:
`$env:BOT_TOKEN="توكنك"; $env:ALLOWED_USER_IDS="رقمك"; python main.py`

## النشر على Railway
1. اربط هذا المستودع بخدمة Railway جديدة واختر البناء عبر Dockerfile.
2. أضف المتغيرات `BOT_TOKEN` و`ALLOWED_USER_IDS` و`LOG_LEVEL=INFO`.
3. شغّل Deploy وراجع Logs. لا يحتاج البوت إلى Domain لأن التشغيل Polling.
4. شغّل نسخة واحدة فقط بالتوكن نفسه لتجنب تعارض polling.

## تحديث yt-dlp
محلياً: `python -m pip install -U "yt-dlp[default]"`.
على Railway: أعد البناء بعد تحديث الاعتماد أو دفع commit جديد.

## الحدود والسلامة
- حد الإرسال 48 MiB بهامش أمان أقل من 50 MB، مع ضغط تدريجي باستخدام FFmpeg.
- خمسة طلبات لكل مستخدم في الدقيقة وتنزيلان متزامنان لكل نسخة.
- الملفات المؤقتة تُحذف بعد الإرسال أو الفشل.
- yt-dlp يعمل كعملية فرعية ويمكن إيقافها عند انتهاء المهلة.
- لا تضع التوكن في GitHub أو داخل الكود. إذا انكشف، أبطله من BotFather فوراً.
