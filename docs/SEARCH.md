# Job search ("sniffer")

Checks the official job boards of companies you list, keeps every posting it sees, and shows in
**New Opportunities → Verified Jobs** only what passes all gates. Deterministic rules, no AI, no paid APIs.

## How a posting is judged

| Gate | Rule | When it fails |
|---|---|---|
| Discovery | Fetched from the employer's own Greenhouse, Lever, Ashby or Comeet board (Comeet: with its description) | Board error is logged; a 403 or timeout never means "closed" |
| Deduplication | Same official job ID / requisition ID, or same company + same role (the title without the notes the tracker adds, such as " — LinkedIn #…" or "(Req 5)") as a tracker record or a job you decided | **Known**: not shown as new |
| | Same company + same level + the same responsibilities and requirements as a job you decided (other city, reworded title). Compared: the role sections only (found by their headings; "about us", benefits and legal text left out), in runs of five words; ≥ 95% alike, or ≥ 70% with a similar title | **Known**: one position. A lead, junior or engineer version of an ad is never merged with another level |
| | Staffing agencies: a title match alone never hides a job | Shown, marked *same title in tracker — check job ID* |
| Verification | Listed on the official board at the latest successful check (within 7 days) | Not shown; the record is kept |
| Relevance | Title contains a search term and no excluded term; regulatory terms in the description | Not shown; reason kept in the run log |
| Geography | Israel, or remote explicitly worldwide | Outside Israel: hidden with reason. Unclear: **Needs geo review** |
| Age | Published within the last 14 days (setting *Show jobs published within*) | Kept behind **Older than 14 days**, not shown as new |
| Your decisions | Good find — applied, Duplicate (seen already: in your tracker, in this list or elsewhere), Wrong match, Not for me, Hide; Restore brings it back | Logged in the append-only decision log |

**Regions** (Search settings → Regions): Tel Aviv & Gush Dan, Sharon & Hadera, Center & Shfela, Jerusalem, Judea &
Samaria, South, North & Haifa — each a list of towns and the district names boards write. Untick a region and a job
whose places are all there is held back, under its own board filter; a town on a border is in both regions
(Caesarea: Sharon and Haifa District), and a job in several places, "Israel" or "Remote" never is. **Places** adds
single towns beyond the regions. "Verification" alone is not a QA title
(your rule, 2026-10-16): a title with it matches only through a QA word.

**Filters on the board.** The panel *Filters* above the new jobs lists every rule that holds a relevant job back
(a search term in the title is required: other titles are not jobs you search for): excluded word in the title,
regulated or manufacturing description, outside Israel, location unclear, older than 14 days, already known. Each
row shows how many live jobs that rule alone holds back. **Turn off** a rule and those jobs join the list, after
the others, each marked *filter off: …* with the reason; it stays off (saved as a new profile version) until you
turn it on again. A job held back by two rules needs both off. Jobs let in this way are never sent in the
new-jobs e-mail. Your own decisions and boards you turned off are listed too, with links: they are undone where
you made them.

**Reposted by the employer.** A known job (in your tracker or decided by you, whatever its status) that the employer
publishes again after your last action on it is shown in its own block below the new jobs, with your history: a sign
they are still hiring. Greenhouse, Lever and Ashby give the real publication date; Comeet only the date of its last
update, which any edit moves.

**Two-week rule in the tracker.** A record still *not applied* or *on hold* with no movement (tracker update date, or
your latest status) for more than 14 days reads as **Expired** (Closed). Applications are never expired. Nothing is
deleted or rewritten, and pressing any status brings the record back.

**Wrong match — whole company** (inside Not a fit): for an employer where nothing is for you (a hardware company).
It marks the job and turns the company's board off; jobs of a board that is off leave every list at once. Search
settings turns it on again.

**Not a fit** is two decisions: **Wrong match** (the search should not have brought it: a search error, and the reason
says which rule to tighten) and **Not for me** (the right kind of job, but your choice: not a search error). Diagnostics
→ Search feedback counts them separately.

Freshness on the cards: *Fresh* (≤ 7 days), *Aging* (≤ 14), *Updated* (Comeet: no posting date, only its last update).
**New** marks jobs that appeared on a board since its previous check; they are listed first.

