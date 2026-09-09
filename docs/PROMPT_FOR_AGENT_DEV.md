# Build a website consent-gap analyser

I'll give you website URLs directly. For each one, load the page in a real
browser and tell me everything about how it handles personal data before the
visitor has consented to anything.

You do NOT need to find, guess, or verify which company a URL belongs to — I
supply the URLs myself and they are already correct. Skip all identity
checking.

## What to build

One function: `analyse(url) -> dict`. A thin CLI or HTTP wrapper around it is
fine. Python + Playwright (chromium, headless).

## The measurement that matters

Open the page in a **fresh browser context** and **never click anything** — no
"Accept", no banner dismissal. Whatever state exists after load is by
definition the pre-consent state. That is the whole experiment; if you click
accept, the data is worthless.

## Output

```json
{
  "url": "...", "final_url": "...", "final_domain": "...",
  "http_status": 200, "page_title": "...",

  "has_cmp": false, "cmp_name": null,
  "has_cookie_banner": false, "has_privacy_policy": true,

  "cookies_total": 27, "tracking_cookie_count": 25,
  "third_party_count": 11,
  "third_party_domains": ["doubleclick.net", "..."],
  "max_cookie_lifetime_days": 399,
  "long_lived_count": 10,
  "pre_consent_cookies": [
    {"name":"IDE","domain":"doubleclick.net","party":"third",
     "session":false,"lifetime_days":399,"secure":true,
     "same_site":"None","http_only":true,"category":"advertising"}
  ],

  "has_data_form": true,
  "form_field_types": ["email","name","phone"],
  "form_consent_checkbox": false,
  "form_privacy_link": false,
  "form_third_party_hosts": ["webto.salesforce.com"],
  "form_embed_provider": "Contact Form 7",

  "tech_cms": "WordPress",
  "tech_ecommerce_platform": null,
  "tech_payment_gateway": "Razorpay",
  "tech_analytics_tools": ["Google Analytics 4","Microsoft Clarity"],
  "tech_marketing_tools": ["Meta Pixel","Google Ads"],

  "consent_gap_score": 100,
  "consent_gap_summary": "No consent management platform detected; 25 of 27 ...",

  "company_tagline": "...", "founder_name": null, "notable_clients": [],
  "page_status": "ok"   // ok | parked | blocked | unreachable
}
```

## Where an LLM is used, and where it must NOT be

**No LLM anywhere in the compliance findings.** Cookies, CMP detection, banner,
privacy policy, forms, tech stack, the score and the summary are ALL exact
string/DOM matching. These are the numbers someone will quote to a prospect, so
they must be reproducible and auditable — the same page must always produce the
same finding, and you must be able to point at why.

**LLM only for soft descriptive fields**, and only these:
`company_tagline`, `founder_name`, `notable_clients`. They are nice-to-have
context. If the model is unreachable, return them as null and carry on — never
fail the analysis over them.

Prompt rule for those: use ONLY what the page text says, never outside
knowledge. Explicitly forbid treating testimonial authors, reviewers, or staff
as clients — a small model will otherwise list customer-review names as
"notable clients". A person's name is never a client.

## Detection details

**CMP** — substring match over the page HTML plus every `<script src>`. Cover at
minimum: OneTrust (`onetrust`, `optanon`, `cookielaw.org`), Cookiebot,
CookieYes, Osano, Termly, Quantcast, Didomi, Usercentrics, iubenda, Complianz,
CookieScript, Borlabs, TrustArc, Seers, CookiePro, Ketch, Klaro, and the
WordPress plugins `cookie-law-info` / `cli-modal` and `moove_gdpr`. Those last
two are very common on Indian SME sites.

**Cookie banner without a CMP** — selector hints (`cookie-banner`,
`cookie-consent`, `cookie-notice`, `gdpr-banner`, `cc-banner`) plus text
patterns ("we use cookies", "this site uses cookies", "accept all cookies").
A banner with no recognised vendor still counts as a banner.

**Cookie categories** — classify each cookie as `advertising`,
`session_replay`, `analytics`, `third_party` or `functional`. Match known
prefixes (`_ga`, `_fbp`, `_gcl`, `_hj`, `_clck`, `MUID`, `IDE`, `test_cookie`,
`YSC`, `VISITOR_INFO1_LIVE`, `__hstc`, `bcookie`, `_uetsid`, …) but **treat any
third-party cookie as a tracker whether or not you recognise its name.** Name
lists always miss things — CLID, SRM_B and ANONCHK were invisible to ours until
we added that rule, and they were a third of one site's trackers.

