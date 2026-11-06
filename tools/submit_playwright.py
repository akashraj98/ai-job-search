#!/usr/bin/env python3
"""Scripted fallback driver for /submit (the low-cost alternative to `chrome`).

The default /submit driver is Claude reading and filling the form in the
user's Chrome. That handles any form but costs model usage on every
application. This driver fills a form with no model in the loop, using the
packet from `tools/submit_packet.py build` and the label -> packet-key map that
earlier `chrome` runs taught it (`documents/form_maps/<platform>.json`). It is
only as good as that map: platforms you have submitted on a few times with the
chrome driver fill well, unseen ones leave most fields for you.

It never submits. It opens a visible browser, fills what it can map, uploads
the CV and cover letter, lists what it left blank, and waits. You review,
complete and click Submit yourself, then press Enter here; it writes the
filled-fields JSON that `submit_packet.py record` takes.

Adding a platform: subclass Adapter, override what differs (most forms only
need `fill_field`/`upload`), and register it in ADAPTERS under the platform
name `detect_platform` returns.

Usage:
  python3 tools/submit_playwright.py --packet documents/applications/<slug>/submit_packet.json
        [--out filled.json] [--profile-dir DIR]

Needs: pip install playwright && python3 -m playwright install chromium
Exit 0 after you confirm, 1 on a usage error, 2 when Playwright is missing,
3 when you abort.
"""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = Path.home() / ".cache" / "ai-job-search" / "playwright-profile"


# --------------------------------------------------------------------------
# Pure planning: no browser needed (and unit-tested)
# --------------------------------------------------------------------------

def resolve(source: str, packet: dict):
    """Value for a packet key such as `identity.email` or `documents.cv_pdf`; None when absent."""
    section, _, key = source.partition(".")
    value = (packet.get(section) or {}).get(key)
    if section == "documents":
        return value.get("path") if isinstance(value, dict) and value.get("exists") else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def plan(packet: dict) -> tuple[list, list]:
    """Split the learned form map into (fills, gaps). Each fill is (label, source, value, is_file)."""
    fills, gaps = [], []
    for label, source in sorted((packet.get("form_map") or {}).items()):
        value = resolve(source, packet)
        if value is None:
            gaps.append({"label": label, "source": source, "reason": "no value in packet"})
        else:
            fills.append((label, source, value, source.startswith("documents.")))
    return fills, gaps


# --------------------------------------------------------------------------
# Adapters: one per hiring platform, generic by default
# --------------------------------------------------------------------------

class Adapter:
    """Fills fields by their visible label. Override per platform where that fails."""

    def __init__(self, page, root: Path):
        self.page = page
        self.root = root

    def open(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")

    def fill_field(self, label: str, value: str) -> bool:
        target = self.page.get_by_label(label, exact=False)
        if target.count() != 1:
            return False
        tag = target.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            target.select_option(label=value)
        else:
            target.fill(value)
        return True

    def upload(self, label: str, path: str) -> bool:
        target = self.page.get_by_label(label, exact=False)
        if target.count() != 1:
            return False
        target.set_input_files(str(self.root / path))
        return True

    def run(self, packet: dict) -> tuple[list, list]:
        fills, gaps = plan(packet)
        done = []
        for label, source, value, is_file in fills:
            ok = self.upload(label, value) if is_file else self.fill_field(label, value)
            if ok:
                done.append({"label": label, "value": Path(value).name if is_file else value, "source": source})
            else:
                gaps.append({"label": label, "source": source, "reason": "field not found on page"})
        return done, gaps


# Platform name (from submit_packet.detect_platform) -> adapter class.
# Unlisted platforms use the generic Adapter.
ADAPTERS: dict[str, type] = {}


def adapter_for(platform: str) -> type:
    return ADAPTERS.get(platform, Adapter)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE,
                        help="persistent browser profile, so portal logins survive between runs")
    parser.add_argument("--root", type=Path, default=REPO)
    args = parser.parse_args(argv)

    try:
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"submit_playwright: cannot read packet: {exc}", file=sys.stderr)
        return 1
    app = packet.get("application") or {}
    if not app.get("posting_url"):
        print("submit_playwright: packet has no posting_url", file=sys.stderr)
        return 1

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("submit_playwright: Playwright is not installed.\n"
              "  pip install playwright && python3 -m playwright install chromium", file=sys.stderr)
        return 2

    out = args.out or args.packet.with_name("filled.json")
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(args.profile_dir), headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        adapter = adapter_for(app.get("platform", ""))(page, args.root)
        adapter.open(app["posting_url"])
        print(f"Opened {app['posting_url']} ({app.get('platform')}). If you must click 'Apply' "
              "or sign in first, do it now, then press Enter to fill.", file=sys.stderr)
        input()
        done, gaps = adapter.run(packet)

        print(f"\nFilled {len(done)} field(s).", file=sys.stderr)
        if gaps:
            print("Left for you:", file=sys.stderr)
            for g in gaps:
                print(f"  - {g['label']}  ({g['reason']})", file=sys.stderr)
        print("Also check any field the map does not know about. This script never clicks Submit.\n"
              "Review the form, submit it yourself, then type 'submitted' and Enter "
              "(anything else aborts):", file=sys.stderr)
        answer = input().strip().lower()
        context.close()

    if answer != "submitted":
        print("submit_playwright: aborted - nothing recorded.", file=sys.stderr)
        return 3
    filled = {"platform": app.get("platform"), "fields": done,
              "uploads": [f["value"] for f in done if f["source"].startswith("documents.")],
              "unfilled_by_script": gaps}
    out.write_text(json.dumps(filled, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"filled_file": str(out), "filled": len(done), "gaps": len(gaps)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