## Where to apply: order of preference

1. **The employer's own site or ATS** (Greenhouse, Lever, Ashby, Comeet, Workday, a careers page). The search
   reads only these boards, so every job in Verified Jobs applies here.
2. **LinkedIn**.
3. **A job board** (Drushim, AllJobs, JobTime, Glassdoor, Indeed…). A staffing agency's own list (Gotfriends) is
   ranked here too, but labelled *Agency site*: for its confidential clients it is the only way in.

A tracker record shows its links in this order (link, evidence and channel columns; notes are left out). When the
best link is LinkedIn or a job board, the employer's posting on the official boards is suggested next to it
(*Likely the official posting*). When the best link is a job board and nothing official is found, *Look for it on
LinkedIn* opens a LinkedIn Jobs search (company + role, Israel) that you run yourself.

## Your history at the company

A new job at a company you already have records at shows **Earlier here** (up to three records, a rejection first,
each one a link) and a tag: **rejected here before** (amber: decide whether to apply again) or **applied here
before**. Staffing agencies are left out: their records are about many different clients.

**Applied here as a lead** (staffing agencies). Your rule: once you applied through an agency at your level (team
lead, manager, head), you do not apply through it again as an engineer — the recruiter would see a step down. An
engineer or junior job at such an agency, on the board or in your tracker, is marked with those applications
(any status after sending a CV, rejections included). Nothing is hidden. Hebrew titles are read with both
genders ("ראש/ת צוות" is a team lead); "leadership" in a title is your level.

A status (e.g. *Rejected by them*) lives on the tracker record, not on the job. Open the record from the card,
press the status, and you come back to the same job. After **Duplicate** the notice links the tracker record
to update. Without a record at that company (applied outside the tracker), write what happened in the feedback
note before pressing the button.

## Default profile (from your tracker's screening rules)

- *Software-only discovery*: search terms for QA / test / automation / verification / validation (also Hebrew
  and Russian); excluded in the title: hardware, embedded, IoT, firmware, HIL, FPGA/ASIC/RTL, semiconductor,
  manufacturing and process quality, quality control, regulatory, medical device, pharma, mechanical, electrical,
  PCB, RF, technician, supplier quality, electromechanical.
- *Excluded by the description* (two or more terms): medical-device regulation (FDA, MDR, ISO 13485, IEC 62304…)
  and manufacturing quality (SPC, 8D, FMEA, DOE, NCR, MTBF, semiconductor, ISO 9001, supplier audits…).
  **Medicine is fine, medical devices are not**: in a *software role* (title with software, SW, automation, SDET,
  web, mobile, API, AI…) these terms are only marked, never a reason to hide the job.
- *Your level*: middle management (lead, team lead, manager, head of QA) is listed first with a **Lead role** badge;
  engineer roles are marked *engineer role*, director / VP / chief *above your level*. Nothing is hidden for its level.
  A title that matched only on quality, validation or verification is marked *check: … only*.
- *Overqualified*: a job whose description asks at most 3 years of QA / engineering experience (setting) is marked
  *likely overqualified* — mid-level, below 10+ years. A range counts by its top ("3–5 years" is not marked); years
  of management or leadership never count (2–3 years of management is a normal lead requirement); optional lines
  ("ideally", "an advantage") are ignored. Your own **Overqualified** button marks a job the same way: it never
  hides it, you can still apply, and Diagnostics counts the marks. *Not for me* also has the reason *Overqualified*.
- *Junior/manual-only filter*: junior / entry level / intern / student / manual are **low priority** (a tag), not hidden.
- *Live-status verification*: only the employer's official board counts; mirrors, aggregators and LinkedIn never do.
- *Already applied* / *Rejected*: never come back as new; only as *Reposted* when published again after your last action.

Everything is editable under **Search settings**; each save is a new version (old versions are kept).

## Sources

The first upgrade added the 19 official boards already linked in your tracker; `boards discover` added the
companies on its built-in list that have jobs in Israel (84 boards on 2026-10-15, 45 of them on Comeet).
**Comeet** is the most common ATS in Israel; some of its pages are branded *Spark Hire Recruit* — the same
system. Comeet boards need the company's internal ID (`company/AB.123`), which cannot be guessed: the known ones
are listed in `search/discover.py` (`COMEET_BOARDS`), and any other is added by pasting one of its job links.

