---
inclusion: auto
name: job-search-workflows
description: Load this when the user asks to scrape jobs, rank jobs, apply to a job, record an outcome, prepare for an interview, or run any job search workflow.
---

# Job Search Workflow Triggers

This workspace has four primary workflows. Trigger them by invoking the matching Kiro agent (see `.kiro/agents/`), or by using the phrases listed below.

## Workflow Map

| User says… | Run this | Kiro agent |
|---|---|---|
| "find jobs", "scrape jobs", "any new positions", "search for jobs" | Scrape workflow | `job-scraper` |
| "rank jobs", "score scraped jobs", "shortlist", "/rank" | Rank workflow | `job-ranker` |
| "apply to [URL or paste]", "write CV for", "draft cover letter for" | Apply workflow | `job-applier` |
| "prepare for interview", "interview prep", "/interview" | Interview prep | `job-interviewer` |
| "record outcome", "I got rejected", "I got an offer", "follow up", "/outcome" | Outcome recording | *(inline, see rules below)* |

## Quick Reference: What Each Workflow Does

### Scrape
1. Reads `search-queries.md` for strategy
2. Checks `bun --version`; runs portal CLIs in parallel (`bun run .agents/skills/*/cli/src/cli.ts search --format json --jobage 14 --limit 20`)
3. Falls back to WebSearch for any portal without a CLI
4. Deduplicates via `python3 tools/scrape_state.py candidates --results <tempfile>`
5. Fetches detail for selected hits via portal `detail` CLI commands
6. Quick fit assessment (High/Medium/Low) + Language Gate check
7. Stores via `python3 tools/scrape_state.py store --results <tempfile>`
8. Generates LinkedIn recruiter + team-peer search links for High/Medium fit
9. Portal health check (Step 4.75 in `.claude/skills/job-scraper/SKILL.md`)
10. Presents results table; suggests `/rank` if 8+ new jobs

Full specification: `.claude/skills/job-scraper/SKILL.md`

### Rank
1. Selects candidates via `python3 tools/rank_state.py candidates --limit 10`
2. Reads `04-job-evaluation.md` + `01-candidate-profile.md` once
3. Dispatches parallel scoring sub-agents (~5 jobs each) with scoring rubric inline
4. Aggregates weighted scores, applies location/language vetoes, deadline urgency
5. Expiry sweep via `python3 tools/rank_state.py sweep --write`
6. Writes results via `python3 tools/rank_state.py apply --results <tempfile>`
7. Presents ranked shortlist with strengths, gaps, excluded, couldn't-fetch rows

Full specification: `.claude/commands/rank.md`

### Apply
1. Fetches and guards the posting (dispatches `posting-fetcher` sub-agent)
2. Evaluates fit (reads `04` + `01`, runs `salary_lookup.py` if configured)
3. Drafts CV (`cv/main_<company>_<role>.tex`) and cover letter (`cover_letters/cover_<company>_<role>.tex`)
4. Dispatches `application-reviewer` sub-agent for company research + critique
5. Applies reviewer feedback (Part A: structured edits; Part B: narrative suggestions)
6. Compiles PDFs: `lualatex` for CV, `xelatex` for cover letter
7. Verifies layout: `python3 tools/verify_pdf.py` + `python3 tools/verify_layout.py`
8. ATS text-layer extraction: `python3 tools/verify_pdf.py --ascii-dates --dump-text`
9. Runs full verification checklist from CLAUDE.md
10. Records row in `job_search_tracker.csv`; archives posting to `documents/applications/`

Full specification: `.claude/commands/apply.md`

### Interview Prep
1. Matches argument to a tracked application in `job_search_tracker.csv`
2. Loads archive: `job_posting.md`, `cv_draft.tex`, `cover_letter.tex`, `outcome.md`
3. Checks company research cache (`company_research/<name>.json`, 30-day TTL)
4. Builds 6-section prep pack: likely questions, STAR mappings, consistency brief, tough questions, questions to ask, logistics
5. Saves to `documents/applications/<company>_<role>/interview_prep_<stage>.md`
6. Offers mock interview roleplay

Full specification: `.claude/commands/interview.md`

### Outcome Recording (inline, no separate agent)
Follow `.claude/commands/outcome.md` directly.
- Normal: update status + append to `documents/applications/<company>_<role>/outcome.md`
- `followup`: draft a 60–120 word follow-up for applications quiet 10+ days
- `stale`: batch-resolve applications quiet 60+ days with explicit confirmation

## Portal Discovery

The scraper auto-discovers every portal skill under `.agents/skills/*/SKILL.md`. Currently installed:
- `linkedin-search` — LinkedIn public job listings, any country (use `-l "City, Country"`)
- `freehire-search` — freehire.me tech aggregator, multi-market
- `jobbank-search`, `jobdanmark-search`, `jobindex-search`, `jobnet-search` — Danish portals (disable with `enabled: false` in their SKILL.md if not targeting Denmark)

To add a new portal for Germany or India, ask Kiro to run the `/add-portal` workflow with the target board's URL.
