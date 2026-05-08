from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from pathlib import Path
import httpx
import json
import re
import time as time_mod
import asyncio
import os
import logging

from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("evcc-tariff")

TIMEZONE = ZoneInfo("Europe/Prague")

CAPTCHA_URL = "https://dip.cezdistribuce.cz/irj/portal/anonymous/captcha"
CEZ_API_URL = "https://dip.cezdistribuce.cz/irj/portal/anonymous/casy-spinani?path=switch-times/signals"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

GEMINI_MODEL = "gemini-3.1-flash-lite"
CAPTCHA_PROMPT = (
    "This is a 4-letter CAPTCHA image. "
    "Reply with only the 4 letters, uppercase, no spaces, no punctuation."
)

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/data"))
REFRESH_INTERVAL = timedelta(days=3)

# ean -> {"schedule": {date_str: [(sh,sm,eh,em), ...]}, "updated": datetime | None}
ean_caches: dict[str, dict] = {}
fetch_locks: dict[str, asyncio.Lock] = {}
fallback_windows: list[tuple[int, int, int, int]] | None = None
_genai_client: genai.Client | None = None


def get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client()
    return _genai_client


def detect_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return "image/png"


def cache_path(ean: str) -> Path:
    return CACHE_DIR / f"{ean}.json"


def save_ean_cache(ean: str):
    cache = ean_caches.get(ean)
    if not cache:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "updated": cache["updated"].isoformat() if cache["updated"] else None,
        "schedule": cache["schedule"],
    }
    cache_path(ean).write_text(json.dumps(data))
    logger.info("Cache saved for EAN %s", ean)


def load_ean_cache(ean: str) -> bool:
    path = cache_path(ean)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        ean_caches[ean] = {
            "schedule": {k: [tuple(w) for w in v] for k, v in data["schedule"].items()},
            "updated": datetime.fromisoformat(data["updated"]) if data["updated"] else None,
        }
        logger.info("Cache loaded for EAN %s: %s", ean, sorted(ean_caches[ean]["schedule"].keys()))
        return True
    except Exception:
        logger.exception("Failed to load cache for EAN %s", ean)
        return False


def load_all_caches():
    if not CACHE_DIR.exists():
        return
    for path in CACHE_DIR.glob("*.json"):
        ean = path.stem
        load_ean_cache(ean)


def ean_cache_is_stale(ean: str) -> bool:
    cache = ean_caches.get(ean)
    if not cache or not cache["updated"]:
        return True
    return datetime.now(TIMEZONE) - cache["updated"] > REFRESH_INTERVAL


def parse_time_windows(s: str) -> list[tuple[int, int, int, int]]:
    windows = []
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        start_str, end_str = part.split("-")
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        windows.append((sh, sm, eh, em))
    return windows


def merge_windows(windows: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    intervals = sorted((sh * 60 + sm, eh * 60 + em) for sh, sm, eh, em in windows)
    merged: list[tuple[int, int]] = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return [(s // 60, s % 60, e // 60, e % 60) for s, e in merged]


async def fetch_captcha(client: httpx.AsyncClient) -> bytes:
    ts = int(time_mod.time() * 1000)
    resp = await client.get(
        f"{CAPTCHA_URL}?t={ts}",
        headers={"User-Agent": USER_AGENT, "Accept": "image/webp,image/png,*/*"},
    )
    resp.raise_for_status()
    return resp.content


async def solve_captcha(image: bytes) -> str:
    mime = detect_image_mime(image)
    client = get_genai_client()
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            CAPTCHA_PROMPT,
            types.Part.from_bytes(data=image, mime_type=mime),
        ],
        config=types.GenerateContentConfig(
            max_output_tokens=16,
            temperature=0.0,
        ),
    )
    text = (response.text or "").strip()
    code = re.sub(r"[^A-Za-z]", "", text).upper()
    if len(code) != 4:
        raise ValueError(f"Invalid CAPTCHA result: {code!r} (raw: {text!r})")
    return code


async def fetch_hdo(ean: str) -> list[dict]:
    logger.info("[EAN %s] fetching HDO schedule from CEZ API", ean)
    for attempt in range(3):
        async with httpx.AsyncClient(timeout=30) as client:
            logger.debug("[EAN %s] fetching CAPTCHA image (attempt %d/3)", ean, attempt + 1)
            image = await fetch_captcha(client)
            try:
                code = await solve_captcha(image)
            except ValueError as e:
                logger.warning("CAPTCHA OCR failed (attempt %d/3): %s", attempt + 1, e)
                continue

            resp = await client.post(
                CEZ_API_URL,
                json={"ean": ean, "captcha": code},
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                },
            )

            if resp.status_code == 400:
                logger.warning("CAPTCHA rejected by CEZ (attempt %d/3)", attempt + 1)
                continue

            resp.raise_for_status()
            data = resp.json()

            signals = data.get("data", {})
            if isinstance(signals, dict) and "data" in signals:
                signals = signals["data"]
            if isinstance(signals, dict):
                signals = signals.get("signals", [])

            logger.info("[EAN %s] CEZ API returned %d signal(s)", ean, len(signals))
            return signals

    raise RuntimeError("Failed to fetch HDO after 3 attempts")


