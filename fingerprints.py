"""
Deterministic detection tables for stage 03.

Everything here is exact substring matching against page HTML, script sources,
and cookie names. No model is involved, deliberately: has_cmp is the cost gate
that decides whether a company ever reaches the paid Apify/Apollo stages, so it
has to be reproducible and auditable rather than probabilistic.
"""

import re

# --- Consent management platforms -------------------------------------------
# Matched against script src URLs and raw HTML. Order matters only for
# reporting: the first hit names the CMP.
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
    # WordPress consent plugins -- very common on Indian SME sites.
    ("GDPR Cookie Consent (WP)", ("cookie-law-info", "cli-modal", "cli_settings")),
    ("Moove GDPR (WP)",   ("moove_gdpr", "gdpr-cookie-compliance")),
)

# --- Generic cookie-banner presence (no recognised CMP vendor) ---------------
# A banner without a known CMP is still a consent gap: it usually only informs,
# with no way to refuse, and sets trackers regardless.
BANNER_TEXT_PATTERNS = (
    r"we use cookies", r"this (?:website|site) uses cookies",
    r"cookie (?:policy|preferences|settings|consent)",
    r"accept (?:all )?cookies", r"by continuing to (?:use|browse)",
    r"manage (?:your )?(?:cookie )?preferences",
)
BANNER_SELECTOR_HINTS = (
    "cookie-banner", "cookie-consent", "cookie-notice", "cookie-bar",
    "cookiebanner", "cookieconsent", "gdpr-banner", "consent-banner",
    "cc-banner", "cookie-popup",
)

PRIVACY_LINK_PATTERNS = (
    r"privacy[-_ ]?policy", r"cookie[-_ ]?policy", r"privacy[-_ ]?notice",
    r"data[-_ ]?protection",
)

# --- Cookies that are tracking, not strictly necessary -----------------------
# Prefix match. Presence of any of these BEFORE consent is the consent gap.
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

# --- Tech stack --------------------------------------------------------------
CMS_SIGNATURES = (
    ("WordPress",   ("wp-content", "wp-includes", "wp-json")),
    ("Shopify",     ("cdn.shopify.com", "shopify.theme")),
    ("Wix",         ("static.wixstatic.com", "wix.com", "_wixCssStates")),
    ("Squarespace", ("squarespace.com", "static1.squarespace")),
    ("Webflow",     ("webflow.com", "wf-page")),
    ("Drupal",      ("/sites/default/files", "drupal.js", "Drupal.settings")),
    ("Joomla",      ("/media/jui/", "joomla", "com_content")),
    ("Ghost",       ("ghost.io", "content/themes/casper")),
    ("Magento",     ("mage/cookies", "static/version", "magento")),
    ("Blogger",     ("blogspot.com", "blogger.com")),
    ("HubSpot CMS", ("hs-sites.com", "hubspotusercontent")),
    ("Duda",        ("dudamobile", "duda_website")),
)

ECOMMERCE_SIGNATURES = (
    ("Shopify",      ("cdn.shopify.com", "shopify-features")),
    ("WooCommerce",  ("woocommerce", "wc-ajax", "wp-content/plugins/woocommerce")),
    ("Magento",      ("magento", "checkout/cart")),
    ("BigCommerce",  ("bigcommerce.com",)),
    ("PrestaShop",   ("prestashop",)),
    ("OpenCart",     ("index.php?route=", "opencart")),
    ("Zoho Commerce",("zohocommerce", "zohostatic")),
)

PAYMENT_SIGNATURES = (
    ("Razorpay",   ("razorpay", "checkout.razorpay.com")),
    ("PayU",       ("payu.in", "payubiz", "secure.payu")),
    ("CCAvenue",   ("ccavenue",)),
    ("Instamojo",  ("instamojo",)),
    ("Cashfree",   ("cashfree",)),
    ("Paytm",      ("paytm", "securegw.paytm")),
    ("BillDesk",   ("billdesk",)),
    ("Stripe",     ("js.stripe.com", "stripe.com/v3")),
    ("PayPal",     ("paypal.com/sdk", "paypalobjects")),
)

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


def _match(haystack, table):
    """First label whose any-token appears in haystack. Case-insensitive."""
    low = haystack.lower()
    for label, tokens in table:
        for tok in tokens:
            # 'G-' and 'GTM-' are case-sensitive measurement IDs; the rest are not.
            if tok in ("G-", "GTM-"):
                if re.search(rf"\b{tok}[A-Z0-9]{{4,}}", haystack):
                    return label
            elif tok.lower() in low:
                return label
    return None


