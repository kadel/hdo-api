from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from pathlib import Path
import httpx
import json
import base64
import re
import time as time_mod
import asyncio
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("evcc-tariff")

TIMEZONE = ZoneInfo("Europe/Prague")

CAPTCHA_URL = "https://dip.cezdistribuce.cz/irj/portal/anonymous/captcha"
CEZ_API_URL = "https://dip.cezdistribuce.cz/irj/portal/anonymous/casy-spinani?path=switch-times/signals"
OCR_URL = "https://api.ocr.space/parse/image"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/data"))
REFRESH_INTERVAL = timedelta(days=3)

# ean -> {"schedule": {date_str: [(sh,sm,eh,em), ...]}, "updated": datetime | None}
ean_caches: dict[str, dict] = {}
fetch_locks: dict[str, asyncio.Lock] = {}
fallback_windows: list[tuple[int, int, int, int]] | None = None


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


async def fetch_captcha(client: httpx.AsyncClient) -> bytes:
    ts = int(time_mod.time() * 1000)
    resp = await client.get(
        f"{CAPTCHA_URL}?t={ts}",
        headers={"User-Agent": USER_AGENT, "Accept": "image/webp,image/png,*/*"},
    )
    resp.raise_for_status()
    return resp.content


async def solve_captcha(image: bytes, api_key: str) -> str:
    b64 = base64.b64encode(image).decode()
    async with httpx.AsyncClient(timeout=30) as c:
        resp = await c.post(
            OCR_URL,
            data={
                "base64Image": f"data:image/png;base64,{b64}",
                "language": "eng",
                "isOverlayRequired": "false",
                "OCREngine": "3",
                "scale": "true",
                "isTable": "false",
            },
            headers={"apikey": api_key},
        )
    resp.raise_for_status()
    result = resp.json()
    if result.get("IsErroredOnProcessing"):
        raise ValueError(f"OCR error: {result.get('ErrorMessage')}")
    text = result.get("ParsedResults", [{}])[0].get("ParsedText", "")
    code = re.sub(r"[^A-Za-z]", "", text).upper()
    if len(code) != 4:
        raise ValueError(f"Invalid CAPTCHA result: {code!r} (raw: {text!r})")
    return code


async def fetch_hdo(ean: str, ocr_key: str) -> list[dict]:
    logger.info("[EAN %s] fetching HDO schedule from CEZ API", ean)
    for attempt in range(3):
        async with httpx.AsyncClient(timeout=30) as client:
            logger.debug("[EAN %s] fetching CAPTCHA image (attempt %d/3)", ean, attempt + 1)
            image = await fetch_captcha(client)
            try:
                code = await solve_captcha(image, ocr_key)
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
    ocr_key = os.environ.get("OCR_API_KEY", "helloworld")
    try:
        signals = await fetch_hdo(ean, ocr_key)
        schedule: dict[str, list[tuple[int, int, int, int]]] = {}
        for sig in signals:
            datum = sig.get("datum", "")
            casy = sig.get("casy", "")
            if datum and casy:
                parts = datum.split(".")
                if len(parts) == 3:
                    key = f"{parts[2]}-{parts[1]}-{parts[0]}"
                    schedule[key] = parse_time_windows(casy)
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
    slots = []
    for hour in range(24):
        start = datetime(day.year, day.month, day.day, hour, 0, 0, tzinfo=TIMEZONE)
        end = start + timedelta(hours=1)
        slots.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "value": nt if is_low(start, windows) else vt,
        })
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
    tomorrow = today + timedelta(days=1)
    result = []
    for day in (today, tomorrow):
        w = windows_for_date(ean, day)
        if w:
            result.extend(make_slots(day, vt, nt, w))
        else:
            logger.warning("[EAN %s] no schedule data for %s", ean, day)
    logger.info("[EAN %s] /api/rates returning %d slots", ean, len(result))
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
