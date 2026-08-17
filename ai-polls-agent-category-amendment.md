# Amendment: Make Quiz Categories Specific, Playful, and Mode-Aware

## Goal

Amend the **working `ai-polls-agent` system** so generated quiz/chat categories are no longer generic topic labels such as:

- Politics
- Society
- News
- Economy
- Israel
- Current events

Categories should instead feel like **interesting identities, perspectives, affiliations, situations, or recognizable groups** that a participant can meaningfully answer as.

Examples:

- `You are a Tel Aviv renter`
- `You are a parent of school-age children`
- `You are a reservist`
- `You run a small business`
- `You commute by car every day`
- `You live near the northern border`
- `You are an Israeli voter who is still undecided`
- `You are a Likud voter`
- `You are a Democrats voter`
- `You are a Haredi parent`
- `You are a student trying to afford rent`
- `You are the person in the office who always reads the fine print`

The categories must remain **non-offensive, non-harassing, non-degrading, and reasonably neutral**.

The behavior must differ between the already-selected quiz modes:

- `important`
- `funny`

Preserve the existing prompt placeholder:

```text
{{CATEGORIES DEFAULT={"ru":[],"en":[]}}}
```

If the production template currently contains a slightly different serialized form, preserve the exact deployed syntax and adapt the code around it.

---

# 1. Core semantic change

`CATEGORIES` must no longer mean “article taxonomy”.

Bad:

```json
["Politics", "Economy", "Society", "Security"]
```

Good:

```json
[
  "You are a small business owner",
  "You are a reservist",
  "You are a parent of school-age children",
  "You commute to work by car",
  "You are an undecided voter"
]
```

The model should answer this question:

> **Who might want to answer this poll from their own perspective?**

not:

> What subject is this article about?

---

# 2. Mode-specific generation

The user already chooses one of:

```text
important
funny
```

Category generation must explicitly receive this value and use different prompts and ranking logic.

## 2.1 `important`

For `important`, categories should identify **real constituencies, affected groups, stakeholders, professions, geographic groups, economic situations, or political affiliations** whose answers could plausibly differ.

Preferred category types:

1. Directly affected people
2. Recognizable social/economic groups
3. Profession or occupation
4. Family/life situation
5. Geographic group
6. Political affiliation, when relevant
7. Practical behavior or service usage
8. General public identity only as a last resort

Examples:

```text
You are a reservist
You are a parent of school-age children
You are a renter
You own an apartment
You run a small business
You work in high tech
You use public transport every day
You live in a border community
You are a new immigrant
You are self-employed
You are a university student
You are retired
You are a Likud voter
You are a Yisrael Beiteinu voter
You are an undecided voter
```

Prompt rule:

> Prefer categories representing people who have a concrete stake in the issue. A category is useful when membership in that category could reasonably change how a person answers the poll.

Do not create distinctions unrelated to the actual story/question.

## 2.2 `funny`

For `funny`, categories may be more playful, situational, observational, or personality-like, but must still describe someone who could naturally answer the poll.

Humor should come from **recognition**, not insult.

Examples:

```text
You are the person who reads every neighborhood WhatsApp message
You are already checking apartment prices abroad
You always say "this could have been an email"
You have three delivery apps on your phone
You argue about parking with your neighbors
You panic when Waze adds seven minutes
You are the family member who organizes everything
You think every government form needs another government form
You are the person who checks the weather and ignores it
You have already forwarded this story to the family chat
```

Russian examples:

```text
Вы тот человек, который читает все сообщения в домовом чате
Вы уже проверяете цены на квартиры за границей
Вы всегда говорите: «это можно было написать в письме»
У вас на телефоне три приложения доставки
Вы спорите с соседями из-за парковки
Вы нервничаете, когда Waze добавляет семь минут
Вы в семье тот, кто всё организует
Вы считаете, что к любой справке нужна ещё одна справка
Вы уже переслали эту новость в семейный чат
```

Prompt rule:

> Humor must come from familiar behavior, everyday Israeli life, mild self-recognition, bureaucracy, family dynamics, commuting, shopping, housing, technology, work, or similar harmless situations.

Do not generate jokes based on protected traits, victims, trauma, military loss, poverty, illness, or demeaning stereotypes.

Funny categories must still be relevant to the story.

---

