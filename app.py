"""
GifSaz · سرویس رندر ffmpeg
-------------------------------------------------------------------------------
این سرویس کوچک، فایل را از تلگرام می‌گیرد، با ffmpeg پردازش می‌کند و خروجی را
مستقیم داخل چت تلگرام می‌فرستد. ورکر کلادفلر فقط تنظیمات را به اینجا می‌فرستد.

اجرا:
    uvicorn app:app --host 0.0.0.0 --port 8080

متغیرهای محیطی:
    BOT_TOKEN        توکن ربات (اجباری)
    SERVICE_SECRET   همان RENDER_SECRET ورکر (اجباری)
    WATERMARK        متن واترمارک (پیش‌فرض @gif_sazbot) - خالی = بدون واترمارک
    MAX_INPUT_MB     سقف حجم ورودی (پیش‌فرض 20 - محدودیت خود تلگرام)
    MAX_CONCURRENCY  تعداد رندر هم‌زمان (پیش‌فرض 1)
"""

import asyncio
import hmac
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import arabic_reshaper
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw, ImageFont

try:
    from bidi.algorithm import get_display
except Exception:  # pragma: no cover
    get_display = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gifsaz.render")

BASE_DIR = Path(__file__).resolve().parent

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SERVICE_SECRET = os.getenv("SERVICE_SECRET", "").strip()
WATERMARK_DEFAULT = os.getenv("WATERMARK", "@gif_sazbot")
MAX_INPUT_MB = int(os.getenv("MAX_INPUT_MB", "20"))
MAX_CONCURRENCY = max(1, int(os.getenv("MAX_CONCURRENCY", "1")))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

app = FastAPI(title="GifSaz Render Service", version="1.0.0")
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENCY)
BACKGROUND_TASKS: set[asyncio.Task] = set()

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".gif", ".mpeg", ".mpg", ".ts"}

EVEN_SCALE = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

CAPTIONS = {
    "gif": "🎬 گیف اختصاصی شما آماده است!\nربات گیف ساز @gif_sazbot",
    "text": "🎬 گیف اختصاصی شما آماده است!\nربات گیف ساز @gif_sazbot",
    "speed": "⏩ سرعت ویدیو افزایش یافت.\nربات گیف ساز @gif_sazbot",
    "bw": "🎨 فیلتر سیاه و سفید اعمال شد.\nربات گیف ساز @gif_sazbot",
}

COLORS = {
    "white": (255, 255, 255, 255),
    "yellow": (255, 255, 0, 255),
    "red": (255, 60, 60, 255),
    "green": (60, 220, 90, 255),
    "blue": (90, 160, 255, 255),
}

DONE_TEXT = "✅ انجام شد! برای اعمال تغییرات روی فایل جدید، آن را بفرستید."


# ============================== تنظیمات فونت =================================

def find_font() -> Path | None:
    candidates = [
        BASE_DIR / "fonts" / "Vazirmatn-Bold.ttf",
        BASE_DIR / "font.ttf",
        Path("/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Bold.ttf"),
        Path("/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


@lru_cache(maxsize=1)
def font_path() -> Path | None:
    path = find_font()
    if path is None:
        log.warning("هیچ فونتی پیدا نشد! متن‌ها بدون فونت اختصاصی رندر می‌شوند.")
    else:
        log.info("فونت: %s", path)
    return path


@lru_cache(maxsize=1)
def has_text_shaping() -> bool:
    """آیا ffmpeg با harfbuzz ساخته شده؟ (فقط برای اطلاع در /health)"""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", "filter=drawtext"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        return "text_shaping" in (out.stdout + out.stderr)
    except Exception:
        return False


def shape(text: str) -> str:
    """
    آماده‌سازی متن فارسی برای رسم با Pillow:
    اتصال حروف (reshaper) + مرتب‌سازی بصری راست‌به‌چپ (bidi).
    """
    reshaped = arabic_reshaper.reshape(str(text))
    if get_display is not None:
        try:
            return get_display(reshaped)
        except Exception:
            return reshaped
    return reshaped


# ============================== تلگرام ======================================

async def tg(method: str, payload: dict | None = None, files=None, timeout: float = 60.0) -> dict:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20.0)) as client:
            if files is not None:
                res = await client.post(f"{API}/{method}", data=payload, files=files)
            else:
                res = await client.post(f"{API}/{method}", json=payload or {})
            data = res.json()
            if not data.get("ok"):
                log.warning("telegram %s: %s", method, data.get("description"))
            return data
    except Exception as exc:
        log.warning("telegram %s failed: %s", method, exc)
        return {"ok": False, "description": str(exc)}


async def edit_status(chat_id, message_id, text: str) -> None:
    if not chat_id or not message_id:
        return
    await tg("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
    }, timeout=20)