**Forms** — for each `<form>`, gather each input's `name`, `id`, `placeholder`,
`type`, `aria-label` and its associated `<label>` text, then classify what
personal data it collects (email / phone / name / address). A form is a finding
only if it collects an *identifying* field — a bare message box is not.
Also capture: is there a checkbox whose nearby text mentions consent / agree /
privacy / terms; is there a privacy link inside the form; and does the form's
`action` post to a **different host** than the page (that is data leaving the
company's infrastructure to a processor they probably haven't documented).
Detect embed providers too: Google Forms, Typeform, HubSpot, Zoho, JotForm,
Formspree, Mailchimp, WPForms, Contact Form 7, Gravity Forms.

**Score (0-100, deterministic)** — trackers weighted by category (advertising
and session-replay count roughly double plain analytics), plus distinct
third-party domains, plus trackers living beyond 180 days, plus 25 for no
banner, 15 for no privacy policy, 10 if session replay is present, 5 if
advertising is present. A form collecting identifying data with no consent
checkbox adds ~25 on its own. **A site with a CMP scores 0.** Tune the weights
however you like — just keep it a pure function of the observations.

**Summary** — assemble the sentence from the findings with string formatting,
not a model. Lead with third-party recipients and cookie lifetimes; those are
far more concrete than a cookie count.

## Gotchas that will each cost you a day

1. **Late-loading trackers.** Hotjar and Clarity fire well after
   `DOMContentLoaded`. Wait for `networkidle` (cap it ~8s) and *then* settle
   ~4s before reading cookies. A 2.5s window missed two trackers on a real site
   and moved its score by 16 points between runs.

2. **Classify party against the FINAL domain, not the requested one.** Sites
   redirect — one we tested was requested at `ronaldweboffset.com` and served
   from `ronaldindia.com`. Compare registrable domains (use `tldextract`), or
   every first-party cookie is mislabelled third-party.

3. **Never store cookie VALUES.** They are live tracking identifiers for real
   visitors and arguably personal data under DPDP. Store name, domain, party,
   lifetime, flags, category — drop the value at capture. A compliance product
   holding other people's tracking IDs is not a good position.

4. **Don't extract page text with "first element matching main/article".**
   Page builders scatter `<article>` over small widgets — on one WordPress site
   that returned 207 characters out of 10,016, and every LLM-extracted field
   came back null as a result. Take the **largest** matching container, and fall
   back to the whole body if it holds under ~40% of the body text.

5. **Distinguish three failure modes**, don't collapse them:
   - *parked / dead* — "domain for sale", "store unavailable", Apache/nginx
     default, or a `<title>` that is just the hostname. Never retry.
   - *blocked* — "Checking your browser", "verify you are human", Cloudflare
     interstitial. Retry-able, but often not worth it.
   - *unreachable* — DNS or connection failure.
   A 4xx/5xx response **with a real body is still useful** — a suspended
   Shopify store still names the shop.

6. **Keep the fetch budget tight.** One attempt per scheme with TLS
   verification off, ~4s connect / 6s read. Retrying https-verified then
   https-unverified then http twice means one slow host eats 60 seconds.

## Appendix — the complete detection tables

These are the exact tables from the working implementation. Copy them; they are
the result of running against several hundred real Indian SME sites, and each
entry is there because something was missed without it.

### Consent management platforms (22)

Match case-insensitively against the page HTML **and** every `<script src>` URL.
First hit names the CMP and sets `has_cmp = true`.

| Vendor | Match any of |
|---|---|
| OneTrust | `onetrust`, `optanon`, `cookielaw.org`, `otSDKStub` |
| Cookiebot | `cookiebot`, `consent.cookiebot.com`, `CybotCookiebot` |
| CookieYes | `cookieyes`, `cdn-cookieyes.com`, `cky-consent` |
| Osano | `osano.com`, `osano-cm` |
| Termly | `termly.io`, `termly-code` |
| Quantcast | `quantcast`, `quantcast.mgr`, `__cmpcallbacks` |
| Didomi | `didomi.io`, `didomi-host`, `window.didomi` |
| Usercentrics | `usercentrics`, `uc-block`, `app.usercentrics.eu` |
| iubenda | `iubenda.com`, `iubenda_cs` |
| Complianz | `complianz`, `cmplz-` |
| CookieScript | `cookie-script.com`, `cookiescript` |
| Borlabs | `borlabs-cookie` |
| TrustArc | `trustarc`, `truste.com`, `consent.trustarc.com` |
| Seers | `seersco.com`, `seers-cmp` |
| Secure Privacy | `secureprivacy.ai` |
| CookiePro | `cookiepro.com` |
| Ketch | `ketchcdn.com`, `ketch.com` |
| Klaro | `klaro-config`, `klaro.js` |
| tarteaucitron | `tarteaucitron` |
| Civic Cookie Control | `civiccomputing`, `cookiecontrol` |
| GDPR Cookie Consent (WP) | `cookie-law-info`, `cli-modal`, `cli_settings` |
| Moove GDPR (WP) | `moove_gdpr`, `gdpr-cookie-compliance` |