async def refresh_ean(ean: str):
    try:
        signals = await fetch_hdo(ean)
        schedule: dict[str, list[tuple[int, int, int, int]]] = {}
        for sig in signals:
            datum = sig.get("datum", "")
            casy = sig.get("casy", "")
            if datum and casy:
                parts = datum.split(".")
                if len(parts) == 3:
                    key = f"{parts[2]}-{parts[1]}-{parts[0]}"
                    schedule.setdefault(key, []).extend(parse_time_windows(casy))
        for key, windows in schedule.items():
            schedule[key] = merge_windows(windows)
        ean_caches[ean] = {"schedule": schedule, "updated": datetime.now(TIMEZONE)}
        save_ean_cache(ean)
        logger.info("EAN %s refreshed: %s", ean, sorted(schedule.keys()))
    except Exception:
        logger.exception("Failed to refresh EAN %s", ean)


async def ensure_ean(ean: str):
    if ean not in ean_caches:
        if load_ean_cache(ean):
            logger.info("[EAN %s] loaded from disk cache (updated %s)", ean, ean_caches[ean]["updated"])
        else:
            logger.info("[EAN %s] no disk cache found, will fetch from CEZ API", ean)
    if ean_cache_is_stale(ean):
        age = ""
        if ean in ean_caches and ean_caches[ean]["updated"]:
            age = f" (age: {datetime.now(TIMEZONE) - ean_caches[ean]['updated']})"
        logger.info("[EAN %s] cache is stale%s, refreshing from CEZ API", ean, age)
        lock = fetch_locks.setdefault(ean, asyncio.Lock())
        async with lock:
            if ean_cache_is_stale(ean):
                await refresh_ean(ean)
    else:
        cache = ean_caches[ean]
        age = datetime.now(TIMEZONE) - cache["updated"]
        logger.debug("[EAN %s] using cached data (age: %s, days: %s)", ean, age, sorted(cache["schedule"].keys()))


async def background_refresh():
    while True:
        await asyncio.sleep(3600)
        stale = [ean for ean in ean_caches if ean_cache_is_stale(ean)]
        if stale:
            logger.info("Background refresh: %d stale EAN(s): %s", len(stale), stale)
            for ean in stale:
                await refresh_ean(ean)
        else:
            logger.debug("Background refresh: all %d EAN(s) up to date", len(ean_caches))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global fallback_windows
    fb = os.environ.get("LOW_TARIFF_WINDOWS")
    if fb:
        fallback_windows = parse_time_windows(fb)
        logger.info("Fallback windows: %s", fallback_windows)
    load_all_caches()
    task = asyncio.create_task(background_refresh())
    yield
    task.cancel()


app = FastAPI(title="hdo-api", lifespan=lifespan)


