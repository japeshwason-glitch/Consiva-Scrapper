# Consent-gap scanner: audit for report planning

Read-only inspection of repo HEAD `18e6fa4` (2026-10-01) and the live
databases. No code, schema or data was changed. The containers that are running
execute the same scanner code as the repo: `stages/03_scrape_playwright.py`,
`fingerprints.py`, `partner_import_analyse.py`, `crm_push.py` and `db.py` are
byte-identical between the repo and both the `scheduler` and `api` containers.

> **Where the headline numbers come from.** The figures 806 severe / 591
> moderate / 861 clean / 93 CMP out of 2,351 are **not** from the Postgres
> `companies` table. They are the **partner-import job** (job 3) results stored
> in the sales team's SQL Server table `CookieConsentDB.dbo.CRM_Scrapper`
> (`Source = 'PartnerImport'`). They are served by `/api/partner/status`, and
> "moderate" is that endpoint's `gap` bucket. Both result sets use the same
> scanner (`scrape_one()` in stage 03). They store very different amounts of
> detail, though. See §4 and §6.

---

## 1. Code locations

| File | Purpose | Key functions / constants |
|---|---|---|
| `stages/03_scrape_playwright.py` | The scanner. One headless Chromium context per site: loads the homepage, collects cookies, HTML, shadow DOM, script srcs and forms, then visits up to 3 subpages for soft fields. | `scrape_one()`, `load()`, `run()`, `main()`, `build_consent_summary()`, `build_form_sentence()`, `pick_subpages()`, `count_roles()`; JS: `EXTRACT_JS`, `SHADOW_JS`, `FORM_JS`; constants `NAV_TIMEOUT_MS`, `SETTLE_MS`, `NETWORK_IDLE_TIMEOUT_MS`, `MAX_CONCURRENCY`, `USER_AGENT`, `SCRAPE_FIELDS` |
| `fingerprints.py` | All deterministic detection and scoring: CMP signatures, banner/policy patterns, cookie classification, tech stack, form analysis, the score. | `detect_cmp()`, `detect_banner()`, `detect_privacy_policy()`, `detect_tech()`, `analyse_cookies()`, `cookie_record()`, `_cookie_category()`, `registrable()`, `form_gap()`, `classify_form_fields()`, `detect_form_embed()`, `gap_score()`, `consent_score()`, `is_lead_score()`, `is_severe_score()`; constants `CMP_SIGNATURES`, `BANNER_TEXT_PATTERNS`, `BANNER_SELECTOR_HINTS`, `PRIVACY_LINK_PATTERNS`, `TRACKING_COOKIE_PREFIXES`, `ADVERTISING_COOKIE_PREFIXES`, `SESSION_REPLAY_COOKIE_PREFIXES`, `ANALYTICS_SIGNATURES`, `MARKETING_SIGNATURES`, `FORM_EMBED_SIGNATURES`, `PERSONAL_FIELD_PATTERNS`, `ALIGNED`, `SEVERE_AT`, `LEAD_SQL`, `SEVERE_SQL`. Dead code: `consent_gap_score()`, `tracking_cookies()` (old scoring, not called). `is_parked()` / `is_blocked()` exist here but only `resolver.py` (stage 02) calls them, never the scanner. |
| `partner_import_analyse.py` | Job 3. Claims `PartnerImport` rows in `dbo.CRM_Scrapper`, runs `scrape_one()` with no LLM, and writes summary columns back. **This is the source of the 2,351 figures.** | `claim()`, `analyse()`, `record()`, `finish()`, `values_for()`, `domain_of()`, `ensure_columns()`, `done_today()`; `DAILY_TARGET`, `CHUNK=50`, `CONCURRENCY=6`, `MAX_ATTEMPTS=3`, `SITE_TIMEOUT_S=120`, `STALE_CLAIM_H=6` |
| `api.py` | Assigns the severe / gap / clean / has_cmp verdicts for partner rows, plus dashboard stats for `companies`. | `partner_status()` (verdict logic), `partner_rows()`, `stats()`, `companies()` |
| `partner_snapshot.py` | Copies partner rows from SQL Server into `/tmp/partner_snapshot.json` (in the api container) every 60 s. The API reads only this file. | `fetch()`, `main()` |
| `crm_push.py` | Pushes MCA-pipeline leads into `dbo.CRM_Scrapper`. Its `form_sentence()` builds the `CollectsViaForm` text for all three jobs. | `to_row()`, `form_sentence()`, `connect()` |
| `inbound_analyse.py` | Job 2: runs `scrape_one()` on the domain behind inbound leads' work emails and writes to `crm.CrmLeads`. | `analyse()`, `write_result()`, `domain_of()` |
| `analyse_urls.py` | Ad-hoc: scans hand-supplied URLs into `companies`. | `analyse()` |
| `recheck_banners.py` | One-off re-measurement after the shadow-DOM fix (Sep 24). | `screen()`, `remeasure()`, `scan()`, `apply()` |
| `backfill_forms.py` | One-off re-measurement for rows delivered before form detection existed. | `measure()` |
| `run_daily.py` / `scheduler.py` | Orchestration: `scheduler.py` runs inbound, then `run_daily.py` (which invokes stage 03), then the partner job. | `cycle()`, `run()`, `run_once()`, `run_partner()` |
| `llm.py` | Local LLM. Used in stage 03 **only** for tagline, clients, founder, open roles and blog post. Never for any consent field. | `extract_soft_fields()`, `chat_json()` |
| `db.py` | Postgres access for the MCA path. | `get_pending()`, `advance_status()`, `mark_failed()`, `mark_not_a_lead()`, `count_by_status()`, `_serialize_json_fields()`; `MAX_RETRIES=3` |
| `schema.sql`, `migrations/001–005` | `companies` DDL; 003/004/005 flip score polarity. | — |

---

## 2. Detection logic

### 2.1 Page loading (`scrape_one()` → `load()`)

| Aspect | Actual behaviour |
|---|---|
| Browser | Chromium via Playwright, `headless=True` (`--headed` CLI flag in docstring is **not implemented**; argparse has no such option) |
| Context | Fresh `browser.new_context()` per site, so there is no prior state. `user_agent` = `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36`, `viewport` 1366×900, `locale="en-IN"`, `ignore_https_errors=True`. No timezone, geolocation or proxy set; traffic exits from the host's own (Indian) IP. |
| URL | `https://{domain}`; if `goto` raises, retries once with `http://{domain}`. Redirects are followed and `final_url` / `final_domain` are recorded. |
| Wait | `goto(wait_until="domcontentloaded", timeout=25 000 ms)`, then `wait_for_load_state("networkidle", timeout=8 000 ms)` (timeout swallowed), then a fixed **4 000 ms** settle. |
| HTTP status | **Not checked.** A 404/500/bot-wall/parked page is scanned like a real site. |
| Subpages | After the consent measurement, up to 3 same-host subpages (first link matching careers / team / blog patterns) are loaded with a 1 200 ms settle. **Used only for soft fields** (roles, blog, team text). Cookies, forms and banner are measured **on the homepage only**. There is no contact-page visit. |
| Concurrency | `asyncio.Semaphore`: stage 03 default 4 (`run_daily` passes 6); partner job 6; inbound 6. |

