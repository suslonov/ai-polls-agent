# AI Polls Agent

## Objective

Produce a daily operator-curated Israeli yes/no poll workflow from current news:
collect → shortlist → operator picks → Kvasir echo → operator edits → publish.

## Hard rules

- Python only.
- SQLite is the source of truth. Rendered HTML is always a projection of it.
- Daily collection is cron-driven and does discovery only: it never creates a
  Kvasir echo and never publishes anything.
- Echo creation and publishing happen only after an explicit operator action in
  ai-home-hub (`/polls`).
- Selection is editable until generation starts, then permanently locked for
  that day. `POST /api/select` returns 409 afterwards.
- Previous days are read-only history.
- Up to one RU selection and one EN selection per day; either may be empty.
- Slot eligibility: RU slot accepts `ru` and `he`; EN slot accepts `en`, `ru`
  and `he`. One story may fill both slots on the same day (two separate echoes,
  one per target course).
- HE is eligible for the EN slot only after English translation (`title_en`
  **and** `short_en` must exist), because collection already produced it. RU in
  the EN slot has no precondition: it is translated **on demand** when the
  operator presses Start, for that one story only. The RU slot never triggers a
  translation — the Russian quiz is written from the Russian or Hebrew original.
- Every selected item must be marked `important` or `funny`.
- Do not create party-preference quizzes.
- Gemini is the cheap layer only: batch prefilter and HE→EN translation.
- Claude does final news selection and quiz design.
- Never send full article text to the prefilter — title/dek/snippet only.
- Credentials come from the repository `.env` via `dotenv_values()`. Never
  `load_dotenv()`, never `os.environ`/`os.getenv` for secrets.
- Never use implicit boto3 credentials — build the session from `.env` values.
- Never write directly to Kvasir SQL. Create/update echoes through the
  `kv2_course` Lambda; read scrolls through `kv2_text`.
- Clone the prompt via S3 copy and never overwrite the template object.
- Model instructions live in `config/prompts/*.txt`, loaded by `src/prompts.py`
  — never inline in Python. Placeholders are `{{ name }}`; an unfilled one is an
  error, not a prompt sent with a hole in it.
- A CATEGORY is a participant identity ("You are a reservist"), never an article
  topic ("Politics"). Categories are generated per poll from the **final yes/no
  question**, with a separate prompt per mode, and validated before use.
- Party categories only on genuinely political stories, and never in `funny`
  fallback. Party names come from the template's own `DEFAULT=` payload first.
- Categories stay editable after generation: `workflow.update_categories()`
  rewrites the `"categories": [ … ]` array of the prompt already in S3 (never a
  re-fill from the template, which would discard the operator's editor changes)
  and the `categories_json` column. An operator's list is taken as written — the
  generator's validation judges a model's output, not a human's.
- Each slot card carries "use default categories, don't invent": with it on,
  generation substitutes the template's `DEFAULT=` payload verbatim and makes no
  model call for categories. The party gate does not apply to it — it is an
  explicit per-poll operator choice, not the machine inventing a lineup. A
  template with no payload falls back to the curated pool, still without a model.
- Only Hebrew is translated (HE→EN, and only for shortlisted stories). Russian
  fills the RU slot from the Russian original and is never sent to the
  translator during collection — only `workflow._translate_on_demand()` does,
  for the single story picked for the EN slot.
  **No English rendering of a Russian story may reach the card**,
  and the selector is the one that keeps producing them:
  `selector.clean_topic()` drops a topic that is a restated headline rather than
  a short filing label, and `selector.clean_why()` drops a `why_candidate` with
  no Cyrillic on a Russian story. `render.candidate_view()` shows `title_en`
  only for Hebrew. The operator-added marker is written in the story's language.
- A collection run voids the day's pending selection (story, tone, category
  choice) for every language that is not locked: the shortlist it replaced is
  what those picks referred to. Locked languages keep everything.
- Changing a slot's story clears that slot's "use default categories" tick;
  changing only the tone does not.
- PERSONA (how the chat behaves) and CATEGORIES (who answers) are different
  fields — do not merge them.
- The prompt template's markers may carry a JSON default:
  `{{CATEGORIES DEFAULT={"ru": [...], "en": [...]}}}`. Marker parsing is
  brace-aware; the CATEGORIES value is substituted as JSON **array contents**
  because the template wraps it in `"categories": [ … ]`.
- Tests must never reach a model API — `tests/conftest.py` blocks the calls, so
  monkeypatch them per test.
- The clone inherits `details.allow_donations` from the template and every
  update sends `author_id` (`KVASIR_USER` from `.env`). kv2_course reads
  `component_record["author_id"]` unguarded when that flag is set, so a record
  with donations on and no author id is an HTTP 500 — `build_component_record`
  turns donations off rather than send such a record.
- kv2_course validates the course only on the **update** path
  (`check_component_access`); creating a component accepts any `course_id`. So
  verify the target course with `echo_builder.check_course_access()` *before*
  creating anything — otherwise a wrong `KVASIR_COURSE_*` yields an orphan
  component and a 404 from the second update. The second update is idempotent
  and is retried once before it is reported.