The last two are WordPress plugins and are the most common CMPs on Indian SME
sites by a wide margin — do not leave them out.

### Cookie banner present, no recognised vendor

A banner with no known CMP behind it still counts as a banner (usually it only
informs and sets trackers regardless).

Selector/class hints: `cookie-banner`, `cookie-consent`, `cookie-notice`, `cookie-bar`, `cookiebanner`, `cookieconsent`, `gdpr-banner`, `consent-banner`, `cc-banner`, `cookie-popup`

Text patterns (regex, case-insensitive): `we use cookies`, `this (?:website|site) uses cookies`, `cookie (?:policy|preferences|settings|consent)`, `accept (?:all )?cookies`, `by continuing to (?:use|browse)`, `manage (?:your )?(?:cookie )?preferences`

### Privacy / cookie policy link

Regex over hrefs and link text: `privacy[-_ ]?policy`, `cookie[-_ ]?policy`, `privacy[-_ ]?notice`, `data[-_ ]?protection`

### Tracking cookie prefixes

Prefix match on the cookie name.

**All trackers** (36): `_ga`, `_gid`, `_gat`, `__utm`, `_gcl`, `_dc_gtm`, `_fbp`, `_fbc`, `fr`, `_hjs`, `_hj`, `_clck`, `_clsk`, `MUID`, `__hstc`, `hubspotutk`, `__hssrc`, `__hssc`, `_lfa`, `li_sugr`, `bcookie`, `lidc`, `UserMatchHistory`, `IDE`, `test_cookie`, `NID`, `DSID`, `mp_`, `ajs_`, `amplitude_`, `_pk_`, `_omappvp`, `_uetsid`, `_uetvid`, `YSC`, `VISITOR_INFO1_LIVE`

**Advertising subset** — cross-site identification, weighted heaviest:
`_fbp`, `_fbc`, `fr`, `IDE`, `test_cookie`, `NID`, `DSID`, `_gcl`, `li_sugr`, `bcookie`, `lidc`, `UserMatchHistory`, `_uetsid`, `_uetvid`, `YSC`, `VISITOR_INFO1_LIVE`

**Session replay subset** — recording visitor behaviour before consent:
`_hj`, `_hjs`, `_clck`, `_clsk`, `MUID`

Anything else that is **third-party** is still a tracker, category
`third_party`. Anything else first-party is `functional`.

### Per-cookie record

Capture for every cookie, tracker or not:

`name`, `domain`, `party` (first/third vs the FINAL page domain),
`session` (bool), `lifetime_days` (int or null), `secure`, `same_site`,
`http_only`, `category`.

**Never `value`.**

Derived counts: `cookies_total`, `tracking_count`, `third_party_domains` (unique),
`third_party_count`, `max_lifetime_days`, `long_lived_count` (>180 days).

### Form field classification

Gather each input's `name`, `id`, `placeholder`, `type`, `aria-label` and its
`<label>` text into one string per field, then regex it:

| Category | Patterns |
|---|---|
| email | `\bemail\b`, `\be-?mail\b`, `\bmail\b` |
| phone | `\bphone\b`, `\bmobile\b`, `\btel\b`, `\bcontact.?number\b`, `\bwhatsapp\b` |
| name | `\bname\b`, `\bfull.?name\b`, `\bfirst.?name\b`, `\blast.?name\b` |
| address | `\baddress\b`, `\bcity\b`, `\bpincode\b`, `\bpin.?code\b`, `\bzip\b` |
| company | `\bcompany\b`, `\borganisation\b`, `\borganization\b` |
| message | `\bmessage\b`, `\benquiry\b`, `\binquiry\b`, `\bcomment\b`, `\brequirement\b` |

Identifying categories (a form is a finding only if it collects one of these):
`email`, `phone`, `name`, `address`. A message box alone is not personal data.

**Watch the overlap:** "Email Address" matches both `email` and `address`. If
`email` matched on a descriptor, discard `address` for that same descriptor —
otherwise every contact form falsely reports collecting postal addresses.

### Form consent + destination

