"""tools/submit_packet.py and the pure half of tools/submit_playwright.py.

submit_packet.py is the one place /submit's drivers touch shared state, so
these pin what would break silently: matching the right tracker row under
either header spelling, rewriting only that line, refusing to record over a
non-drafted row, keeping answer values out of the learned form map, and the
/submit spec's never-submit-without-approval rule.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "submit_packet.py"
SPEC = REPO / ".claude" / "commands" / "submit.md"

sys.path.insert(0, str(REPO / "tools"))
import submit_packet  # noqa: E402
import submit_playwright  # noqa: E402

SPEC_HEADER = (
    "date,company,sector,role,role_type,channel,status,contact_person,"
    "fit_rating,notes,cv_file,cover_letter_file,source,deadline"
)
LEGACY_HEADER = "date_added,company,role,status,fit_rating,cv_file,cover_letter_file,source_url,channel,deadline,notes"

CV_TEX = r"""
\name{Ada}{Lovelace}
\address{London, UK}{}{}
\phone[mobile]{+44 123}
\email{ada@example.com}
\social[linkedin]{ada-l}
"""


class Workspace(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "cv").mkdir()
        (self.root / "cover_letters").mkdir()
        (self.root / "documents").mkdir()
        (self.root / "cv" / "main_example.tex").write_text(CV_TEX, encoding="utf-8")
        for p in ("cv/main_acme_be.tex", "cv/main_acme_be.pdf",
                  "cover_letters/cover_acme_be.tex", "cover_letters/cover_acme_be.pdf"):
            (self.root / p).write_text("x", encoding="utf-8")

    def tracker(self, header, rows, newline="\n"):
        path = self.root / "job_search_tracker.csv"
        path.write_bytes((newline.join([header, *rows]) + newline).encode("utf-8"))
        return path

    def spec_row(self, company="Acme", role="Backend Engineer", status="drafted", notes="fit ok"):
        return (f"2026-11-01,{company},SaaS,{role},,online,{status},,80,{notes},"
                f"cv/main_acme_be.tex,cover_letters/cover_acme_be.tex,"
                f"https://job-boards.greenhouse.io/acme/jobs/1,")

    def run_tool(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, str(TOOL), *args, "--root", str(self.root)],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, expect, proc.stderr)
        return json.loads(proc.stdout) if expect == 0 else proc.stderr

    def filled(self, fields, **extra):
        path = self.root / "filled.json"
        path.write_text(json.dumps({"fields": fields, **extra}), encoding="utf-8")
        return str(path)


class Build(Workspace):
    def test_packet_resolves_platform_identity_documents_and_archive(self):
        self.tracker(SPEC_HEADER, [self.spec_row()])
        packet = self.run_tool("build", "--company", "acme")
        app = packet["application"]
        self.assertEqual(app["platform"], "greenhouse")
        self.assertEqual(app["archive_dir"], "documents/applications/acme_backend_engineer")
        self.assertEqual(packet["identity"]["email"], "ada@example.com")
        self.assertEqual(packet["identity"]["full_name"], "Ada Lovelace")
        self.assertEqual(packet["identity"]["linkedin_url"], "https://www.linkedin.com/in/ada-l")
        self.assertEqual(packet["documents"]["cv_pdf"], {"path": "cv/main_acme_be.pdf", "exists": True})
        self.assertTrue((self.root / packet["packet_file"]).is_file())
        self.assertTrue(any("application_answers.json" in w for w in packet["warnings"]))

    def test_answers_file_overrides_cv_and_empty_answers_are_missing(self):
        self.tracker(SPEC_HEADER, [self.spec_row()])
        (self.root / "documents" / "application_answers.json").write_text(json.dumps({
            "identity": {"email": "work@example.com"},
            "answers": {"notice_period": "30 days", "expected_salary_india": ""},
        }), encoding="utf-8")
        packet = self.run_tool("build", "--company", "Acme")
        self.assertEqual(packet["identity"]["email"], "work@example.com")
        self.assertEqual(packet["answers"]["notice_period"], "30 days")
        self.assertIn("answers.expected_salary_india", packet["missing"])
        self.assertNotIn("answers.notice_period", packet["missing"])

    def test_legacy_header_is_read_by_name(self):
        self.tracker(LEGACY_HEADER, [
            "2026-10-27,Acme,Backend Engineer,drafted,76,cv/main_acme_be.tex,"
            "cover_letters/cover_acme_be.tex,https://jobs.ashbyhq.com/acme/1,ashby,,note"
        ])
        packet = self.run_tool("build", "--company", "Acme")
        self.assertEqual(packet["application"]["platform"], "ashby")
        self.assertEqual(packet["application"]["posting_url"], "https://jobs.ashbyhq.com/acme/1")

    def test_missing_pdf_is_reported(self):
        (self.root / "cv" / "main_acme_be.pdf").unlink()
        self.tracker(SPEC_HEADER, [self.spec_row()])
        packet = self.run_tool("build", "--company", "Acme")
        self.assertIn("documents.cv_pdf", packet["missing"])

    def test_two_open_roles_need_role_and_closed_rows_are_skipped(self):
        self.tracker(SPEC_HEADER, [
            self.spec_row(role="Backend Engineer"),
            self.spec_row(role="Data Engineer"),
            self.spec_row(role="ML Engineer", status="rejected"),
        ])
        err = self.run_tool("build", "--company", "Acme", expect=1)
        self.assertIn("pass --role", err)
        packet = self.run_tool("build", "--company", "Acme", "--role", "data engineer")
        self.assertEqual(packet["application"]["role"], "Data Engineer")
        err = self.run_tool("build", "--company", "Acme", "--role", "ML Engineer", expect=1)
        self.assertIn("only closed rows", err)


class Record(Workspace):
    def test_record_updates_only_the_matched_row(self):
        other = '2026-10-01,Other,,Dev,,online,applied,,70,"quoted, note",a.tex,b.tex,https://x.com/1,'
        path = self.tracker(SPEC_HEADER, [other, self.spec_row()], newline="\r\n")
        result = self.run_tool(
            "record", "--company", "Acme", "--driver", "chrome", "--date", "2026-11-06",
            "--filled", self.filled([{"label": "Email", "value": "ada@example.com", "source": "identity.email"}]),
        )
        self.assertEqual(result["status"], "applied")
        lines = path.read_bytes().decode("utf-8").split("\r\n")
        self.assertEqual(lines[0], SPEC_HEADER)
        self.assertEqual(lines[1], other, "an untouched row must stay byte-identical")
        row = lines[2].split(",")
        self.assertEqual(row[0], "2026-11-06")
        self.assertEqual(row[6], "applied")
        self.assertEqual(row[9], "fit ok; applied 2026-11-06 via greenhouse (chrome driver)")
        self.assertEqual(row[12], "https://job-boards.greenhouse.io/acme/jobs/1")

    def test_record_writes_archive_and_learns_labels_but_never_values(self):
        self.tracker(SPEC_HEADER, [self.spec_row()])
        self.run_tool(
            "record", "--company", "Acme", "--driver", "chrome",
            "--filled", self.filled([
                {"label": "Email *", "value": "ada@example.com", "source": "identity.email"},
                {"label": "Why Acme?", "value": "Because of the product.", "source": "drafted"},
                {"label": "Notice period", "value": "30 days", "source": "answers.notice_period"},
                {"label": "Resume/CV", "value": "main_acme_be.pdf", "source": "documents.cv_pdf"},
            ], confirmation="Thanks for applying"),
        )
        archive = self.root / "documents" / "applications" / "acme_backend_engineer"
        answers = (archive / "submitted_answers.md").read_text(encoding="utf-8")
        self.assertIn("Thanks for applying", answers)
        self.assertIn("Because of the product.", answers)
        self.assertTrue((archive / "cv_draft.tex").is_file())
        self.assertTrue((archive / "cover_letter.tex").is_file())

        form_map = json.loads((self.root / "documents" / "form_maps" / "greenhouse.json").read_text())
        self.assertEqual(form_map["fields"], {
            "email": "identity.email",
            "notice period": "answers.notice_period",
            "resume/cv": "documents.cv_pdf",
        })
        self.assertNotIn("30 days", json.dumps(form_map))

    def test_record_refuses_a_non_drafted_row_without_force(self):
        path = self.tracker(SPEC_HEADER, [self.spec_row(status="applied")])
        before = path.read_bytes()
        filled = self.filled([{"label": "Email", "value": "x", "source": "identity.email"}])
        err = self.run_tool("record", "--company", "Acme", "--driver", "chrome", "--filled", filled, expect=1)
        self.assertIn("--force", err)
        self.assertEqual(path.read_bytes(), before)
        self.run_tool("record", "--company", "Acme", "--driver", "chrome", "--filled", filled, "--force")

    def test_note_never_carries_a_comma(self):
        self.assertEqual(submit_packet.clean_note('a, "b"\nc'), "a - 'b' c")

    def test_legacy_header_records_into_date_added(self):
        path = self.tracker(LEGACY_HEADER, [
            "2026-10-27,Acme,Backend Engineer,drafted,76,cv/main_acme_be.tex,"
            "cover_letters/cover_acme_be.tex,https://jobs.ashbyhq.com/acme/1,ashby,,"
        ])
        self.run_tool("record", "--company", "Acme", "--driver", "playwright", "--date", "2026-11-06",
                      "--filled", self.filled([]))
        row = path.read_text(encoding="utf-8").splitlines()[1].split(",")
        self.assertEqual((row[0], row[3]), ("2026-11-06", "applied"))
        self.assertEqual(row[10], "applied 2026-11-06 via ashby (playwright driver)")


class Helpers(unittest.TestCase):
    def test_archive_slug_follows_documents_readme(self):
        self.assertEqual(submit_packet.archive_slug("Novo Nordisk A/S", "ML Engineer"), "novo_nordisk_as_ml_engineer")

    def test_platform_detection(self):
        cases = {
            "https://job-boards.greenhouse.io/x/jobs/1": "greenhouse",
            "https://jobs.lever.co/x/1": "lever",
            "https://acme.wd5.myworkdayjobs.com/x": "workday",
            "https://www.linkedin.com/jobs/view/1": "linkedin",
            "https://careers.acme.com/job/1": "company_site",
            "https://evilgreenhouse.io/x": "company_site",
            "": "unknown",
        }
        for url, platform in cases.items():
            self.assertEqual(submit_packet.detect_platform(url), platform, url)

    def test_playwright_plan_splits_fills_and_gaps(self):
        packet = {
            "identity": {"email": "a@b.c", "phone": ""},
            "documents": {"cv_pdf": {"path": "cv/x.pdf", "exists": True},
                          "cover_letter_pdf": {"path": "cl/x.pdf", "exists": False}},
            "form_map": {"email": "identity.email", "phone": "identity.phone",
                         "resume": "documents.cv_pdf", "cover letter": "documents.cover_letter_pdf"},
        }
        fills, gaps = submit_playwright.plan(packet)
        self.assertEqual(fills, [("email", "identity.email", "a@b.c", False),
                                 ("resume", "documents.cv_pdf", "cv/x.pdf", True)])
        self.assertEqual({g["label"] for g in gaps}, {"phone", "cover letter"})


class Spec(unittest.TestCase):
    def test_spec_keeps_the_approval_and_record_rules(self):
        text = SPEC.read_text(encoding="utf-8")
        rules = text[text.index("## Important Rules"):]
        for phrase in (
            "Never click the final submit button without the user's explicit",
            "One application per run",
            "Never type a password, create an account, or solve a CAPTCHA",
            "Never record a submission that wasn't confirmed",
        ):
            self.assertIn(phrase, rules)
        self.assertIn("--dry-run` never submits", rules)


if __name__ == "__main__":
    unittest.main()
