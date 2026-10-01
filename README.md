# Yuktiva Job Portal

A self-hosted job-search platform. It watches company career pages, scores each
new role against what you are looking for, cuts a resume for it from your own
material, applies by email or by filling in the application form, and keeps
track of everything that went out.

It runs on your machine, with your data in a local folder. Out of the box it
prepares everything and sends nothing until you approve.

## What it does

1. **Reads career pages directly.** Greenhouse, Lever, Ashby and Workday boards
   are read at the source about once an hour, so a role shows up the day it is
   posted. Recruiter requirements can be read from a mail folder or added by hand.
2. **Reads the posting for you.** Years asked for, clearance, sponsorship,
   travel, pay, remote / hybrid / on-site are pulled out of the description and
   shown on the card.
3. **Scores it against your lanes.** A lane is one kind of role you would take
   (for example "career roles" and "contracts", scored separately). Every score
   lists its reasons; every skip names the rule that caused it.
4. **Tailors your resume.** For each role it selects and orders bullets and
   skills from your evidence bank and renders a PDF and a Word file. What the
   posting asks for that you cannot back up is listed as a gap.
5. **Applies.** By email (from your own account, resume attached) or by filling
   in the application form in a real browser.
6. **Tracks.** A queue of what needs you, a tracker with follow-up dates, and a
   ledger of who submitted you to which company.

## The lines it does not cross

These are built in, not settings.

- **It never invents experience.** Tailoring only selects and orders what is in
  your `resume.yaml`. The optional Claude rewording is off by default, and each
  rewrite is rejected if it adds a number, a skill or a name the original bullet
  did not have.
- **It never guesses an answer.** A form field is filled from your profile, an
  answer you gave before, or one of your standard answers. A required question
  with none of those stops the application and asks you, once. Work
  authorisation and sponsorship are answered from your profile only when the
  question uses the plain standard wording; anything unusual is yours.
- **It does not work around bot checks.** The browser is not disguised. A form
  behind a CAPTCHA or a login, a multi-step form, or a page the site's
  `robots.txt` closes to automated clients is handed to you with your answers
  ready. There is no LinkedIn or job-board account automation.
- **It does not double-submit you.** A second route to a client already in the
  ledger (another vendor, or you directly) is stopped for your decision, and so
  is a vendor that will not name the client.
- **It does not report a send it cannot prove.** A form counts as submitted only
  when the site confirms it. An unconfirmed or interrupted send is surfaced for
  you to check and is never retried on its own.

## Quick start

Requires Python 3.11 or newer.

```bash
git clone https://github.com/sankar276/yuktivajobportal
cd yuktivajobportal
python -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium        # renders the resume PDFs and reads forms

jobportal init                     # creates data/ with three example files
```

Put your own details into the three files in `data/` (see below), then:

```bash
jobportal check                    # validates the files and shows what is switched on
jobportal sources import config/sources.example.txt   # or: jobportal sources add <url>
jobportal serve                    # http://127.0.0.1:8000
```

`serve` also runs the background worker: it reads the boards, scores what is
new, prepares applications for the best of the shortlist and sends what you
approve.

To look around first with invented sample data:

```bash
JOBPORTAL_DATA_DIR=demo-data python scripts/seed_demo.py
JOBPORTAL_DATA_DIR=demo-data jobportal serve --no-worker
```

## Your three files

Everything about you lives in `data/`, which is git-ignored. The repository
only ships the examples in `config/`.

| File | What it holds |
| --- | --- |
| `data/profile.yaml` | Name, contact details, work authorisation, contract terms, standard answers to recurring questions. |
| `data/search.yaml` | Your lanes (titles, skills, seniority, locations, pay floor), blocked companies, and the sending policy. |
| `data/resume.yaml` | The evidence bank: every role and every bullet you could defend, tagged with what it shows, plus one or more positionings ("variants"). |

They are validated on every pass. A typo (an unknown key, a bad value) is
reported with its location, and sending pauses until it is fixed rather than
running on stale rules.

The resume file deserves the most time. Write more than fits on a page: each
job gets the bullets that matter for it, and the rest stay in the bank.

## Daily use

- **Feed.** New roles in the last 24 hours, the shortlist, and everything open
  with the reason each was scored the way it was. Filter by lane, workplace,
  commitment, posting date, pay or text. Apply, Save or Hide from the row.
- **Queue.** What is waiting on you, in four groups: a question to answer, an
  application to approve (email drafts are editable), a form that is yours to
  finish, and anything that did not go through.
- **Tracker.** Everything sent, by stage, with follow-up dates.
- **Ledger.** One line per submission: client, role, through whom, terms. Add
  the submissions you made elsewhere so they are guarded too.
- **Answers.** The questions forms have asked and what you said.

For a form that needs a person, run `jobportal assist` on your own computer. It
opens the form in a visible browser with your answers filled in and waits; you
check it, complete anything outlined in orange, and submit it yourself.

## Email

Outgoing mail uses SMTP with your own account, so replies come back to your
inbox. For Gmail, create an app password and set in `.env`:

```bash
JOBPORTAL_SMTP_HOST=smtp.gmail.com
JOBPORTAL_SMTP_USERNAME=you@gmail.com
JOBPORTAL_SMTP_PASSWORD=your-app-password
```

Without it, email applications are written as `.eml` drafts in `data/outbox/`
for you to open and send.