- **Consent checkbox** — any `input[type=checkbox]` whose enclosing label or
  parent text matches `consent|agree|privacy|terms|permission|opt.?in`.
  Report `form_consent_checkbox = true` only if EVERY collecting form has one.
- **Privacy link in form** — an `<a>` inside the form whose href or text
  matches `privacy|policy|terms`.
- **External submit** — resolve the form's `action` against the page URL; if the
  host differs from the page host, record it in `form_third_party_hosts`. This
  is personal data going to a processor the company almost certainly has not
  documented, and it is one of the strongest findings you can produce.

### Third-party form providers

| Provider | Match any of |
|---|---|
| Google Forms | `docs.google.com/forms`, `google.com/forms` |
| Typeform | `typeform.com` |
| HubSpot Forms | `js.hsforms.net`, `hsforms.com`, `forms.hsforms` |
| Zoho Forms | `forms.zohopublic`, `zohoforms` |
| JotForm | `jotform.com` |
| Wufoo | `wufoo.com` |
| Formspree | `formspree.io` |
| Mailchimp Form | `list-manage.com`, `chimpstatic` |
| WPForms | `wpforms` |
| Contact Form 7 | `wpcf7`, `contact-form-7` |
| Gravity Forms | `gform_`, `gravityforms` |

### Tech stack

CMS: `WordPress`, `Shopify`, `Wix`, `Squarespace`, `Webflow`, `Drupal`, `Joomla`, `Ghost`, `Magento`, `Blogger`, `HubSpot CMS`, `Duda`

Ecommerce: `Shopify`, `WooCommerce`, `Magento`, `BigCommerce`, `PrestaShop`, `OpenCart`, `Zoho Commerce`

Payment: `Razorpay`, `PayU`, `CCAvenue`, `Instamojo`, `Cashfree`, `Paytm`, `BillDesk`, `Stripe`, `PayPal`

Analytics: `Google Analytics 4`, `Universal Analytics`, `Google Tag Manager`, `Hotjar`, `Microsoft Clarity`, `Matomo`, `Mixpanel`, `Segment`, `Plausible`, `Yandex Metrica`

Marketing: `Meta Pixel`, `LinkedIn Insight`, `HubSpot`, `Mailchimp`, `Zoho`, `Freshworks`, `Intercom`, `Drift`, `Tawk.to`, `Crisp`, `Google Ads`, `WhatsApp Business`

Match on script src + HTML. `G-` and `GTM-` are case-sensitive measurement IDs
— regex them as `\bG-[A-Z0-9]{4,}`, not a lowercase substring, or every page
containing the letter g matches.

### Page status — parked / dead

If any of these appear in title or body, the page is `parked`. Never retry it,
and never score it:

`このドメインは`, `お名前.com`, `sedoparking`, `sedo.com/search`, `afternic.com`, `hugedomains.com`, `dan.com/buy-domain`, `parkingcrew`, `bodis.com`, `domain is for sale`, `buy this domain`, `this domain is parked`, `domain for sale`, `inquire about this domain`, `apache2 ubuntu default page`, `welcome to nginx`, `iis windows server`, `future home of something`, `index of /`, `site is under construction`, `under construction`, `coming soon`, `default web site page`, `account suspended`, `bandwidth limit exceeded`, `store unavailable`, `this store is currently unavailable`, `shop is currently unavailable`, `this store is unavailable`, `site is temporarily unavailable`, `this account has been suspended`, `website is temporarily unavailable`, `service temporarily unavailable`

Also parked: a `<title>` that is just a hostname (`elohi.in`,
`esengineering.com`), or an empty title with under ~80 characters of body text.

### Page status — blocked

`checking your browser`, `just a moment`, `please wait while we verify`, `verify you are human`, `verifying you are human`, `are you a robot`, `enable javascript and cookies to continue`, `ddos protection by`, `attention required! | cloudflare`, `cf-browser-verification`, `access denied`, `request unsuccessful`, `unusual traffic`, `captcha`, `hcaptcha`, `recaptcha`, `incapsula incident`, `this website is using a security service to protect itself`

Plus these HTTP statuses **when the body is under ~400 characters**:
`401`, `402`, `403`, `407`, `423`, `429`, `451`, `500`, `502`, `503`, `504`

A 402 or 503 that still renders real content is NOT blocked — a suspended
Shopify store still names the shop, and that page is usable. Check parked
BEFORE blocked, or dead stores get retried forever.

## Do not build

Domain discovery, name-to-URL guessing, identity verification, company
registry lookups, LinkedIn or contact enrichment. I handle all of that; you
receive URLs that are already correct.
