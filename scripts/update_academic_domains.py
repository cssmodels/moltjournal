"""Merge the Hipo university domains list into app/data/academic_domains.txt.

    python scripts/update_academic_domains.py

Source: https://github.com/Hipo/university-domains-list (MIT licence). Domains the
suffix rules already cover (.edu, .edu.XX, .ac.XX) are skipped, so the file stays
small. Review the diff before committing it.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import vocab  # noqa: E402

URL = "https://raw.githubusercontent.com/Hipo/university-domains-list/master/world_universities_and_domains.json"
TARGET = ROOT / "app" / "data" / "academic_domains.txt"


def main() -> None:
    with urllib.request.urlopen(URL, timeout=60) as response:
        universities = json.load(response)
    have = vocab.academic_domains()
    added = set()
    for university in universities:
        for domain in university.get("domains", []):
            domain = domain.strip().lower()
            if domain and domain not in have and vocab.academic_domain(f"x@{domain}") is None:
                added.add(domain)
    if not added:
        print("nothing to add")
        return
    with TARGET.open("a") as out:
        out.write("# --- merged from the Hipo university-domains-list (MIT licence)\n")
        for domain in sorted(added):
            out.write(domain + "\n")
    print(f"added {len(added)} domains to {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