### 2.2 Cookie capture

- `context.cookies()` is read **once, after the homepage load and settle, before any interaction**. Nothing is ever clicked, so every cookie present was set without consent. That is the core measurement.
- Each cookie is turned into a record by `cookie_record()`. The **cookie value is deliberately dropped**. Recorded fields: `name`, `domain`, `party` (first/third), `session`, `lifetime_days`, `secure`, `same_site`, `http_only`, `category`.
- **First- vs third-party:** `registrable(cookie.domain) != registrable(final_domain)`, using `tldextract` offline. It compares against the domain the browser **landed on**, not the one requested.
- **Category** (`_cookie_category()`, first match wins):
  1. `advertising`: prefix in `ADVERTISING_COOKIE_PREFIXES`
  2. `session_replay`: prefix in `SESSION_REPLAY_COOKIE_PREFIXES`
  3. `analytics`: any other prefix in `TRACKING_COOKIE_PREFIXES`
  4. `third_party`: unknown name but third-party domain (a backstop)
  5. `functional`: everything else
- "Tracking" = categories 1–4. `functional` cookies are counted in `cookies_total` but are not a finding.
- **Timing:** no per-cookie timestamp. The only timing is "present at roughly domcontentloaded + up to 8 s idle + 4 s".

```python
TRACKING_COOKIE_PREFIXES = (
    "_ga", "_gid", "_gat", "__utm", "_gcl", "_dc_gtm",      # Google
    "_fbp", "_fbc", "fr",                                    # Meta
    "_hjs", "_hj",                                           # Hotjar
    "_clck", "_clsk", "MUID",                                # Microsoft Clarity
    "__hstc", "hubspotutk", "__hssrc", "__hssc",             # HubSpot
    "_lfa", "li_sugr", "bcookie", "lidc", "UserMatchHistory",# LinkedIn
    "IDE", "test_cookie", "NID", "DSID",                     # DoubleClick
    "mp_", "ajs_", "amplitude_",                             # product analytics
    "_pk_", "_omappvp", "_uetsid", "_uetvid",                # Matomo/Bing
    "YSC", "VISITOR_INFO1_LIVE",                             # YouTube embeds
)
ADVERTISING_COOKIE_PREFIXES = (
    "_fbp", "_fbc", "fr", "IDE", "test_cookie", "NID", "DSID",
    "_gcl", "li_sugr", "bcookie", "lidc", "UserMatchHistory",
    "_uetsid", "_uetvid", "YSC", "VISITOR_INFO1_LIVE",
)
SESSION_REPLAY_COOKIE_PREFIXES = ("_hj", "_hjs", "_clck", "_clsk", "MUID")
```

### 2.3 Network requests / trackers

- **Network requests are not captured.** There is no `page.on("request")`, `route()`, HAR or request log anywhere in the code.
- "Trackers" in the score means **tracking cookies only**. A tracker that fires a beacon without setting a cookie, or that uses localStorage, is invisible to the score.
- Separately, the **tech stack** (`detect_tech()`) does substring matching of vendor tokens against `HTML + shadow-DOM HTML + script src URLs`. It records **vendor names** (lists), but this **does not feed the score or the summary**:

```python
ANALYTICS_SIGNATURES = (
    ("Google Analytics 4", ("gtag/js", "google-analytics.com/g/collect", "G-")),
    ("Universal Analytics",("google-analytics.com/analytics.js", "ga('create'")),
    ("Google Tag Manager",  ("googletagmanager.com/gtm.js", "GTM-")),
    ("Hotjar",              ("static.hotjar.com", "hjSetting")),
    ("Microsoft Clarity",   ("clarity.ms",)),
    ("Matomo",              ("matomo.js", "piwik.js")),
    ("Mixpanel",            ("mixpanel",)),
    ("Segment",             ("segment.com/analytics.js", "cdn.segment")),
    ("Plausible",           ("plausible.io",)),
    ("Yandex Metrica",      ("mc.yandex.ru",)),
)
MARKETING_SIGNATURES = (
    ("Meta Pixel",       ("connect.facebook.net", "fbq(")),
    ("LinkedIn Insight", ("snap.licdn.com", "_linkedin_partner_id")),
    ("HubSpot",          ("js.hs-scripts.com", "hs-analytics")),
    ("Mailchimp",        ("chimpstatic.com", "mailchimp")),
    ("Zoho",             ("zoho.com", "salesiq.zoho")),
    ("Freshworks",       ("freshchat", "freshworks")),
    ("Intercom",         ("intercom.io", "widget.intercom")),
    ("Drift",            ("drift.com", "driftt")),
    ("Tawk.to",          ("tawk.to",)),
    ("Crisp",            ("crisp.chat",)),
    ("Google Ads",       ("googleadservices", "googlesyndication")),
    ("WhatsApp Business",("wa.me/", "api.whatsapp.com")),
)
```

`G-` and `GTM-` match case-sensitively as `\bG-[A-Z0-9]{4,}`; all other tokens match case-insensitively. These columns (`tech_analytics_tools`, `tech_marketing_tools`) exist only in Postgres. The partner job does not write them anywhere.

### 2.4 Cookie-banner detection (`detect_banner()`)

The banner check is a substring/regex search over the **whole blob** (raw HTML + shadow-root HTML + script srcs), lowercased. It returns true if any selector hint or text pattern appears **anywhere**. It does not check visibility, position or whether the element is a banner at all.

```python
BANNER_SELECTOR_HINTS = (
    "cookie-banner", "cookie-consent", "cookie-notice", "cookie-bar",
    "cookiebanner", "cookieconsent", "gdpr-banner", "consent-banner",
    "cc-banner", "cookie-popup",
)
BANNER_TEXT_PATTERNS = (
    r"we use cookies", r"this (?:website|site) uses cookies",
    r"cookie (?:policy|preferences|settings|consent)",
    r"accept (?:all )?cookies", r"by continuing to (?:use|browse)",
    r"manage (?:your )?(?:cookie )?preferences",
)
```

Shadow DOM has been included since Sep 24 (`SHADOW_JS`); before that, WPConsent-style banners were missed.

### 2.5 CMP detection (`detect_cmp()`)

Substring match against the same blob. The first vendor in tuple order whose any token appears wins.

