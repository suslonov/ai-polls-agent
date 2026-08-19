# Problems

- pls remove sha256: e73c81ebbdae918e… from the interface and possible from calculation if it is not needed for some purposes, I dont need it

**Done — it was purely informational, so it is gone entirely, not just hidden.**

Nothing depended on it: it was never compared, verified or sent anywhere. It was
computed after writing the prompt, stored, logged and shown under the prompt path
in the panel.

Removed from:

- the panel (`templates/hub/index.jinja2`) and the view model (`render.echo_view`);
- the calculation — `echo_builder.prompt_sha256()` is deleted, along with
  `BuiltEcho.prompt_hash` and the `hashlib` import;
- the log line, which now reads `Echo 1442 prompt written: s3://… (11613 chars),
  cloned from s3://…`;
- both writers (`workflow.generate_language` and `workflow.update_categories`)
  and the echo-write allowlist in `db.py`, so nothing can set it again;
- the schema, so a new database has no such column.

The `prompt_sha256` column still exists in your `~/polls_data/state.db` with its
old values — SQLite can drop a column, but a dead column costs nothing and
rewriting a live table for cosmetics is not worth the risk. Nothing reads it. Say
the word if you want it dropped.

Other sha256 uses are unrelated and stay: `extraction.content_hash()` for exact
duplicate detection and `dedupe` for story-group keys. Neither is shown in the
interface.

245 tests pass (the two tests that only asserted the hash existed are gone; the
category-edit test now compares the prompt text itself, which is what it actually
cared about).
