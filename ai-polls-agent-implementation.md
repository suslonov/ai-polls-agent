# AI Polls Agent — implementation instructions for Claude Code

**important: this plan is created by chat GPT, implement it reasonably according to the repos structures and logic you see**

## 0. Manual actions, constants to fill, and decisions to make before implementation

### 0.1 Manual actions

- [x] Create GitHub repository `ai-polls-agent`.
- [x] Clone it to the machine that runs `ai-home-hub`, preferably:
  - `/home/anton/git/ai-polls-agent`
- [x] Give Claude Code access to both working trees:
  - `/home/anton/git/ai-polls-agent`
  - `/home/anton/git/ai-home-hub`
- [x] Keep `kvasir_proto` available locally as a **read-only reference** while implementing:
  - `/home/anton/git/kvasir_proto`
- [x] Do **not** modify `kvasir_proto` for this task unless explicitly requested later.
- [-] Create the Conda environment for the new repository.
- [x] Copy `.env.example` to `.env`, fill it, and run:
  - `chmod 600 .env`
- [x] Choose or create the Kvasir course where generated echoes will live.
- [x] Choose one template echo.
- [x] Ensure both template prompt files contain the exact markers:
  - `{{CATEGORIES}}`
  - `{{NEWS_SUMMARY}}`
  - `{{PERSONA}}`
  - `{{LOCALE}}`
- [x] Confirm the AWS Lambda function name/ARN for the deployed `kv2_course`.
- [ ] Add the Telegram bot to both target channels as an administrator with permission to post.
- [ ] Run one complete dry run with Telegram publishing disabled.
- [ ] Run one complete live test using test/private Telegram channels before enabling the production channels.

### 0.2 Secrets: `.env`

All runtime credentials must be read **directly from the repository `.env` file**.

Do not use environment variables as the source of credentials. Do not call `load_dotenv()`. Do not use `os.getenv()` or `os.environ` for secrets.

Use `dotenv_values(repo_root / ".env")`, validate once, and explicitly pass credentials to clients.

Required `.env.example`:

```dotenv
# LLMs
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=

# AWS
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=

# Kvasir identity used in the synthetic API Gateway authorizer event.
# This must be a Cognito/user "sub" that has author/admin access to TARGET_COURSE_ID.
KVASIR_USER_SUB=

# Telegram publishing
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHANNEL_EN=
TELEGRAM_CHANNEL_RU=
```

Optional Telegram reader credentials are needed **only** if public `t.me/s/...` pages are insufficient and a Telethon fallback is explicitly enabled:

```dotenv
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
```

`.env` must be in `.gitignore`.

### 0.3 Non-secret constants: `config/settings.yaml`

Start with this structure and replace every `FILL_ME`:

```yaml
app:
  timezone: "Asia/Jerusalem"
  db_path: "~/polls_data/state.db"
  render_path: "~/polls_data/rendered/index.html"
  log_dir: "~/logs"
  history_days_in_ui: 30

schedule:
  # Informational; cron itself is installed manually.
  local_time: "FILL_ME"

collection:
  lookback_hours: 30
  max_items_per_source: 30
  max_candidates_before_prefilter: 250
  prefilter_keep: 45
  final_candidates_min: 10
  final_candidates_max: 20
  max_article_chars_for_selector: 800
  max_article_chars_for_enrichment: 3500

models:
  # Cheap first-stage classifier.
  prefilter: "gemini-3.5-flash-lite"

  # Keep this aligned with ai-news-agent unless deliberately changed.
  selector: "claude-sonnet-4-6"
  quiz_designer: "claude-sonnet-4-6"

kvasir:
  aws_region: "us-east-1"
  kv2_course_lambda_name: "FILL_ME"
  courses_bucket: "kv-courses"

  target_course_id: FILL_ME
  template_echo_id_en: FILL_ME
  template_echo_id_ru: FILL_ME

  # Creation starts as raw so it is not accidentally published before manual editing.
  initial_component_status: "raw"

  echo_editor_base_url: "https://quizly.pub/echo-edit?id="
  scroll_quiz_base_url: "https://quizly.pub/scroll-quiz"

publishing:
  quizly_web_bucket: "FILL_ME"
  cloudfront_distribution_id: "FILL_ME_OR_EMPTY"

  # These must map to the externally visible routes /today and /today_ru.
  today_object_key_en: "FILL_ME"
  today_object_key_ru: "FILL_ME"

  public_url_en: "https://quizly.pub/today"
  public_url_ru: "https://quizly.pub/today_ru"

  telegram_enabled: false

categories:
  en:
    - society
    - everyday life
    - government and civic life
    - security
    - economy
    - consumer
    - transport
    - education
    - health
    - technology
    - science
    - environment
    - culture
    - media
    - sport
    - bureaucracy
    - weird news
  ru:
    - общество
    - повседневная жизнь
    - государство и гражданская жизнь
    - безопасность
    - экономика
    - потребительские темы
    - транспорт
    - образование
    - здоровье
    - технологии
    - наука
    - экология
    - культура
    - медиа
    - спорт
    - бюрократия
    - странные новости

personas:
  important:
    en: >
      You are an editor of a concise Israeli public-opinion poll.
      Frame a consequential yes/no question rooted in the supplied news.
      Both answers must remain plausible. Do not turn it into a party-preference question.
    ru: >
      Ты редактор короткого израильского опроса общественного мнения.
      Сформулируй содержательный вопрос с ответом да/нет, основанный на данной новости.
      Оба ответа должны оставаться осмысленными. Не превращай вопрос в опрос о партиях.
  funny:
    en: >
      You are an editor of a dry, playful Israeli daily poll.
      Use the supplied real news as the premise and make the yes/no framing amusing,
      but do not invent facts and do not turn it into a party-preference question.
    ru: >
      Ты редактор короткого ироничного израильского опроса дня.
      Используй реальную новость как основу, сделай формулировку да/нет забавной,
      но не выдумывай факты и не превращай вопрос в опрос о партиях.
```

