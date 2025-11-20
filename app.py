import asyncio
import hashlib
import hmac
import os
import random
import sys
import time
from typing import Final, Optional

import aiohttp
import orjson

BASE_URL: Final[str] = os.getenv("BASE_URL", "https://pixells-basic.onrender.com")
API_KEY: Final[str] = os.getenv("API_KEY", "bG7QmP4rZ2HkV9tFx8NcW3sLd1RyJpBqT6vUwY0aErSfDhGi")
HMAC_SECRET: Final[str] = os.getenv("HMAC_SECRET", "38f48941fafd5cdb8436893f82d9c4ff")

CONCURRENCY: int = int(os.getenv("CONCURRENCY", "10"))
TIMEOUT: float = float(os.getenv("TIMEOUT", "0.5"))
INITIAL_BACKOFF: float = float(os.getenv("INITIAL_BACKOFF", "0.02"))
MAX_BACKOFF: float = float(os.getenv("MAX_BACKOFF", "0.2"))
RETRIES: int = int(os.getenv("RETRIES", "2"))

API_KEY_HEADER = "X-API-KEY"
TIMESTAMP_HEADER = "X-TIMESTAMP"
SIGNATURE_HEADER = "X-SIGNATURE"

URL_NEXT = f"{BASE_URL.rstrip('/')}/job/next"

sem = asyncio.Semaphore(CONCURRENCY)
stopping = False


def _signature_headers() -> dict:
    timestamp = str(int(time.time()))
    signature = hmac.new(HMAC_SECRET.encode(), timestamp.encode(), hashlib.sha256).hexdigest()
    return {
        API_KEY_HEADER: API_KEY,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: signature,
        "Accept": "application/json",
    }


async def fetch_once(session: aiohttp.ClientSession, timeout: aiohttp.ClientTimeout) -> Optional[dict]:
    headers = _signature_headers()
    try:
        async with session.get(URL_NEXT, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.read()
                return orjson.loads(data)
            if resp.status == 204:
                return None
            text = await resp.text()
            print(f"Unexpected status {resp.status}: {text}", file=sys.stderr)
            return None
    except asyncio.TimeoutError:
        raise
    except aiohttp.ClientError:
        raise

async def worker_loop(session: aiohttp.ClientSession, worker_id: int) -> None:
    """
    حلقة مستمرة لكل worker: تحاول تعمل fetch مع retries و backoff.
    """
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    backoff = INITIAL_BACKOFF

    while not stopping:
        # نتحكّم في عدد الطلبات المتزامنة عبر semaphore
        async with sem:
            attempt = 0
            while attempt <= RETRIES:
                try:
                    res = await fetch_once(session, timeout)
                    # لو رجع Job (dict) — معالجته وطباعة مختصرة
                    if isinstance(res, dict):
                        # إعادة ضبط الـ backoff لأنه فيه شغل
                        backoff = INITIAL_BACKOFF
                        # طباعة مُركّزة لتقليل I/O
                        job_id = res.get("job_id") or res.get("id") or "?"
                        amount = res.get("money", "?")
                        job_type = res.get("type") or res.get("name") or "?"
                        print(f"[W{worker_id}] JOB id={job_id} amount={amount} label={job_type}")
                        # لو محتاج تعمل معالجة فعلية هنا، استدعي دالة معالجة
                        # await process_job(res)
                    elif res is None:
                        # مفيش شغل — نزود الـ backoff مع jitter
                        await asyncio.sleep(0.001)
                    break
                except asyncio.TimeoutError:
                    attempt += 1
                    if attempt > RETRIES:
                        print(f"[W{worker_id}] timeout, retries exhausted.", file=sys.stderr)
                        # نطبّق backoff قبل المحاولة التالية للحد من الspam
                        await asyncio.sleep(min(MAX_BACKOFF, backoff))
                        backoff = min(MAX_BACKOFF, backoff * 2)
                        break
                    else:
                        # retry سريع
                        await asyncio.sleep(0.001)
                except aiohttp.ClientError as e:
                    attempt += 1
                    if attempt > RETRIES:
                        print(f"[W{worker_id}] client error: {e}", file=sys.stderr)
                        await asyncio.sleep(min(MAX_BACKOFF, backoff))
                        backoff = min(MAX_BACKOFF, backoff * 2)
                        break
                    else:
                        await asyncio.sleep(0.001 * 5 * attempt)

async def main() -> None:
    global stopping
    connector = aiohttp.TCPConnector(limit_per_host=CONCURRENCY, force_close=False, enable_cleanup_closed=True)
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    # Session واحد مع keep-alive
    async with aiohttp.ClientSession(connector=connector) as session:
        # شغّل N worker موازي
        tasks = [asyncio.create_task(worker_loop(session, i)) for i in range(CONCURRENCY)]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            stopping = True
        except KeyboardInterrupt:
            stopping = True
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