# 3. Political parties as allowed defaults

The `CATEGORIES` placeholder already supports defaults:

```text
{{CATEGORIES DEFAULT={"ru":[],"en":[]}}}
```

Extend the working system so configured Israeli political parties can serve as a **default/fallback category pool**, especially for political or public-policy questions.

Do not force party categories into every quiz.

Use parties when:

- the question is genuinely political or policy-oriented;
- supporters of different parties could plausibly answer differently;
- generated stakeholder categories are weak;
- the deployed template default already supplies party categories.

Examples where party categories may be useful:

- conscription
- judicial reform
- taxation
- settlements
- coalition policy
- religion/state
- public transport on Shabbat
- election law

Examples where they usually are not needed:

- a supermarket product story
- a weather-related oddity
- a local app malfunction
- a restaurant or consumer story with no meaningful political dimension

---

# 5. Dedicated category-generation contract

Create a dedicated function/module instead of burying category behavior inside generic quiz generation.

Suggested interface:

```python
def generate_categories(
    *,
    language: Literal["en", "ru"],
    mode: Literal["important", "funny"],
    news_title: str,
    news_summary: str,
    proposed_question: str,
    party_defaults: list[str],
) -> CategoryResult:
    ...
```

Suggested model:

```python
class CategoryResult(BaseModel):
    categories: list[str]
    party_categories_used: bool
    rationale: str | None = None
```

`rationale` is optional and for logs/debugging only. Do not insert it into the prompt template.

---

# 6. Count and diversity

Target:

```text
6–10 categories
```

Preferred default:

```text
8
```

Hard bounds:

```text
minimum 4
maximum 12
```

Do not pad a weak result with generic labels just to reach a target count.

Avoid near-duplicates.

Bad:

```text
You are a parent
You are a mother
You are a father
You have children
```

Better:

```text
You are a parent of school-age children
You are a teacher
You are a taxpayer without children
You run a small business
You are a municipal employee
You are an undecided voter
```

For `important`, cover different reasons for caring.

For `funny`, cover different recognizable behaviors/situations.

---

# 7. Safety requirements

Categories must not:

- insult or mock protected groups;
- imply inferiority, stupidity, criminality, dirtiness, dishonesty, or danger based on protected characteristics;
- joke about victims of violence, terrorism, war casualties, hostages, bereavement, disability, serious illness, or trauma;
- sexualize protected or vulnerable groups;
- target a named private person;
- use slurs;
- invent extremist affiliations;
- label participants as mentally ill, addicted, criminal, or otherwise stigmatized;
- use accusatory or hostile “you are…” formulations.

Neutral political affiliation is allowed:

```text
You are a Likud voter
You are a Ra'am voter
```

Political insult is not.

Religion may be used when genuinely relevant and neutrally formulated:

```text
You are a Haredi parent
You are a religious-Zionist voter
You are a secular Israeli
```

Do not generate humorous religious stereotypes.

---

# 8. Prompt recommendation — `important`

Use a prompt equivalent to:

```text
You generate participant categories for a Yes/No poll based on a current Israeli news story.

MODE: IMPORTANT

A category is NOT a news topic.
A category describes a type of person answering the poll.

Generate 6–10 concise participant identities for whom this question may feel meaningfully different.

Prefer:
- people directly affected by the issue;
- professions;
- family or economic situations;
- geographic groups;
- users of the affected service;
- relevant political affiliations;
- recognizable constituencies.

Use political-party categories only when political affiliation is genuinely relevant to the question.

Good category style:
"You are a reservist"
"You are a parent of school-age children"
"You run a small business"
"You rent your home"
"You are an undecided voter"
"You are a Likud voter"

Bad categories:
"Politics"
"Economy"
"Israel"
"Current events"
"Citizens"
"People"

Rules:
- each category must describe a person/group, not a subject;
- categories should differ meaningfully;
- avoid generic categories;
- do not invent facts;
- keep wording short;
- keep wording neutral and non-offensive;
- do not use humor in IMPORTANT mode;
- return only categories relevant to this specific story/question.

NEWS TITLE:
{title}

NEWS SUMMARY:
{summary}

PROPOSED YES/NO QUESTION:
{question}

OPTIONAL POLITICAL PARTY DEFAULTS:
{party_defaults}

Return strict JSON:
{
  "categories": ["...", "..."],
  "party_categories_used": false
}
```

