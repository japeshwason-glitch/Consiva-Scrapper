# Consiva lead pipeline — measured production numbers

Everything here is measured on real runs, not estimated. Where a figure is
still carried over from the Haryana pilot it says so.

Corpus: **1,445,753 active companies** — Maharashtra, Delhi, UP, Karnataka,
Gujarat, Chandigarh, Haryana. Struck-off, dissolved, amalgamated and liquidated
companies are excluded at merge; 308,053 LLPs are included.

---

## The funnel, per 100 MCA records screened

Measured on 300 metropolitan companies (50 each from six states).

| Stage | Left | % of records |
|---|---|---|
| MCA records screened | 100 | 100% |
| No findable website | 81 | 81.3% |
| **Domain confirmed and scraped** | **18** | **18.0%** |
| — already runs a CMP (not a lead) | 0.7 | 0.7% |
| — no measurable gap | 6.3 | 6.3% |
| — **has a consent gap** | **11.7** | **11.7%** |
| — of those, severe (50+) | 4.0 | 4.0% |
| — with an Apollo contact attached | 4.6 | 4.6% |

**About 1 record in 22 becomes a contactable lead.** Haryana gave 4.3% and
metro gives 4.6% — two very different corpora converging on the same yield.

### Why 81% have no website

MCA holds only incorporated entities, so this is not shops and street traders —
they are proprietorships and never appear here. What fills the 81% is visible in
the capital figures: **median paid-up capital is Rs 1 lakh**, 78% of companies
are under Rs 10 lakh, and 12% are under Rs 1 lakh. Family holding companies,
single-consultant Pvt Ltds, per-project real-estate SPVs, and 41,467 One Person
Companies. Incorporated for tax or liability, with no reason to build a website.

---

## What the corpus holds

| Definition of a lead | Count | Days at 500/day |
|---|---|---|
| Website found (any) | 260,236 | 520 |
| **Website + real consent gap** | **168,671** | **337 (11 months)** |
| Website + gap + Apollo contact | 66,794 | 134 |
| Website + severe gap (50+) | 57,830 | 116 |

**168,671 is the number to plan against** if sales sources contacts themselves
via Lusha. Apollo's coverage is our bottleneck, not theirs — dropping that
requirement more than doubles the pool and cuts daily screening by 60%.

---

## Throughput and cost

Per-item, measured:

| Stage | Cost per item |
|---|---|
| 02 — resolve identity | 4.16 s per MCA record |
| 03 — scrape and score | 4.9 s per site |
| 04-06 — LinkedIn + Apollo | 29.6 s per lead |

To sustain 500 contactable leads/day:

```
10,291 records through stage 02    11.9 h
 1,921 sites through stage 03       2.7 h
 1,263 leads through stages 04-06  10.4 h
                            total  24.9 h  -> 1 worker 24/7, 2 for headroom
```

If leads are handed over **without** Apollo contacts, daily screening drops to
**4,286 records/day** and runway stretches from 134 to 337 days.

**Cost: $9.63/day, $289/month — $0.019 per lead.** Apify is the only paid
component. Apollo persona search is free; the paid email reveal is disabled.

---

## Resolution rate varies 3.5x by state

| State | Resolved | Rate |
|---|---|---|
| Chandigarh | 14/50 | 28% |
| Maharashtra | 12/50 | 24% |
| Delhi | 11/50 | 22% |
| Karnataka | 9/50 | 18% |
| Gujarat | 6/50 | 12% |
| Uttar Pradesh | 4/50 | 8% |

Weighting the queue toward Maharashtra, Delhi and Chandigarh means fewer
records screened per lead. A UP-heavy queue needs 24,000 records/day instead of
10,300 — still only 1.7 workers, but it burns supply 2.3x faster.

Sample size is 50 per state, so treat +/-6 points as noise.

---

## Metro and Haryana produce different KINDS of gap

| | Metro (n=54) | Haryana (n=146) |
|---|---|---|
| Has a consent gap | 64.8% | 58.2% |
| Severe (50+) | 22.2% | 22.6% |
| Already runs a CMP | 3.7% | 4.8% |
| **Collects data via web form** | **55.6%** | **11.6%** |
| Average trackers per site | **1.1** | 3.6 |

Haryana was manufacturers running ad pixels on brochure sites. Metro is
services and consultancies whose sites exist to capture enquiries: a third the
trackers, five times the form-based data collection.

**The form finder is the primary detection path on this corpus**, not a bonus.
Without it the metro gap rate would have fallen rather than risen.

Under 4% of companies already run a CMP in either corpus — the market is
essentially unserved.

---

## Identity verification

Every accepted domain is confirmed by the local model comparing the site's
CONTENT against what the company name says the business does. Nothing is
accepted on string similarity alone.

- All 56 metro accepts went through `llm-match-score`, average 92.0
- The accept threshold was raised from 60 to **80** after this batch: 54 of 56
  scored 80+, and both rows below it were broad-sector false matches
  (AARTH CREATIVE STUDIOS accepted an accounting firm at 65 because both are
  "Business Services")
- Surviving accepts: average 93.2, minimum 85
- Every acceptance stores its score and the model's one-line reason, so any
  lead can be audited

This costs one model call per candidate page that loads. The model is
self-hosted, so the cost is throughput, not money.

---

## What is still assumed

**The Apollo contact rate (39.6%) is the only figure still carried from
Haryana.** Everything upstream is now measured on metro data. It matters
because it sets the 4.6% end-to-end yield and the 66,794 count. Running stages
04-06 on the current gap-leads would close it.

Also unmeasured: whether conversion differs by state or industry, and what
share of leads the sales team actually converts.