def _match_all(haystack, table):
    """Every label that matches -- for the multi-value analytics/marketing columns."""
    low = haystack.lower()
    out = []
    for label, tokens in table:
        for tok in tokens:
            hit = (
                re.search(rf"\b{tok}[A-Z0-9]{{4,}}", haystack)
                if tok in ("G-", "GTM-")
                else tok.lower() in low
            )
            if hit:
                out.append(label)
                break
    return out


def detect_cmp(blob):
    """Returns the CMP vendor name, or None. Exact substring match, no guessing."""
    return _match(blob, CMP_SIGNATURES)


def detect_banner(blob):
    """True if a cookie banner appears present, even without a recognised vendor."""
    low = blob.lower()
    if any(hint in low for hint in BANNER_SELECTOR_HINTS):
        return True
    return any(re.search(p, low) for p in BANNER_TEXT_PATTERNS)


def detect_privacy_policy(blob):
    low = blob.lower()
    return any(re.search(p, low) for p in PRIVACY_LINK_PATTERNS)


def tracking_cookies(cookies):
    """Names of tracking (non-essential) cookies among those given."""
    out = []
    for c in cookies:
        name = c.get("name", "")
        if any(name.startswith(p) for p in TRACKING_COOKIE_PREFIXES):
            out.append(name)
    return sorted(set(out))


def detect_tech(blob):
    """The whole tech-stack picture in one pass over the page blob."""
    return {
        "tech_cms": _match(blob, CMS_SIGNATURES),
        "tech_ecommerce_platform": _match(blob, ECOMMERCE_SIGNATURES),
        "tech_payment_gateway": _match(blob, PAYMENT_SIGNATURES),
        "tech_analytics_tools": _match_all(blob, ANALYTICS_SIGNATURES),
        "tech_marketing_tools": _match_all(blob, MARKETING_SIGNATURES),
    }


# --- Cookie categories, for consent-gap scoring ------------------------------
# Not all pre-consent cookies are equally damning. Advertising and session-replay
# trackers are the ones that make a DPDP complaint concrete: they identify a
# person across sites, or record what they did on the page, before anyone agreed
# to anything. Plain analytics is a lighter finding.
ADVERTISING_COOKIE_PREFIXES = (
    "_fbp", "_fbc", "fr", "IDE", "test_cookie", "NID", "DSID",
    "_gcl", "li_sugr", "bcookie", "lidc", "UserMatchHistory",
    "_uetsid", "_uetvid", "YSC", "VISITOR_INFO1_LIVE",
)
SESSION_REPLAY_COOKIE_PREFIXES = ("_hj", "_hjs", "_clck", "_clsk", "MUID")


def categorise_cookies(names):
    """Splits tracking cookie names into advertising / session-replay / analytics."""
    advertising, replay, analytics = [], [], []
    for name in names:
        if any(name.startswith(p) for p in ADVERTISING_COOKIE_PREFIXES):
            advertising.append(name)
        elif any(name.startswith(p) for p in SESSION_REPLAY_COOKIE_PREFIXES):
            replay.append(name)
        else:
            analytics.append(name)
    return {"advertising": advertising, "session_replay": replay, "analytics": analytics}


def consent_gap_score(has_cmp, tracking, banner, policy):
    """
    0-100, deterministic. Answers "how much of a consent problem does this site
    actually have", which is a different question from has_cmp.

    A site with no CMP but also no trackers scores 0: there is no gap to close,
    nothing to sell against, and no reason to spend an Apify or Apollo credit on
    it. That is the whole point of the score -- has_cmp=false alone lets those
    through, and on the first real batch they were 3 of 4 "leads".

    Scoring is additive and capped:
      trackers present   up to 45  (count, weighted by how invasive the category is)
      no cookie banner        25   (nothing even pretends to ask)
      no privacy policy       15   (no disclosure at all)
      session replay          10   (recording behaviour pre-consent)
      advertising present      5   (cross-site identification pre-consent)
    """
    if has_cmp or not tracking:
        return 0

    cats = categorise_cookies(tracking)
    weighted = (
        3.0 * len(cats["advertising"])
        + 3.0 * len(cats["session_replay"])
        + 1.5 * len(cats["analytics"])
    )
    score = min(45.0, weighted)

    if not banner:
        score += 25
    if not policy:
        score += 15
    if cats["session_replay"]:
        score += 10
    if cats["advertising"]:
        score += 5

    return int(min(100, round(score)))


