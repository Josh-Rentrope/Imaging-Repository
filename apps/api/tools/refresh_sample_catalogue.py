"""Regenerate `app/sample_catalogue.py` from the catalogue page.

    uv run python tools/refresh_sample_catalogue.py

Why generated: the page publishes every study as JSON-LD, including its licence
URL, its source collection and the exact byte size of the series zip. Twenty-eight
records transcribed by hand is twenty-eight chances to attach the wrong licence
to the wrong study, and a wrong licence on a downloaded dataset is the kind of
error nobody notices until it matters.

**Licences are checked, not trusted.** The page states that it excludes
non-commercial collections, and this script refuses to write an entry whose
licence is not a CC BY variant — so if that policy ever changes upstream, the
failure is loud at regeneration time rather than silent in shipped code.

The page is behind Cloudflare, which rejects a plain request. A browser
user-agent gets through; there is no scraping here beyond one page load.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

PAGE = "https://saga-it.com/dicom/samples"
OUT = Path(__file__).resolve().parents[1] / "app" / "sample_catalogue.py"

#: Only these may appear in the shipped catalogue. Anything else aborts.
ALLOWED_LICENCES = {
    "https://creativecommons.org/licenses/by/3.0/": "CC BY 3.0",
    "https://creativecommons.org/licenses/by/4.0/": "CC BY 4.0",
}

HEADER = '''"""Demo datasets, generated from the catalogue they are listed in.

Each entry below was read from the JSON-LD metadata that
https://saga-it.com/dicom/samples publishes for its tiles -- name, description,
licence, anatomy, source collection, DOI and the exact size of the series zip.
Transcribing 28 records by hand is how a licence ends up attributed to the wrong
study, so they are extracted instead.

**Do not edit by hand.** Re-run tools/refresh_sample_catalogue.py against that
page to update them; it rewrites this file. `app/samples.py` holds the logic and
the reasoning about licensing, and is the file to read first.

Every study here is CC BY 3.0 or 4.0, which permits commercial use **with
attribution**. That is why `collection`, `source` and `doi` travel with each
entry: showing the licence without the attribution does not satisfy it.
"""

from __future__ import annotations

#: Where the series zips are hosted. Named so a deployment can mirror them.
SAGA_BASE = "https://saga-it.com/dicom/samples/files"

ENTRIES: list[dict] = [
'''


def fetch() -> str:
    request = urllib.request.Request(
        PAGE,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def studies(html: str) -> list[dict]:
    match = re.search(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S | re.I
    )
    if match is None:
        raise SystemExit("no JSON-LD on that page; its structure has changed")

    document = json.loads(match.group(1))
    items: list[dict] = []
    for node in document.get("@graph", []):
        if node.get("@type") == "ItemList":
            items += [li["item"] for li in node.get("itemListElement", [])]
    if not items:
        raise SystemExit("no studies listed; the page's structure has changed")
    return items


def build(entries: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for entry in entries:
        licence = ALLOWED_LICENCES.get(entry.get("license", ""))
        if licence is None:
            # Refused rather than carried through. A non-commercial study here
            # would be shipped as usable in a commercial product.
            name = entry.get("name", entry.get("@id", "?"))
            raise SystemExit(
                f"{name!r} is licensed {entry.get('license')!r}, which is not an "
                "allowed CC BY variant. Investigate before shipping this catalogue."
            )

        archive = next(
            (
                d
                for d in entry.get("distribution", [])
                if d.get("encodingFormat") == "application/zip"
            ),
            None,
        )
        if archive is None:
            continue

        keywords = [k.strip() for k in entry.get("keywords", "").split(",")]
        rows.append(
            {
                "id": entry["@id"].split("#")[-1],
                "title": entry["name"],
                "description": entry["description"],
                "url": archive["contentUrl"],
                "licence": licence,
                "bytes": int(archive["contentSize"]),
                "modality": keywords[0] if keywords else "",
                "anatomy": keywords[1] if len(keywords) > 1 else "",
                "collection": keywords[2] if len(keywords) > 2 else "",
                "source": entry.get("url", ""),
                "doi": entry.get("sameAs", ""),
            }
        )
    return rows


def render(rows: list[dict]) -> str:
    lines = [HEADER]
    for row in sorted(rows, key=lambda r: (r["modality"], r["id"])):
        lines.append("    {\n")
        for key, value in row.items():
            lines.append(f"        {key!r}: {value!r},\n")
        lines.append("    },\n")
    lines.append("]\n")
    return "".join(lines)


def main() -> int:
    rows = build(studies(fetch()))
    if not rows:
        raise SystemExit("nothing to write")
    OUT.write_text(render(rows), encoding="utf-8")
    total = sum(row["bytes"] for row in rows) / 1_000_000
    print(f"wrote {len(rows)} studies to {OUT} ({total:.0f} MB total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