```python
CMP_SIGNATURES = (
    ("OneTrust",          ("onetrust", "optanon", "cookielaw.org", "otSDKStub")),
    ("Cookiebot",         ("cookiebot", "consent.cookiebot.com", "CybotCookiebot")),
    ("CookieYes",         ("cookieyes", "cdn-cookieyes.com", "cky-consent")),
    ("Osano",             ("osano.com", "osano-cm")),
    ("Termly",            ("termly.io", "termly-code")),
    ("Quantcast",         ("quantcast", "quantcast.mgr", "__cmpcallbacks")),
    ("Didomi",            ("didomi.io", "didomi-host", "window.didomi")),
    ("Usercentrics",      ("usercentrics", "uc-block", "app.usercentrics.eu")),
    ("iubenda",           ("iubenda.com", "iubenda_cs")),
    ("Complianz",         ("complianz", "cmplz-")),
    ("CookieScript",      ("cookie-script.com", "cookiescript")),
    ("Borlabs",           ("borlabs-cookie",)),
    ("TrustArc",          ("trustarc", "truste.com", "consent.trustarc.com")),
    ("Seers",             ("seersco.com", "seers-cmp")),
    ("Secure Privacy",    ("secureprivacy.ai",)),
    ("CookiePro",         ("cookiepro.com",)),
    ("Ketch",             ("ketchcdn.com", "ketch.com")),
    ("Klaro",             ("klaro-config", "klaro.js")),
    ("tarteaucitron",     ("tarteaucitron",)),
    ("Civic Cookie Control", ("civiccomputing", "cookiecontrol")),
    ("GDPR Cookie Consent (WP)", ("cookie-law-info", "cli-modal", "cli_settings")),
    ("Moove GDPR (WP)",   ("moove_gdpr", "gdpr-cookie-compliance")),
    ("WPConsent (WP)",    ("wpconsent",)),
)
```

A CMP being **present** is enough. Whether it actually blocks trackers is never checked. In Postgres, **218 of 292** CMP rows still set tracking cookies before consent; in the partner set, **66 of 93**.

### 2.6 Interaction with the banner

**None. The scanner only observes.** No click, no scroll, no Accept or Reject. Post-consent behaviour (trackers after Reject, banner reappearing) is never measured.

### 2.7 Privacy / cookie-policy detection (`detect_privacy_policy()`)

This is a regex over the whole blob, not a link check and not a page fetch:

```python
PRIVACY_LINK_PATTERNS = (
    r"privacy[-_ ]?policy", r"cookie[-_ ]?policy", r"privacy[-_ ]?notice",
    r"data[-_ ]?protection",
)
```

The policy page is never opened, so its contents (grievance officer, purposes, retention) are not inspected.

### 2.8 Forms: **yes**, homepage only (`FORM_JS` + `form_gap()`)

- Up to 12 `<form>` elements on the **homepage**. Hidden, submit, button and image inputs are ignored.
- Each field is described by `name`, `id`, `placeholder`, `type`, `aria-label` and `<label>` text, then classified by regex into `email` / `phone` / `name` / `address` / `company` / `message` (`PERSONAL_FIELD_PATTERNS`). A form counts as "collecting" only if it has `email`, `phone`, `name` or `address`.
- `has_consent_checkbox`: any `input[type=checkbox]` whose enclosing label or parent text matches `/consent|agree|privacy|terms|permission|opt.?in/`. **The `checked` state is not read**, so a pre-ticked box counts as consent. A terms-only box also counts.
- `has_privacy_link`: an `<a>` **inside the `<form>` element** whose href or text matches `/privacy|policy|terms/`. Links next to the form but outside it are not seen.
- `external_action`: the form `action` host differs from the page host.
- Aggregation: `consent_checkbox` = **all** collecting forms have one; `privacy_link` = **any** has one; `external_hosts` = set of foreign action hosts.
- `detect_form_embed()`: substring match for Google Forms, Typeform, HubSpot Forms, Zoho Forms, JotForm, Wufoo, Formspree, Mailchimp Form, WPForms, Contact Form 7 and Gravity Forms. Reported only; it does not affect the score.

### 2.9 Other checks

| Check | Implemented? | How |
|---|---|---|
| Reject button presence | **No** | — |
| Pre-ticked consent categories (banner) | **No** | — |
| Pre-ticked form checkbox | **No** | `checked` never read |
| "By continuing you agree" wording | **Partial** | `by continuing to (use\|browse)` is one of the banner-presence patterns. It sets `has_cookie_banner=true` and is **not** recorded or scored as a separate implied-consent finding. |
| Preference / withdraw link | **No** | "manage preferences" / "cookie settings" text only feeds banner presence |
| Language options | **No** | — |
| Grievance / contact info | **No** | `footer_phone` / `footer_address` are extracted from footer text by regex for CRM use, not as a compliance check |
| Child-directed signals / age fields | **No** | No DOB/age field category exists |

---

## 3. Severity classification

### 3.1 The score (`fingerprints.py`)

```python
ALIGNED = 100          # nothing to sell: no gap, or already runs a CMP
SEVERE_AT = 50         # at or below this is a severe gap
LEAD_SQL = f"consent_gap_score < {ALIGNED}"
SEVERE_SQL = f"consent_gap_score <= {SEVERE_AT}"

def gap_score(has_cmp, analysis, banner, policy):
    if has_cmp or not analysis["tracking"]:
        return 0
    cats = analysis["by_category"]
    weighted = (
        3.0 * len(cats["advertising"])
        + 3.0 * len(cats["session_replay"])
        + 2.0 * len(cats["third_party"])
        + 1.5 * len(cats["analytics"])
    )
    score = min(40.0, weighted)
    score += min(15.0, 3.0 * analysis["third_party_count"])
    score += min(10.0, 2.0 * analysis["long_lived_count"])
    if not banner:
        score += 25
    if not policy:
        score += 15
    if cats["session_replay"]:
        score += 10
    if cats["advertising"]:
        score += 5
    return int(min(100, round(score)))

def consent_score(has_cmp, analysis, banner, policy, forms=None):
    base = gap_score(has_cmp, analysis, banner, policy)
    if has_cmp:
        return ALIGNED  # they already run a CMP; not a lead
    form = forms or {}
    if not form.get("has_data_form"):
        return ALIGNED - base
    extra = 0
    if not form.get("consent_checkbox"):
        extra += 25
    if not form.get("privacy_link") and not policy:
        extra += 10
    if form.get("external_hosts"):
        extra += 10
    return ALIGNED - int(min(100, base + extra))
```

### 3.2 Verdict buckets (`api.py` `partner_status()`, used for the 2,351)

```python
if r["status"] == "done" and r["gap_score"] is not None:
    if (r["finding"] or "").startswith("CMP detected"):
        verdicts["has_cmp"] += 1
    elif r["gap_score"] <= fingerprints.SEVERE_AT:
        verdicts["severe_gap"] += 1
    elif r["gap_score"] < fingerprints.ALIGNED:
        verdicts["gap"] += 1          # = "moderate"
    else:
        verdicts["clean"] += 1
```