### 0.4 Decisions that must be explicit

1. **Cron time**  
   - Will be set manually. It should be clear which script to run by cron.

2. **Kvasir target course**  
   Take from .env The user represented by `KVASIR_USER_SUB` must be able to create components in it.

3. **Template echoes**  
   Take from .env, it is the same for both languages, don't translate it, just insert needed parts.

4. **Stable `/today` publishing mechanism**  
   Write and update today.html and today_ru.html in kvasir_proto/src/html/kvasir.pub/

5. **Public scroll lookup**  
   Before implementing finalization, inspect the current `kvasir_proto` scroll implementation and identify the supported callable path for listing a component's public scrolls. Encapsulate it in one adapter. Do not invent an API action.

6. **Multiple public quizzes in one echo**  
   Default behavior for this project: finalization succeeds only if exactly one public `scroll-quiz` exists for that echo. If zero or more than one exist, show an actionable error and publish nothing.

7. **One-language day**  
   If only RU was selected, update only `today_ru.html`. If only EN/HE was selected, update only `today.html`. Do not erase or replace the other language's existing page.

---

## 1. Repositories and existing code to reuse

Claude Code must inspect these files before writing new code.

### `ai-news-agent`

Use it as the reference for collection, normalization, SQLite, deduplication, scheduling, logging, and hub integration:

```text
ai-news-agent/
  src/db.py
  src/models.py
  src/dedupe.py
  src/extraction.py
  src/pipeline.py
  src/settings.py
  src/scheduler_entry.py
  src/hub_module.py
  config/sources.yaml
  CLAUDE.md
```

Do not copy AI-news-specific rules blindly. Reuse the patterns.

### `ai-home-hub`

The new project must be mounted as a normal hub module.

Relevant files:

```text
ai-home-hub/
  hub/module.py
  hub/loader.py
  hub/server.py
  config/hub.yaml
```

The module constructor must be compatible with the existing loader:

```python
PollsModule(prefix: str, config: dict, repo_path: Path)
```

The module must implement:

```python
handle(
    method: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
) -> tuple[int, str, bytes]
```

### `kvasir_proto`

Use this repository only as the source of truth for Kvasir behavior.

At minimum inspect:

```text
src/lambda_v2/kv2_course/app/lambda_function.py
src/html/quizly.pub/js/echo-edit.js
src/html/quizly.pub/js/common.js
src/html/quizly.pub/js/scroll-quiz.js
src/lambda_v2/layer_db/python/common_sql.py
```

Important existing behavior:

- `kv2_course` receives a JSON body with an `action`.
- Echo creation/update uses:
  - `action = "component_update"`
  - `component_record = {...}`
- Creating a component means calling `component_update` without `component_record.id`.
- The Lambda returns `component_id`.
- The echo editor saves the prompt text only after an ID exists, then performs a second component update to persist the text asset.
- Echo text assets are stored in the Kvasir courses bucket under the component's `text/` path.
- The external editor URL for this project is:
  - `https://quizly.pub/echo-edit?id={component_id}`

Do not bypass `kv2_course` by writing directly to Kvasir SQL.

The production script will write to local kvasir_proto/src/html/kvasir.pub/ today.html and today_ru.html.

---

## 2. Recomended final repository structure

Create:

```text
ai-polls-agent/
├── .gitignore
├── .env.example
├── CLAUDE.md
├── README.md
├── requirements.txt
├── config/
│   ├── settings.yaml
│   └── sources.yaml
├── data/
│   └── .gitkeep
├── scripts/
│   ├── run.sh
│   └── check_config.py
├── src/
│   ├── __init__.py
│   ├── main.py
│   ├── settings.py
│   ├── secrets.py
│   ├── models.py
│   ├── db.py
│   ├── dedupe.py
│   ├── extraction.py
│   ├── pipeline.py
│   ├── prefilter.py
│   ├── selector.py
│   ├── quiz_designer.py
│   ├── kvasir_client.py
│   ├── echo_builder.py
│   ├── scroll_lookup.py
│   ├── publisher.py
│   ├── telegram_publish.py
│   ├── render.py
│   ├── scheduler_entry.py
│   ├── hub_module.py
│   └── collectors/
│       ├── __init__.py
│       ├── base.py
│       ├── rss.py
│       ├── site_index.py
│       └── telegram_public.py
├── templates/
│   ├── hub/
│   │   └── index.jinja2
│   └── public/
│       └── today_redirect.html.jinja2
└── tests/
    ├── test_dedupe.py
    ├── test_db_state_machine.py
    ├── test_prefilter.py
    ├── test_selection_lock.py
    ├── test_description_html.py
    ├── test_kvasir_event.py
    ├── test_echo_clone.py
    ├── test_scroll_lookup.py
    ├── test_publish_idempotency.py
    └── test_hub_module.py
```

Use Python only.

Recommended dependencies:

```text
anthropic
boto3
beautifulsoup4
feedparser
google-genai
httpx
jinja2
pydantic
python-dotenv
PyYAML
tenacity
trafilatura
```

Add `telethon` only if the optional authenticated Telegram fallback is actually enabled.

---

## 3. News sources to scan

All sources must be configuration-driven in `config/sources.yaml`.

Each source record should support:

```yaml
- id:
  enabled:
  language: he|ru|en
  type: rss|site_index|telegram_public
  name:
  urls: []
  priority: 1
  authoritative: true
  tags: []
```

The collector interface must return one normalized model regardless of source type.

### 3.1 Hebrew sources

Start with:

1. **Ynet**
   - `https://www.ynet.co.il/`
   - RSS should be preferred where available.
   - Ynet has an RSS index/documentation; discover the live feed rather than freezing an obsolete feed URL if redirects have changed.

2. **N12 / Channel 12**
   - `https://www.n12.co.il/`
   - `https://www.mako.co.il/news-dailynews`
   - Useful for both hard news and lighter Israeli stories.