def upload_sync(path: Path, data: dict) -> dict:
    """آپلود فایل به تلگرام در یک ترد جدا (تا event loop بلاک نشود)."""
    with open(path, "rb") as fh:
        files = {"video": (path.name, fh, "video/mp4")}
        res = httpx.post(
            f"{API}/sendVideo",
            data=data,
            files=files,
            timeout=httpx.Timeout(900.0, connect=30.0),
        )
    try:
        return res.json()
    except Exception:
        return {"ok": False, "description": res.text[:300]}


async def download_file(file_id: str, dest: Path) -> None:
    info = await tg("getFile", {"file_id": file_id}, timeout=30)
    if not info.get("ok"):
        raise RuntimeError(f"دریافت فایل از تلگرام ناموفق بود: {info.get('description')}")

    file_path = info["result"]["file_path"]
    max_bytes = MAX_INPUT_MB * 1024 * 1024
    written = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
        async with client.stream("GET", f"{FILE_API}/{file_path}") as res:
            if res.status_code != 200:
                raise RuntimeError(f"دانلود فایل ناموفق بود (HTTP {res.status_code}).")
            with open(dest, "wb") as fh:
                async for chunk in res.aiter_bytes(1024 * 256):
                    written += len(chunk)
                    if written > max_bytes:
                        raise RuntimeError(f"حجم فایل بیشتر از {MAX_INPUT_MB} مگابایت است.")
                    fh.write(chunk)

    if written == 0:
        raise RuntimeError("فایل دریافتی خالی بود.")


# ============================== ffmpeg ======================================

