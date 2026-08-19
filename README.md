# ai-polls-agent

A daily Israeli yes/no poll, assembled by a human from a machine-built shortlist.

Every morning a cron job reads Hebrew, Russian and English Israeli news, narrows
the day down to 10–20 candidate stories, and stops. You open `/polls` in
ai-home-hub, pick at most one Russian story and one English story, mark each
`important` or `funny`, and press **Start creating chats**. That creates one
Kvasir echo per language from a template, with the prompt already filled in.
You edit the echo on Quizly, create its public quiz, and press **Finalize** —
which publishes the poll to the stable `/daily-israel-polls-en` (or
`/daily-israel-polls-ru`) page and
announces it on Telegram.

Nothing is created or published without a human pressing a button.

## Pipeline

```
collect ─ 19 sources (rss / site index / public Telegram)
   │
   ├─ deterministic filter   stale, empty, index pages, tracking-only URLs
   ├─ exact dedupe           canonical URL, content hash
   ├─ near dedupe            normalized-title similarity inside a time window
   ├─ Gemini prefilter       cheap keep/drop + interesting/funny scores
   │                         (never sees article bodies)
   ├─ Claude selection       one global shortlist of 10–20, all languages
   └─ enrich                 fetch bodies, translate Hebrew finalists
                             (Russian is translated only on demand, when the
                              operator starts it in the English slot)
                             → SQLite → /polls
```

Everything after that is operator-driven: **select → start → edit on Quizly →
finalize**.

## Setup

This project shares the **`ai-news` conda environment** with `ai-news-agent`
and `ai-home-hub` — there is no per-project environment. The scripts call its
interpreter by absolute path (the `ai-news-agent/scripts/run_no_conda.sh`
convention), so cron needs no conda hook:

```
/home/anton/miniconda3/envs/ai-news/bin/python
```

Override for any script with `PYTHON_BIN=/path/to/python`.

```bash
# dependencies (already satisfied in the shared env; google-genai was added for this repo)
/home/anton/miniconda3/envs/ai-news/bin/pip install -r requirements.txt

cp .env.example .env      # fill it in
chmod 600 .env

bash scripts/check.sh            # offline checks
bash scripts/check.sh --remote   # also verify the Kvasir template
```

`.env` holds every credential; nothing is read from the process environment,
which is why the scripts here never `source .env` the way the sibling repos do.
Non-secret configuration lives in `config/settings.yaml` and
`config/sources.yaml`.

## Running

```bash
bash scripts/run.sh              # one collection pass (what cron runs)
bash scripts/run.sh --dry-run    # collect + filter only; no LLM calls, no writes
bash scripts/test.sh             # 116 tests, no network
/home/anton/miniconda3/envs/ai-news/bin/python -m src.main --render-only
```