3. **Kan News**
   - `https://www.kan.org.il/content/kan/news/`

4. **Walla News**
   - `https://news.walla.co.il/`
   - RSS directory:
     - `https://www.walla.co.il/rss`

5. **Maariv**
   - `https://www.maariv.co.il/news`

6. **Israel Hayom**
   - `https://www.israelhayom.co.il/news`

7. **Haaretz**
   - `https://www.haaretz.co.il/news`
   - Use headline/dek metadata when the article body is unavailable.

Optional secondary Hebrew discovery sources can be added later, but the first implementation should work with the list above.

### 3.2 Russian-language Israeli sources

Start with:

1. **NEWSru.co.il**
   - `https://www.newsru.co.il/`

2. **Vesty**
   - `https://www.vesty.co.il/`

3. **9 Channel**
   - `https://www.9tv.co.il/`

4. **IsraelInfo**
   - `https://www.israelinfo.co.il/`

5. **Cursor**
   - `https://cursorinfo.co.il/`
   - Lower priority than the sources above; use mainly for additional discovery.

### 3.3 English-language Israeli sources

Start with:

1. **The Times of Israel**
   - `https://www.timesofisrael.com/latest/`

2. **The Jerusalem Post**
   - `https://www.jpost.com/`

3. **Ynetnews**
   - `https://www.ynetnews.com/`

4. **i24NEWS English**
   - `https://www.i24news.tv/en`

5. **Haaretz English**
   - `https://www.haaretz.com/`
   - Headline/dek-only collection is acceptable when full text is unavailable.

N12 English may be added as a secondary source:

- `https://www.mako.co.il/news-specials/n12_english_edition`

### 3.4 Telegram sources

Telegram is an additional discovery layer, not a requirement for every story.

Implement a `telegram_public` collector against public web views:

```text
https://t.me/s/<channel>
```

Start with:

- `https://t.me/s/ynetalerts`
- `https://t.me/s/NEWSruIsrael`
- `https://t.me/s/israelhayomofficial`

Rules:

- Prefer the publisher's normal article URL if the Telegram post contains one.
- Store the Telegram post URL as `discovery_url`.
- Store the article URL as the canonical `url` when available.
- Do not create a duplicate candidate when the same story was already collected from the publisher site.
- If a Telegram post has no external article URL, it may be used as a candidate only if it contains enough text to understand the story.
- Keep an `authoritative` flag in source configuration.
- Add more Telegram channels only after manually confirming the exact channel identity.
- Do not make the entire daily run fail when Telegram is unavailable.

---

## 4. Normalized news model

Use a Pydantic model similar to `ai-news-agent`, with at least:

```python
class NewsItem(BaseModel):
    id: int | None = None
    run_id: int | None = None
    source_id: str
    source_name: str
    source_type: str
    source_language: Literal["he", "ru", "en"]

    title_original: str
    title_en: str | None = None

    url: str
    canonical_url: str
    discovery_url: str | None = None

    published_at: datetime | None = None
    fetched_at: datetime

    dek_original: str | None = None
    snippet_original: str | None = None
    full_text: str | None = None

    short_en: str | None = None

    content_hash: str
    duplicate_group: str | None = None

    prefilter_keep: bool | None = None
    prefilter_relevance_score: float | None = None
    prefilter_interesting_score: float | None = None
    prefilter_funny_score: float | None = None

    selector_rank: int | None = None
    selector_interesting_score: float | None = None
    selector_funny_score: float | None = None
    topic: str | None = None
    final_candidate: bool = False

    first_seen_at: datetime
    last_seen_at: datetime
```

For Hebrew final candidates:

- retain the Hebrew original,
- create `title_en`,
- create `short_en` of approximately one sentence,
- show both in the UI.

For English final candidates:

- `title_en` may equal `title_original`,
- `short_en` is still useful.

For Russian final candidates:

- no English translation is required for the operator UI unless it is convenient.

---

## 5. SQLite schema

Use SQLite as the source of truth. Do not store workflow state only in rendered HTML or memory.

### 5.1 `runs`

Minimum fields:

```text
id
run_date
status
started_at
finished_at
collected_count
prefiltered_count
final_candidate_count
error
```

`status`:

```text
running
complete
failed
```

### 5.2 `source_fetches`

```text
id
run_id
source_id
started_at
finished_at
status
http_status
items_found
error
```

### 5.3 `news_items`

Use the normalized fields above.

Indexes:

```text
UNIQUE(canonical_url)
INDEX(published_at)
INDEX(run_id)
INDEX(final_candidate)
INDEX(source_language)
INDEX(duplicate_group)
```

If the same canonical URL appears on another daily run, update `last_seen_at`; do not insert another physical story row unless the existing `ai-news-agent` migration pattern makes run/item association cleaner through a join table.

### 5.4 `daily_workflow`

One row per local date:

```text
day PRIMARY KEY
status
ru_item_id NULL
en_item_id NULL
ru_tone NULL
en_tone NULL
selection_locked_at NULL
generation_started_at NULL
generation_finished_at NULL
created_at
updated_at
```

Allowed workflow status:

```text
ready
generating
editing
partially_finalized
finalized
generation_failed
```

Allowed tone:

```text
important
funny
```

`en_item_id` may point to a source-language `en` **or `he`** item.

`ru_item_id` must point to a source-language `ru` item.

### 5.5 `echoes`

```text
id
day
target_language       # ru|en
news_item_id
tone                   # important|funny

kvasir_course_id
kvasir_echo_id
template_echo_id

title
description_html
prompt_s3_key
editor_url

picture_suggestions_json

scroll_id NULL
scroll_public_url NULL

status                 # creating|editing|ready_to_publish|published|error
created_at
finalized_at NULL
error NULL
```

Unique index:

```text
UNIQUE(day, target_language)
```

### 5.6 `publish_events`

```text
id
day
target_language
kvasir_echo_id
scroll_id
stable_public_url
quiz_target_url
s3_object_key
page_published_at NULL
telegram_sent_at NULL
idempotency_key
status
error
created_at
updated_at
```