The MCA pipeline uses the same thresholds. A CMP site is set to `status='not_a_lead'` by stage 03 (`db.mark_not_a_lead`).

### 3.3 Plain English

The score runs from **0 (worst) to 100 (no measured gap)**. Gap points are added up and then subtracted from 100.

1. **CMP found anywhere in the page source** → score 100, verdict **has_cmp**, not a lead. Nothing else matters, even if trackers fire.
2. Otherwise, **cookie gap points** (only if at least one tracking cookie was set):
   - Tracker weight, capped at 40: 3 per advertising cookie, 3 per session-replay cookie, 2 per unknown third-party cookie, 1.5 per analytics cookie.
   - +3 per distinct third-party cookie domain, cap 15.
   - +2 per tracker with lifetime > 180 days, cap 10.
   - +25 if no banner text, +15 if no policy text, +10 if any session replay, +5 if any advertising.
   - **With zero tracking cookies, the cookie part is 0, whatever the banner or policy status.**
3. **Form gap points** (only if a homepage form takes email/phone/name/address): +25 if not every such form has a consent-ish checkbox; +10 if no privacy link in any form **and** no policy text on the page; +10 if a form posts to another host.
4. Score = 100 − min(100, cookie + form points).
5. Bucket: **severe** ≤ 50 · **moderate** 51–99 · **clean** = 100.

Consequences worth knowing for the report:
- **"Clean" means "no tracking cookies and no homepage data form"**, not "compliant". Of the 861 partner "clean" rows, 824 have no banner and 678 have no privacy policy. In Postgres, 1,902 of 2,746 clean rows set zero cookies of any kind.
- A site with only a no-consent form and nothing else scores 75 (moderate).
- A site needs trackers **plus** a missing banner or policy, or trackers plus a bad form, to reach severe.

Worked check (severe sample in §5.2, score 7): tracker weight 9.5 + third-party 3 + long-lived 6 + no banner 25 + no policy 15 = 58 (rounded); form no checkbox 25 + no link/policy 10 = 35; 100 − 93 = **7** ✓.

---

## 4. Output schema

### 4.1 Partner job → `CookieConsentDB.dbo.CRM_Scrapper` (source of the 2,351)

Live column types from `INFORMATION_SCHEMA` (the table belongs to the sales team, not this repo). Columns the scanner job writes are marked **W**.

| Column | Type | W | Filled from |
|---|---|---|---|
| Id | int, not null | | identity |
| CompanyName | nvarchar(250), not null | | partner |
| Website | nvarchar(500) | W* | `final_domain` |
| GapScore | int | W* | `consent_gap_score` |
| ContactName / ContactTitle | nvarchar(200) | | partner/sales (excluded from this doc) |
| CompanyScale | nvarchar(100) | | partner headcount band |
| Industry | nvarchar(200) | | partner |
| TrackersPreConsent | int | W* | `tracking_cookie_count` |
| ThirdPartiesCount | int | W* | `third_party_count` |
| CookieBanner | nvarchar(100) | W* | `"present"` / `"none"` |
| CollectsViaForm | nvarchar(max) | W* | `crm_push.form_sentence()` |
| ConsentFinding | nvarchar(max) | W* | `consent_gap_summary`, or `"could not analyse …"` on terminal failure |
| TargetTitle1–3 | nvarchar(120) | W* | `personas.predict()` (only if no ContactName) |
| CreatedAt / UpdatedAt | datetime2 | W (UpdatedAt) | server |
| Lusha* (8 cols) | various | | Lusha enrichment (excluded) |
| Source | nvarchar(50) | | `'PartnerImport'` |
| SourceMarketingPartnerId | uniqueidentifier | | partner |
| ScrapeStatus | nvarchar(20) | W | `NULL → running → done / retry / failed / no_website` |
| ScrapeAttempts | int, not null | W | failure count |
| ScrapedAt | datetime2 | W | claim/finish time, UTC |

\* Written as `COALESCE(existing, ours)`: **fill-blanks only**. A value already on the row wins.

**Not stored for partner rows:** per-cookie records, cookie names (except the ≤6 quoted in ConsentFinding), third-party domain list (except ≤5 quoted), `has_cmp`/`cmp_name` as columns (only as the ConsentFinding prefix "CMP detected: X"), `has_privacy_policy` (only in the text), form field list (only in the CollectsViaForm text), tech stack, lifetimes. There are no JSON columns.

### 4.2 MCA / ad-hoc scanner → Postgres `consiva.companies`

Scanner-written columns (from `SCRAPE_FIELDS` and `schema.sql`); full DDL in `schema.sql` / `migrations/001_initial.sql`.

| Column | Type | Notes |
|---|---|---|
| status | text | `scraped`, `not_a_lead` (CMP), or stays `resolved` → `scrape_failed` |
| retry_count / last_error | int / text | failure path; `last_error` = `"already has a CMP: X"` for not_a_lead |
| updated_at | timestamptz | the only scan timestamp |
| final_domain | text | landed host |
| has_cmp / cmp_name | boolean / text | |
| has_cookie_banner / has_privacy_policy | boolean | |
| consent_gap_score | integer | 0 = worst, 100 = aligned |
| consent_gap_summary | text | deterministic sentence |
| cookies_total / tracking_cookie_count | integer | |
| third_party_count / max_cookie_lifetime_days | integer | |
| **pre_consent_cookies** | **jsonb** | array of cookie records (below) |
| **third_party_domains** | **jsonb** | array of strings |
| has_data_form / form_consent_checkbox | boolean | checkbox NULL when no data form |
| **form_field_types** | **jsonb** | sorted array of `email/phone/name/address/company/message` |
| **form_third_party_hosts** | **jsonb** | array of hosts |
| form_embed_provider | text | |
| tech_cms / tech_ecommerce_platform / tech_payment_gateway | text | |
| **tech_analytics_tools / tech_marketing_tools** | **jsonb** | arrays of vendor labels |
| company_tagline, recent_blog_title, recent_blog_date | text | LLM soft fields |
| **notable_clients / notable_open_roles** | **jsonb** | arrays of strings (LLM) |
| open_roles_count / team_headcount_site | integer | headcount retired (always NULL) |
| founder_name_site, footer_address, footer_phone | text | person/contact-ish; excluded from samples |

`cookie_categories` and `subpages` are computed in `scrape_one()` but **not** written (not in `SCRAPE_FIELDS`).

`pre_consent_cookies` element:

```json
{
  "name": "_ga",
  "domain": "sanatantech.com",
  "party": "first",            // "first" | "third"
  "session": false,
  "lifetime_days": 399,        // null for session cookies
  "secure": false,
  "same_site": "Lax",          // "Lax" | "Strict" | "None"
  "http_only": false,
  "category": "analytics"      // advertising | session_replay | analytics | third_party | functional
}
```

