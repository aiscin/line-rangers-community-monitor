"""Skip backup schedules only after today's full collection succeeded."""

import json
import os
import sys
from datetime import datetime, time, timezone, timedelta
from pathlib import Path


KST = timezone(timedelta(hours=9))
REQUIRED_SOURCES = {
    "pantip-search", "facebook-th", "bahamut", "facebook-tw",
    "spgame", "x-jp", "naver-kr",
}


def needs_refresh(event: str, cache: dict, current: datetime) -> bool:
    if event != "schedule":
        return True
    try:
        updated = datetime.fromisoformat(cache["updated_at"]).astimezone(KST)
        details = cache["source_details"]
    except (KeyError, TypeError, ValueError, AttributeError):
        return True
    morning = datetime.combine(current.date(), time(9, 45), tzinfo=KST)
    return not (
        morning <= updated <= current
        and all(details.get(source, {}).get("status") == "ok" for source in REQUIRED_SOURCES)
    )


def main() -> None:
    try:
        cache = json.loads(Path("latest-cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    refresh = needs_refresh(os.environ.get("EVENT_NAME", ""), cache, datetime.now(KST))
    print(f"refresh={'true' if refresh else 'false'}")
    print("Refresh required" if refresh else "Today's collection is complete; skipping backup run", file=sys.stderr)


if __name__ == "__main__":
    main()