A Greenhouse, Lever, Ashby or Comeet link you save anywhere in the console (a lead, a note on a job or record)
adds that board automatically at the next run (origin *a link you saved*); a board you turned off stays off.

**Workday** (`<company>.wd5.myworkdayjobs.com/<site>`: NVIDIA, Edwards Lifesciences, Medtronic) is read through
the site's own job search, filtered to Israel with the site's location filter, newest first: pages stop at jobs
older than a month and at 15 pages (20 jobs each), with a pause between them. The list has no descriptions: for a job
with a title you search for, the description is read once (at most 40 a run) and kept, so the description rules
(manufacturing terms, years asked) and the card's summary work for Workday jobs too. Add another
Workday employer by pasting a link to its careers site in Search settings.

Not supported yet: Oracle, SmartRecruiters, Workable, Raz Technologies (blocks automated requests),
company-specific careers sites. Coverage is exactly the boards on the list — the tool does
not search the whole internet and does not scrape LinkedIn.

## The same job through several recruiters

An employer often hires through several recruiters at once and gives each the same requirements. Tracker
records from different sources — a recruiter on at least one side, the same level, no different cities — are
linked as **possibly the same position** when they name the same end client ("NESS / Bank Acme"; a unit named
with its parent, "Pepper (Bank Acme)", is the unit), or share at least three rare requirements from the setting
*Requirement terms* ("react native, appium, detox, cucumber, gherkin"). Job postings with full descriptions are
linked when their role text overlaps by 60% or more. Any status; nothing is merged; each link says why.

## Job-alert e-mails (LinkedIn, AllJobs, Drushim)

LinkedIn, AllJobs and Drushim send you job alerts. LinkedIn's terms forbid automated visits to its site, so the console
reads the alerts from your mailbox instead: with every search (and with **Check mail now** on the *From alerts*
tab) it opens the mailbox **read-only** (IMAP EXAMINE, BODY.PEEK: nothing is marked read, moved, labelled or
deleted) and takes the alerts of the last two days (`MAIL_DAYS`) from jobalerts-noreply@linkedin.com,
jobs-noreply@linkedin.com, alljob.co.il and drushim.co.il (its smart agent, “הסוכן החכם”).
Each e-mail is read once; each job is stored once, with how many
e-mails named it.

Jobs go through the same rules as the boards' jobs: a QA title without excluded words, in Israel (AllJobs and
Drushim are Israeli by definition), not in your tracker (same LinkedIn job number, job ID or company + title), not at a company
whose board you turned off; your level first. What a rule keeps off is listed with the rule. **Applied** or
**Interested** on a card puts the job into your tracker as an added record (code AD), with the alert's link;
**Already applied** (you applied to this job before, it may have been rejected: not again, though the job is fine),
**Not a fit** (the job does not suit you) and **Hide** take it off; all three are listed under *Your decisions*,
each with its own label, and **Restore** brings a job back.
LinkedIn's confirmation e-mail (“your application was sent to …”) is read too: the job you applied to goes
into your tracker as Applied by itself and is never shown as new (the “similar jobs” in it are shown as usual).

**Replies to your applications** (search/replies.py) are read with the alerts: an employer's confirmation ("We Got
It: Thanks for applying for <role>", "Thank you for your application to <company>", "קיבלנו את מועמדותך") sets
the record to Applied, or adds it (AD) when the tracker has nothing at that company; a no ("decided not to move
forward", "other candidates", "החלטנו שלא להמשיך") sets it to Rejected by them. Rules, no AI: the company and the role
come from the subject (or the text, or the sender), the record from your tracker by company and role. A reply
older than your own update of the record never overwrites it; what the rules cannot place changes nothing and is
listed under *From alerts → Replies to your applications*. Your forwards ("Fwd:") are read from their original
sender. Only the mailboxes connected in Search settings are read.

Where to apply, in your order: the employer's own posting first (when a board the console reads lists the same
job), else **Find on <company>'s site** (a web search for the job on the employer's site or ATS, LinkedIn and job
boards left out, opened by you in your browser), then contacts (recruiters, QA leads, people you know there), and
LinkedIn last; if there is only LinkedIn, it is only LinkedIn. Jobs LinkedIn marks "Apply with resume & profile"
are tagged **Easy Apply — employer page first**: one-click applications are said to work worst.

