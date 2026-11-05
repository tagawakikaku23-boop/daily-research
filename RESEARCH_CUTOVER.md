# Ownership update — 2026-11-05 JST

General-news publication is now owned solely by PR #7 / fix/news-delivery-resilience.
This branch rejects news --publish even on enabled main, and research_news.yml
is read-only comparison only. Do NOT merge this original mixed-scope PR as a
second rollout. Its investment/shared components are preserved for Investment
Work to reconcile with PR #5. No new holdings reader is authorized.

Common handoff: Notion operations hub 40cfc83b0e47819cadb8f46531a3fd49 and
PR #7 docs/work-ownership.md. Read current PRs and records before shared changes.
Original staged design and validation history follow; historical news cutover
instructions below are superseded by PR #7 and must NOT be activated.

# Separate research: staging and cutover record

Status: draft, NOT enabled for production. Updated 2026-11-05 JST.

## Verified handoff
- Original staged head: dbabefc5a79cd3c845cdf4338589e9aaafb99a0c.
- Main remains f15562595fa07db116f75e26bbb9dd8f298ff0d3.
- PR #5 remains draft at 437201325d43ac45085d9183673d55434a470c39; PR #4 remains draft.
- Original validation run 41750340781: tests and public-sources succeeded.
- Existing My Investment Research staging page fetched: five tabs and existing database references present; no linked views or visual verification completed.
- Existing news schema has multi_select 分野 and the expected categories. No schema changes made.
- Existing operational notes confirm cloud scheduling was already enabled. Desktop scheduling status is historical evidence, not freshly inspected.

## Changes in this follow-up
- Named feed-category mapping replaces dictionary-position mapping. Unknown legacy categories fail closed.
- News manifests persist categories; save uses existing category options only and never creates new options.
- Investment news excludes article IDs from report history and duplicates within the same report.
- Publication requires an explicit JST date, including manual workflow input. Retry with the SAME date. The 48-hour bound still applies.
- Existing degraded reports remain failures on rerun instead of becoming successful skips. No automatic replacement of existing content.
- Regression suite: 71 passing tests on Python 3.10.6; new module compile check passed. This is not real API integration evidence.

## Safe staging procedure
1. Run regression and public-source CI on the exact PR head.
2. Use read-only manual runs to inspect source health; publication remains disabled on branches.
3. Establish an isolated Notion integration destination under the EXISTING staging page, first checking for any existing sandbox database. Do not create duplicate validation pages.
4. Reuse existing credentials only inside GitHub Actions; never copy secrets or private source rows into repository artifacts or logs.
5. Run the Python Store against that isolated destination: create full body, retrieve properties and body, compare hashes, rerun same date and verify exactly one page.
6. Exercise timeout reconciliation and partial-source failure against sandbox records. Mock tests do not substitute for this step.
7. Validate actual Groq summaries separately; public RSS CI does not prove summary quality.

## Degraded-report repair policy (current implementation)
A degraded report is retained and subsequent runs fail for that date. Do not change its target date to evade the duplicate check. Automatic repair is NOT implemented. Review a private report and design a sandbox-tested revision/update protocol before production cutover. Pending corrupted content also blocks publication. Do not delete or overwrite historical reports merely to rerun.

## Outstanding release gates
- Stable scheduled-event target date across multi-day queue delays and GitHub reruns; explicit dates currently protect manual publication only. No schedule has been added.
- Reviewed degraded-report repair protocol; safe revision identity and concurrency handling.
- Legacy/new article deduplication boundary: legacy reports have no v2 manifests. Agree a cutover date and overlap strategy before enabling schedules.
- Primary IR/TDnet evidence, original article URLs, disclosure coverage and numerical as-of dates; Yahoo/Google references remain preliminary.
- Previous financial snapshot comparison, deep evaluation, ETF/REIT/financial-sector rules; verified holdings/currency/as-of inputs before concentration calculations.
- Investment history lookback is 35 days; Tips choose within 30 days. Old legacy articles and historical degraded reports still need explicit operational treatment.
- Existing report has one page per day containing multiple categories: category views filter daily reports, not individual articles.
- Real Notion Python integration, Groq quality review, linked views and desktop/mobile visual QA.
- Notification delivery and alignment with existing operations health checks.

## Approval-required production proposal
Only after the release gates are resolved:
- Add news cron 0 22 * * * and investment cron 30 22 * * 0-4 (UTC), accounting for GitHub queue delay and Japanese holidays.
- Decide whether old morning/evening stock reports and weekly reports remain. Remove old news generation for the agreed boundary to avoid double delivery. Do not disable unrelated monthly/watchlist jobs.
- Update operations health expectations for both streams and verify a failure reaches the intended user.
- Review PR #5 adapter/analysis overlap before merging. No automatic merge is authorized.
- Obtain explicit approval for merge, production variables, scheduling changes and any important schema changes.

## Rollback proposal
Keep production untouched during staging. For an approved future cutover, record the exact previous workflow/code revisions and changed variables. If new delivery fails, disable only the newly approved schedules/publish gate, preserve reports and manifests, and restore the agreed old delivery configuration under the approved rollback scope. Do not run old and new publishers for the same target day. Validate read-only health before restoring writes.