Unique index:

```text
UNIQUE(idempotency_key)
```

Use:

```text
{day}:{target_language}:{kvasir_echo_id}:{scroll_id}
```

as the idempotency key.

---

## 6. Daily cron pipeline

The cron job does **news discovery and candidate preparation only**.

It must not create Kvasir echoes and must not publish anything at this stage.

Pipeline:

```text
collect
→ normalize
→ canonicalize URLs
→ deterministic filter
→ exact dedupe
→ cross-source near-dedupe
→ cheap Gemini prefilter
→ Claude final selection
→ translate/enrich final Hebrew candidates
→ save final 10–20 candidates
→ render/update hub view
```

### 6.1 Collection window

Use local timezone `Asia/Jerusalem`.

Default:

```text
lookback_hours: 30
```

This allows a morning run to include stories published the previous evening.

Reject obviously stale stories unless the source provides no usable timestamp and the item is newly discovered.

### 6.2 Deterministic pre-filter before any LLM

Use only title/dek/snippet/source/time/URL.

Drop:

- items older than the configured window,
- empty titles,
- obvious navigation pages,
- category/index pages,
- exact duplicate URLs,
- tracking-only URL variants,
- duplicated syndicated titles,
- non-Israel stories with no plausible Israel relevance,
- articles already processed recently unless materially updated.

Canonicalize URLs:

- remove `utm_*`,
- remove Facebook/Google tracking params,
- normalize host casing,
- normalize trailing slash,
- preserve query parameters that are actual article identifiers.

### 6.3 Near-deduplication

Deduplicate **across languages**, not only inside each language.

Use a cheap two-step approach:

1. normalized-title similarity within a time window;
2. only for ambiguous cases, ask the prefilter model for a `story_group_hint`.

Store the best source item as representative but keep source alternatives if useful.

Prefer:

1. original/primary publisher,
2. more complete article,
3. earlier publication timestamp,
4. source with a direct article URL rather than a Telegram-only post.

---

## 7. Token-saving prefilter

Use `gemini-3.5-flash-lite`.

The prefilter must receive only compact data, never full article text.

Per item send approximately:

```json
{
  "id": 123,
  "lang": "he",
  "source": "Ynet",
  "title": "...",
  "dek": "...",
  "snippet": "max 300-500 chars",
  "age_hours": 2.1
}
```

Batch multiple items in one request.

Ask for strict structured output:

```json
{
  "items": [
    {
      "id": 123,
      "keep": true,
      "israel_relevance": 0,
      "interesting_score": 0,
      "funny_score": 0,
      "topic": "",
      "story_group_hint": "",
      "reason": ""
    }
  ]
}
```

Score ranges should be `0..100`.

Prefilter instruction:

- Keep stories that could support an engaging daily yes/no public-opinion quiz.
- Favor concrete events, changes, oddities, dilemmas, policy effects, consumer issues, daily-life friction, technology, culture, bureaucracy, social behavior, and genuinely strange news.
- Do not require stories to be “important”.
- Funny potential and important potential are separate dimensions.
- Do not select by political-party usefulness.
- Avoid repetitive war/liveblog micro-updates unless there is a distinct poll-worthy question.
- Avoid pure sports scores unless there is a broader or amusing question.
- Avoid celebrity filler unless the event genuinely supports a funny/general-interest poll.

Keep approximately the top `45` items, not a fixed quota by language.

However, before proceeding, ensure the candidate pool has enough material for both operator slots when available:

- aim for at least 5 Russian items,
- aim for at least 5 English-eligible items (`en` or `he`).

Do not fabricate low-quality candidates merely to meet those targets.

---

## 8. Claude final candidate selection

The main selector must follow the same general calling conventions/error handling as `ai-news-agent`.

Do not send every collected article to Claude.

Input: only the Gemini-kept set.

For each item send:

- ID,
- language,
- source,
- original title,
- dek/snippet,
- at most `max_article_chars_for_selector`,
- publication time,
- prefilter scores,
- duplicate-group metadata.

Ask Claude to select **10–20 total items across all three source languages together**.

Required output per kept item:

```json
{
  "id": 123,
  "rank": 1,
  "interesting_score": 92,
  "funny_score": 37,
  "topic": "transport",
  "why_candidate": "one short sentence"
}
```

Selection rules:

- The resulting list is global, not 10–20 per language.
- Russian items are candidates for the RU slot.
- English items are candidates for the EN slot.
- Hebrew items are also candidates for the EN slot after short English translation.
- Do not impose equal language quotas.
- Prefer diversity of topics.
- Deduplicate the same underlying event across languages.
- Favor stories that can produce a clear yes/no question.
- Reject stories where a yes/no question would depend on misinformation or excessive context.
- Reject pure party-preference questions.

### 8.1 Full text and enrichment

Only after Claude has chosen the final 10–20:

- fetch article body for those stories if not already available,
- cap clean text at `max_article_chars_for_enrichment`,
- fall back to dek/snippet when the body is unavailable.

For final Hebrew candidates, use the cheap model for:

```json
{
  "title_en": "short English title translation",
  "short_en": "one short English sentence"
}
```

Do not replace the original Hebrew fields.

---

## 9. Operator UI in `ai-home-hub`

Mount at:

```text
/polls
```

The page is served by `PollsModule` from the new repository.

**add to ai-home-hub directly, don't leave it for manual implementation**


**for the debug purposes, allow to do publishing (poll creation step) many times for a selected news**


### 9.1 Current-day layout

Show:

- run date,
- last collection time,
- run status,
- 10–20 candidate cards.

Each card:

- original source language badge,
- source,
- publication time,
- original title,
- English translation below Hebrew title,
- short snippet/summary,
- topic,
- interesting score,
- funny score,
- link to original story.

Provide two independent selection slots:

```text
Russian quiz
English quiz
```

Rules:

- RU slot accepts `source_language in {"ru", "he"}`.
- EN slot accepts `source_language in {"en", "he"}`.
- One item maximum in each slot.
- The user may select only one slot and leave the other empty.
- The user may select both.
- For each selected item the user must choose:
  - `important`
  - `funny`
- Selection can be changed or cleared until the user clicks **Start creating chats**.
- At the instant generation begins, selection becomes immutable.
- Do not permit changing selection after generation has started.

### 9.2 Start button

Button:

```text
Start creating chats
```

Disabled when:

- no item is selected,
- a selected item lacks a tone,
- generation is already running,
- the day's selection has already been locked.

The corresponding server endpoint must atomically:

1. begin an immediate SQLite transaction;
2. verify workflow is still `ready`;
3. validate selected IDs and language eligibility;
4. write selections and tones;
5. set `selection_locked_at`;
6. set status to `generating`;
7. commit;
8. only then start LLM/Kvasir work.

This prevents double-clicks or two browser tabs from creating duplicate echoes.

### 9.3 Generated-chat display

For each successfully generated language show:

- generated title,
- source story,
- tone,
- direct editor link:
  - `https://quizly.pub/echo-edit?id={id}`
- 3–5 title-picture suggestions,
- creation status,
- **Finalize** button.

If the source language is Hebrew, translate insertion to the target language.

The title-picture suggestions are text only. Example form:

```text
- Overhead photo of ...
- Close crop of ...
- Minimal illustration of ...
```

Do not generate an image automatically.

### 9.4 History

Previous days are read-only history.

Show:

- date,
- selected news,
- source,
- tone,
- echo editor link,
- final stable URL,
- publication status,
- Telegram status.

No selection/start/finalize controls for previous dates.

---

## 10. Hub API endpoints

Implement inside `PollsModule`.

Suggested routes:

```text
GET  /
GET  /api/day/current
GET  /api/history

POST /api/select
POST /api/start-generation
POST /api/finalize
POST /api/re-render
```

### `POST /api/select`

Request:

```json
{
  "slot": "ru",
  "item_id": 123,
  "tone": "funny"
}
```

or:

```json
{
  "slot": "en",
  "item_id": null,
  "tone": null
}
```

Reject if selection is already locked.

### `POST /api/start-generation`

No IDs should need to be trusted from the browser at this point. Use the selections stored in `daily_workflow`.

Response:

```json
{
  "ok": true,
  "workflow_status": "editing",
  "echoes": [
    {
      "language": "ru",
      "echo_id": 1234,
      "editor_url": "https://quizly.pub/echo-edit?id=1234",
      "picture_suggestions": []
    }
  ]
}
```

### `POST /api/finalize`

Request:

```json
{
  "language": "ru"
}
```

The server determines the correct echo from SQLite.

Do not accept arbitrary Kvasir component IDs from the browser.

---

## 11. Quiz design step

Run this only after the daily selection is locked.

For each selected language independently call the `quiz_designer` Claude model.

Input:

- target output language (`ru` or `en`),
- selected tone (`important` or `funny`),
- source language,
- original title,
- English translation if applicable,
- article summary/body excerpt,
- original URL,
- configured categories,
- configured persona.

The model must return strict JSON:

```json
{
  "title": "short chat title",
  "description_text": "very short plain-text description",
  "news_summary_for_prompt": "compact factual summary",
  "yes_no_question": "one proposed yes/no question",
  "picture_suggestions": [
    "...",
    "...",
    "..."
  ]
}
```

Requirements:

### Title

- short,
- specific,
- no clickbait padding,
- in the target language.

### Description

- very short,
- plain text from the model,
- do not ask the model to emit HTML.

The application constructs the HTML itself.

English:

```html
DESCRIPTION <a href="ORIGINAL_URL" target="_blank" rel="noopener noreferrer">source</a>
```

Russian:

```html
DESCRIPTION <a href="ORIGINAL_URL" target="_blank" rel="noopener noreferrer">источник</a>
```

There must be **exactly one `<a>` element** in `description_html`.

Escape the model-produced description text before concatenating the anchor.

Validate the URL as `http` or `https`.

### Yes/no question

- grounded in the selected news,
- understandable without reading the entire article,
- suitable for two meaningful positions,
- not “Which party do you support?” or another party-preference question,
- not a factual trivia question,
- not a prediction whose result will simply be known tomorrow unless that framing is deliberately the point,
- if `funny`: the framing may be ironic, but the underlying news fact must stay true,
- if `important`: focus on consequence, policy, behavior, or public preference.

---

## 12. Prompt-template filling

The template echo's prompt text is the base.

Mandatory markers:

```text
{{LOCALE}}
{{CATEGORIES}}
{{NEWS_SUMMARY}}
{{PERSONA}}
```

Fail generation if any mandatory marker is missing. Do not silently append text to a malformed template.

Replacement rules:

### `{{CATEGORIES}}`

Replace with the configured categories for the target language, one item per line.

Example:

```text
society
everyday life
government and civic life
...
```

### `{{NEWS_SUMMARY}}`

Replace with a compact block containing:

```text
News:
<generated factual summary>

Proposed yes/no question:
<generated yes/no question>
```

Use the equivalent Russian labels for the Russian echo.

Do not include the article URL in this prompt block unless the existing template explicitly requires it. The required source link belongs in the echo description.

### `{{PERSONA}}`

Use:

```text
personas.<tone>.<target_language>
```

Do not ask the model to rewrite the configured persona.

After replacement, assert that no mandatory `{{...}}` marker remains.

Store a SHA-256 of the final prompt in the local DB/log for debugging.

---

## 13. Kvasir client

Implement all Kvasir/AWS code in `src/kvasir_client.py`.

Create AWS clients from explicit `.env` credentials:

```python
session = boto3.Session(
    aws_access_key_id=secrets.aws_access_key_id,
    aws_secret_access_key=secrets.aws_secret_access_key,
    aws_session_token=secrets.aws_session_token or None,
    region_name=settings.kvasir.aws_region,
)

lambda_client = session.client("lambda")
s3_client = session.client("s3")
cloudfront_client = session.client("cloudfront")
```

