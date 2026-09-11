# ICP & persona architecture — build summary

Condensed from the CEO's workbook into implementable form.
Status column: **HAVE** = already collected, **PARTIAL** = collected sometimes,
**MISSING** = not collected at all today.

---

## 1. The flow (7 steps, credits spent only at step 5)

| Step | Logic | Output | Costs money |
|---|---|---|---|
| 1 | Account Score from MCA + web signals | qualified / not | no |
| 2 | Assign band B1–B6 (+ BR override) | company band | no |
| 3 | Industry overlay → privacy problem | priority use case | no |
| 4 | Persona shortlist by band + industry | P1/P2/P3 families | no (search is free) |
| 5 | **Contact enrichment — A/A+ only** | name, title, verified contact | **YES** |
| 6 | Contact score (seniority, function, relevance) | 0–100 | no |
| 7 | CRM handoff with reason + trigger + next action | sales-ready lead | no |

---

## 2. Account Score — 6 dimensions, 100 points

| Dimension | Weight | Signals | Source | Status |
|---|---|---|---|---|
| Privacy Exposure | **25** | cookies, trackers, pixels, forms, customer accounts, third parties | our scanner | **HAVE** — this is `consent_gap_score` |
| Digital Exposure | **20** | website, login, forms, ecommerce, app, API, payment | our scanner | **HAVE** — partial (no login/API detection) |
| Company Potential | **20** | employees, revenue, paid-up capital, growth, entity count | MCA + enrichment | **PARTIAL** — capital yes, employees 58%, revenue no |
| Industry Priority | **15** | SaaS, D2C, BFSI, Healthcare, EdTech, Manufacturing… | MCA + classification | **PARTIAL** — MCA gives 20 broad categories, spec wants 12 specific |
| Buying Capacity | **10** | presence of CTO/CIO/CISO/legal/compliance | people enrichment | **MISSING** — needs Apollo search on every account (free) |
| Trigger / Intent | **10** | funding, hiring, expansion, new digital estate, enterprise customers | web/search | **MISSING** — no source today |

**Bands:**

| Score | Class | Action |
|---|---|---|
| 80–100 | A+ / Sales Now | spend enrichment credits, P1 persona, outreach |
| 65–79 | A / High Potential | enrich P1/P2, targeted campaign |
| 45–64 | B / Nurture | product-led, free scan, nurture |
| <45 | C / Low Priority | no paid enrichment, automated revisit |

**Ceiling today is 90/100**, since Trigger/Intent has no data source.

---

## 3. Company bands B1–B6

| Band | Label | Employees | Domains | Buying centre | Plan | Motion |
|---|---|---|---|---|---|---|
| B1 | Micro / Emerging | <100 | 1–2 | founder-led | Free | product-led |
| B2 | Small Digital | <100–250 | 1–3 | founder + tech/ops | Free → Pro | PLG + targeted outbound |
| B3 | Growth / Startup | 50–500 | 3–5 | CTO + founder + sec/legal | Pro | sales-assisted |
| B4 | Mid-Market | 100–1,000 | 3–5 | CIO/CTO + CISO + legal | Pro → Enterprise | consultative |
| B5 | Upper Mid-Market | 500–5,000 | 6+ | CIO + CISO + DPO + legal | Enterprise | account-based |
| B6 | Enterprise / Group | 1,000+ | 6+ | CIO + CPO/DPO + CISO + GC + procurement | Enterprise + services | named account / ABM |
| **BR** | **Regulated override** | any | any | CIO/CISO/DPO/GC | Enterprise | ABM / exec-led |

**BR overrides the B-band** whenever BFSI, healthcare, insurance, government or
complex regulated signals are present — regardless of size.

Banding inputs we lack: **domain count** (we resolve one domain per company,
never count them) and **employee count** for 42% of enriched accounts.

---

## 4. Persona families — search title clusters, never exact match

