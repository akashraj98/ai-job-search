#!/usr/bin/env python3
"""Driver-agnostic data layer for /submit.

/submit fills a job application form for an application /apply already
drafted. Filling is done by a *driver*: today Claude driving the user's Chrome
(the `chrome` driver, run by the /submit spec itself), later optionally a
Playwright script (`tools/submit_playwright.py`) when per-application model
cost matters more than flexibility. Everything a driver needs in, and
everything it reports out, goes through this file, so a driver can be swapped
without touching the tracker, the archive or the answers file.

Two subcommands:

  build    resolve one tracker row into a fill packet: posting URL, detected
           hiring platform, identity fields, CV and cover-letter PDF paths,
           standard answers, the learned field map for that platform, and a
           `missing` list naming every value the driver would have to ask for.
           Writes the packet into the application's archive folder and prints it.

  record   after the user has confirmed the submission, move the tracker row
           from `drafted` to `applied` (submission date, a dated note), write
           `submitted_answers.md` to the archive, copy the submitted .tex
           sources there the way /outcome Step 3 does, and add the form's
           label -> packet-key mapping to `documents/form_maps/<platform>.json`.
           That map is what lets a scripted driver fill the same platform later
           without a model reading the page.

Nothing here opens a browser, fetches a page or submits anything.

Usage:
  python3 tools/submit_packet.py build  --company NAME [--role ROLE]
  python3 tools/submit_packet.py record --company NAME [--role ROLE]
        --filled filled.json --driver chrome|playwright [--date YYYY-MM-DD] [--force]

Every subcommand also takes --root (defaults to the repo) so tests can run
against a scratch workspace. Prints JSON on stdout. Exit 0 on success, 1 on a
usage or state error (message on stderr).
"""

import argparse
import csv
import io
import json
import re
import shutil
import sys
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
PACKET_VERSION = 1

ANSWERS_REL = Path("documents") / "application_answers.json"
ANSWERS_EXAMPLE_REL = Path("documents") / "application_answers.example.json"
FORM_MAPS_REL = Path("documents") / "form_maps"
ARCHIVE_REL = Path("documents") / "applications"
TRACKER_REL = Path("job_search_tracker.csv")

# /outcome's Tracker status vocabulary. Legacy space spellings read as final.
FINAL_STATUSES = {"hired", "rejected", "no_response", "no response", "offer_declined", "offer declined", "withdrawn"}

# Trackers in the wild use either the spec header or an older one; read both.
COLUMN_ALIASES = {
    "date": ("date", "date_added"),
    "source": ("source", "source_url"),
}