Do not use the implicit boto3 credential chain for this application.

### 13.1 Lambda invocation helper

Build a synthetic API Gateway event because `kv2_course` reads the user ID from authorizer claims.

Use:

```python
def invoke_kv2_course(payload: dict) -> dict:
    event = {
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": secrets.kvasir_user_sub
                }
            }
        },
        "body": json.dumps(payload, ensure_ascii=False),
    }

    response = lambda_client.invoke(
        FunctionName=settings.kvasir.kv2_course_lambda_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event, ensure_ascii=False).encode("utf-8"),
    )

    outer = json.loads(response["Payload"].read())

    if outer.get("statusCode", 500) >= 400:
        raise KvasirError(...)

    body = json.loads(outer.get("body") or "{}")
    return body
```

Also inspect `FunctionError` on the Lambda invocation response.

Log:

- action,
- request correlation ID if available,
- returned status,
- component ID.

Never log secrets or the entire `.env`.

---

## 14. Creating an echo from the template

Create one echo only for each selected target language.

If only one daily slot was selected, create only one echo.

### 14.1 Read the template component

Invoke:

```json
{
  "action": "get_component",
  "component_id": TEMPLATE_ECHO_ID,
  "siblings": false
}
```

Validate:

```text
type == "echo"
assets.text exists
assets.text.name exists
```

Use the template's:

- `details`,
- `assets`,
- `dependency`,
- `voice`,
- relevant echo settings.

Do not copy read-only/server-derived fields such as:

- `id`,
- `author_id`,
- `nickname`,
- `course_title`,
- `course_status`,
- `is_creator`,
- `components`,
- likes/statistics,
- derived URLs.

### 14.2 Derive the template prompt S3 key

Use the same naming convention as current Kvasir echo prompt handling.

Given:

```python
template_course_id
template_language
assets["text"]["name"]
assets["text"]["ext"]
```

derive:

```text
{template_course_id}/text/{name}{language_postfix}.{ext}
```

where:

```text
language_postfix = "" for en
language_postfix = ".ru" for ru
```

Use the actual template language rather than assuming it from the requested target language.

### 14.3 First `component_update`: create the empty target echo

Build a new component record:

```python
{
    "course_id": TARGET_COURSE_ID,
    "title": generated_title,
    "type": "echo",
    "language": target_language,
    "status": "raw",
    "total_adviser": template_value_or_0,
    "voice": template_value_or_empty,
    "dependency": template_dependency_or_0,
    "assets": cloned_assets_without_text,
    "details": cloned_and_overridden_details,
}
```

Override at minimum:

```python
details["description"] = generated_description_html
details["public"] = True
```

Preserve the template's intentional game/echo settings such as:

```text
conv_type
llm_model
polishing_enabled
polishing_model
theme
effort
ignore_system_prompt
allow_image
moderation
plate_view_default
```

unless there is an explicit project setting overriding them.

Invoke:

```json
{
  "action": "component_update",
  "component_record": NEW_RECORD
}
```

Read:

```json
{
  "component_id": 1234
}
```

Persist that ID to SQLite immediately.

### 14.4 Copy the template prompt directly in S3

The destination must use the new echo ID.

Destination key:

```text
{TARGET_COURSE_ID}/text/{NEW_ECHO_ID}.txt
```

for English, and:

```text
{TARGET_COURSE_ID}/text/{NEW_ECHO_ID}.ru.txt
```

for Russian.

First perform an S3-side copy:

```python
s3_client.copy_object(
    Bucket=COURSES_BUCKET,
    CopySource={
        "Bucket": COURSES_BUCKET,
        "Key": template_prompt_key,
    },
    Key=destination_key,
)
```

Then:

1. read the copied destination object;
2. replace `{{CATEGORIES}}`, `{{NEWS_SUMMARY}}`, `{{PERSONA}}`;
3. upload the completed prompt back to **the destination key only**.

Do not overwrite the template object.

Set UTF-8 text content type where useful:

```text
text/plain; charset=utf-8
```

### 14.5 Second `component_update`: persist `assets.text`

After S3 is complete:

```python
target_record["id"] = new_echo_id
target_record["assets"]["text"] = {
    "region": settings.kvasir.aws_region,
    "name": str(new_echo_id),
    "ext": "txt",
}
```

Invoke:

```json
{
  "action": "component_update",
  "component_record": target_record
}
```

Then optionally read the component back once and assert:

- correct ID,
- correct language,
- correct title,
- description contains exactly one `<a>`,
- `assets.text.name == str(new_echo_id)`.

### 14.6 Result

Store:

```text
editor_url = https://quizly.pub/echo-edit?id={new_echo_id}
```

Set local echo state to:

```text
editing
```

Set daily workflow:

- `editing` after all requested echoes succeed,
- `generation_failed` only if none can be created,
- keep per-language error state if one language succeeds and the other fails.

Do not automatically delete a successfully created language just because the other language failed.

Provide a **Retry failed language** action if partial creation occurs. It must be idempotent and must not recreate the successful echo.

---

## 15. Selection locking and idempotency

This is mandatory.

### 15.1 Before generation

Selection is editable.

### 15.2 At generation start

Lock it atomically in SQLite.

After `selection_locked_at` exists:

- `/api/select` returns HTTP 409,
- UI disables all candidate-selection controls,
- refreshing the page preserves the lock.

### 15.3 Echo creation idempotency

Before creating an echo:

```sql
SELECT * FROM echoes WHERE day=? AND target_language=?
```

If a valid `kvasir_echo_id` already exists:

- return it,
- do not create another one.

If status is `creating` from an interrupted run, reconcile by checking stored IDs and Kvasir before retrying.

---

## 16. Finalization

Finalization is per generated language.

The operator edits the echo manually on Quizly first, including any chat work needed to create the public scroll quiz.

Then the operator clicks **Finalize** next to that language in `/polls`.

