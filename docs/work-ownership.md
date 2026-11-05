# Ongoing ownership and single news publisher

User decision: 2026-11-05 JST. Read this file, current PR heads and the Notion operations hub before shared changes.

| Owner | Scope | Branch / proposal |
| --- | --- | --- |
| General-news Work | Public economic, financial, Niigata, SME/M&A/succession, telecom, AI/IT, social/business news; summaries, Notion delivery, delays, failures and duplicates | fix/news-delivery-resilience / PR #7 |
| Investment Work | Investment HOME, holdings/watch/purchase candidates, personal investment analysis, dividends, decision/learning logs, Tips, assets/contributions/goals and their updates | feat/investment-home-v1 / PR #5; investment/shared pieces of PR #6 require owner review |

Economic/financial news belongs to general news unless it uses personal holdings to assess purchases/sales. Never add new automatic holdings reads to bypass a previously rejected action.

## One publication route

PR #7's independent news job in the EXISTING daily_report.yml is the sole proposed general-news publication route. It retains legacy date/edition matching and the existing report destination, so old posts are still recognized. Do not enable research_news.yml, a second scheduler, or a v2 news writer from PR #6 alongside it.

PR #6 remains a draft reference and investment handoff, not a second rollout. Its news workflow is being made read-only, and its runner rejects news publication even with main/publish flags. Investment code is preserved. Do not merge PR #6 as a whole without reconciling ownership and both other PRs.

PR #6 URL/publication checks and financial/M&A public search queries are reused in PR #7; news_content.py is the canonical public-source helper on this delivery route. Investment Work should review/reuse shared helpers when reconciling its branch, rather than activate another general-news job.

## Shared change contract

General-news Work leads shared execution, Notion connection, edition identity and duplicate controls when needed for news delivery. Check latest code, PRs and Notion records first. Document affected files, behavior, investment impact, validation and rollback. A cross-cutting change has one implementation proposal, reused by the other owner. Do not delete/rebuild the other owner's features.

Current investment impact: daily_report.py contains the news-only entrypoint, RSS collector, public headline renderer and general-news prompt; no stock calculations, holdings loaders or investment decisions were changed in this follow-up. Stock/weekly/monthly/watchlist retain their existing logic. PR #5 also touches daily_report.py, so resolve that shared-file merge explicitly before merging; retain both owners' work. The public helpers do not query holdings or private settings.

## Current validation and unresolved gates

- PR #7: 59 tests pass locally; mock collection-to-save is NOT live Notion verification.
- PR #6: previous Python sandbox check failed before saving with HTTP 404; connector visibility is not proof of API visibility. Reuse the existing sandbox; do not create another one.
- Real news API save/full readback, actual AI summary quality, source login and scheduled delivery remain unverified.
- Cross-edition article suppression, persisted source-health/recovery state and notification delivery still need work. Same story may appear in morning and evening; report identity still prevents duplicate posts within an edition.
- Original desired daily 07:00 JST cadence is not silently activated here. Existing 07:00/19:00 schedules remain unchanged in production; final cadence, old evening/weekly behavior and health expectations need approval together.

## Required records and approvals

Record important ownership/shared changes and production changes in GitHub and Notion. Chats do not synchronize progress automatically. Preserve DBs, relations, history and report IDs; keep secrets and personal finance data out of code/logs.

Hub: https://app.notion.com/p/40cfc83b0e47819cadb8f46531a3fd49

News: https://app.notion.com/p/40cfc83b0e47813b9f97f6b522518d63

Investment: https://app.notion.com/p/40cfc83b0e4781cfa99acc93542852cc

Before merge/production updates/scheduling/HOME switch, show validation results and impact and obtain explicit user approval. Unit tests, real API checks and observed production delivery are separate evidence. Do not duplicate schedules, management records or validation pages.
