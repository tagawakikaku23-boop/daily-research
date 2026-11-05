# Partial general-news delivery (2026-11-05 JST)

PR #7 was already merged as b8c128d978f23835830d3dcd89fea16468d81653
at 13:10 JST when this continuation read the repository. Earlier notes saying
unmerged are historical. This follow-up does not merge or dispatch production.

## Behavior
RSS source failures or missing/failed AI summary preserve available headlines in
one page with final icon ⚠️. The original news kind/date/title remain unchanged.
The creating run fails after saving, making the partial result visible in Actions.
An existing warning news page blocks retries without overwriting or duplicating it.
Health checks exclude warning news pages from completed editions. Normal news and
stock report defaults are preserved. Provider exception text is not saved in digest.

## Repair
Inspect the existing page and failing run first. Do not blindly rerun: a warning
page intentionally blocks the slot. This patch provides explicit repair-needed
state, not automatic repair or a new revision publisher. A reviewed repair path
must preserve the original content and verify the replacement before resolving
the warning. Never clear the icon just to make health green.

## Scope and validation
66 unit tests and syntax checks pass locally (59 prior plus 7 new).
No live AI or Notion write/readback was tested. Existing sandbox API 404 remains
unresolved. No notification delivery or recipient configuration is claimed.
The current production destination is the existing reports database. The legacy
parent-page fallback lacks icons in its child listing and therefore does not
provide the warning-state retry guard; this must be addressed before using that
fallback for news. Newspaper getters still cannot distinguish no news vs login
failure, and unmarked historical partial reports cannot be retroactively detected.
Cross-edition article suppression, safe repair revisions and live end-to-end
verification remain open.

## Shared impact and rollout
General-news owns these changes. report_delivery.publish gets an optional final
icon argument; all stock callers retain the default. ops_health changes only the
news completion test. daily_report changes only news summary/page paths plus an
optional create_page argument. Investment owners should reuse these helpers,
preserve both sides on conflict, and avoid a second news publisher.
Merge, production write and schedule changes require a separate review/approval.
Revert this follow-up via a new PR if needed; do not delete existing warning pages.
