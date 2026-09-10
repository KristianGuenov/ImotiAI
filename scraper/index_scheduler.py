import asyncio
import os
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = (value or "").strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError):
        return 0, 30
    return max(0, min(23, hour)), max(0, min(59, minute))


def _next_run_time(tz: ZoneInfo, hour: int, minute: int) -> datetime:
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


async def main() -> int:
    tz_name = os.getenv("TZ", "Europe/Sofia")
    tz = ZoneInfo(tz_name)
    hour, minute = _parse_hhmm(os.getenv("INDEX_RUN_AT", "00:30"))
    run_on_start = os.getenv("INDEX_RUN_ON_START", "0").lower() in {
        "1",
        "true",
        "yes",
    }
    concurrency = str(max(1, int(os.getenv("SCRAPER_DOMAIN_CONCURRENCY", "6"))))

    while True:
        if run_on_start:
            run_on_start = False
        else:
            next_run = _next_run_time(tz, hour, minute)
            sleep_s = max(0.0, (next_run - datetime.now(tz)).total_seconds())
            print(f"next index inventory at {next_run.isoformat()} ({tz_name})")
            await asyncio.sleep(sleep_s)

        command = [
            "python",
            "-m",
            "scraper.runner",
            "--domain-concurrency",
            concurrency,
        ]
        try:
            process = subprocess.run(command, check=False)
            print(f"index inventory finished, exit={process.returncode}")
        except Exception as exc:
            print(f"index inventory crashed: {exc}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
