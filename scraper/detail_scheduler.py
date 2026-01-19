import asyncio
import os
import subprocess
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore


def _parse_hhmm(s: str) -> tuple[int, int]:
    s = (s or "").strip()
    if not s:
        return (5, 0)
    parts = s.split(":")
    if len(parts) != 2:
        return (5, 0)
    h = int(parts[0])
    m = int(parts[1])
    return (max(0, min(23, h)), max(0, min(59, m)))


def _next_run_time(tz: ZoneInfo, hh: int, mm: int) -> datetime:
    now = datetime.now(tz)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return target


async def main() -> int:
    tz_name = os.getenv("TZ", "Europe/Sofia")
    tz = ZoneInfo(tz_name)

    run_at = os.getenv("DETAIL_RUN_AT", "05:00")
    hh, mm = _parse_hhmm(run_at)

    # Optional: run immediately on container start
    run_on_start = os.getenv("DETAIL_RUN_ON_START", "0").lower() in ("1", "true", "yes")

    while True:
        if run_on_start:
            run_on_start = False
        else:
            nxt = _next_run_time(tz, hh, mm)
            sleep_s = max(0.0, (nxt - datetime.now(tz)).total_seconds())
            print(f"⏰ next detail drain at {nxt.isoformat()} ({tz_name})")
            await asyncio.sleep(sleep_s)

        print("▶️ running detail drain")

        # Use the same container env for API_BASE/API_KEY/rules.
        # Drain entire queue (multi-domain). Any failing URLs remain for next day.
        cmd = ["python", "-m", "scraper.detail_runner", "--drain"]

        try:
            proc = subprocess.run(cmd, check=False)
            print(f"✅ detail drain finished, exit={proc.returncode}")
        except Exception as e:
            print(f"❌ detail drain crash: {e}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