---

# 9. Prompt recommendation — `funny`

Use a separate prompt, not merely one extra sentence appended to the important prompt.

```text
You generate participant categories for a playful Yes/No poll based on a current Israeli news story.

MODE: FUNNY

A category is NOT a news topic.
A category is a recognizable type of person answering the poll.

Generate 6–10 short categories that are:
- relevant to the story;
- mildly funny;
- recognizable;
- based on everyday behavior, habits, work, family life, bureaucracy, housing, transport, shopping, phones, apps, neighborhood life, or similar harmless situations.

The humor should make a person think:
"Yes, that is me,"
not:
"They are mocking those people."

Good style:
"You are the person who reads every neighborhood WhatsApp message"
"You already checked apartment prices abroad"
"You panic when Waze adds seven minutes"
"You are the family member who organizes everything"
"You have already forwarded this story to the family chat"

Do NOT:
- mock ethnicity, religion, nationality, disability, sexuality, age, illness, poverty, trauma, military casualties, terror victims, hostages, or bereaved families;
- use degrading stereotypes;
- create categories unrelated to the actual story;
- output generic topics such as "Politics", "Society", "Israel", or "News";
- force political parties into a humorous poll unless they are genuinely useful and the wording stays neutral.

NEWS TITLE:
{title}

NEWS SUMMARY:
{summary}

PROPOSED YES/NO QUESTION:
{question}

OPTIONAL POLITICAL PARTY DEFAULTS:
{party_defaults}

Return strict JSON:
{
  "categories": ["...", "..."],
  "party_categories_used": false
}
```

---

# 10. Language rules

Generate categories directly in the target chat language.

English:

```text
You are a reservist
You rent your home
You are an undecided voter
```

Russian:

```text
Вы резервист
Вы снимаете квартиру
Вы ещё не определились, за кого голосовать
```

Do not mechanically translate English wording into Russian if the model can produce idiomatic Russian directly.

For funny mode, localization matters.

Familiar Israeli references are acceptable when relevant and harmless:

```text
Waze
WhatsApp family chat
Arnona
Bituach Leumi
kupat holim
parking
delivery apps
reserve duty
school WhatsApp group
```

Avoid obscure in-jokes.

---

# 11. Filling `{{CATEGORIES ...}}`

When building the final prompt for the newly created chat:

1. Generate categories with the new generator.
2. Validate them.
3. Serialize them in the exact structure expected by the chat template.
4. Replace the `{{CATEGORIES ...}}` placeholder.
5. If generation fails or yields too few valid categories, use fallback logic.

Conceptual structure:

```json
{
  "ru": [
    "Вы резервист",
    "Вы родитель школьника",
    "Вы владелец малого бизнеса"
  ],
  "en": [
    "You are a reservist",
    "You are a parent of school-age children",
    "You run a small business"
  ]
}
```

If only one target language is being created, the unused language array may remain empty if that matches the current template contract.

Never leave the literal placeholder unresolved.

---

# 12. Fallback logic

## Important mode

If generation fails and the story is political/public-policy related:

- use a curated subset of configured party defaults;
- include `Undecided voter` / its Russian equivalent when configured;
- add one or two neutral stakeholder groups if safely inferable;
- cap total categories at 10;
- do not dump every party into the prompt.

For non-political stories, use a small stakeholder fallback library, keyed by domain.

Example:

```yaml
stakeholder_fallbacks:
  consumer:
    en:
      - You shop for your household
      - You compare prices before buying
      - You run a small business
      - You are raising a family
    ru:
      - Вы покупаете продукты для семьи
      - Вы сравниваете цены перед покупкой
      - У вас малый бизнес
      - Вы растите детей
```

Only use a fallback group if it is relevant.

## Funny mode

Do not fall back to party lists unless the story is clearly political.

Prefer safe everyday archetypes.

Example:

```yaml
funny_fallbacks:
  general:
    en:
      - You already sent this to the family chat
      - You are the one who reads the fine print
      - You have an opinion before finishing the article
      - You will check what people wrote in the comments
    ru:
      - Вы уже переслали это в семейный чат
      - Вы тот человек, который читает мелкий шрифт
      - У вас уже есть мнение, хотя вы ещё не дочитали новость
      - Вы всё равно пойдёте читать комментарии
```

