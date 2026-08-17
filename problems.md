# Problems

when the script publishes a new quiz into daily-israel-polls pages, it should not delete anything even if there is a quiz for this date. Just add a new one to the head of the list.

**Done.** `publisher.update_page()` is append-only now. The regex that used to
strip an existing entry for the same `(day, language)` before inserting the new
one is gone, so a second poll on a day that already has one is added *above* it
and both stay. The chronological sort is stable, so same-day entries keep
insertion order — the fresh one on top of the poll it joins.

Repeated Finalize clicks still cannot duplicate an entry, but that is enforced
earlier and more precisely: `workflow.finalize` skips the page write once a
publish event exists for `{day}:{language}:{echo_id}:{scroll_id}`. Closing a
poll and building a second one gives a different key, which is exactly the case
that should append.

The only thing that still removes an entry is the configured cap,
`publishing.max_entries_per_page` (60) — and it now logs when it drops
something, and accepts **0** to keep every poll ever published:

    max_entries_per_page: 0

Nothing outside the `<!-- POLLS:START -->` … `<!-- POLLS:END -->` region is ever
touched, and the other language's page is still never opened.

Tests: two polls on one day both surviving with the newest on top, an earlier
entry surviving byte for byte across a later publish, the other language
untouched, the cap being switchable off, and the existing repeated-Finalize test
that still asserts a single entry. 229 tests pass.
