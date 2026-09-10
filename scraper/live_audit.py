from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import os
import re
from typing import Dict, List

import httpx

from scraper.listing_health import ListingHealthValidator, canonical_domain
from scraper.runner import ScrapeTarget, TargetsLoader


def _targets_by_domain(path: str) -> Dict[str, List[ScrapeTarget]]:
    grouped: Dict[str, List[ScrapeTarget]] = defaultdict(list)
    for target in TargetsLoader(path).load():
        grouped[canonical_domain(target.url)].append(target)
    return grouped


def _target_for_url(
    targets: List[ScrapeTarget], url: str
) -> ScrapeTarget | None:
    # Prefer a configured strict listing pattern over older broad browser
    # targets.  This makes the audit enforce the same final-path contract as
    # the authoritative inventory target.
    ordered = sorted(
        targets,
        key=lambda item: (item.listing_url_regex is not None, item.mode == "sitemap"),
        reverse=True,
    )
    for target in ordered:
        if not target.listing_url_regex:
            continue
        if re.search(target.listing_url_regex, url, re.I):
            return target
    return ordered[0] if ordered else None


async def run(sample_size: int, targets_file: str) -> int:
    api_base = os.getenv("API_BASE", "http://api:8787").rstrip("/")
    api_key = os.getenv("API_KEY", "")
    grouped = _targets_by_domain(targets_file)
    validator = ListingHealthValidator(
        global_concurrency=min(24, max(1, len(grouped) * sample_size)),
        per_domain_concurrency=min(3, sample_size),
    )
    results = Counter()
    failures = []

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            queued = []
            for domain, targets in sorted(grouped.items()):
                response = await client.get(
                    f"{api_base}/api/v1/detail-queue",
                    params={"domain": domain, "limit": sample_size},
                    headers={"X-API-Key": api_key},
                )
                response.raise_for_status()
                for url in response.json().get("urls", []):
                    target = _target_for_url(targets, url)
                    if target is not None:
                        queued.append((domain, target, url))

            async def check(domain: str, target: ScrapeTarget, url: str):
                mode = str(target.validation_mode or "auto").lower().strip()
                if mode == "auto":
                    mode = "head"
                verdict = await validator.check(
                    url,
                    mode=mode,
                    listing_url_regex=target.listing_url_regex,
                )
                return domain, target.name, url, verdict

            verdicts = await asyncio.gather(
                *(check(domain, target, url) for domain, target, url in queued)
            )
    finally:
        await validator.close()

    for domain, target_name, url, verdict in verdicts:
        results[verdict.state] += 1
        if verdict.state != "valid":
            failures.append(
                (domain, target_name, verdict.state, verdict.reason, url)
            )

    print(
        "LIVE_AUDIT "
        f"checked={sum(results.values())} valid={results['valid']} "
        f"dead={results['dead']} unverifiable={results['unverifiable']}"
    )
    for domain, target_name, state, reason, url in failures:
        print(
            f"  {state.upper()} domain={domain} target={target_name} "
            f"reason={reason} url={url}"
        )
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe a bounded live sample from each configured domain."
    )
    parser.add_argument("--sample-size", type=int, default=3)
    parser.add_argument(
        "--targets-file",
        default=os.getenv("TARGETS_FILE", "/app/targets.yml"),
    )
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(run(max(1, min(args.sample_size, 20)), args.targets_file))
    )


if __name__ == "__main__":
    main()