Each employer named in the alerts is checked once for a public Greenhouse / Lever / Ashby board; a board with jobs
in Israel joins the boards the search reads (origin "found: a company in your alert e-mails"), so its jobs come into
Verified Jobs with the official link from then on. Staffing agencies are skipped (their client is not named).

Setup, once: turn on 2-Step Verification in your Google account, create an app password named "Career Console"
at https://myaccount.google.com/apppasswords, and paste it with your address in Search settings → Job-alert
e-mails. It is stored only in `data/mail.env` (readable by you only, never in git). Mail forwarded into this Gmail
from your other addresses is read too; a separate mailbox needs its own line. Removing a mailbox deletes its app
password from this computer; revoke it in your Google account as well.

## Possible contacts

Every job card and open tracker record has three LinkedIn people searches you open yourself, in Israel:
recruiters at the company, its QA leaders (often the hiring manager), and your 1st and 2nd degree connections who
mention it. Nothing is fetched, collected or stored; whether you know someone there is for you to see.

## Running

- **Run search now** on the Verified Jobs tab, or `uv run career-console search` (`--dry-run` saves nothing,
  `--force` also re-checks boards checked in the last 10 minutes).
- One run at a time. Boards checked in the last 10 minutes are skipped (repeat clicks). About one request per board, one second apart.
- Cost: $0. Every run writes a `cost_ledger` row with 0 paid calls.

### Finding more boards

`uv run career-console boards discover` tries the official Greenhouse, Lever and Ashby boards of about 75
Israeli tech companies, plus the known Comeet boards, and adds those that currently list jobs in Israel
(`--dry-run` only reports). Any other Comeet board: paste one of its job links in Search settings.

### Notifications (e-mail or Telegram)

Only new verified jobs are sent: company, title, freshness, location, official link. Never contacts or notes.

1. Copy `examples/notify.env.example` to `data/notify.env` (data/ is never committed).
2. Pick one channel and fill it in:
   - **E-mail**: turn on 2-Step Verification in your Google account, create an app password at
     https://myaccount.google.com/apppasswords, paste it as `SMTP_PASSWORD`; set `SMTP_USER` and `NOTIFY_EMAIL_TO`.
   - **Telegram**: message @BotFather, send `/newbot`, copy the token into `TELEGRAM_BOT_TOKEN`, then send your
     new bot any message.
3. `uv run career-console notify --test` sends one test message.

After that every search (manual or scheduled) sends the jobs that are new since the boards' previous check,
each job at most once. Failures are logged and never stop a search.

### Scheduling (optional, you install it)

Searches run only while this computer is on. One command sets it up with the right paths:

```bash
uv run career-console schedule --install --every 2 --from 8 --to 22
```

Without `--install` it only shows the plan. `--uninstall` removes it. The log is `data/logs/search.log`. A time
missed while the Mac slept runs once after it wakes. For reference, the generated job looks like this:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.career-console.search</string>
  <key>ProgramArguments</key><array>
    <string>/Users/YOU/.local/bin/uv</string><string>run</string>
    <string>--project</string><string>/path/to/career-console</string>
    <string>career-console</string><string>search</string>
  </array>
  <key>StartCalendarInterval</key><array>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>0</integer></dict>
  </array>
</dict></plist>
```

Times are the Mac's local time (Israel time on a Mac set to Israel).

## Not done yet

- Fit and gaps against your CV are not assessed (no AI by design); "Matches" shows which search terms matched.
- Decisions "same / different requisition" in the review queue are recorded but do not yet change matching.
- Potential Leads (LinkedIn posts, recruiters) are a separate, manual list: you paste the post link and what it
  says; the console never opens LinkedIn itself. The tab offers LinkedIn searches you run yourself (hiring posts
  in the last 24 h, recruiters in Israel by network degree).