- The clone also copies the template's `title_picture` asset object. A missing
  picture is logged and skipped; it must never cost the whole echo.
- Every published string — page entries, Telegram text, echo fields and the
  prompt written to S3 — goes through `text_utils.normalize_dashes()`.
- Languages are independent paths: locks, generation, reset and finalize are all
  per language, and closing a finished poll frees that language's slot for
  another one the same day. One poll per language per day is the intended
  rhythm, not a constraint the code enforces (`closed_at` + a partial unique
  index on the open row).
- Reset discards local generation state only and never refuses — `/polls` is an
  operator tool, so no confirmation dialog and no "you may not" message.
  Resetting a published language is allowed: it drops that language's publish
  events (returned as `dropped_publish_events`) while the page entry and any
  Telegram message stay as they were. Kvasir components are never deleted —
  orphaned drafts are reported, not removed.
- The echo description contains exactly one `<a>` — the original-news link the
  application builds itself. Model text is HTML-escaped before concatenation.
- Finalize only when exactly one public `scroll-quiz` exists for the echo.
  Zero or many → actionable error, publish nothing.
- Update only the selected language's stable page (`/daily-israel-polls-en` or
  `/daily-israel-polls-ru`), and only ever by *adding* an entry: the page is
  append-only, so a second poll on a day that already has one joins it above
  rather than replacing it. Repeated Finalize clicks are stopped earlier, by the
  publish event, not by rewriting the page.
- Telegram publishing is idempotent, keyed on
  `{day}:{language}:{echo_id}:{scroll_id}`.
- One source failure must never abort collection.
- Render the UI from structured DB state using Jinja2; never patch generated
  HTML in place.
- Anything injected into a `<script>` block must go through `|tojson`. Plain
  `{{ value }}` is HTML-escaped by autoescape, which is a JS syntax error that
  silently disables every handler on the page.
- Never name a template context key `items`, `keys` or `values`: Jinja resolves
  `state.items` to the dict method, not your data.
- Add tests for every state transition and every external side effect.

## Layout

```
src/
  main.py            CLI (--dry-run, --render-only)
  scheduler_entry.py cron entry point, holds an flock
  pipeline.py        the daily collection pass
  workflow.py        operator-triggered steps (generate / finalize / retry)
  hub_module.py      PollsModule for ai-home-hub
  settings.py        config/*.yaml loading      secrets.py  .env loading
  prompts.py         config/prompts/*.txt loading + rendering
  models.py          pydantic models            db.py       SQLite
  collectors/        rss, site_index, telegram_public
  dedupe.py extraction.py
  prefilter.py       Gemini: screening + HE→EN translation
  selector.py        Claude: the day's shortlist
  quiz_designer.py   Claude: title/description/question + description HTML
  category_designer.py Claude: participant categories + validation/fallbacks
  kvasir_client.py   kv2_course / kv2_text / S3
  echo_builder.py    template clone + prompt filling
  scroll_lookup.py   the one public scroll-quiz
  publisher.py       stable poll pages      telegram_publish.py
  render.py          view-model + Jinja2
```

## External state this repo touches

| What | Where | Written by |
|---|---|---|
| Candidates, workflow, echoes, publish events | `~/polls_data/state.db` | pipeline, workflow |
| Operator page | `~/polls_data/rendered/index.html` | render |
| Kvasir echoes | `kv2_course` Lambda | echo_builder |
| Echo prompts | `s3://kv-courses/{course}/text/{echo}[.ru].txt` | kvasir_client |
| Stable poll pages | `kvasir_proto/src/html/kvasir.pub/daily-israel-polls-{en,ru}.html` | publisher |
| Announcements | Telegram channels from `.env` | telegram_publish |

The stable pages are updated between the `<!-- POLLS:START -->` and
`<!-- POLLS:END -->` markers. Keep those markers; everything else on the page is
hand-maintained.

## Model choices

`config/settings.yaml` pins them. `claude-sonnet-4-6` for selection and quiz
design (aligned with ai-news-agent), `gemini-3.5-flash-lite` for prefilter and
translation. Change them there, not in code.

## Environment

Use the shared **`ai-news`** conda environment — the same one `ai-news-agent`
and `ai-home-hub` use. Do not create a per-project environment. Scripts call
`/home/anton/miniconda3/envs/ai-news/bin/python` by absolute path (following
`ai-news-agent/scripts/run_no_conda.sh`), overridable via `PYTHON_BIN`.

Unlike the sibling repos' scripts, nothing here sources `.env` into the
environment: secrets must reach the code only through `dotenv_values()`.

## Testing

`bash scripts/test.sh` (or `pytest` in the `ai-news` env) — no network, no API
keys, no AWS. External systems are faked
(`tests/conftest.py::FakeKvasirClient`); model calls are monkeypatched.