Other JSONB columns are flat string arrays, e.g. `"third_party_domains": ["www.google.com"]`, `"form_field_types": ["email","message","name","phone"]`.

`crm.CrmLeads` (inbound job) receives the same summary fields as `dbo.CRM_Scrapper`, plus `ScrapperWebsite`.

---

## 5. Sample records

Contact/person fields (`ContactName`, `ContactTitle`, Lusha*, `contact_*`, `apollo_person_id`, `founder_name_site`, `footer_phone`, `footer_address`) are excluded throughout.

### 5.1 Partner rows (`dbo.CRM_Scrapper`, as served by `/api/partner/rows`)

This is **everything** stored for the 2,351 rows. `finding` is truncated to 300 chars in the snapshot.

**severe**
```json
{"id": 7090, "company": "Drawings", "website": "www.drawings.co.in", "status": "done",
 "attempts": 0, "scraped_at": "2026-09-29T12:25:08.344248Z", "gap_score": 10,
 "trackers": 5, "third_parties": 1, "banner": "none", "scale": "1-50",
 "industry": "Paper , Publishing , Printing",
 "finding": "No consent management platform detected; 5 of 5 cookies are tracking cookies set before consent: _GRECAPTCHA, _gid, _gat_gtag_UA_126704679_1, _ga_M8EY1VNMYY, _ga; data sent to 1 third-party domains before consent: www.google.com; longest tracker lifetime 399 days, 2 trackers persist beyond 180 days;"}
```

**moderate** (score driven entirely by the form)
```json
{"id": 7208, "company": "NutBolt Entertainment Pvt Ltd", "website": "thenutbolt.com",
 "status": "done", "attempts": 0, "scraped_at": "2026-09-29T12:30:22.547839Z",
 "gap_score": 75, "trackers": 0, "third_parties": 0, "banner": "present",
 "scale": "1-50", "industry": "Advertising , Media",
 "finding": "No consent management platform detected; no tracking cookies set before consent; cookie banner present; privacy/cookie policy linked; collects personal data via 1 web form(s) (company, email, message, name, phone) with no consent checkbox and no privacy link on the form."}
```

**clean** (no banner, no policy, still "clean")
```json
{"id": 7178, "company": "Trijal Enterprise Pvt Ltd", "website": "trijalenterprise.com",
 "status": "done", "attempts": 0, "scraped_at": "2026-09-29T12:27:48.149677Z",
 "gap_score": 100, "trackers": 0, "third_parties": 0, "banner": "none",
 "scale": "1-50", "industry": "Gems , Jewellery , Watches",
 "finding": "No consent management platform detected; no tracking cookies set before consent; no cookie banner; no privacy policy linked."}
```

**has_cmp** (CMP present, trackers still fire pre-consent)
```json
{"id": 6570, "company": "Rich Offset India Pvt Ltd", "website": "www.richoffset.com",
 "status": "done", "attempts": 0, "scraped_at": "2026-09-29T12:04:27.239141Z",
 "gap_score": 100, "trackers": 4, "third_parties": 2, "banner": "none",
 "scale": "51-100", "industry": "Paper , Publishing , Printing",
 "finding": "CMP detected: Cookiebot; 4 of 5 cookies are tracking cookies set before consent: __cf_bm, _ga, _gid, NID; data sent to 2 third-party domains before consent: google.com, teepublic.com; longest tracker lifetime 399 days, 2 trackers persist beyond 180 days; no cookie banner; privacy/cookie policy linke"}
```

**failed** (not counted in the 2,351)
```json
{"id": 7269, "company": "Godhani Impex", "website": "godhaniimpex.com", "status": "failed",
 "attempts": 2, "scraped_at": "2026-09-30T04:40:41.731524Z", "gap_score": null,
 "trackers": null, "third_parties": null, "banner": null, "scale": "101-250",
 "industry": "Gems , Jewellery , Watches",
 "finding": "could not analyse godhaniimpex.com: the domain does not resolve (no DNS record) -- gave up after 2 attempts"}
```

### 5.2 Full scanner records (Postgres `companies`, MCA pipeline)

These show every field the scanner produces. Partner rows went through the same scanner, but only the subset in §4.1 was kept.

<details><summary><b>severe</b>: SANATAN TECH INNOVATIONS PRIVATE LIMITED (score 7)</summary>

```json
{
 "cin": "U72200GJ2020PTC111956",
 "company_name": "SANATAN TECH INNOVATIONS PRIVATE LIMITED",
 "status": "exported", "retry_count": 0, "last_error": null,
 "created_at": "2026-09-11T18:21:35.665596+05:30",
 "updated_at": "2026-09-30T09:19:28.288781+05:30",
 "resolved_domain": "sanatantech.com", "tld_used": ".com", "resolve_confidence": 95,
 "resolve_verified_by": "llm-match-score", "resolve_match_score": 95,
 "final_domain": "sanatantech.com",
 "has_cmp": false, "cmp_name": null,
 "has_cookie_banner": false, "has_privacy_policy": false,
 "consent_gap_score": 7,
 "consent_gap_summary": "No consent management platform detected; 6 of 6 cookies are tracking cookies set before consent: _GRECAPTCHA, _gid, _gat_gtag_UA_76628358_1, _lfa, _ga_DNTS9ZFQ29, _ga; data sent to 1 third-party domains before consent: www.google.com; longest tracker lifetime 399 days, 3 trackers persist beyond 180 days; no cookie banner; no privacy policy linked; collects personal data via 1 web form(s) (email, message, name, phone) with no consent checkbox and no privacy link on the form form provider: Contact Form 7.",
 "cookies_total": 6, "tracking_cookie_count": 6,
 "third_party_domains": ["www.google.com"], "third_party_count": 1,
 "max_cookie_lifetime_days": 399,
 "pre_consent_cookies": [
  {"name": "_GRECAPTCHA", "party": "third", "domain": "www.google.com", "secure": true, "session": false, "category": "third_party", "http_only": true, "same_site": "None", "lifetime_days": 179},
  {"name": "_gid", "party": "first", "domain": "sanatantech.com", "secure": false, "session": false, "category": "analytics", "http_only": false, "same_site": "Lax", "lifetime_days": 0},
  {"name": "_gat_gtag_UA_76628358_1", "party": "first", "domain": "sanatantech.com", "secure": false, "session": false, "category": "analytics", "http_only": false, "same_site": "Lax", "lifetime_days": 0},
  {"name": "_lfa", "party": "first", "domain": "sanatantech.com", "secure": false, "session": false, "category": "analytics", "http_only": false, "same_site": "Lax", "lifetime_days": 364},
  {"name": "_ga_DNTS9ZFQ29", "party": "first", "domain": "sanatantech.com", "secure": false, "session": false, "category": "analytics", "http_only": false, "same_site": "Lax", "lifetime_days": 399},
  {"name": "_ga", "party": "first", "domain": "sanatantech.com", "secure": false, "session": false, "category": "analytics", "http_only": false, "same_site": "Lax", "lifetime_days": 399}
 ],
 "has_data_form": true, "form_field_types": ["email", "message", "name", "phone"],
 "form_consent_checkbox": false, "form_third_party_hosts": [],
 "form_embed_provider": "Contact Form 7",
 "tech_cms": "WordPress", "tech_ecommerce_platform": "WooCommerce", "tech_payment_gateway": null,
 "tech_analytics_tools": ["Google Analytics 4", "Universal Analytics"], "tech_marketing_tools": [],
 "company_tagline": "Making Your Dreams Stand Out In The Digital Jungle",
 "notable_clients": [], "notable_open_roles": [], "open_roles_count": null,
 "recent_blog_title": "insight dental", "recent_blog_date": "2021-12-14"
}
```
</details>

