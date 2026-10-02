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
   skills from your evidence bank and renders a PDF and a Word file. Skills the
   posting names that your bank does not cover are listed as gaps. That check
   uses your lanes' skills and a built-in list of common technologies, so an
   unusual tool can go unnoticed.
5. **Applies.** By email (from your own account, resume attached) or by filling
   in the application form in a real browser.
6. **Tracks.** A queue of what needs you, a tracker with follow-up dates, and a
   ledger of who submitted you to which company.

## The lines it does not cross

These are built in, not settings.

- **It never invents experience.** Tailoring only selects and orders what is in
  your `resume.yaml`. The optional Claude rewording is off by default. When it
  is on, a rewrite is kept only if every word and number in it comes from the
  original bullet (joining words aside) and each number still counts the same
  thing; otherwise your own wording stays.
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
  when the site confirms it. A send that was started and never confirmed (the
  site said nothing, the mail server dropped the line, the app was stopped
  half-way) is shown as "Not known whether it went out". It counts against
  your caps as if it had gone, is never retried on its own, and waits for you
  to say which it was.
- **It does not answer a stranger's email with your resume.** A role that
  arrives by email always waits for your approval, whatever the mode. Anyone
  can send you an email, and a sender's address proves nothing.
- **It stays on the public internet.** The crawler and the browser only connect
  to public addresses. A board, a posting or a page cannot point them at your
  own machine or the network it sits on.

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

They are validated on every pass. A typo (an unknown key, a bad value, the
same key written twice) is reported with its location, and sending pauses until
it is fixed rather than running on stale rules.

The resume file deserves the most time. Write more than fits on a page: each
job gets the bullets that matter for it, and the rest stay in the bank.

## Daily use

- **Feed.** New roles in the last 24 hours, the shortlist, and everything open
  with the reason each was scored the way it was. Filter by lane, workplace,
  commitment, posting date, pay or text. Apply, Save or Hide from the row.
- **Queue.** What is waiting on you, in five groups: a send whose outcome is not
  known, a question to answer, an application to approve (email drafts are
  editable), a form that is yours to finish, and anything that did not go
  through.
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
received"; a bounce of an application still waiting for an answer marks it as
not gone through.

A requirement that arrives by email is prepared like any other role, and then
waits for you: it is never sent unattended, and the reply written for it does
not repeat the title or client name the sender gave.

`.env.example` lists every setting.

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

- it is on the shortlist with a score at or above `min_score`;
- the role came from a board or was added by you, not from an email;
- the posting's age is known and under `max_job_age_days`;
- the daily and per-company caps have room (sends whose outcome is unknown
  count as sent);
- every required question has an answer, and the form has no bot check, login
  or field the app could not label;
- nothing about the posting had to be flagged (an unreadable location, a
  clearance that could not be compared with yours);
- the ledger has no conflict.

The rules are checked when the application is prepared and again at the moment
of sending. A form is sent only if it is still the form that was prepared: if
its fields or the answers that would go into them have changed, nothing is
typed and it comes back to you. Everything else waits in the queue.

Run in `review` mode for a while first and read what it prepares.

## Sources

`jobportal sources add <url>` accepts a board address or a company's careers
page that links to one.

| System | How it is read |
| --- | --- |
| Greenhouse | Public Job Board API |
| Lever | Public Postings API |
| Ashby | Public Job Posting API |
| Workday | The JSON endpoint the career site itself loads, searched for the titles in your lanes (target titles first, at most 25 searches per reading); descriptions are fetched only for titles that match a lane. A posting that drops out of the search results is asked about directly before it is treated as closed. Applying is on the company's site (it needs an account). |

Greenhouse boards hosted in the EU (`job-boards.eu.greenhouse.io`) are not
supported yet.

Every request identifies itself with a descriptive User-Agent, is checked
against the host's `robots.txt` (RFC 9309) at every hop of a redirect, is
spaced at one request per second per host, and backs off on errors. There is no
switch to turn the robots check off. Responses are limited in size and time, so
one slow or oversized board cannot hold up the rest.

Removing a board keeps the applications you made through it: its postings with
an application stay in your tracker, the rest are deleted, and adding the board
again picks up where it left off.

## Docker

```bash
cp .env.example .env               # set POSTGRES_PASSWORD and JOBPORTAL_PASSWORD
mkdir -p data && sudo chown 10001:10001 data
docker compose run --rm app jobportal init
docker compose up --build          # http://127.0.0.1:8000
```