def windows_for_date(ean: str, day: date) -> list[tuple[int, int, int, int]] | None:
    cache = ean_caches.get(ean)
    if cache:
        key = day.strftime("%Y-%m-%d")
        windows = cache["schedule"].get(key)
        if windows:
            return windows
    return fallback_windows


def is_low(dt: datetime, windows: list[tuple[int, int, int, int]]) -> bool:
    t = dt.hour * 60 + dt.minute
    for sh, sm, eh, em in windows:
        s = sh * 60 + sm
        e = eh * 60 + em
        if e <= s:
            if t >= s or t < e:
                return True
        else:
            if s <= t < e:
                return True
    return False


def make_slots(day: date, vt: float, nt: float, windows: list[tuple[int, int, int, int]]) -> list[dict]:
    midnight = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=TIMEZONE)

    def at(minutes: int) -> str:
        return (midnight + timedelta(minutes=minutes)).isoformat()

    def append(slots: list[dict], s: int, e: int, value: float):
        if e > s:
            slots.append({"start": at(s), "end": at(e), "value": value})

    slots: list[dict] = []
    cursor = 0
    for sh, sm, eh, em in sorted(windows):
        s = sh * 60 + sm
        e = eh * 60 + em
        append(slots, cursor, s, vt)
        append(slots, s, e, nt)
        cursor = e
    append(slots, cursor, 1440, vt)
    return slots


@app.get("/api/rates")
async def rates(
    ean: str = Query(description="18-digit EAN number"),
    vt: float = Query(description="High tariff price (VT)"),
    nt: float = Query(description="Low tariff price (NT)"),
):
    logger.info("[EAN %s] /api/rates request (vt=%.2f, nt=%.2f)", ean, vt, nt)
    await ensure_ean(ean)
    today = datetime.now(TIMEZONE).date()
    cache = ean_caches.get(ean)
    if cache and cache["schedule"]:
        days = sorted(
            date.fromisoformat(k)
            for k in cache["schedule"].keys()
            if date.fromisoformat(k) >= today
        )
    else:
        days = [today, today + timedelta(days=1)]
    result = []
    for day in days:
        w = windows_for_date(ean, day)
        if w:
            result.extend(make_slots(day, vt, nt, w))
        else:
            logger.warning("[EAN %s] no schedule data for %s", ean, day)
    logger.info("[EAN %s] /api/rates returning %d slots across %d day(s)", ean, len(result), len(days))
    return result


@app.get("/api/price", response_class=PlainTextResponse)
async def price(
    ean: str = Query(description="18-digit EAN number"),
    vt: float = Query(description="High tariff price (VT)"),
    nt: float = Query(description="Low tariff price (NT)"),
):
    logger.info("[EAN %s] /api/price request (vt=%.2f, nt=%.2f)", ean, vt, nt)
    await ensure_ean(ean)
    now = datetime.now(TIMEZONE)
    w = windows_for_date(ean, now.date())
    if not w:
        logger.warning("[EAN %s] no schedule data, returning high tariff %.2f", ean, vt)
        return str(vt)
    current = nt if is_low(now, w) else vt
    tariff_type = "NT (low)" if current == nt else "VT (high)"
    logger.info("[EAN %s] /api/price returning %.2f (%s)", ean, current, tariff_type)
    return str(current)


@app.get("/api/status")
def status():
    result = {}
    for ean, cache in ean_caches.items():
        result[ean] = {
            "cached_days": sorted(cache["schedule"].keys()),
            "cache_updated": cache["updated"].isoformat() if cache["updated"] else None,
        }
    return result


@app.delete("/api/cache/{ean}")
def delete_cache(ean: str):
    if not ean.isdigit():
        raise HTTPException(status_code=400, detail="EAN must be numeric")
    removed_memory = ean_caches.pop(ean, None) is not None
    fetch_locks.pop(ean, None)
    path = cache_path(ean)
    removed_disk = path.exists()
    if removed_disk:
        path.unlink()
    logger.info("[EAN %s] cache cleared (memory=%s, disk=%s)", ean, removed_memory, removed_disk)
    return {"ean": ean, "removed_memory": removed_memory, "removed_disk": removed_disk}