Incoming mail (optional) is read over IMAP, strictly read-only: the folder is
opened read-only and messages are fetched without marking them. Point
`JOBPORTAL_IMAP_FOLDER` at a label that holds recruiter mail. Requirements
become jobs; a reply to something you sent moves that application to "Reply
received"; a bounce marks it failed.

See `.env.example` for every setting.

## Letting it send unattended

In `data/search.yaml`:

```yaml
policy:
  mode: auto          # the default is review
  auto:
    min_score: 80
    daily_cap: 10
    per_company_per_week: 2
    channels: [email, form]
```

In `auto` mode an application goes out by itself only when all of these hold:
it is on the shortlist with a score at or above `min_score`, the posting's age
is known and under `max_job_age_days`, the daily and per-company caps have room,
every required question has an answer, the form has no bot check or login, and
the ledger has no conflict. The rules are checked when the application is
prepared and again at the moment of sending. Everything else waits in the queue.

Run in `review` mode for a while first and read what it prepares.

## Sources

`jobportal sources add <url>` accepts a board address or a company's careers
page that links to one.

| System | How it is read |
| --- | --- |
| Greenhouse | Public Job Board API |
| Lever | Public Postings API |
| Ashby | Public Job Posting API |
| Workday | The JSON endpoint the career site itself loads, queried with your target titles; descriptions are fetched only for titles that match a lane. Applying is on the company's site (it needs an account). |

Every request identifies itself with a descriptive User-Agent, is checked
against the host's `robots.txt` (RFC 9309), is spaced at one request per second
per host, and backs off on errors. There is no switch to turn the robots check
off.

## Docker

```bash
cp .env.example .env               # set POSTGRES_PASSWORD and JOBPORTAL_PASSWORD
mkdir -p data && sudo chown 10001:10001 data
docker compose run --rm app jobportal init
docker compose up --build          # http://127.0.0.1:8000
```

The app refuses to listen beyond `127.0.0.1` without `JOBPORTAL_PASSWORD`. The
compose file publishes the port on loopback only; to reach it from elsewhere,
put a TLS reverse proxy in front and list the host name in
`JOBPORTAL_ALLOWED_HOSTS`.

## Command line

| Command | What it does |
| --- | --- |
| `jobportal init` | Create `data/` from the examples and the database. |
| `jobportal check` | Validate your files; show what is switched on. |
| `jobportal sources add / import / list / remove` | Manage the boards to watch. |
| `jobportal crawl` | Read the boards and score what is new. |
| `jobportal inbox` | Read new recruiter mail. |
| `jobportal jobs` | Print the shortlist. |
| `jobportal prepare` | Tailor resumes and prepare applications. Sends nothing. |
| `jobportal queue` / `approve` | See what is waiting; approve by number or `--all`. |
| `jobportal send` | Send what is approved. |
| `jobportal run` | One full pass: crawl, score, prepare, send. |
| `jobportal assist [id]` | Open a form pre-filled in a visible browser for you to submit. |
| `jobportal serve` / `worker` | The web app (with worker) / the worker alone. |

## How a score is made

Each lane first applies hard filters: blocked company, wrong commitment,
excluded title, title outside the lane, a level two or more steps below the
lane, a location you would not work in, advertised pay under your floor, more
travel than your limit, a clearance you do not hold, or no sponsorship when you
need it. What passes is scored out of 100 on title (30), skills found in the
posting (35), level (10), location (15) and freshness (10); the weights are
yours to change per lane. A posting keeps its best lane. The job page shows the
points for each part.

## Development

```bash
pip install -e ".[dev,llm]"
pytest                               # SQLite
JOBPORTAL_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/jobportal_test pytest
ruff check src tests scripts && ruff format --check src tests scripts && mypy src/jobportal
```

Tests run without network access: boards are served from recorded fixtures,
application forms from a local stand-in server, mail through a local SMTP
server. `alembic revision --autogenerate -m "..."` creates a migration; a test
fails if the models and the migrations drift apart.

```
src/jobportal/
  sources/        one adapter per applicant-tracking system
  crawl.py        new / changed / closed tracking
  facts.py        facts read from descriptions
  scoring.py      lanes, hard filters, weighted rubric
  resume/         evidence bank, tailoring, fact guard, PDF and DOCX
  apply/          policy, ledger, email, form filler, application lifecycle
  inbox/          read-only IMAP, requirement parsing
  pipeline.py     crawl -> score -> prepare -> send
  worker.py       the background loop
  web/            the app
```

## Status and limits

This is a first version. What is and is not proven:

- The crawler adapters are tested against recorded responses. Greenhouse, Lever
  and Ashby payloads and the Workday detail payload were checked against the
  live APIs on 2026-09-30. The Workday listing request is written from the
  endpoint's known shape and has not been run against a live tenant.
- The form filler is tested end to end against local forms built to resemble
  Greenhouse, Lever and single-page application forms. It has not been run
  against live application pages. Expect most real forms to carry a bot check
  and land in "Yours to finish"; `jobportal assist` is the path for those.
- The Docker image and compose file have not been built in CI yet.
- Single user. Every user-owned table carries a `user_id`, but there is no
  sign-up; profile, lanes and resume are files.
- Not built yet: automatic follow-up emails, drafting replies to recruiters, a
  browser extension for one-click fill, Gmail and Outlook OAuth.

## Privacy

Your resume, answers, credentials, database, rendered resumes, screenshots and
drafts live in `data/` and `.env`. Both are git-ignored; keep it that way, since
this repository is public. Self-identification answers default to "decline".