# Hosts -> platform name. The name keys the learned form map, so a scripted
# driver can pick an adapter per platform.
PLATFORM_HOSTS = (
    ("greenhouse.io", "greenhouse"),
    ("lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
    ("myworkdayjobs.com", "workday"),
    ("myworkdaysite.com", "workday"),
    ("workday.com", "workday"),
    ("linkedin.com", "linkedin"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("workable.com", "workable"),
    ("icims.com", "icims"),
    ("successfactors.com", "successfactors"),
    ("successfactors.eu", "successfactors"),
    ("teamtailor.com", "teamtailor"),
    ("personio.de", "personio"),
    ("personio.com", "personio"),
    ("recruitee.com", "recruitee"),
    ("naukri.com", "naukri"),
    ("oraclecloud.com", "oracle_hcm"),
    ("taleo.net", "taleo"),
)

IDENTITY_KEYS = (
    "first_name", "last_name", "full_name", "email", "phone",
    "location", "linkedin_url", "github_url", "portfolio_url",
)
# Identity values a form almost always requires; the rest are optional.
REQUIRED_IDENTITY = ("first_name", "last_name", "email", "phone")


class UsageError(Exception):
    pass


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def archive_slug(company: str, role: str) -> str:
    """`<company>_<role>` by the Subfolder naming rule in documents/README.md."""
    raw = f"{company}_{role}".lower().replace(" ", "_")
    kept = "".join(ch for ch in raw if ch.isalnum() or ch == "_")
    slug = re.sub(r"_+", "_", kept).strip("_")
    if not slug:
        raise UsageError("company and role give an empty folder name - need at least one letter or digit")
    return slug


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower() if url else ""
    for suffix, name in PLATFORM_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return name
    return "company_site" if host else "unknown"


def clean_note(text: str) -> str:
    """Tracker notes may hold no commas, double quotes or line breaks (/outcome Step 4)."""
    text = re.sub(r"[\r\n]+", " ", text).replace('"', "'").replace(",", " -")
    return re.sub(r"\s{2,}", " ", text).strip()


def normalize_label(label: str) -> str:
    text = unicodedata.normalize("NFKC", label).lower()
    text = re.sub(r"[*:?()\[\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_json(path: Path, what: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UsageError(f"{what} at {path} is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------------
# Tracker access: read by column name, rewrite only the matched line
# --------------------------------------------------------------------------

class Tracker:
    def __init__(self, path: Path):
        self.path = path
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise UsageError(f"no tracker at {path} - run /apply first so there is a drafted row to submit")
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UsageError(f"{path} is not UTF-8 ({exc}) - re-save it as UTF-8") from exc
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.trailing_newline = text.endswith(("\n", "\r"))
        self.lines = text.splitlines()
        if not self.lines:
            raise UsageError(f"{path} is empty")
        self.header = next(csv.reader([self.lines[0]]))
        self.index = {name.strip(): i for i, name in enumerate(self.header)}

    def col(self, logical: str):
        for name in COLUMN_ALIASES.get(logical, (logical,)):
            if name in self.index:
                return self.index[name]
        return None

    def rows(self):
        """(line_number, fields) for every data line, padded to header width."""
        for n, line in enumerate(self.lines[1:], start=1):
            if not line.strip():
                continue
            fields = next(csv.reader([line]))
            fields += [""] * (len(self.header) - len(fields))
            yield n, fields

    def get(self, fields, logical: str) -> str:
        i = self.col(logical)
        return fields[i].strip() if i is not None and i < len(fields) else ""

    def find(self, company: str, role: str | None):
        """The one open row for company (+role). Raises with the candidates otherwise."""
        company_l = company.strip().lower()
        hits = [(n, f) for n, f in self.rows() if self.get(f, "company").lower() == company_l]
        if role:
            role_l = role.strip().lower()
            exact = [(n, f) for n, f in hits if self.get(f, "role").lower() == role_l]
            hits = exact or [(n, f) for n, f in hits if role_l in self.get(f, "role").lower()]
        open_hits = [(n, f) for n, f in hits if self.get(f, "status").lower() not in FINAL_STATUSES]
        if len(open_hits) == 1:
            return open_hits[0]
        if not open_hits:
            closed = "; ".join(f"{self.get(f, 'role')} ({self.get(f, 'status')})" for _, f in hits)
            detail = f" - only closed rows: {closed}" if closed else ""
            raise UsageError(f"no open tracker row for {company!r}{' / ' + role if role else ''}{detail}")
        roles = "; ".join(self.get(f, "role") for _, f in open_hits)
        raise UsageError(f"{len(open_hits)} open rows match {company!r} - pass --role (one of: {roles})")

    def replace(self, line_no: int, fields) -> None:
        buf = io.StringIO()
        csv.writer(buf, lineterminator="").writerow(fields[: len(self.header)])
        self.lines[line_no] = buf.getvalue()
        text = self.newline.join(self.lines) + (self.newline if self.trailing_newline else "")
        data = text.encode("utf-8")
        self.path.write_bytes((b"\xef\xbb\xbf" if self.bom else b"") + data)


# --------------------------------------------------------------------------
# Identity: answers file first, master CV as fallback
# --------------------------------------------------------------------------

def identity_from_cv(root: Path) -> dict:
    """Name, email, phone, location and profile links from cv/main_example.tex."""
    try:
        tex = (root / "cv" / "main_example.tex").read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return {}
    found = {}
    if m := re.search(r"\\name\{([^}]*)\}\{([^}]*)\}", tex):
        found["first_name"], found["last_name"] = m.group(1).strip(), m.group(2).strip()
    if m := re.search(r"\\email\{([^}]*)\}", tex):
        found["email"] = m.group(1).strip()
    if m := re.search(r"\\phone(?:\[[^\]]*\])?\{([^}]*)\}", tex):
        found["phone"] = m.group(1).strip()
    if m := re.search(r"\\address\{([^}]*)\}", tex):
        found["location"] = m.group(1).strip()
    for kind, key, prefix in (("linkedin", "linkedin_url", "https://www.linkedin.com/in/"),
                              ("github", "github_url", "https://github.com/")):
        if m := re.search(r"\\social\[" + kind + r"\]\{([^}]*)\}", tex):
            handle = m.group(1).strip()
            found[key] = handle if handle.startswith("http") else prefix + handle
    # Template placeholders are not values.
    return {k: v for k, v in found.items() if v and "[" not in v}


def resolve_identity(root: Path, answers_file: dict) -> dict:
    identity = {k: "" for k in IDENTITY_KEYS}
    for source in (identity_from_cv(root), (answers_file or {}).get("identity", {})):
        for key, value in source.items():
            if key in identity and isinstance(value, str) and value.strip():
                identity[key] = value.strip()
    if not identity["full_name"] and identity["first_name"]:
        identity["full_name"] = f"{identity['first_name']} {identity['last_name']}".strip()
    return identity


def pdf_for(root: Path, source_path: str) -> dict:
    """The compiled PDF next to a tracker .tex/.typ path, with an existence flag."""
    if not source_path:
        return {"path": "", "exists": False}
    pdf = Path(source_path).with_suffix(".pdf")
    return {"path": pdf.as_posix(), "exists": (root / pdf).is_file()}


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def cmd_build(args) -> dict:
    root = args.root
    tracker = Tracker(root / TRACKER_REL)
    _, row = tracker.find(args.company, args.role)
    company, role = tracker.get(row, "company"), tracker.get(row, "role")
    status = tracker.get(row, "status").lower()
    url = tracker.get(row, "source")
    platform = detect_platform(url)
    slug = archive_slug(company, role)

    answers_path = root / ANSWERS_REL
    answers_file = load_json(answers_path, "answers file") or {}
    identity = resolve_identity(root, answers_file)
    answers = {k: v for k, v in (answers_file.get("answers") or {}).items()}

    form_map = load_json(root / FORM_MAPS_REL / f"{platform}.json", "form map") or {"fields": {}}

    documents = {
        "cv_pdf": pdf_for(root, tracker.get(row, "cv_file")),
        "cover_letter_pdf": pdf_for(root, tracker.get(row, "cover_letter_file")),
    }

    missing = []
    if not url:
        missing.append("application.posting_url")
    missing += [f"identity.{k}" for k in REQUIRED_IDENTITY if not identity[k]]
    missing += [f"answers.{k}" for k, v in answers.items() if v in ("", None)]
    missing += [f"documents.{k}" for k, d in documents.items() if not d["exists"]]

    warnings = []
    if status != "drafted":
        warnings.append(f"tracker status is {status!r}, not 'drafted' - this may already be submitted")
    if answers_file == {}:
        warnings.append(
            f"no {ANSWERS_REL.as_posix()} - copy {ANSWERS_EXAMPLE_REL.as_posix()} to it and fill it in"
        )

    packet = {
        "packet_version": PACKET_VERSION,
        "application": {
            "company": company,
            "role": role,
            "posting_url": url,
            "platform": platform,
            "tracker_status": status,
            "archive_dir": (ARCHIVE_REL / slug).as_posix(),
        },
        "identity": identity,
        "documents": documents,
        "answers": answers,
        "form_map": form_map.get("fields", {}),
        "missing": missing,
        "warnings": warnings,
    }
    out_dir = root / ARCHIVE_REL / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "submit_packet.json").write_text(json.dumps(packet, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    packet["packet_file"] = (ARCHIVE_REL / slug / "submit_packet.json").as_posix()
    return packet


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------

def read_filled(path: Path) -> dict:
    filled = load_json(path, "filled-fields file")
    if filled is None:
        raise UsageError(f"no filled-fields file at {path}")
    fields = filled.get("fields")
    if not isinstance(fields, list):
        raise UsageError("filled-fields file needs a 'fields' list of {label, value, source}")
    for i, f in enumerate(fields):
        if not isinstance(f, dict) or not str(f.get("label", "")).strip():
            raise UsageError(f"fields[{i}] needs a non-empty 'label'")
    return filled


def write_submitted_answers(path: Path, company, role, when, platform, driver, url, filled) -> None:
    lines = [
        f"## Submitted {when} via {platform} ({driver} driver)",
        "",
        f"- Posting: {url or 'unknown'}",
    ]
    if filled.get("confirmation"):
        lines.append(f"- Confirmation shown: {filled['confirmation']}")
    uploads = filled.get("uploads") or []
    if uploads:
        lines.append("- Uploaded: " + ", ".join(str(u) for u in uploads))
    lines.append("")
    for f in filled["fields"]:
        value = str(f.get("value", "")).strip()
        source = f.get("source") or "unspecified"
        if "\n" in value or len(value) > 120:
            lines += [f"### {f['label'].strip()}", f"_source: {source}_", "", value, ""]
        else:
            lines.append(f"- **{f['label'].strip()}**: {value}  _(source: {source})_")
    lines.append("")
    if path.exists():
        body = path.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + "\n".join(lines)
    else:
        body = f"# Submitted answers: {company} - {role}\n\n" + "\n".join(lines)
    path.write_text(body, encoding="utf-8")


def merge_form_map(path: Path, platform: str, fields: list) -> int:
    """Add label -> packet-key entries. Values are never stored here, only where they came from."""
    data = load_json(path, "form map") or {"platform": platform, "fields": {}}
    data.setdefault("fields", {})
    added = 0
    for f in fields:
        source = str(f.get("source") or "")
        # Only sources a script can resolve on its own are worth learning.
        if not re.fullmatch(r"(identity|answers|documents)\.[a-z0-9_]+", source):
            continue
        key = normalize_label(f["label"])
        if key and data["fields"].get(key) != source:
            data["fields"][key] = source
            added += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return added


def copy_sources(root: Path, archive: Path, tracker: Tracker, row) -> list:
    """Copy (never move) the submitted sources, as /outcome Step 3.1 does. Existing copies win."""
    copied = []
    for col, name in (("cv_file", "cv_draft"), ("cover_letter_file", "cover_letter")):
        src = tracker.get(row, col)
        if not src or not (root / src).is_file():
            continue
        dest = archive / (name + Path(src).suffix)
        if not dest.exists():
            shutil.copy2(root / src, dest)
            copied.append(dest.relative_to(root).as_posix())
    return copied


def cmd_record(args) -> dict:
    root = args.root
    when = args.date or date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
        raise UsageError(f"--date must be YYYY-MM-DD, got {when!r}")
    filled = read_filled(args.filled)

    tracker = Tracker(root / TRACKER_REL)
    line_no, row = tracker.find(args.company, args.role)
    company, role = tracker.get(row, "company"), tracker.get(row, "role")
    status = tracker.get(row, "status").lower()
    if status != "drafted" and not args.force:
        raise UsageError(f"tracker row is {status!r}, not 'drafted' - pass --force to record a second submission")

    url = tracker.get(row, "source")
    platform = filled.get("platform") or detect_platform(url)

    # Tracker: status, submission date, one dated note. Every other field untouched.
    for logical in ("status", "notes"):
        if tracker.col(logical) is None:
            raise UsageError(f"tracker has no {logical!r} column")
    fields = list(row)
    fields[tracker.col("status")] = "applied"
    if (date_col := tracker.col("date")) is not None:
        fields[date_col] = when
    note = clean_note(f"applied {when} via {platform} ({args.driver} driver)")
    old_notes = fields[tracker.col("notes")].strip()
    fields[tracker.col("notes")] = f"{old_notes}; {note}" if old_notes else note
    tracker.replace(line_no, fields)

    archive = root / ARCHIVE_REL / archive_slug(company, role)
    archive.mkdir(parents=True, exist_ok=True)
    answers_md = archive / "submitted_answers.md"
    write_submitted_answers(answers_md, company, role, when, platform, args.driver, url, filled)
    copied = copy_sources(root, archive, tracker, row)
    learned = merge_form_map(root / FORM_MAPS_REL / f"{platform}.json", platform, filled["fields"])

    return {
        "company": company,
        "role": role,
        "status": "applied",
        "date": when,
        "platform": platform,
        "tracker_note": note,
        "submitted_answers": answers_md.relative_to(root).as_posix(),
        "copied": copied,
        "form_map_entries_added": learned,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("build", "record"):
        p = sub.add_parser(name)
        p.add_argument("--company", required=True)
        p.add_argument("--role")
        p.add_argument("--root", type=Path, default=REPO)
        if name == "record":
            p.add_argument("--filled", type=Path, required=True)
            p.add_argument("--driver", choices=("chrome", "playwright"), required=True)
            p.add_argument("--date")
            p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = cmd_build(args) if args.cmd == "build" else cmd_record(args)
    except UsageError as exc:
        print(f"submit_packet: {exc}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