The app refuses to listen beyond `127.0.0.1` without `JOBPORTAL_PASSWORD` (at
least 8 characters). The compose file publishes the port on loopback only; to
reach it from elsewhere, put a TLS reverse proxy in front, list the host name
in `JOBPORTAL_ALLOWED_HOSTS`, set `JOBPORTAL_COOKIE_SECURE=true`, and name the
proxy in `JOBPORTAL_TRUSTED_PROXIES` so that sign-in attempts are counted per
visitor. Signing out ends every session.

Chromium's own sandbox does not start in an ordinary container, so inside
Docker pages are opened without it and the log says so. The request guard
described under "Status and limits" applies either way.

## Command line

| Command | What it does |
| --- | --- |
| `jobportal init` | Create `data/` from the examples and the database. |
| `jobportal check` | Validate your files; show what is switched on. |
| `jobportal sources add / import / list / remove` | Manage the boards to watch. |
| `jobportal crawl` / `score` | Read the boards and score what is new / score again without reading. |
| `jobportal inbox` | Read new recruiter mail. |
| `jobportal jobs` | Print the shortlist. |
| `jobportal prepare` | Tailor resumes and prepare applications. Sends nothing. |
| `jobportal queue` / `approve` | See what is waiting; approve by number or `--all`. |
| `jobportal send` | Send what is approved. |
| `jobportal run` | One full pass: crawl, score, prepare, send. |
| `jobportal assist [id]` | Open a form pre-filled in a visible browser for you to submit. |
| `jobportal serve` / `worker` | The web app (with worker) / the worker alone. |
| `jobportal since [hours]` | New postings, applications sent and replies received lately. |

## How a score is made

Each lane first applies hard filters. A posting is skipped when:

- the company is on your blocked list;
- the commitment is wrong for the lane, or the title is excluded or outside it;
- the level reads two or more steps below or above the lane's range;
- the place is one you would not work in;
- advertised pay in your currency tops out under your floor;
- it states more travel than your limit;
- it says one of the lane's `skip_if_description_has` phrases (said, not
  denied: "no relocation required" does not count), or names none of its
  `must_have_any` skills;
- it requires an active clearance you do not hold, or one above yours;
- it says sponsorship is not available and you need it.

What passes is scored out of 100 on title (30), skills found in the posting
(35), level (10), location (15) and freshness (10); the weights are yours to
change per lane. A posting goes to the lane whose bar it clears, or failing
that the lane where it scores best. The job page shows the points for each
part.

Three things are read with care rather than guessed:

- **Location.** Only a place that was recognised, and is not one of yours, is a
  reason to skip. A place the app cannot read ("CA, Remote" could be California
  or Canada) is scored as unknown and the posting is kept off the shortlist
  until you have looked, so nothing is prepared or sent for it on its own.
  Recognition is built around the United States: states, large cities and
  "City, ST". For other regions, list the names you would accept (for example
  `remote_regions: [UK, United Kingdom, London]`).
- **Level.** Titles are read as written: "Cloud Architect - AVP" is an
  architect, not an officer; "Office of the CTO" is where a role sits.
- **Skills.** Names that are also words or letters ("Go", "R", "C", "Helm",
  "Spark") count where a skill would stand, not in "go above and beyond".

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
  http.py         the crawler's client: robots.txt, pacing, size and time limits
  netguard.py     keeps requests on the public internet
  egress.py       the checked way out for the unattended browser
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
- The Docker image is built and started by CI on every pull request. It has
  not been run for real use.
- The unattended browser reaches the internet only through a small proxy on
  your machine that looks each name up once, refuses anything that is not a
  public address, and connects to the address it checked. If your machine
  itself needs an outbound proxy (`HTTPS_PROXY`), that proxy does the
  connecting instead, and only the page-level request guard applies; the
  crawler's address pinning is off in that case too.
- Reading postings is pattern matching, not understanding. Location, level,
  pay, clearance, sponsorship and travel are each read conservatively, with
  tests for the phrasings found so far, and will still miss some. A wrong skip
  shows its reason on the job page and you can apply anyway.
- Single user. Jobs, scores, applications, the ledger and answers carry a
  `user_id`, but there is no sign-up; profile, lanes and resume are files.
- Not built yet: automatic follow-up emails, drafting replies to recruiters, a
  browser extension for one-click fill, Gmail and Outlook OAuth.

## Privacy

Your resume, answers, credentials, database, rendered resumes, screenshots and
drafts live in `data/` and `.env`. Both are git-ignored; keep it that way, since
this repository is public. The data folder and the files written into it are
readable by your user only. Self-identification answers default to "decline".
