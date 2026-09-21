# Evolution Golf — AI reply drafting

Drafts customer support replies in Evolution Golf's own voice, grounded in how
the team has actually answered ~1,300 past Zendesk tickets, plus live Shopify
order data.

**Nothing auto-sends.** Every draft is reviewed by an agent before it reaches a
customer.

---

## How it works

```
Zendesk history ──► corpus ──► voice & policy guide (human-reviewed)
                      │                    │
 new ticket ──────────┼── similar past ────┤
                      │    replies         ├──► Claude ──► draft ──► agent approves ──► send
 Shopify order ───────┴────────────────────┘
```

Four phases, each usable on its own:

| Phase | What it does | Status |
|-------|--------------|--------|
| **1. Export** | Pull every ticket + comment from Zendesk into a SQLite corpus, stripped of quoted history and redacted of PII. Deployed on Railway. | ✅ built |
| **2. Mine** | Analyse the corpus into a written voice-and-policy guide and a categorised bank of the team's best replies | ⬜ next |
| **3. Draft** | Retrieve similar past tickets + live Shopify order, generate a reply with Claude | ⬜ |
| **4. Deliver** | Zendesk sidebar app (ZAF) for `help@`, Gmail drafts for `online@` | ⬜ |

### Why not fine-tune a model?

1,300 tickets is far too small to fine-tune usefully — it costs money, takes a
retraining cycle for every policy change, and produces a model that has
memorised phrasing without understanding policy. Retrieval plus an explicit,
human-reviewed policy guide gives better answers, updates instantly when a
policy changes, and lets you see exactly which past tickets informed a draft.

---

## Phase 1 — Export (built)

### Where the Zendesk token goes

**Never in GitHub.** A committed secret stays in git history even after the
file is deleted, so the only fix is revoking it. The token belongs in one of
two places, both of which keep it out of the repo:

- **Railway variables** (how this is deployed) — Railway stores them
  encrypted and injects them at runtime. Set them in the service's
  **Variables** tab.
- **A local `.env`** if you ever run it on your own machine. `.env` is
  gitignored.

Get the token from **Admin Center → Apps and integrations → Zendesk API →
Settings → Add API token**.

### Running on Railway

The service is a normal Railway deployment built from the `Dockerfile`:

| Variable | Value |
|---|---|
| `ZENDESK_SUBDOMAIN` | `evolutiongolf` |
| `ZENDESK_EMAIL` | the agent email the token belongs to |
| `ZENDESK_API_TOKEN` | the token — paste in Railway's Variables tab |
| `CORPUS_DB` | `/data/corpus.sqlite3` (set by the Dockerfile) |
| `ADMIN_TOKEN` | optional; needed only to call `/stats` and `/export` |
| `AUTO_EXPORT` | `true` (default) — full export on first boot |

A **volume mounted at `/data`** is required. Without it the corpus is wiped on
every deploy and the service re-exports the whole history each time.

On first boot the service sees an empty corpus and exports the full history in
the background — no manual trigger needed. Watch Railway's deploy logs to
follow it. Later boots skip the export because the volume still holds the
corpus.

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | none | Railway healthcheck |
| `GET /stats` | `ADMIN_TOKEN` | what's in the corpus, and the last export result |
| `POST /export` | `ADMIN_TOKEN` | incremental export; `?full=true` re-walks everything |

If `ADMIN_TOKEN` is unset, `/stats` and `/export` return 403 — an unset secret
means the endpoint is closed, never open.

Schedule `POST /export` (Railway cron, or any scheduler) to keep the corpus
current. It resumes from the saved cursor, so it only fetches what changed.

### Running locally instead

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # fill in the Zendesk values

evogolf verify              # check the credentials work
evogolf export --limit 20   # trial run against 20 tickets first
evogolf export              # full history (resumes if interrupted)
evogolf stats               # what's in the corpus
```

The export is **incremental**. It saves Zendesk's cursor after every page, so
re-running picks up only what changed — safe to put on a daily schedule.
`--full` re-walks everything from the start.

### What gets cleaned

Raw Zendesk bodies carry noise that would actively teach the bot bad habits,
so each comment is stripped of:

- **quoted thread history** (`On 26 Aug 2026, X wrote:`, `>` blocks) — otherwise
  the same text is counted many times over
- **Zendesk notification furniture** (`Open Ticket #1248`, `Requester … Assignee …`)
- **sign-offs** (`Kind regards / Brad`) — kept out so the style guide is built
  from substance rather than from boilerplate

Then PII is redacted: emails, UK phone numbers, postcodes and card-shaped digit
runs become `[EMAIL]`, `[PHONE]`, `[POSTCODE]`, `[CARD]`. **Order numbers and
product references survive on purpose** — they are the context a draft needs.
Turn this off with `REDACT_PII=false` only if you have a specific reason.

### Data protection

The corpus is real customer data and UK GDPR applies:

- `data/` and all `*.sqlite3` files are gitignored — the corpus must never be
  committed to this repo. On Railway it lives on a volume, not in the image.
- PII redaction is on by default
- Before go-live, decide and write down: a retention period for the corpus,
  where it is hosted, and how a deletion request propagates from Zendesk to it
- Your Zendesk DPA covers Zendesk; sending ticket content to Anthropic is a new
  processor and should be reflected in your privacy notice

---

## Phases 2–4 — planned

**2. Mine the corpus.** Cluster tickets by theme (returns, delivery chasing,
wrong item, warranty, sizing, click-and-collect). For each theme, extract the
team's actual practice — return windows, who pays return postage, when a refund
versus replacement is offered, when something escalates — into a written guide.
This guide is reviewed and corrected by you, and becomes the bot's constitution.
Expect this step to surface inconsistencies between agents; that is useful.

**3. Draft generation.** For a new ticket: retrieve the most similar resolved
tickets, pull the live Shopify order (status, tracking, items, refunds), and
prompt Claude with those plus the policy guide. The draft cites which past
tickets and which order it used, so an agent can sanity-check it in seconds.

**4. Delivery.**
- **Zendesk sidebar app (ZAF)** — a panel in the ticket view with a *Generate
  draft* button that inserts text straight into the reply composer. Backend runs
  on Railway; the app is a small static frontend installed into Zendesk.
- **Gmail drafts on `online@`** — a separate flow. That mailbox is mostly
  supplier and ops mail (Amer Sports, Motocaddy, DPD, Under Armour), not
  customer support, so it needs its own prompt and its own context sources
  rather than the support voice guide.

---

## Layout

```
src/evogolf_support/
  config.py          env/.env loading, Zendesk credentials
  cli.py             evogolf verify | export | stats
  zendesk/
    client.py        API client: auth, 429 backoff, cursor pagination
    export.py        full/incremental export orchestration
  corpus/
    clean.py         quote/footer/signature stripping + PII redaction
    store.py         SQLite schema and upserts
  api/
    app.py           FastAPI service: health, stats, export trigger
Dockerfile           Railway build
railway.json         healthcheck + restart policy
tests/               21 tests, no network required
```

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

The suite mocks the Zendesk API, so it runs without credentials.