### 16.1 Required checks

On finalize:

1. load today's echo from SQLite;
2. verify it belongs to the requested target language;
3. fetch the current Kvasir echo/component;
4. find public scrolls belonging to that echo;
5. filter to `scroll-quiz`;
6. require exactly one public quiz;
7. derive the real public `scroll-quiz` target URL using the current Kvasir frontend/API convention;
8. store `scroll_id` and the target URL;
9. publish the stable `/today` or `/today_ru` page;
10. invalidate CloudFront if required;
11. verify the stable public URL is reachable;
12. send the Telegram message;
13. mark the publish event complete.

If any precondition before step 9 fails, publish nothing.

### 16.2 Discover the current scroll API instead of guessing

Before writing `src/scroll_lookup.py`, Claude Code must inspect current `kvasir_proto` for:

- `kv2_scrolls`,
- functions that list component scrolls,
- public/private scroll flags,
- scroll `item_type`,
- the frontend URL parameters expected by `scroll-quiz.html` / `scroll-quiz.js`,
- any Lambda already exposing this data.

Use the existing supported API/Lambda if available.

Do not add a new Kvasir endpoint for this project unless separately authorized.

Keep the chosen implementation behind:

```python
class ScrollLookup:
    def get_public_quiz(self, echo_id: int) -> PublicQuizScroll:
        ...
```

Tests may mock this adapter.

### 16.3 Exactly one quiz

If zero:

```text
No public scroll-quiz found for echo 1234.
Open the echo, create/publish the quiz, then retry Finalize.
```

If more than one:

```text
More than one public scroll-quiz found for echo 1234: ...
Leave exactly one public quiz or extend the UI to select one explicitly.
Nothing was published.
```

Do not silently choose the newest one.

---

## 17. Publishing `/today` and `/today_ru`

At the coding stage, create nice empty pages today.html and today_ru.html in kvasir_proto/src/html/kvasir.pub/ today.html and today_ru.html. Make them using styles from kvasir_proto/src/html/kvasir.pub/resources, like example lists.
Production scripts will update these pages with new polls, adding to the top.

---

## 18. Telegram publishing

Implement with Telegram Bot API using the token from `.env`.

Do not use Telegram publishing during collection.

Only publish after the stable Quizly page has been successfully updated.

### English channel

Send a compact message in English:

```text
<TITLE>

<TINY DESCRIPTION>

https://quizly.pub/today
```

### Russian channel

Send a compact message in Russian:

```text
<TITLE>

<TINY DESCRIPTION>

https://quizly.pub/today_ru
```

Use the relevant stable URL, not the internal `echo-edit` URL.

Store Telegram API response identifiers in `publish_events` if useful.

### Idempotency

Before sending:

```sql
SELECT telegram_sent_at
FROM publish_events
WHERE idempotency_key=?
```

If already set, do not send again.

If S3 publish succeeds and Telegram fails:

- leave page published,
- record status `page_published_telegram_failed`,
- show **Retry Telegram** rather than re-running the whole finalize operation.

If Telegram succeeds, do not allow another normal Finalize click to duplicate the message.

---

## 19. `.env` loader

Implement a dedicated immutable secrets model.

Example:

```python
from pathlib import Path
from dotenv import dotenv_values
from pydantic import BaseModel

class Secrets(BaseModel):
    anthropic_api_key: str
    google_api_key: str

    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: str = ""

    kvasir_user_sub: str

    telegram_bot_token: str
    telegram_channel_en: str
    telegram_channel_ru: str


def load_secrets(repo_root: Path) -> Secrets:
    raw = dotenv_values(repo_root / ".env")
    return Secrets(
        anthropic_api_key=raw.get("ANTHROPIC_API_KEY", ""),
        google_api_key=raw.get("GOOGLE_API_KEY", ""),
        aws_access_key_id=raw.get("AWS_ACCESS_KEY_ID", ""),
        aws_secret_access_key=raw.get("AWS_SECRET_ACCESS_KEY", ""),
        aws_session_token=raw.get("AWS_SESSION_TOKEN", "") or "",
        kvasir_user_sub=raw.get("KVASIR_USER_SUB", ""),
        telegram_bot_token=raw.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_channel_en=raw.get("TELEGRAM_CHANNEL_EN", ""),
        telegram_channel_ru=raw.get("TELEGRAM_CHANNEL_RU", ""),
    )
```

Validate non-empty required values on startup.

For cron collection, Kvasir/Telegram publishing secrets may be validated lazily if this makes dry-run collection easier. LLM keys required by the cron pipeline must fail fast.

---

## 20. Source collectors

Follow the isolation pattern from `ai-news-agent`.

One failed source must not fail the run.

### 20.1 RSS

Use RSS whenever a reliable feed exists.

Return normalized `NewsItem` values.

Do not fetch full article bodies at collection time unless the source does not expose enough headline metadata to classify the story.

### 20.2 Site index

Use `httpx` + BeautifulSoup.

Collector responsibilities:

- fetch current listing page,
- locate article anchors,
- extract title/dek/time if present,
- resolve absolute URL,
- return normalized metadata.

Do not place source-specific selectors throughout the generic collector. Put source-specific selector/config logic in source configuration or a small adapter.

### 20.3 Telegram public

Fetch:

```text
https://t.me/s/<channel>
```

Extract:

- message text,
- timestamp,
- message permalink,
- first useful publisher article URL.

If Telegram changes markup, fail that source with a warning.

Do not make Telegram a hard dependency of the daily job.

---

## 21. Rendering

Use Jinja2.

Render from SQLite state every time.

Do not modify generated HTML in place.

`templates/hub/index.jinja2` must contain a single configurable API prefix, similar to the existing `ai-news-agent` module, so it works correctly under `/polls`.

Use small vanilla JavaScript only.

Required client actions:

- select/clear RU candidate,
- select/clear EN candidate,
- set important/funny,
- start generation,
- refresh workflow state,
- open echo editor,
- finalize one language,
- retry a failed generation language,
- retry Telegram when applicable.

