# Official iPhone research monitor

## Scope and evidence

An independent, append-only price-research workflow; no purchases, buyback applications, login, account cookies, card incentives or profit recommendations. Existing daily_report, refresh_watchlist, monthly_review and ops_health code/workflows are unchanged. Base main commit: f15562595fa07db116f75e26bbb9dd8f298ff0d3.

The public MobileMix HOME initially displays the featured (買取強化) table. A normal first-party browser session with cookies enabled rendered it on 2026-10-29. A stateless reader redirected to /cookie-error. No CAPTCHA handling, stealth, proxy rotation, cookie fabrication or restrictions bypass is used. Robots are checked (MobileMix returned 404); access denials and unknown robots responses stop collection. Terms are retrieved and hashed on each run. A detected automation-specific condition stops collection for review. This does not constitute a guarantee that future terms or markup will remain unchanged.

Parse only visible featured model/price/condition cells; never submit or click buyback forms. Apple official public product-selection data are matched by model, capacity, color and UNLOCKED_JP; JPY, tax text, non-refurbished condition and valid prices are checked. Parse no third-party prices. Previous third-party Notion records remain unchanged and distinct from new MM公式 records. Capacity/model targets currently cover the featured Duo and 18 Pro/Pro Max; other categories are out of scope. New/unknown featured iPhones without an exact Apple mapping are recorded as unacquired, requiring mapping review.

Color deductions come from the visible official note; ambiguous conditions produce null values. Buyback prices are after the color deduction; original featured base and publication date are retained in 備考. Gross difference is color-adjusted buyback minus same-variant Apple tax-inclusive retail. Shipping, other costs and net profit remain null. No confirmed color-specific Apple shipping quote was present in captured bootstrap data, so 納期 explicitly says 未取得. Generic "earliest tomorrow" banners and third-party delivery estimates are never copied as variant-specific delivery dates.

## Preservation, validation and rollback

Before implementation, preserve the main commit/tree and the destination page/schema locally. This is a scoped backup, not a full Notion export. Existing records and properties are not updated or deleted. Test fixtures contain reduced public official source data captured on 2026-10-29, without cookies, CSRF tokens, forms or account information; fixtures are never production price input.

Run `pip install -r requirements-iphone.txt`, `python -m playwright install chromium`, and `python -m unittest discover -s tests -v`. Live dry run: `python iphone_monitor.py --slot dry-run --output iphone-observation.json`. Notion save is opt-in `--save`; it uses the existing NOTION_API_KEY secret and Notion API 2025-09-03. Give that integration access to the destination page if needed; a missing grant fails without modifying schema.

Branch push runs regression tests, then one explicit live append validation (36 color observations in the captured structure), with immediate read-back of every field and a second save attempt proving no second row is created. This new official-source snapshot is intentionally distinct from any existing comparison-source snapshot. A failed test stops the write. The slot is validation-GITHUB_RUN_ID; reruns retain it. Scheduled rows use JST date + 08/20 slot + model/capacity/color in the title. A writer concurrency group serializes this workflow. Query failures stop writes. Multiple matching records stop for review. An uncertain POST result is never blindly retried; on a rerun, query before create. Notion has no unique title constraint; do not add another writer using the same prefix outside this serialized workflow.

All newly created records are read back; original data are never corrected automatically. API schema mismatch, read-back mismatch and transport failures fail the job. Source failures produce nulls and 取得失敗 records. No successful paired price makes the job fail after recording unacquired results. Public-source JSON evidence is kept as a seven-day Actions artifact; secrets and private Notion responses are not uploaded.

## Activation and operation

The new schedule is JST 08:00 and 20:00. It remains gated by repository variable IPHONE_MONITOR_ENABLED == true, which defaults to disabled. A branch's cron does not run until merged to the default branch. Keep the gate off until the existing ChatGPT iPhone task is paused and live branch validation succeeds. Main merge and gate activation require a reviewed cutover; do not alter the existing news schedule. Delays beyond 12:00 for morning or midnight for evening stop, rather than label old observations with a new slot.

Stop: set IPHONE_MONITOR_ENABLED=false (or remove it). Manual workflow_dispatch defaults to read-only; the save input explicitly requests an append. Disable the workflow if necessary. Roll code back by reverting only this change; don't revert unrelated news fixes. Keep research records as history; any removal/correction requires user approval. The pre-existing scheduled reporter had a late-night dispatch rejected by its safe delivery window before this change; this monitor does not modify that behavior.