# --- Parked / placeholder pages ----------------------------------------------
# A domain that resolves and returns 200 is not automatically a company website.
# daifuku.biz scored on a Japanese domain-parking page during testing.
PARKED_SIGNATURES = (
    "このドメインは", "お名前.com", "sedoparking", "sedo.com/search",
    "afternic.com", "hugedomains.com", "dan.com/buy-domain", "parkingcrew",
    "bodis.com", "domain is for sale", "buy this domain", "this domain is parked",
    "domain for sale", "inquire about this domain",
    # server defaults / unfinished sites
    "apache2 ubuntu default page", "welcome to nginx", "iis windows server",
    "future home of something", "index of /", "site is under construction",
    "under construction", "coming soon", "default web site page",
    "account suspended", "bandwidth limit exceeded",
    # dead storefronts: the domain is genuinely the company's, but the site is
    # switched off. No cookies, no consent gap, nothing to sell -- and unlike a
    # bot wall, retrying with a browser will never change the answer.
    "store unavailable", "this store is currently unavailable",
    "shop is currently unavailable", "this store is unavailable",
    "site is temporarily unavailable", "this account has been suspended",
    "website is temporarily unavailable", "service temporarily unavailable",
)


def is_parked(title, text, domain=None):
    """True if the page looks like parking, a server default, or a stub."""
    blob = f"{title} {text[:1500]}".lower()
    if any(sig.lower() in blob for sig in PARKED_SIGNATURES):
        return True

    # A <title> that is just the hostname ("elohi.in", "esengineering.com") is a
    # placeholder or an unconfigured host, never a real company page. Common
    # enough in this dataset to be worth a deterministic rule rather than
    # leaving it to the verifier, which judged these inconsistently.
    stripped = title.strip().lower().removeprefix("www.")
    if stripped and re.fullmatch(r"[a-z0-9-]+(\.[a-z]{2,})+", stripped):
        return True
    if domain and stripped == domain.lower().removeprefix("www."):
        return True

    # A page with almost no text and no title is not a usable company site.
    return not title.strip() and len(text.strip()) < 80


# --- Full cookie records ------------------------------------------------------
import time

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=())  # offline; no network at import


def registrable(host):
    """example.co.in from www.example.co.in -- so party comparison is not fooled
    by subdomains or a www prefix."""
    parts = _extract(host.lstrip("."))
    return parts.registered_domain.lower() or host.lstrip(".").lower()


