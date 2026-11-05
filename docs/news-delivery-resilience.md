# News delivery resilience — review before production
## Verified 2026-11-05 JST
- main: f15562595fa07db116f75e26bbb9dd8f298ff0d3.
- PR #5 remains draft at 437201325d43ac45085d9183673d55434a470c39.
- No README or AGENTS.md exists in the main repository tree.
- Schedules remain 07:00/19:00 JST, weekly Monday 07:30.
- Run 41881184027: evening schedule started 2026-11-05 02:51 JST and failed
  before collection with the safe delivery window error.
- Run 41756769724 succeeded; provider login/content success is not inferred.
- Watchlist and operations health runs succeeded. Masked non-empty Notion,
  Groq and parent-page secrets appear in job metadata. Secret-list access is
  unavailable through the connector; newspaper credentials are not verified.
- Separate branch codex/separate-news-investment already exists with validation.
  It is not merged, changed, or silently enabled by this patch.
## Proposed behavior
The existing scheduled workflow gains an independent news job (no new schedule).
Stock runs retain the old safe window. News does not load holdings or state.
A run's immutable created_at anchors its last scheduled 07:00/19:00 occurrence;
a retry uses the same date/edition even after midnight. Maximum age is 36 hours.
GitHub does not expose the exact intended occurrence for a schedule delayed more
than a full cycle; such missed occurrences cannot be reliably reconstructed.
Explicit bounded report_date is available for manual news-only retry.
Delivery titles retain legacy date/time matching for existing report lookup.
Actual retrieval start is shown separately. Existing pending pages block reruns;
ambiguous creation is not retried. Existing concurrency prevents parallel runs
of this workflow. Do not run another publisher against these slots concurrently.
Repeated links within a report are normalized and deduplicated. The same public
story may appear in both morning and evening reports; cross-edition story
suppression is not implemented. RSS older than 3 days, future dated or undated
items are excluded. Newspaper publication timestamps remain unverified.
All-source empty collection fails without posting. Individual empty categories
retain the existing failure display; no cached report is reused as fresh news.
## Validation and limits
Run the main-only tests plus tests/test_news_resilience.py. Tests include
cross-midnight/retry identity, stock-free import, news entrypoint, mock
collection-to-save pipeline, empty collection and tracking-link duplicates.
No live provider login, Groq generation or Notion report write has been run.
Notion API save behavior is tested with mocks; this is not production verification.
Monthly, watchlist and investment-HOME code is unchanged.
## Production approval and rollout
1. Review this PR, the passing CI and the remaining live-validation gap.
2. Before any production write, disclose: one news page in the existing reports
   DB for one unposted date/edition; no holdings/state/schema changes.
3. With user approval, merge and run a bounded manual date/edition once. An
   existing report is skipped; a pending report requires manual inspection.
4. Verify full Notion content, actual retrieval time, login/category status and
   next scheduled morning/evening delivery. Do not enable research_news.yml or
   any second scheduler in parallel.
5. This patch intentionally leaves push activation on the legacy stock route;
   activation pushes do not trigger independent news. Use scheduled/manual news.
## Rollback
First pause using CLOUD_REPORTS_ENABLED=false; let active workflow finish.
Revert this PR commit through a new revert PR, then restore the previous setting
after review. Do not delete Notion reports or state, or reactivate PC schedules.
Reverting restores the pre-existing delay failure, so keep the limitation visible.



## Ownership consolidation and public-source follow-up (2026-11-05 JST)
PR #7 is the ONLY proposed general-news publication route. See
[work-ownership.md](work-ownership.md) before any shared change or restart.
PR #6 news publication is disabled in its staging branch; its investment code
remains an investment-owner handoff. Do not enable or merge both routes as-is.
The existing production workflows and schedules have NOT changed.

Reused PR #6 URL normalization and publication-time contract in news_content.py:
reject credential-bearing/non-web/nonstandard-port links, strip tracking, reject
articles with only update timestamps, and retain publication/retrieval/source
metadata in string-compatible RSS headlines. Google News links are explicitly
marked original-URL-unconfirmed. HTTP collection has bounded connect/read
timeouts and a feed-size limit. Partial feed failures are displayed separately
from no eligible unpublished items. If the entire collection has no items, do
not publish. No previous report is reused as current data.

Added financial/rates/FX and SME/M&A/succession queries on the NEWS path only.
The general-news prompt excludes personal buy/sell analysis and unsupported
background claims; its output is labeled unverified. Actual model quality is
still untested. Manual morning/evening news publication now requires an explicit
JST report_date, preserving the retry identity; stock jobs keep their prior
manual behavior unless a report_date selects news-only retry.

Validation: 59 local tests and syntax checks pass. Added tests cover publication
vs update time, unsafe URLs, source/retrieval metadata in actual block rendering,
partial source failure vs valid empty feeds, and missing manual target date.
These do not establish live Notion save or full provider/AI quality.

Legacy matching/destination and current morning/evening identity are preserved.
The requested daily 07:00 cadence, any evening/weekly change and operations-health
alignment remain a single future approval decision; this PR does not authorize
production activation. Cross-edition article history, repair state and delivery
notifications remain open. The prior PR #6 sandbox 404 is unresolved; no additional
sandbox or holdings reader is created here.

Public smoke at f17e06e / run 41947999508 accepted 22 items, but Japanese economy
and telecom primary feeds had no eligible publication-dated articles. Follow-up
adds named Google News supplements for these categories while retaining the
primary feeds. This is search-based coverage with original URLs unconfirmed,
not evidence that those primary feeds are healthy/current. HTTP 200 non-feed
content is now a failure (requires a recognized feed version), distinct from a
valid empty RSS. Final-head CI must confirm the supplemental collection.