<details><summary><b>moderate</b>: CONTIS TECHNOLOGIES PRIVATE LIMITED (score 68)</summary>

```json
{
 "cin": "U72200GJ2004PTC043822",
 "company_name": "CONTIS TECHNOLOGIES PRIVATE LIMITED",
 "status": "exported", "retry_count": 0, "last_error": null,
 "created_at": "2026-09-11T18:21:35.665596+05:30",
 "updated_at": "2026-09-28T13:22:53.758565+05:30",
 "resolved_domain": "contis.in", "tld_used": ".in", "resolve_confidence": 85,
 "resolve_verified_by": "llm-match-score", "resolve_match_score": 85,
 "final_domain": "contis.in",
 "has_cmp": false, "cmp_name": null,
 "has_cookie_banner": false, "has_privacy_policy": true,
 "consent_gap_score": 68,
 "consent_gap_summary": "No consent management platform detected; 2 of 2 cookies are tracking cookies set before consent: _ga, _ga_BV5JC0B8PB; longest tracker lifetime 399 days, 2 trackers persist beyond 180 days; no cookie banner; privacy/cookie policy linked.",
 "cookies_total": 2, "tracking_cookie_count": 2,
 "third_party_domains": [], "third_party_count": 0, "max_cookie_lifetime_days": 399,
 "pre_consent_cookies": [
  {"name": "_ga", "party": "first", "domain": "contis.in", "secure": false, "session": false, "category": "analytics", "http_only": false, "same_site": "Lax", "lifetime_days": 399},
  {"name": "_ga_BV5JC0B8PB", "party": "first", "domain": "contis.in", "secure": false, "session": false, "category": "analytics", "http_only": false, "same_site": "Lax", "lifetime_days": 399}
 ],
 "has_data_form": false, "form_field_types": [], "form_consent_checkbox": null,
 "form_third_party_hosts": [], "form_embed_provider": null,
 "tech_cms": "WordPress", "tech_ecommerce_platform": null, "tech_payment_gateway": null,
 "tech_analytics_tools": ["Google Analytics 4"], "tech_marketing_tools": [],
 "company_tagline": "Modern Walling Solutions",
 "notable_clients": [], "notable_open_roles": ["Real Estate Sales Manager"], "open_roles_count": 1,
 "recent_blog_title": "Working With AAC Blocks: Industry Standard Practices", "recent_blog_date": "2026-02-23"
}
```
Score: analytics 2×1.5=3 + long-lived 2×2=4 + no banner 25 = 32 → 100−32 = 68.
</details>

<details><summary><b>clean</b>: JRKITES CONSULTANTS PRIVATE LIMITED (score 100)</summary>

```json
{
 "cin": "U72900CH2011PTC033437",
 "company_name": "JRKITES CONSULTANTS PRIVATE LIMITED",
 "status": "scraped", "retry_count": 0, "last_error": null,
 "created_at": "2026-09-11T18:21:35.665596+05:30",
 "updated_at": "2026-09-30T09:10:20.658187+05:30",
 "resolved_domain": "jrkites.com", "tld_used": ".com", "resolve_confidence": 95,
 "resolve_verified_by": "llm-match-score", "resolve_match_score": 95,
 "final_domain": "jrkites.com",
 "has_cmp": false, "cmp_name": null,
 "has_cookie_banner": false, "has_privacy_policy": false,
 "consent_gap_score": 100,
 "consent_gap_summary": "No consent management platform detected; no tracking cookies set before consent; no cookie banner; no privacy policy linked.",
 "cookies_total": 1, "tracking_cookie_count": 0,
 "third_party_domains": [], "third_party_count": 0, "max_cookie_lifetime_days": null,
 "pre_consent_cookies": [
  {"name": "ASP.NET_SessionId", "party": "first", "domain": "jrkites.com", "secure": false, "session": true, "category": "functional", "http_only": true, "same_site": "Lax", "lifetime_days": null}
 ],
 "has_data_form": false, "form_field_types": [], "form_consent_checkbox": null,
 "form_third_party_hosts": [], "form_embed_provider": null,
 "tech_cms": null, "tech_ecommerce_platform": null, "tech_payment_gateway": null,
 "tech_analytics_tools": [], "tech_marketing_tools": [],
 "company_tagline": null, "notable_clients": [], "notable_open_roles": [], "open_roles_count": null,
 "recent_blog_title": null, "recent_blog_date": null
}
```
</details>

<details><summary><b>has_cmp / not_a_lead</b>: SARA INFOTECH PRIVATE LIMITED (Secure Privacy, GA still fires)</summary>