def cookie_record(cookie, site_domain, now=None):
    """
    One cookie as we store it: everything the browser gave us EXCEPT `value`.

    Values are live tracking identifiers for real visitors -- arguably personal
    data under DPDP. A consent-compliance company holding a database of other
    people's tracking IDs is not a position worth being in, so the value is
    dropped at the point of capture and never written anywhere.
    """
    now = now or time.time()
    expires = cookie.get("expires", -1)
    session = expires in (-1, 0, None)
    lifetime_days = None if session else max(0, int((expires - now) // 86400))

    domain = (cookie.get("domain") or "").lstrip(".")
    third_party = registrable(domain) != registrable(site_domain) if site_domain else False

    name = cookie.get("name", "")
    category = _cookie_category(name, third_party)

    return {
        "name": name,
        "domain": domain,
        "party": "third" if third_party else "first",
        "session": session,
        "lifetime_days": lifetime_days,
        "secure": bool(cookie.get("secure")),
        "same_site": cookie.get("sameSite"),
        "http_only": bool(cookie.get("httpOnly")),
        "category": category,
    }


def _cookie_category(name, third_party):
    """
    advertising / session_replay / analytics / third_party / functional.

    Name matching alone made unknown trackers invisible -- a real page set CLID,
    MUID, MR, SRM_B and ANONCHK (Clarity and Bing advertising) that no prefix
    list caught. So party is used as a backstop: a third-party cookie set before
    anyone consented is a tracker whether or not we recognise its name.
    """
    if any(name.startswith(p) for p in ADVERTISING_COOKIE_PREFIXES):
        return "advertising"
    if any(name.startswith(p) for p in SESSION_REPLAY_COOKIE_PREFIXES):
        return "session_replay"
    if any(name.startswith(p) for p in TRACKING_COOKIE_PREFIXES):
        return "analytics"
    return "third_party" if third_party else "functional"


TRACKING_CATEGORIES = ("advertising", "session_replay", "analytics", "third_party")


def analyse_cookies(cookies, site_domain):
    """
    Every cookie set before consent, classified. Returns the records plus the
    counts the export and the score are built from.
    """
    records = [cookie_record(c, site_domain) for c in cookies]
    tracking = [r for r in records if r["category"] in TRACKING_CATEGORIES]
    third_party_domains = sorted({r["domain"] for r in records if r["party"] == "third"})
    lifetimes = [r["lifetime_days"] for r in tracking if r["lifetime_days"]]

    return {
        "records": records,
        "tracking": tracking,
        "cookies_total": len(records),
        "tracking_count": len(tracking),
        "third_party_domains": third_party_domains,
        "third_party_count": len(third_party_domains),
        "max_lifetime_days": max(lifetimes) if lifetimes else None,
        "long_lived_count": sum(1 for d in lifetimes if d > 180),
        "by_category": {
            cat: [r["name"] for r in records if r["category"] == cat]
            for cat in TRACKING_CATEGORIES
        },
    }


def gap_score(has_cmp, analysis, banner, policy):
    """
    0-100 from the full cookie picture. Supersedes consent_gap_score(), which
    only ever saw a list of names and so undercounted -- on a real page it
    scored 13 of 24 cookies and missed an entire advertising cluster.

      trackers, weighted by category      up to 40
      distinct third-party domains        up to 15  (3 each)
      trackers living beyond 180 days     up to 10  (2 each)
      no cookie banner                        25
      no privacy policy                       15
      session replay present                  10
      advertising present                      5
    """
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


# --- bot walls and interstitials ---------------------------------------------
# A challenge page means we could not LOOK at the site. That is a different
# outcome from "this is the wrong company", and collapsing the two threw away
# real companies: rginternational.com served "Checking your browser..." and was
# scored 0 on identity.
BLOCK_SIGNATURES = (
    "checking your browser", "just a moment", "please wait while we verify",
    "verify you are human", "verifying you are human", "are you a robot",
    "enable javascript and cookies to continue", "ddos protection by",
    "attention required! | cloudflare", "cf-browser-verification",
    "access denied", "request unsuccessful", "unusual traffic",
    "captcha", "hcaptcha", "recaptcha", "incapsula incident",
    "this website is using a security service to protect itself",
)

# Statuses that mean "cannot look right now" rather than "wrong site".
BLOCKING_STATUSES = {401, 402, 403, 407, 423, 429, 451, 500, 502, 503, 504}


def is_blocked(title, text, status=None):
    """True if we were stopped by a wall rather than shown the site."""
    blob = f"{title} {text[:2000]}".lower()
    if any(sig in blob for sig in BLOCK_SIGNATURES):
        return True
    # A blocking status with almost no readable body is a wall, not a page.
    # A 402 suspended store that still renders its shop name is NOT blocked --
    # that page identifies the company perfectly well.
    return bool(status in BLOCKING_STATUSES and len((text or "").strip()) < 400)


# --- data-collection forms ---------------------------------------------------
# Cookies are only half of DPDP. A contact or enquiry form that takes a name,
# phone or email IS personal-data processing, and it needs notice and consent
# whether or not the site sets a single cookie. Roughly 58% of the sites we
# scrape set no cookies at all and score zero on the cookie axis -- some of
# them are collecting personal data in plain sight.

# Third-party form embeds: the data leaves the company's own infrastructure
# before it reaches them, which is a processor relationship they usually have
# not documented.
FORM_EMBED_SIGNATURES = (
    ("Google Forms",   ("docs.google.com/forms", "google.com/forms")),
    ("Typeform",       ("typeform.com",)),
    ("HubSpot Forms",  ("js.hsforms.net", "hsforms.com", "forms.hsforms")),
    ("Zoho Forms",     ("forms.zohopublic", "zohoforms")),
    ("JotForm",        ("jotform.com",)),
    ("Wufoo",          ("wufoo.com",)),
    ("Formspree",      ("formspree.io",)),
    ("Mailchimp Form", ("list-manage.com", "chimpstatic")),
    ("WPForms",        ("wpforms",)),
    ("Contact Form 7", ("wpcf7", "contact-form-7")),
    ("Gravity Forms",  ("gform_", "gravityforms")),
)

PERSONAL_FIELD_PATTERNS = {
    "email":   (r"\bemail\b", r"\be-?mail\b", r"\bmail\b"),
    "phone":   (r"\bphone\b", r"\bmobile\b", r"\btel\b", r"\bcontact.?number\b", r"\bwhatsapp\b"),
    "name":    (r"\bname\b", r"\bfull.?name\b", r"\bfirst.?name\b", r"\blast.?name\b"),
    "address": (r"\baddress\b", r"\bcity\b", r"\bpincode\b", r"\bpin.?code\b", r"\bzip\b"),
    "company": (r"\bcompany\b", r"\borganisation\b", r"\borganization\b"),
    "message": (r"\bmessage\b", r"\benquiry\b", r"\binquiry\b", r"\bcomment\b", r"\brequirement\b"),
}

# Personal data in the DPDP sense -- a message box alone is not.
IDENTIFYING_FIELDS = ("email", "phone", "name", "address")


def detect_form_embed(blob):
    """Third-party form provider present on the page, or None."""
    return _match(blob, FORM_EMBED_SIGNATURES)


def classify_form_fields(field_descriptors):
    """
    field_descriptors: list of strings gathered from each input's name, id,
    placeholder, type and nearby label. Returns the set of personal-data
    categories the form collects.
    """
    found = set()
    for descriptor in field_descriptors:
        text = (descriptor or "").lower()
        hits = {c for c, patterns in PERSONAL_FIELD_PATTERNS.items()
                if any(re.search(p, text) for p in patterns)}
        # "Email Address" is one field, not two: the word 'address' inside an
        # email label is not a postal address.
        if "email" in hits:
            hits.discard("address")
        found |= hits
    return sorted(found)


def form_gap(forms):
    """
    Summarises the form findings for scoring and for the export line.

    `forms` is what FORM_JS returns from the page: one dict per form with
    `fields` (descriptors), `has_consent_checkbox`, `has_privacy_link`,
    `action_host`, `external_action`.
    """
    collecting = []
    for form in forms or []:
        categories = classify_form_fields(form.get("fields") or [])
        if any(c in IDENTIFYING_FIELDS for c in categories):
            collecting.append({**form, "categories": categories})

    if not collecting:
        return {"has_data_form": False, "forms_collecting": 0, "field_categories": [],
                "consent_checkbox": None, "privacy_link": None, "external_hosts": []}

    categories = sorted({c for f in collecting for c in f["categories"]})
    return {
        "has_data_form": True,
        "forms_collecting": len(collecting),
        "field_categories": categories,
        # True only if EVERY collecting form asks for consent
        "consent_checkbox": all(f.get("has_consent_checkbox") for f in collecting),
        "privacy_link": any(f.get("has_privacy_link") for f in collecting),
        "external_hosts": sorted({f["action_host"] for f in collecting
                                  if f.get("external_action") and f.get("action_host")}),
    }


def consent_score(has_cmp, analysis, banner, policy, forms=None):
    """
    0-100 where 100 is CLEAN and 0 is worst. Higher is better.

    The scale reads the way a score is normally read: a site with nothing
    wrong scores 100, a site tracking heavily before consent scores near 0.
    Internally the parts are still accumulated as gap POINTS -- more points
    means more wrong -- and inverted once, here, at the end.

    Supersedes gap_score(): that returned 0 whenever a site set no tracking
    cookies, which wrote off 58% of scraped sites -- including any that quietly
    collect names, phone numbers and emails through an enquiry form with no
    consent checkbox and no privacy notice. Under DPDP that is processing
    personal data without notice, and it is a gap worth selling against even
    when not a single cookie fires.

    Forms add up to 45 on their own:
      collects identifying data, no consent checkbox   25
      collects identifying data, no privacy link       10
      posts to a third-party host                      10
    """
    base = gap_score(has_cmp, analysis, banner, policy)
    if has_cmp:
        return 100      # they already run a CMP; compliant, not a lead

    form = forms or {}
    if not form.get("has_data_form"):
        return 100 - base

    extra = 0
    if not form.get("consent_checkbox"):
        extra += 25
    if not form.get("privacy_link") and not policy:
        extra += 10
    if form.get("external_hosts"):
        extra += 10

    return 100 - int(min(100, base + extra))