All state-changing requests use POST.

---

## 22. `ai-home-hub` change

Modify only `config/hub.yaml` unless the existing loader cannot support the module.

Add:

```yaml
  - name: polls
    enabled: true
    path: /home/anton/git/ai-polls-agent
    module: src.hub_module:PollsModule
    prefix: /polls
    config:
      settings_yaml: config/settings.yaml
      db_path: ~/polls_data/state.db
      output_html: ~/polls_data/rendered/index.html
```

Do not duplicate server/router code inside `ai-home-hub`.

The existing hub already loads external modules by repository path.

---

## 23. Cron entry point

Implement:

```text
python -m src.scheduler_entry
```

and:

```text
scripts/run.sh
```

`run.sh` must:

- use the chosen Conda environment,
- `cd` to the repository root,
- run exactly one pipeline pass,
- write logs,
- exit nonzero on unrecoverable pipeline failure.

Add a lock so two cron runs cannot overlap.

Example crontab after the actual time/path is chosen:

```cron
15 07 * * * cd /home/anton/git/ai-polls-agent && /FULL/PATH/TO/CONDA/ENV/bin/python -m src.scheduler_entry >> /home/anton/logs/ai-polls-agent-cron.log 2>&1
```

Cron uses server-local time only if the machine is confirmed to be `Asia/Jerusalem`; otherwise set `CRON_TZ=Asia/Jerusalem` in the crontab.

---

## 24. Logging

Log structured information sufficient to answer:

- which sources succeeded/failed,
- how many items each source returned,
- how many deterministic drops,
- how many dedupes,
- Gemini input/output counts,
- Claude selected IDs,
- workflow lock changes,
- Kvasir echo IDs,
- S3 destination keys,
- final scroll IDs,
- public page object keys,
- Telegram result status.

Never log:

- `.env`,
- API keys,
- AWS secret keys,
- Telegram bot token,
- complete Lambda authorizer identity data if not needed.

---

## 25. Failure behavior

### Collection source fails

Continue other sources. Record `source_fetches.status = failed`.

### Gemini fails

Retry with bounded exponential backoff.

If still failed, use deterministic ranking to produce a reduced candidate set and continue to Claude.

### Claude selector fails

Do not overwrite the previous complete day's state. Mark current run failed and show the error in the hub.

### Translation fails

Keep Hebrew candidate with original text and mark translation missing. It may not be selectable for the EN slot until `title_en` and `short_en` are present.

### Generation fails before any Kvasir echo exists

Set workflow `generation_failed`; allow explicit retry without unlocking/changing the selected news.

### One language succeeds, second language fails

Keep the successful echo. Allow retry of only the failed language.

### S3 prompt upload fails after echo creation

Keep the echo ID in SQLite with error status. Retry prompt copy/fill and the second `component_update`; do not create a new echo.

### Finalization finds no public quiz

Do not modify `/today*`. Do not send Telegram.

### Static page publish succeeds, Telegram fails

Do not roll back the page. Allow Telegram-only retry.

---

## 26. Tests

At minimum implement these tests before enabling live publishing.

### Deduplication

- same URL with UTM params → one item,
- same headline from website + Telegram → one story,
- same story in Hebrew and English → same duplicate group when detectable.

### Language eligibility

- RU item can be selected only in RU slot,
- EN item can be selected only in EN slot,
- HE item can be selected only in EN slot,
- untranslated HE final candidate cannot be selected for EN.

### Locking

- selection changes before Start,
- selection cannot change after Start,
- two concurrent Start requests create at most one echo per language.

### Description HTML

- exactly one `<a>`,
- source URL escaped/validated,
- model text cannot inject another anchor,
- Russian and English link labels correct.

### Prompt template

- all three required markers must exist,
- all three are replaced,
- template source object is never overwritten,
- EN and RU S3 destination keys have correct language postfix.

### Kvasir event

Assert the Lambda event shape contains:

```text
requestContext.authorizer.claims.sub
body
```

and that the body uses:

```text
action: component_update
```

### Echo creation

Mock:

1. template fetch,
2. first component update,
3. S3 copy,
4. S3 get/put,
5. second component update.

Assert there are exactly two component updates for a newly created echo.

### Finalize

- zero public quizzes → no S3 stable-page write,
- two public quizzes → no write,
- one public quiz → correct target,
- repeated Finalize → no duplicate Telegram message,
- RU-only finalize does not touch EN object,
- EN-only finalize does not touch RU object.

---


## 28. `CLAUDE.md` for the new repository

Create a concise repository-level `CLAUDE.md` containing these hard rules:

```text
# AI Polls Agent

## Objective
Produce a daily operator-curated Israeli yes/no poll workflow from current news.

## Hard rules
- Python only.
- SQLite is the source of truth.
- Daily collection is cron-driven.
- Echo creation/publishing happens only after explicit operator actions in ai-home-hub.
- Selection is editable until generation starts, then permanently locked for that day.
- Previous days are read-only.
- Up to one RU selection and one EN selection per day.
- HE sources are eligible for the EN slot after English translation.
- Every selected item must be marked important or funny.
- Do not create party-preference quizzes.
- Use Gemini only as the cheap prefilter/translation layer.
- Use Claude for final news selection and quiz design.
- Do not send full articles to the cheap prefilter.
- Credentials come from repository .env via dotenv_values(), never process environment variables.
- Do not call load_dotenv().
- Do not use implicit boto3 credentials.
- Do not write directly to Kvasir SQL.
- Create/update echoes through kv2_course Lambda.
- Clone the prompt via S3 and never overwrite the template.
- Description HTML contains exactly one original-news link.
- Finalize only when exactly one public scroll-quiz exists.
- Update only the selected language's stable /today page.
- Telegram publishing is idempotent.
- One source failure must not abort collection.
- Render UI from structured DB state using Jinja2.
- Add tests for every state transition and external side effect.
```

---