```json
{
 "cin": "U72200MH1982PTC026456",
 "company_name": "SARA INFOTECH PRIVATE LIMITED",
 "status": "not_a_lead", "retry_count": 0, "last_error": "already has a CMP: Secure Privacy",
 "created_at": "2026-09-11T18:21:35.665596+05:30",
 "updated_at": "2026-09-28T14:57:12.992338+05:30",
 "resolved_domain": "sarainfotech.com", "tld_used": ".com", "resolve_confidence": 95,
 "resolve_verified_by": "llm-match-score", "resolve_match_score": 95,
 "final_domain": "sarainfotech.com",
 "has_cmp": true, "cmp_name": "Secure Privacy",
 "has_cookie_banner": false, "has_privacy_policy": true,
 "consent_gap_score": 100,
 "consent_gap_summary": "CMP detected: Secure Privacy; 2 of 8 cookies are tracking cookies set before consent: _ga_PRS49NPYSD, _ga; longest tracker lifetime 399 days, 2 trackers persist beyond 180 days; no cookie banner; privacy/cookie policy linked; collects personal data via 1 web form(s) (email, message, name, phone) with no consent checkbox and no privacy link on the form.",
 "cookies_total": 8, "tracking_cookie_count": 2,
 "third_party_domains": [], "third_party_count": 0, "max_cookie_lifetime_days": 399,
 "pre_consent_cookies": [
  {"name": "cf_zaraz_client", "party": "first", "domain": "sarainfotech.com", "secure": true, "session": false, "category": "functional", "http_only": true, "same_site": "Lax", "lifetime_days": 364},
  {"name": "cfz_zaraz-analytics", "party": "first", "domain": "sarainfotech.com", "secure": true, "session": false, "category": "functional", "http_only": true, "same_site": "Lax", "lifetime_days": 364},
  {"name": "_tccl_visitor", "party": "first", "domain": "sarainfotech.com", "secure": true, "session": false, "category": "functional", "http_only": false, "same_site": "None", "lifetime_days": 364},
  {"name": "_tccl_visit", "party": "first", "domain": "sarainfotech.com", "secure": true, "session": false, "category": "functional", "http_only": false, "same_site": "None", "lifetime_days": 0},
  {"name": "_scc_session", "party": "first", "domain": "sarainfotech.com", "secure": true, "session": false, "category": "functional", "http_only": false, "same_site": "None", "lifetime_days": 0},
  {"name": "_ga_PRS49NPYSD", "party": "first", "domain": "sarainfotech.com", "secure": false, "session": false, "category": "analytics", "http_only": false, "same_site": "Lax", "lifetime_days": 399},
  {"name": "_ga", "party": "first", "domain": "sarainfotech.com", "secure": false, "session": false, "category": "analytics", "http_only": false, "same_site": "Lax", "lifetime_days": 399},
  {"name": "cf_clearance", "party": "first", "domain": "sarainfotech.com", "secure": true, "session": false, "category": "functional", "http_only": true, "same_site": "None", "lifetime_days": 364}
 ],
 "has_data_form": true, "form_field_types": ["email", "message", "name", "phone"],
 "form_consent_checkbox": false, "form_third_party_hosts": [], "form_embed_provider": null,
 "tech_cms": null, "tech_ecommerce_platform": null, "tech_payment_gateway": null,
 "tech_analytics_tools": ["Google Analytics 4", "Google Tag Manager"],
 "tech_marketing_tools": ["WhatsApp Business"],
 "company_tagline": "Sara Infotech delivers white-label trading platforms, broker CRM software, copy trading systems, PAMM/MAM engines, and risk management automation.",
 "notable_clients": [],
 "notable_open_roles": ["Frontend Developer", "Backend Developer", "QA Automation Engineer", "UI/UX Designer"],
 "open_roles_count": 4,
 "recent_blog_title": "Forex Broker Configuration Management & IaC Guide", "recent_blog_date": null
}
```
</details>

---

## 6. Run metadata

### 6.1 Invocation

| Job | Command | Writes to |
|---|---|---|
| MCA scanner | `python stages/03_scrape_playwright.py [--limit N] [--concurrency N] [--no-llm] [--dry-run] [--out file.json]`; in production `run_daily.py` calls it with `--limit 500 --concurrency 6` | Postgres `companies` |
| Partner (the 2,351) | `python partner_import_analyse.py [--max-minutes M] [--status] [--dry-run]`; in production `scheduler.py` runs it daily at `PARTNER_RUN_AT` (11:05) after the MCA run | `dbo.CRM_Scrapper` |
| Inbound | `python inbound_analyse.py [--date D] [--days N] [--force] [--no-llm] [--dry-run]`; runs first in each scheduler wake-up | `crm.CrmLeads` |
| Ad-hoc | `python analyse_urls.py <url…>` | Postgres `companies` |

Runs inside the `scheduler` container (`docker compose --profile pipeline`), image `mcr.microsoft.com/playwright/python:v1.62.0-noble`, `shm_size: 1gb`, DNS via the local `unbound` resolver.

Env vars (`config.py`, `.env.docker`): `DATABASE_URL`, `CRM_DB_HOST/PORT/NAME/USER/PASSWORD`, `CRM_DB_TABLE` (default `dbo.CRM_Scrapper`), `CRM_LEADS_TABLE`, `CRM_LEADS_DB_NAME`, `LLM_BASE_URL`, `LLM_ENABLED`, `LLM_TIMEOUT`, `RUN_AT`, `DAILY_LEAD_TARGET`, `MAX_RUN_HOURS`, `PARTNER_RUN_AT`, `PARTNER_DAILY_TARGET` (500), `RUN_INBOUND`, `RUN_PARTNER`, `INBOUND_DAYS`. The live container's `CRM_DB_NAME` is `CookieConsentDB` (the example files say `CookieConsentDB_Staging`). Pause the partner job by creating `data/partner.pause`.

### 6.2 Concurrency, retries, failures

- **MCA (stage 03):** semaphore 4 by default, 6 in production. On exception, `db.mark_failed(cin, "scrape", "<ExcType>: <first line>")` increments `retry_count`; the row stays `resolved` until the 3rd failure, then moves to `scrape_failed` with `last_error`. No per-site total timeout here; each `goto` is bounded at 25 s. Currently 13 rows are `scrape_failed`.
- **Partner:** chunks of 50 claimed atomically (`UPDATE TOP … WITH (READPAST)`), concurrency 6, whole-site timeout 120 s. On error, `ScrapeAttempts+1`. Below 3 → `retry` (reclaimable after 6 h). At 3 → `failed` with `ConsentFinding = "could not analyse <domain>: <error>"`. A row with no parseable Website → `no_website`.
- **Current partner queue:** `done` 2,351 · `failed` 325 · `no_website` 863. Failure reasons: DNS does not resolve 238, 25 s timeout 55, connection refused 7, unreachable 6, broken TLS 5, other ≤2 each. **But** see bug #1: 316 of the 325 failed rows carry text and an attempt count that the committed code cannot produce.
- There is no "invalid URL" status beyond `no_website`. A page that loads but is parked, under construction or a bot wall is **scored**, not failed (stage 03 does not call `is_parked`/`is_blocked`).

### 6.3 Evidence

**No evidence is stored.** There are no screenshots, no HAR files and no network logs anywhere in the code. The only optional artefact is `stage 03 --out file.json`: a JSON dump of the result dicts (the same fields as the DB plus `page_title`, `subpages`, `cookie_categories`). Production (`run_daily.py`) does not pass `--out`, and `logs/` is empty. For the 2,351 partner rows, even the per-cookie records are discarded; only the counts and the ConsentFinding sentence survive.

### 6.4 Current counts (queried 2026-10-01)

**Partner (`/api/partner/status`, snapshot 05:03Z): matches the target exactly.**