| Family | Titles to search | Bands | Priority |
|---|---|---|---|
| Founder / CEO | Founder, Co-Founder, CEO, MD, Managing Director, Director | B1–B3 | P1 in B1–B2 |
| CTO / Technology | CTO, VP Engineering, Head of Technology, Technology Director, Head IT | B2–B6 | P1 in B3–B5 |
| CIO / IT | CIO, IT Director, Head of IT, Chief Digital Officer | B4–B6 | P1 in B4–B6 |
| CISO / Security | CISO, Head InfoSec, Head Cyber Security, Security Director | B3–B6 | P1/P2 |
| DPO / Privacy | DPO, Chief Privacy Officer, Privacy Officer, Head Privacy | B4–B6 | P1 |
| Legal / Compliance | General Counsel, Chief Legal Officer, Head Legal, Head Compliance | B3–B6 | P1/P2 |
| COO / Operations | COO, Head Operations, Operations Director | B2–B5 | P2 |
| Product / Digital | CPO, VP Product, Head Product, Head Digital | B2–B5 | P2 |
| HR / People | CHRO, HR Director, Head HR, HR Operations | B4–B6 | P2 |
| Procurement | CPO, Head Procurement, Procurement Director | B5–B6 | P3 |

Note: **CTO outranks Founder once a real technology function exists** (SaaS rule).
**5–8 stakeholders for B5/B6** — never single-thread an enterprise account.

---

## 5. Industry priority + the privacy problem to lead with

| Industry | Pri | First privacy problem | Primary persona | Special rule |
|---|---|---|---|---|
| SaaS / IT / Software | A+ | fiduciary + processor dual role | CTO | CTO outranks founder |
| D2C / Ecommerce | A+ | tracker sprawl, loyalty/returns data | Founder / CEO | digital exposure can outweigh capital |
| BFSI / FinTech | A+ | retention vs erasure conflict | CIO / CISO | **regulated override** |
| Healthcare / HealthTech | A+ | fragmented patient data | CIO / CISO | multi-system estate > cookie count |
| EdTech | A+ | children's / minor data | Founder / CTO | must validate, do not infer from sector |
| Insurance | A+ | retention + claims + third parties | CIO / CISO | **regulated override** |
| Manufacturing & Logistics | A | workforce / field data | CIO / IT Head | **low cookie count ≠ low opportunity** |
| Retail / Consumer | A | identity + marketing + loyalty | CIO / CTO | score on digital footprint |
| Travel / Hospitality | A | booking, loyalty, third-party processing | CIO / CTO | third-party processor signals matter |
| Staffing / HR Services | A | candidate + client data | CEO / COO | prioritise digital candidate journeys |
| Education / Universities | A | student data, many systems | CIO / IT Director | institutional complexity > website signals |
| Professional / Business Services | B | client + employee data, manual processes | Founder / MD | founder-led decisions common |

---

## 6. Operating rules (the eight non-negotiables)

1. **MCA is the universe, not the ICP** — not every active company is relevant.
2. **Paid-up capital is a proxy only** — never band on capital alone.
3. **Website signals are intent signals** — never claim regulatory non-compliance from a scanner.
4. **Spend credits late** — enrich people only after the Account Score clears.
5. **Search title families** — never exact-title matching; Indian titles vary wildly.
6. **Multi-thread enterprise** — 5–8 stakeholders for B5/B6.
7. **Industry overrides size** — don't downgrade BFSI/healthcare for low website exposure.
8. **Keep human judgement** — scores prioritise, they don't determine compliance.

---

## 7. What to build, in dependency order

**Already have, needs mapping only**
- Privacy Exposure ← `consent_gap_score` (rescale 0–100 → 0–25)
- Digital Exposure ← forms, ecommerce, payment, tech stack (add login/API detection)
- Company Potential ← `paidup_capital` + `linkedin_employee_count`

**Small additions**
- Industry taxonomy map: MCA's 20 categories → the 12 above. "Business Services" (26% of corpus) is ambiguous — resolve it from scraped site content, not MCA.
- Band assignment B1–B6 from employees + domains + entity count.
- Regulated override BR from NIC code / industry.

**Needs new collection**
- **Domain count per company** — currently we find one and stop.
- **Buying Capacity** — Apollo search (free) on every scored account to detect whether CTO/CISO/legal exist.
- **Trigger / Intent** — no source today. Funding, hiring, expansion. Would need a new feed.

**Known tension to settle first**
Scored against our current 124 analysed companies, ~1% reach A/A+. Our pipeline
finds small companies with real exposure; this framework rewards company size and
buying-centre maturity. Both are valid, but they select different accounts — decide
whether a 12-person firm with 18 trackers is a **lead** or a **free-tier signup**
before wiring the gate.