Cron (the machine's clock is not assumed to be Israeli time):

```cron
CRON_TZ=Asia/Jerusalem
15 07 * * * cd /home/anton/git/ai-polls-agent && bash scripts/run.sh >> /home/anton/logs/ai-polls-agent-cron.log 2>&1
```

Two runs cannot overlap: `scripts/run.sh` takes an `flock`, and
`src/scheduler_entry.py` takes one independently.

## The operator page

Mounted by ai-home-hub at `/polls` (already registered in
`ai-home-hub/config/hub.yaml`).

| Route | Purpose |
|---|---|
| `GET /` | candidates, slots, generated chats, history |
| `GET /collected` · `/collected/p/<n>` | table of **every** collected story, with "add to shortlist" |
| `GET /api/day/current` · `GET /api/history` | the same state as JSON |
| `POST /api/select` | set/clear one slot — **409** once *that language* is locked |
| `POST /api/default-categories` | use the template's own category list for one slot |
| `POST /api/categories` | edit one generated echo's categories (prompt included) |
| `POST /api/add-candidate` | promote a collected story onto today's shortlist |
| `POST /api/start-generation` | lock one language's selection, then create its echo |
| `POST /api/retry-generation` | retry one language after an error |
| `POST /api/reset-generation` | discard one language's generation and unlock its selection — never refuses |
| `POST /api/finalize` | publish one finished language |
| `POST /api/close` | retire a published poll and free that language's slot |
| `POST /api/retry-telegram` | re-send only the announcement |
| `POST /api/re-render` | re-render from current DB state |

A collection run voids the day's pending selection — story, tone and category
choice — for any language that is not locked yet, because the shortlist those
picks referred to has just been replaced. A locked language keeps everything.

The two languages are independent paths. Starting, resetting or finalizing
Russian leaves the English slot fully editable, so you can take one language all
the way to a published quiz and only then start the other. Once a language is
published, **Close** retires its panel and frees the slot — a second poll the
same day is allowed; one per language per day is the intended rhythm, not a
restriction.

Generation and finalization run synchronously inside the request (up to ~a
minute); the hub's threading server keeps other tabs responsive, and SQLite
holds the authoritative state either way.

`GET /` re-renders from SQLite on every request, so **Refresh** is all you need —
there is no separate re-render button. (`POST /api/re-render` still exists for
refreshing the on-disk HTML outside the browser.)

## Prompts

The instructions this repo sends to models are editable text, not code:

| File | Model call | Placeholders |
|---|---|---|
| `config/prompts/prefilter.txt` | Gemini — screening | — |
| `config/prompts/translate.txt` | Gemini — HE→EN | — |
| `config/prompts/selector.txt` | Claude — the day's shortlist | `{{ min_items }}`, `{{ max_items }}` |
| `config/prompts/quiz_designer.txt` | Claude — quiz design | `{{ language_name }}`, `{{ persona }}` |
| `config/prompts/categories_important.txt` | Claude — participant categories | `{{ language_name }}`, `{{ min_items }}`, `{{ max_items }}`, `{{ title }}`, `{{ summary }}`, `{{ question }}`, `{{ party_defaults }}` |
| `config/prompts/categories_funny.txt` | Claude — participant categories | same |
| `config/settings.yaml` | personas substituted into the echo prompt | `personas.<tone>.<lang>` |

`src/prompts.py` loads them and refuses to send a prompt with an unfilled
placeholder; `bash scripts/check.sh` verifies all four are present and complete.

Separately, the **echo prompt** is the Kvasir template's own text asset: cloned
in S3 to `s3://kv-courses/<course>/text/<echo id>[.ru].txt` and filled there.
The template object is never written to.

## Categories

A category is **not** an article topic — it is a person who could answer the
poll from their own perspective, so the chat can compare how different groups
see the same question:

```
important                          funny
You are a reservist                You already checked apartment prices abroad
You employ reservists              You panic when Waze adds seven minutes
You run a small business           You forwarded this to the family chat
You are a Likud voter              You argue with parking apps more than with people
You are an undecided voter
```

They are generated per poll from the **final yes/no question** (not the article
title), in the target language, with a separate prompt per mode. Party
categories are offered only when the story is genuinely political — the party
names come from the deployed template's own
`{{CATEGORIES DEFAULT={"ru": […], "en": […]}}}` payload.

Output is validated before it ships: taxonomy labels ("Politics", "Общество"),
duplicates, over-long entries, URLs and multi-sentence text are dropped, and the
result is capped. If fewer than four survive, a curated fallback library keyed by
domain (housing, transport, security, education, health, consumer) tops it up
rather than padding with filler. Categories, and whether parties or the fallback
were used, are stored on the echo row and shown in `/polls`.

Tuning lives in `config/settings.yaml` under `category_generation`, `parties`,
`political_keywords`, `stakeholder_fallbacks` and `funny_fallbacks`.

## Slot rules

|  | RU slot | EN slot |
|---|---|---|
| Russian story | ✅ | ✅ translated when you press Start |
| English story | ✗ | ✅ |
| Hebrew story | ✅ | ✅ *after* `title_en` + `short_en` exist |

The poll is written in the target language from whatever the source says, and
one story may carry both languages of a day (two separate echoes, one per target
course).

Nothing is translated in bulk. Hebrew finalists get their English rendering
during collection, because the EN slot's Hebrew stories are picked and designed
from it. A Russian story chosen for the EN slot is translated **on demand**, when
Start is pressed — one call, for that one story. The RU slot never translates
anything.

Either slot may be left empty; a Russian-only day never touches the English page.

## What gets published where

- **Stable pages** — `kvasir_proto/src/html/kvasir.pub/daily-israel-polls-en.html`
  and `daily-israel-polls-ru.html`. `kvasir.pub/` and `quizly.pub/` are separate
  sites, not mirrors (`deploy.sh` syncs them to different buckets), so the polls
  appear on kvasir.pub — that is the host in `publishing.public_url_*` and in the
  Telegram announcement. The publisher adds one entry at the top of the region
  between `<!-- POLLS:START -->` and `<!-- POLLS:END -->` and never removes one:
  a second poll on a day that already has one is added above it. Repeated
  Finalize clicks do not append twice, because the page write is skipped once
  that echo and scroll have a publish event. `publishing.max_entries_per_page`
  caps the page (60); set it to 0 to keep every poll ever published. These files
  ship with the rest of the site, so the poll goes live with the next deploy.
- **Telegram** — off by default. Set `publishing.telegram_enabled: true` in
  `config/settings.yaml` once the bot is an admin in both channels and you have
  done a live test in private channels.

## Safety properties worth knowing

- **The selection lock is taken and committed before any model or Kvasir call**,
  in a `BEGIN IMMEDIATE` transaction. Eight simultaneous Start requests produce
  exactly one winner (there is a test for this).
- **Echo creation is idempotent per `(day, language)`.** The component id is
  persisted the instant it exists, so a failure during the S3 step is resumed by
  the retry rather than creating a second echo. Only **Close** ends an echo's
  claim on its `(day, language)` slot.
- **The template prompt object is never written to.** The build refuses to
  proceed if the destination key would equal the source key.
- **Finalize refuses to guess.** Zero or more than one public `scroll-quiz` for
  the echo means nothing is published and the error tells you what to fix.
- **Telegram is keyed on `{day}:{language}:{echo_id}:{scroll_id}`.** A second
  Finalize click cannot repost. If the page publishes and Telegram fails, the
  page stays up and only the announcement is retried.

## Manual steps this repo does not do for you

1. Add the Telegram bot to both channels as an admin that can post, and fill
   `TELEGRAM_*` in `.env`.
2. Run one full live pass with `telegram_enabled: false`.
3. Run one live test against private/test channels before enabling production.
4. Install the crontab entry.

## Related repositories

- `ai-news-agent` — the collection/SQLite/hub patterns this project follows.
- `ai-home-hub` — hosts the `/polls` UI.
- `kvasir_proto` — Kvasir behavior (read-only reference) and the stable pages.