def _probe_size(path: Path) -> tuple[int, int]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        stream = (json.loads(result.stdout or "{}").get("streams") or [{}])[0]
        w, h = int(stream.get("width") or 0), int(stream.get("height") or 0)
        if not w or not h:
            return 640, 360

        rotation = 0
        tags = stream.get("tags") or {}
        if "rotate" in tags:
            rotation = int(float(tags["rotate"]))
        for side in stream.get("side_data_list") or []:
            if "rotation" in side:
                rotation = int(float(side["rotation"]))
        if abs(rotation) % 180 == 90:
            w, h = h, w

        return max(2, w // 2 * 2), max(2, h // 2 * 2)
    except Exception as exc:
        log.warning("probe failed: %s", exc)
        return 640, 360


def run_ffmpeg(args: list[str], cwd: Path) -> None:
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(cwd), timeout=600,
        )
    except FileNotFoundError:
        raise RuntimeError("ffmpeg روی سیستم نصب نیست.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("پردازش طولانی شد و متوقف شد.")

    if result.returncode != 0:
        message = (result.stderr or "").strip()[-800:]
        raise RuntimeError(message or "ffmpeg با خطا مواجه شد.")


@lru_cache(maxsize=128)
def _load_font(path: str, size: int):
    layout = getattr(ImageFont, "Layout", None)
    basic = layout.BASIC if layout is not None else 0
    return ImageFont.truetype(path, size, layout_engine=basic)


def get_metrics(size: int):
    path = font_path()
    if path is not None:
        try:
            font = _load_font(str(path), size)
            ascent, descent = font.getmetrics()
            return (lambda text: font.getlength(text)), ascent, descent
        except Exception:
            pass
    return (lambda text: len(text) * size * 0.6), int(size * 1.0), int(size * 0.25)


def watermark_metrics(W: int, H: int):
    short = min(W, H)
    size = max(10, int(short * 0.035))
    margin = max(6, int(short * 0.02))
    border = max(2, size // 4)
    bottom = margin + int(size * 1.25) + 2 * border + margin
    return size, margin, border, bottom


def _split_long_word(word, measure, max_w):
    if measure(shape(word)) <= max_w:
        return [word]
    pieces, cur = [], ""
    for ch in word:
        if cur and measure(shape(cur + ch)) > max_w:
            pieces.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        pieces.append(cur)
    return pieces


def _wrap_paragraphs(paragraphs, measure, max_w):
    lines = []
    for para in paragraphs:
        cur = ""
        for word in para.split():
            for piece in _split_long_word(word, measure, max_w):
                cand = f"{cur} {piece}" if cur else piece
                if not cur or measure(shape(cand)) <= max_w:
                    cur = cand
                else:
                    lines.append(cur)
                    cur = piece
        if cur:
            lines.append(cur)
    return lines


@dataclass
class TextLayout:
    lines: list
    size: int
    pad: int
    ascent: int
    line_h: int
    x: int
    y: int
    w: int
    h: int


def layout_text(raw_text: str, W: int, H: int, pos: str) -> TextLayout | None:
    paragraphs = [p.strip() for p in str(raw_text).splitlines() if p.strip()]
    if not paragraphs:
        return None

    short = min(W, H)
    margin = max(6, int(short * 0.03))
    wm_bottom = watermark_metrics(W, H)[3]
    side = pos in ("left", "right")

    max_block_w = int(W * (0.55 if side else 0.94))
    max_block_h = int(H * (0.60 if side else 0.40))
    if pos == "top":
        max_block_h = min(max_block_h, H - wm_bottom - margin)

    max_size = max(10, int(short * 0.075))
    min_size = max(8, int(short * 0.03))

    size = max_size
    measure = None
    while True:
        measure, ascent, descent = get_metrics(size)
        pad = max(4, int(size * 0.35))
        lines = _wrap_paragraphs(paragraphs, measure, max_block_w - 2 * pad)
        text_h = ascent + descent
        line_h = int(text_h * 1.12)
        block_h = (len(lines) - 1) * line_h + text_h + 2 * pad
        if block_h <= max_block_h or size <= min_size:
            break
        size = max(min_size, size - max(1, size // 12))

    widest = max(measure(shape(line)) for line in lines)
    block_w = min(W, int(widest * 1.03) + 2 * pad)

    if pos == "left":
        bx = margin
    elif pos == "right":
        bx = W - margin - block_w
    else:
        bx = (W - block_w) // 2

    if pos == "top":
        by = wm_bottom
    elif pos == "bottom":
        by = H - margin - block_h
    else:
        by = (H - block_h) // 2

    bx = max(0, min(bx, W - block_w))
    by = max(0, min(by, H - block_h))

    return TextLayout(lines=lines, size=size, pad=pad, ascent=ascent, line_h=line_h,
                      x=bx, y=by, w=block_w, h=block_h)


def _font(size: int) -> ImageFont.FreeTypeFont:
    path = font_path()
    if path is None:
        return ImageFont.load_default(size)
    return _load_font(str(path), size)


def render_overlay(workdir: Path, W: int, H: int, layout: TextLayout | None,
                   color: str, box: str, watermark: str) -> Path | None:
    """
    یک PNG شفاف هم‌اندازه‌ی ویدیو می‌سازد: کاور + متن فارسی + واترمارک.
    بعداً با فیلتر overlay روی ویدیو می‌نشیند. (بدون نیاز به drawtext/harfbuzz)
    """
    if layout is None and not watermark:
        return None

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if layout is not None:
        if box == "yes":
            draw.rectangle(
                [layout.x, layout.y, layout.x + layout.w - 1, layout.y + layout.h - 1],
                fill=(0, 0, 0, 255),
            )
        font = _font(layout.size)
        fill = COLORS.get(str(color), COLORS["white"])
        cx = layout.x + layout.w // 2
        for i, line in enumerate(layout.lines):
            y = layout.y + layout.pad + i * layout.line_h
            draw.text((cx, y), shape(line), font=font, fill=fill, anchor="mt")

    if watermark:
        wm_size, wm_margin, wm_border, _ = watermark_metrics(W, H)
        wm_font = _font(wm_size)
        text = watermark
        width = wm_font.getlength(text)
        ascent, descent = wm_font.getmetrics()
        pad = wm_border
        draw.rectangle(
            [wm_margin - pad, wm_margin - pad,
             wm_margin + width + pad, wm_margin + ascent + descent + pad],
            fill=(0, 0, 0, 128),
        )
        draw.text((wm_margin, wm_margin), text, font=wm_font, fill=(255, 255, 0, 255), anchor="lt")

    path = workdir / "overlay.png"
    img.save(path)
    return path


def overlay_filter(has_overlay: bool) -> tuple[str, list[str]]:
    """filter_complex + map برای ترکیب ویدیو با PNG شفاف"""
    if not has_overlay:
        return f"[0:v]fps=30,{EVEN_SCALE},format=yuv420p[v]", ["-map", "[v]"]
    return (
        f"[0:v]fps=30,{EVEN_SCALE}[base];"
        f"[base][1:v]overlay=0:0,format=yuv420p[v]",
        ["-map", "[v]"],
    )


# ============================== پردازش =====================================

def ext_for(job: dict) -> str:
    name = str(job.get("file_name") or "")
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXTS or suffix in VIDEO_EXTS:
        return suffix
    return {
        "photo": ".jpg",
        "sticker": ".webp",
        "animation": ".mp4",
        "video": ".mp4",
    }.get(job.get("file_type", ""), ".mp4")


async def process(job: dict) -> None:
    async with SEMAPHORE:
        workdir = Path(tempfile.mkdtemp(prefix="gifsaz-"))
        chat_id = job["chat_id"]
        message_id = job["message_id"]

        await edit_status(chat_id, message_id, "📥 در حال دریافت فایل...")

        try:
            ext = ext_for(job)
            source = workdir / f"input{ext}"
            await download_file(job["file_id"], source)

            is_image = ext in IMAGE_EXTS
            W, H = _probe_size(source)

            await edit_status(chat_id, message_id, "⚙️ در حال پردازش تصویر/ویدیو...")
            output = render_video(job, source, workdir, W, H, is_image)

            if not output.exists() or output.stat().st_size == 0:
                raise RuntimeError("خروجی ساخته نشد.")

            await edit_status(chat_id, message_id, "📤 در حال ارسال نتیجه...")

            action = job.get("action") or "gif"
            caption = CAPTIONS.get(action, CAPTIONS["gif"])
            data = {
                "chat_id": str(chat_id),
                "caption": caption,
                "supports_streaming": "true",
            }
            if is_image:
                data["duration"] = "4"

            result = await asyncio.to_thread(upload_sync, output, data)
            if not result.get("ok"):
                raise RuntimeError(f"ارسال به تلگرام ناموفق بود: {result.get('description')}")

            await edit_status(chat_id, message_id, DONE_TEXT)
            log.info("job %s done (%s, %.1f KB)", job.get("job_id"), action,
                     output.stat().st_size / 1024)

        except Exception as exc:
            log.exception("job %s failed", job.get("job_id"))
            await edit_status(chat_id, message_id, f"❌ خطا در رندر:\n{exc}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def render_video(job: dict, source: Path, workdir: Path, W: int, H: int, is_image: bool) -> Path:
    action = job.get("action") or "gif"
    watermark = str(job.get("watermark") if job.get("watermark") is not None else WATERMARK_DEFAULT)

    layout = None
    if action == "text":
        layout = layout_text(job.get("text", ""), W, H, job.get("position") or "bottom")
        if layout is None:
            raise RuntimeError("متنی برای نوشتن وجود ندارد.")

    output = workdir / "output.mp4"
    input_args = []
    if is_image:
        input_args += ["-loop", "1", "-t", "4", "-framerate", "30"]
    input_args += ["-i", source.name]

    if action == "speed":
        speed_vf = f"setpts=0.5*PTS,{EVEN_SCALE}"
        args = input_args + [
            "-map", "0:v:0", "-map", "0:a?",
            "-filter:v", speed_vf, "-filter:a", "atempo=2.0",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", output.name,
        ]
        try:
            run_ffmpeg(args, workdir)
        except RuntimeError:
            args = input_args + [
                "-an", "-filter:v", speed_vf,
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", output.name,
            ]
            run_ffmpeg(args, workdir)
    elif action == "bw":
        args = input_args + [
            "-an", "-filter:v", f"hue=s=0,{EVEN_SCALE},format=yuv420p",
            "-vcodec", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", output.name,
        ]
        run_ffmpeg(args, workdir)
    else:
        overlay = render_overlay(
            workdir, W, H, layout,
            job.get("color") or "white",
            job.get("box") or "no",
            watermark,
        )
        filter_complex, mapping = overlay_filter(overlay is not None)
        args = input_args + (["-i", overlay.name] if overlay else []) + [
            "-an",
            "-filter_complex", filter_complex,
            *mapping,
            "-vcodec", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", output.name,
        ]
        run_ffmpeg(args, workdir)

    return output


# ============================== API ========================================

def authorized(authorization: str, x_auth: str) -> bool:
    if not SERVICE_SECRET:
        return False
    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    token = token or x_auth.strip()
    return bool(token) and hmac.compare_digest(token, SERVICE_SECRET)


REQUIRED_FIELDS = ("chat_id", "message_id", "file_id")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "font": str(font_path()) if font_path() else None,
        "text_shaping": has_text_shaping(),
        "concurrency": MAX_CONCURRENCY,
    }


@app.post("/render")
async def render(request: Request,
                 authorization: str = Header(default=""),
                 x_auth: str = Header(default="")):
    if not authorized(authorization, x_auth):
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        job = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    missing = [f for f in REQUIRED_FIELDS if job.get(f) in (None, "")]
    if missing:
        raise HTTPException(status_code=400, detail=f"missing fields: {', '.join(missing)}")

    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN is not configured")

    # جواب فوری؛ کار در پس‌زمینه ادامه پیدا می‌کند
    task = asyncio.create_task(process(job))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    log.info("accepted job %s action=%s chat=%s", job.get("job_id"), job.get("action"), job.get("chat_id"))
    return JSONResponse({"ok": True, "job_id": job.get("job_id")}, status_code=202)


@app.on_event("startup")
async def on_startup():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN تنظیم نشده است!")
    if not SERVICE_SECRET:
        log.error("SERVICE_SECRET تنظیم نشده است!")
    log.info("ffmpeg: %s | text_shaping=%s | font=%s",
             shutil.which("ffmpeg"), has_text_shaping(), font_path())
