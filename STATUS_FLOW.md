# Pipeline status flow

Every row in `companies` moves through this chain. Each stage script reads
rows at exactly one status, does its work, and writes them to the next one.
Nothing else drives the pipeline -- the `status` column *is* the queue, which
is what makes every stage idempotent and resumable: a crash mid-batch leaves
finished rows advanced and unfinished rows untouched, so a rerun picks up
exactly where it stopped.

```
                          stage 01: ingest MCA CSV
                                    |
                              [ discovered ]
                                    |
                          stage 02: resolve domain
                    /            |             \
           [ resolved ]  [ duplicate_domain ]  [ resolve_failed ]
                |          (terminal)            (terminal)
                |
              stage 03: Playwright scrape
              /         |             \
   [ not_a_lead ]  [ scraped ]   [ scrape_failed ]              (terminal x2)
    (has_cmp=true)      |
                        |  stage 04 pulls WHERE has_cmp = false
                        |
                stage 04: Apify LinkedIn
                /                    \
       [ linkedin_done ]      [ linkedin_failed ]               (terminal)
              |
      stage 05: classify scale   (rule-based, cannot fail externally)
              |
        [ classified ]
              |
      stage 06: Apollo / Lusha contact
        /              \
[ contact_done ]  [ contact_failed ]                            (terminal)
        |
   stage 07: export CRM CSV
        |
   [ exported ]                                                 (terminal)
```

## Statuses

| Status | Set by | Meaning |
| --- | --- | --- |
| `discovered` | stage 01 | Row inserted from the MCA CSV. Schema default. |
| `resolved` | stage 02 | A domain was found and confidence-scored. |
| `duplicate_domain` | stage 02 | Resolved to a site another CIN already claims. Terminal. |
| `resolve_failed` | stage 02 | No domain after `MAX_RETRIES`. Terminal. |
| `scraped` | stage 03 | Page loaded, no CMP found -- a live lead. |
| `not_a_lead` | stage 03 | CMP detected (`has_cmp = true`). Terminal. |
| `excluded` | `exclude_llp.py` | Ruled out by policy, not by the gap check -- LLPs (`db.LLP_SQL`), reason in `last_error`. Terminal. |
| `scrape_failed` | stage 03 | Page never loaded after `MAX_RETRIES`. Terminal. |
| `linkedin_done` | stage 04 | LinkedIn company data written back. |
| `linkedin_failed` | stage 04 | No LinkedIn match after `MAX_RETRIES`. Terminal. |
| `classified` | stage 05 | `business_scale` + confidence + basis assigned. |
| `contact_done` | stage 06 | A contact with a revealed email is attached. |
| `contact_failed` | stage 06 | Neither Apollo nor Lusha yielded a usable contact. Terminal. |
| `exported` | stage 07 | Written to the CRM CSV. Terminal. |

## Two CINs, one website

Group companies routinely share a site. They are legally distinct (different
CINs, never merged -- see stage 01's dedupe), but only one should reach the
paid stages: letting both through bills Apify and Apollo twice for the same
site, and usually the same person. Stage 02 checks `db.domain_owner()` before
advancing, lets the first claimant through, and parks the rest at
`duplicate_domain` with the winning CIN recorded in `last_error`.

## `consent_gap_score` decides who is worth paying for

`has_cmp = false` only says a site has no consent platform. It does not say the
site has a problem worth selling against: on the first real batch, 3 of 4
companies clearing that filter set **zero** tracking cookies. Nothing fires
before consent, so there is no gap, no complaint to point at, and no reason to
spend an Apify or Apollo credit.

`consent_gap_score` (0-100, computed deterministically in stage 03) closes that
hole. Trackers present scores up to 45, weighted by category -- advertising and
session-replay cookies count for more than plain analytics, because they
identify a person across sites or record what they did before anyone agreed.
Absent banner adds 25, absent policy 15, session replay 10, advertising 5.
No trackers, or a CMP present, scores 0.

Stage 04 should therefore pull:

    db.get_pending("scraped", extra_where="has_cmp = false AND consent_gap_score > 0")

ordered by score descending, so scarce paid credits hit the strongest leads
first rather than whatever happens to sort earliest.

## `not_a_lead` is the cost gate

Stage 03 calls `db.mark_not_a_lead(cin, reason)` on any company that already
runs a consent-management platform. This is a *terminal* status, not a
failure status, and that distinction is the whole point: the paid stages
never query it back, so Apify (stage 04) and Apollo (stage 06) can never
spend a credit on a company that was already compliant.

Two things enforce it, deliberately belt-and-braces:

1. Stage 04 reads `db.get_pending("linkedin"...)`-side statuses only -- a
   `not_a_lead` row is not at `scraped`, so it is never selected.
2. Stage 04's read is additionally filtered:
   `db.get_pending("scraped", extra_where="has_cmp = false")`.

`has_cmp` is a native Postgres `BOOLEAN`. Always spell the filter
`has_cmp = false` / `has_cmp = true`; the SQLite-era `0`/`1` spelling is
stale and will error.

`mark_not_a_lead` records its reason in `last_error` -- that column carries
the CMP name for `not_a_lead` rows, and a genuine error message for
`*_failed` rows.

## Retry semantics

`db.mark_failed(cin, prefix, err)` increments `retry_count`:

- Under `MAX_RETRIES` (3): the row **keeps its current status**, so the same
  stage picks it up on the next cron run. A transient network blip never
  permanently drops a company.
- At or past `MAX_RETRIES`: the row moves to `{prefix}_failed` for manual
  review and leaves the pipeline.

`db.advance_status` resets `retry_count` to 0 and clears `last_error` on any
successful transition -- whatever failed before no longer matters. A caller
may override either default by passing it explicitly (this is how
`mark_not_a_lead` sets its reason).

`get_pending` orders by `updated_at ASC`, so a requeued row is retried ahead
of newly ingested ones rather than starving behind them.