These are last-resort fallbacks only.

---

# 13. Validation

Add a category validator.

Reject:

- blank entries;
- duplicates after normalization;
- entries longer than ~90 characters;
- obvious taxonomy labels;
- generic terms such as Politics, Economy, Society, Israel, News, Current events, Government, People, Citizens;
- malformed JSON fragments;
- URLs;
- multi-sentence paragraphs.

Suggested helper:

```python
GENERIC_CATEGORY_TERMS = {
    "politics",
    "economy",
    "society",
    "israel",
    "news",
    "current events",
    "government",
    "people",
    "citizens",
}
```

Do not rely only on this literal set. Also reject taxonomy-looking labels that clearly do not describe a participant identity.

Post-processing:

1. normalize whitespace;
2. remove exact duplicates;
3. remove obvious semantic duplicates if existing tooling makes this cheap;
4. validate safety/basic form;
5. cap at 10 by default;
6. preserve the strongest/distinct categories;
7. use fallback only if fewer than 4 valid categories remain.

Do not add another expensive ranking call unless necessary.

---

# 14. Integrate with quiz generation

Current quiz generation already returns data such as:

```json
{
  "title": "...",
  "description_text": "...",
  "news_summary_for_prompt": "...",
  "yes_no_question": "...",
  "picture_suggestions": ["..."]
}
```

Either extend this result to include categories:

```json
{
  "title": "...",
  "description_text": "...",
  "news_summary_for_prompt": "...",
  "yes_no_question": "...",
  "categories": ["...", "..."],
  "picture_suggestions": ["..."]
}
```

or call a dedicated category generator immediately after the quiz concept is created.

Preferred flow:

```text
News item
   ↓
Quiz concept generation
   ↓
Final Yes/No question
   ↓
Category generation using:
   - final question
   - summary
   - mode
   - target language
   - party defaults
   ↓
Validation
   ↓
Insert into chat prompt
```

The category generator must see the **final proposed yes/no question**, not only the article title.

---

# 15. Keep `CATEGORIES` separate from `PERSONA`

Do not confuse these fields.

`PERSONA` controls how the newly created chat behaves.

`CATEGORIES` controls participant identities/perspectives used by the chat for poll creation or segmentation.

Example:

```text
PERSONA:
Playful, dry, observant, never insulting.

CATEGORIES:
- You are already checking apartment prices abroad
- You are a renter
- You own an apartment
- You are the person who reads every mortgage-rate story
```

---

# 16. Funny-mode persona amendment

If the current `funny` persona is generic, amend it with wording like:

```text
You are witty, concise, and observant.

Use dry situational humor and familiar everyday behavior.
The humor should come from recognition, not ridicule.

Do not insult users or groups.
Do not use ethnic, religious, gender, disability, illness, trauma, poverty, military-loss, terror-victim, hostage, or bereavement stereotypes.

When suggesting categories or poll framings, prefer:
- everyday habits;
- family chats;
- bureaucracy;
- housing;
- commuting;
- work;
- apps and phones;
- shopping;
- neighborhood life;
- mild self-deprecating situations.

The result should feel clever enough to share, but safe enough to publish publicly.
```

---

# 17. Important-mode persona amendment

If the current `important` persona is generic, amend it with:

```text
You are concise, analytical, and practical.

Treat the poll as a way to compare how different affected groups view the issue.

Prefer concrete stakeholder perspectives over abstract topic labels.
Use political affiliation when it genuinely helps explain differences in opinion.

Avoid sensationalism.
Avoid generic categories.
Do not frame the poll as party-preference polling unless the user explicitly turns the conversation in that direction.
```

---

# 18. Hub visibility

In `ai-home-hub`, show generated categories on the generated-chat card or in an expandable debug section.

Example:

```text
Categories:
• You are a reservist
• You are a parent of school-age children
• You run a small business
• You are an undecided voter
```

Optional improvement:

```text
Regenerate categories
```

If implemented:

- regenerate only categories;
- do not recreate the echo;
- update only the affected prompt asset;
- keep unavailable after final publication if finalized items are immutable.

This button is optional.

---

# 19. Persistence/logging

If categories are not already stored, persist them in the local agent database.

Possible schema:

```sql
ALTER TABLE echoes ADD COLUMN categories_json TEXT;
ALTER TABLE echoes ADD COLUMN category_fallback_used INTEGER NOT NULL DEFAULT 0;
```

Use the project's existing migration pattern.

Log:

```text
day
echo_id
language
mode
generated_categories
party_categories_used
fallback_used
validation_rejections_count
```

Do not log secrets or full prompt contents unless current policy explicitly allows it.

---

# 20. Tests

Add tests for:

### Important mode rejects taxonomy labels

Input:

```json
{
  "categories": [
    "Politics",
    "Economy",
    "You are a reservist",
    "You run a small business"
  ]
}
```

Expected: generic taxonomy labels rejected; participant identities retained.

### Funny mode

Harmless situational categories are accepted.

### Duplicate removal

Exact duplicates must be removed.

If semantic dedupe exists, near-duplicates such as:

```text
You are a renter
You rent your home
```

should not both survive.

### Party fallback

When mode is `important`, the story is political/policy-oriented, and generated categories fail validation:

- use configured party defaults;
- cap category count;
- include undecided when configured.

### Non-political fallback

Do not inject political parties into a clearly non-political story.

### Language

RU echo gets Russian categories.

EN echo gets English categories.

### Placeholder

Final prompt must contain no unresolved:

```text
{{CATEGORIES
```

marker.

### Safety regression

Reject/regenerate clearly degrading category formulations.

---

# 21. Acceptance examples

## Important

News:

```text
Government proposes changes to reserve-duty compensation.
```

Question:

```text
Should reserve-duty compensation increase automatically when service exceeds a defined number of days?
```

Good:

```json
[
  "You are a reservist",
  "You employ reservists",
  "You are self-employed",
  "You are a parent of young children",
  "You work in the public sector",
  "You are an undecided voter",
  "You are a Likud voter",
  "You are a Yisrael Beiteinu voter"
]
```

Bad:

```json
["Army", "Politics", "Economy", "Israel"]
```

## Funny

News:

```text
A municipality introduces a new digital parking system.
```

Question:

```text
Will the new system make parking less annoying?
```

Good:

```json
[
  "You circle the block three times before giving up",
  "You know every parking shortcut in the neighborhood",
  "You open three parking apps before leaving the car",
  "You are the person who reads every parking sign twice",
  "You argue with parking apps more than with people",
  "You sold your car and feel superior about it"
]
```

Bad:

```json
["Transport", "Technology", "Municipality", "Drivers"]
```

`Drivers` is not offensive, but it is too generic for the new standard.

## Political-party use

News:

```text
A proposal changes public transport rules on Shabbat.
```

Good important categories:

```json
[
  "You rely on public transport",
  "You do not own a car",
  "You are a secular Israeli",
  "You are a Haredi parent",
  "You are a Likud voter",
  "You are a Democrats voter",
  "You are a Shas voter",
  "You are an undecided voter"
]
```

Do not automatically include every party.

---

# 22. Implementation order

1. Add category config and party defaults.
2. Add `CategoryResult`.
3. Add separate `important` and `funny` category prompts.
4. Pass final yes/no question + summary + mode + language to generator.
5. Add validator/post-processing.
6. Add fallback logic.
7. Insert categories into `{{CATEGORIES ...}}`.
8. Persist categories.
9. Show categories in hub UI/debug view.
10. Add tests.
11. Test against real existing stories from the database.

---

# 23. Manual QA

Before deployment, inspect at least:

- 5 important political stories;
- 3 important economic/social stories;
- 5 funny stories;
- 3 Russian echoes;
- 3 English echoes;
- 2 Hebrew-source → English echoes.

Reject the change if results frequently resemble:

```text
Politics
Society
Economy
Israel
Government
Current events
```

Accept when most categories look like **people, constituencies, perspectives, habits, or recognizable situations**, and when `important` and `funny` outputs are clearly different.

---

# 24. Hard rule for Claude Code

Do not rewrite unrelated parts of the working system.

This is an amendment to the existing production workflow.

Preserve:

- current echo creation flow;
- current S3 prompt handling;
- current `kv2_course` invocation;
- current `ai-home-hub` integration;
- current publication workflow;
- current selection locking;
- existing `important` / `funny` user choice.

Change only what is required to produce and insert substantially better categories.