| Verdict | Count |
|---|---|
| severe (≤50) | **806** |
| moderate (51–99) | **591** |
| clean (100) | **861** |
| has CMP | **93** |
| **total analysed (`done`)** | **2,351** |

**Postgres `companies` (MCA pipeline, for comparison):** 8,119 rows scored. Live (excl. `discovered`/`resolve_failed`): severe 2,282 · moderate 2,799 · clean 2,746 · has_cmp 292 (CookieYes 82, Cookiebot 53, Complianz 45, OneTrust 37, GDPR Cookie Consent (WP) 24, WPConsent 10, others ≤6).

---

## 7. Gaps checklist

| Check | Status | Note |
|---|---|---|
| C1 Non-essential trackers fire before consent | ⚠️ partial | Measured through **cookies only**, after one homepage load with no interaction. No network-request capture, so cookieless beacons, pixels and localStorage are missed. Category comes from a name-prefix list plus a third-party backstop. |
| C2 No banner/notice at all | ⚠️ partial | Text/class substring anywhere in HTML + shadow DOM. Any "cookie policy" footer link counts as a banner, so false "present" is likely. Visibility is not checked. |
| C3 Accept but no equally easy Reject | ❌ | Banner buttons are never located or compared. |
| C4 Implied-consent wording / pre-ticked categories | ⚠️ partial | "by continuing to use/browse" is matched, but only as banner presence; it is not recorded or scored as a finding. Pre-ticked categories are not checked. |
| C5 No way to change/withdraw preferences | ❌ | "manage preferences" text only feeds banner presence. No persistent-link check. |
| C6 No language option on banner/notice | ❌ | — |
| F1 Form collects personal data with no notice/privacy link nearby | ⚠️ partial | Homepage forms only (no contact page). The link must be **inside** `<form>`. Scored +10 only if the page also has no policy text. |
| F2 Pre-ticked marketing/consent checkbox | ❌ | The checkbox `checked` state is never read; a pre-ticked box counts as "has consent". |
| F3 Single checkbox bundles T&C + marketing | ❌ | The regex includes "terms", so a T&C-only box counts as consent. No bundling analysis. |
| F4 No published privacy contact / grievance route | ❌ | The policy page is never opened. Footer phone/address are extracted for CRM, not checked. |
| F5 Age/DOB fields or child-directed signals | ❌ | No DOB/age field category, no content signals. |
| Evidence capture (screenshot / network log per site) | ❌ | Nothing stored. Partner rows don't even keep cookie records. |

---

## Bugs and inconsistencies noticed (not fixed)

1. **Partner failure rows don't match the code.** 316 of 325 `failed` partner rows have `ScrapeAttempts = 2` and human-readable messages ("the domain does not resolve (no DNS record) -- gave up after 2 attempts"). The committed code fails only at `MAX_ATTEMPTS = 3` and writes the raw exception text. That string exists nowhere in the repo, its git history or the running containers. All 316 rows were stamped between 2026-09-30 04:28 and 04:40 UTC, which suggests an out-of-repo script or a manual SQL update. Their provenance should be confirmed before quoting failure numbers.
2. **The comment says contact-page forms are merged in; they aren't.** `scrape_one()`'s comment says "Contact pages carry the real enquiry form far more often than the homepage does, so their forms are merged in below", but no code does this, and `SUBPAGE_PATTERNS` has no contact pattern. Form findings are homepage-only and likely undercounted.
3. **"Clean" ≠ compliant.** Score 100 is given to any non-CMP site with no tracking cookies and no homepage data form, even with no banner and no policy (824 of 861 partner "clean" rows have no banner). This matters for how the report labels this bucket.
4. **The CMP gate trusts presence, not behaviour.** 66 of 93 partner CMP rows (218/292 in Postgres) still set tracking cookies before consent, and many also show "no cookie banner". These may be misconfigured CMPs and potential leads, yet they are scored 100 and dropped.
5. **CMP false-positive risk from loose tokens.** Substring tokens such as `ketch.com` (also matches `sketch.com`), `cookiecontrol`, `quantcast`, `complianz` and `usercentrics` will match a mere mention or link in page text. There are 3 Ketch hits in Postgres worth eyeballing.
6. **Banner false positives.** `cookie (policy|…)` in `BANNER_TEXT_PATTERNS` means a footer "Cookie Policy" link sets `has_cookie_banner = true`, which removes the +25 "no banner" points.
7. **Partner writes are fill-blanks only (`COALESCE`).** If a partner row already had a GapScore, ConsentFinding, etc., the scan result is silently not written. `ensure_columns()` also marks pre-existing rows with both fields as `done`. So some of the 2,351 verdicts may reflect values this scanner never produced, or a score and finding from different sources.
8. **Partner `has_cmp` verdict is inferred from text.** `api.py` classifies CMP by `ConsentFinding.startswith("CMP detected")` because no CMP column exists on the CRM table. Combined with #7, a partner-supplied finding would break this.
9. **No HTTP-status / parked / bot-wall check in the scanner.** `is_parked()` and `is_blocked()` are applied only in stage 02. The partner and inbound jobs skip stage 02 entirely, so challenge pages, parked domains and error pages are scored, most likely as "clean".
10. **Inconsistent severe threshold.** `api.py`, `crm_schema.sql` and `DEPLOY.md` use ≤ 50; `ui/index.html` labels and colours "Severe" at ≤ 30.
11. **Stale docs on score polarity.** `STATUS_FLOW.md` still describes the old additive score ("trackers up to 45… No trackers, or a CMP present, scores 0") and recommends `consent_gap_score > 0` as the stage-04 filter. Under the current polarity that filter would select almost everything, including CMP rows (100). `docs/PRODUCTION_NUMBERS.md` says "severe (50+)" (old polarity). The `schema.sql` comment on `consent_gap_score` ("how much of a consent problem") and its `DESC` index imply the old direction too.
12. **Missing separator in the summary.** `build_form_sentence()` joins with spaces, producing "…no privacy link on the form form provider: Contact Form 7." (see the severe sample).
13. **`--headed` advertised but not implemented.** The stage 03 docstring shows `--headed`; argparse has no such flag, and the browser is hard-coded `headless=True`.
14. **reCAPTCHA counted as a tracker.** `_GRECAPTCHA` on `www.google.com` is categorised `third_party` (tracking) by the backstop rule. It appears in many severe findings, so it inflates tracker counts and adds points.
15. **Dead scoring code.** `fingerprints.consent_gap_score()` (original polarity) and `tracking_cookies()` are unused but still present next to the live `consent_score()`. Easy to call by mistake.
16. **Unwritten computed fields.** `cookie_categories` and `subpages` are computed per site but not in `SCRAPE_FIELDS`, so they are never persisted.
17. **Minor:** the `scheduler` container's `api.py` differs from the repo (the api container matches). This does not affect scanning, since the scheduler doesn't serve the API.
