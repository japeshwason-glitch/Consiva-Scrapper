"""
Stage 3: resolved -> scraped / scrape_failed
One page load, multiple jobs: consent-gap detection, tech stack, tagline,
careers page, team headcount. Companies with has_cmp=true get marked
'not_a_lead' here -- they never reach the paid stages below.

    python stages/03_scrape_playwright.py --limit 10
    python stages/03_scrape_playwright.py --limit 10 --out logs/run.json
    python stages/03_scrape_playwright.py --limit 10 --headed      # watch it work

Division of labour, deliberate:
  * has_cmp / cmp_name / cookies / tech stack -> fingerprints.py, exact string
    matching only. This is the cost gate deciding whether a company reaches the
    paid Apify and Apollo stages, so it must be reproducible and auditable.
  * tagline / clients / founder / headcount / open roles -> the local LLM, which
    only ever reports what the page text already says.
  * consent_gap_summary -> assembled deterministically from the findings above.
    It is a raw fact for the CRM person, never model-written prose.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

import db
import fingerprints
import llm

NAV_TIMEOUT_MS = 25_000
# Trackers that matter most load late: Hotjar and Clarity fire well after
# DOMContentLoaded. A 2.5s window missed both on a site that had them, which
# moved its consent_gap_score by 16 points between runs. We wait for network
# idle first and only then settle, so the cookie set is stable.
SETTLE_MS = 4_000
NETWORK_IDLE_TIMEOUT_MS = 8_000
MAX_CONCURRENCY = 4
MAX_TEXT_CHARS = 3_500     # what the model sees; enough for a homepage's substance

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SUBPAGE_PATTERNS = {
    "careers": (r"career", r"job", r"vacanc", r"we[-_ ]?are[-_ ]?hiring", r"opening"),
    "team": (r"about", r"team", r"leadership", r"management", r"who[-_ ]?we[-_ ]?are"),
    "blog": (r"blog", r"news", r"insight", r"article", r"press"),
}

# Text that reads like a job posting, for the deterministic role count.
ROLE_HINT = re.compile(
    r"\b(engineer|developer|manager|executive|officer|analyst|designer|intern|"
    r"lead|head|director|associate|assistant|consultant|technician|operator|"
    r"supervisor|accountant|sales|marketing)\b", re.I,
)

# --- page-side extraction ----------------------------------------------------
# Runs in the browser: strips chrome so the model gets substance, not navigation.
EXTRACT_JS = """
() => {
  const clone = document.body ? document.body.cloneNode(true) : null;
  if (!clone) return {main: "", footer: "", links: []};
  clone.querySelectorAll('script,style,noscript,svg,nav,header,form').forEach(e => e.remove());

  const footerEl = clone.querySelector('footer');
  const footer = footerEl ? footerEl.innerText : "";
  if (footerEl) footerEl.remove();

  // Pick the LARGEST content container, not the first one matching. Page
  // builders (Elementor especially) scatter <article> and .content over small
  // widgets, so 'first match' can hand back a single blog card -- on one real
  // site that meant 207 characters out of 10,016, and every extracted field
  // came back null as a result.
  const whole = clone.innerText || "";
  let main = whole;
  const candidates = clone.querySelectorAll('main,article,[role=main],#content,.content');
  for (const el of candidates) {
    const t = el.innerText || "";
    if (t.length > main.length) main = t;
  }
  // A container holding only a fraction of the page is a fragment, not the
  // content. Fall back to the whole body rather than trust it.
  if (main.length < whole.length * 0.4) main = whole;

  const links = Array.from(document.querySelectorAll('a[href]'))
    .map(a => ({href: a.getAttribute('href') || "", text: (a.innerText || "").trim().slice(0, 80)}))
    .slice(0, 400);
  return {main, footer, links};
}
"""



# Runs in the page: the markup inside every shadow root, which page.content()
# does not serialise. WPConsent moves its whole banner into one on load
# (div#wpconsent-container), so a page whose raw HTML says "We use cookies"
# read as having no banner at all. Any vendor rendering that way hid the same.
SHADOW_JS = r"""
() => {
  const out = [];
  const walk = root => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) { out.push(el.shadowRoot.innerHTML); walk(el.shadowRoot); }
    }
  };
  walk(document);
  return out.join('\n');
}
"""

# Runs in the page: describes every form well enough to classify what personal
# data it collects, and whether the visitor is told anything before giving it.
FORM_JS = r"""
() => {
  const host = location.hostname.replace(/^www\./, '');
  return Array.from(document.querySelectorAll('form')).slice(0, 12).map(f => {
    const fields = Array.from(f.querySelectorAll('input,textarea,select'))
      .filter(el => !['hidden','submit','button','image'].includes((el.type||'').toLowerCase()))
      .map(el => {
        let label = '';
        if (el.id) {
          const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
          if (l) label = l.innerText || '';
        }
        if (!label && el.closest('label')) label = el.closest('label').innerText || '';
        return [el.name, el.id, el.placeholder, el.type, el.getAttribute('aria-label'), label]
          .filter(Boolean).join(' ');
      });

    // A consent checkbox is an unchecked box whose wording mentions consent,
    // agreement, a privacy policy or terms.
    const hasConsent = Array.from(f.querySelectorAll('input[type=checkbox]')).some(cb => {
      const near = ((cb.closest('label') || cb.parentElement || {}).innerText || '').toLowerCase();
      return /consent|agree|privacy|terms|permission|opt.?in/.test(near);
    });
    const hasPrivacyLink = Array.from(f.querySelectorAll('a[href]')).some(a =>
      /privacy|policy|terms/i.test(a.getAttribute('href') + ' ' + (a.innerText || '')));

    let actionHost = '', external = false;
    try {
      const action = f.getAttribute('action');
      if (action) {
        const u = new URL(action, location.href);
        actionHost = u.hostname.replace(/^www\./, '');
        external = actionHost !== host;
      }
    } catch (e) {}

    return {fields, has_consent_checkbox: hasConsent, has_privacy_link: hasPrivacyLink,
            action_host: actionHost, external_action: external};
  });
}
"""


def clean_text(raw, limit=MAX_TEXT_CHARS):
    return re.sub(r"\s{2,}", " ", re.sub(r"\n{2,}", "\n", (raw or ""))).strip()[:limit]


def pick_subpages(base_url, links):
    """First same-host link matching each of careers/team/blog."""
    host = urlparse(base_url).netloc.lower().removeprefix("www.")
    found = {}
    for kind, patterns in SUBPAGE_PATTERNS.items():
        for link in links:
            href, text = link.get("href", ""), link.get("text", "")
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            full = urljoin(base_url, href)
            link_host = urlparse(full).netloc.lower().removeprefix("www.")
            if link_host and link_host != host:
                continue
            haystack = f"{href} {text}".lower()
            if any(re.search(p, haystack) for p in patterns):
                found[kind] = full
                break
    return found


def count_roles(text):
    """Deterministic floor on open roles -- lines that read like job titles."""
    lines = [ln.strip() for ln in (text or "").splitlines() if 3 < len(ln.strip()) < 90]
    return len({ln for ln in lines if ROLE_HINT.search(ln)})


def build_form_sentence(form_gap, embed):
    """The form finding, in the same factual register as the cookie finding."""
    if not form_gap["has_data_form"]:
        return None
    fields = ", ".join(form_gap["field_categories"])
    parts = [f"collects personal data via {form_gap['forms_collecting']} web form(s) "
             f"({fields})"]
    parts.append("with no consent checkbox" if not form_gap["consent_checkbox"]
                 else "with a consent checkbox")
    if not form_gap["privacy_link"]:
        parts.append("and no privacy link on the form")
    if form_gap["external_hosts"]:
        parts.append(f"submitted to third-party host(s): "
                     f"{', '.join(form_gap['external_hosts'])}")
    if embed:
        parts.append(f"form provider: {embed}")
    return " ".join(parts)


def build_consent_summary(cmp_name, analysis, banner, policy, form_gap=None, embed=None):
    """
    The factual consent finding, assembled from the scrape. Deterministic by
    design: this string reaches the CRM person via stage 07, and CLAUDE.md
    requires those export fields be raw facts rather than written copy.

    Written to be usable verbatim in a sales conversation -- third-party
    recipients and cookie lifetimes are the concrete part, far more than a count.
    """
    parts = []
    parts.append(f"CMP detected: {fingerprints.cmp_display_name(cmp_name)}" if cmp_name
                 else "No consent management platform detected")

    n = analysis["tracking_count"]
    if n:
        names = [r["name"] for r in analysis["tracking"]]
        shown = ", ".join(names[:6])
        more = f" (+{n - 6} more)" if n > 6 else ""
        parts.append(
            f"{n} of {analysis['cookies_total']} cookies are tracking cookies set "
            f"before consent: {shown}{more}"
        )
        if analysis["third_party_count"]:
            doms = ", ".join(analysis["third_party_domains"][:5])
            extra = (f" (+{analysis['third_party_count'] - 5} more)"
                     if analysis["third_party_count"] > 5 else "")
            parts.append(
                f"data sent to {analysis['third_party_count']} third-party "
                f"domains before consent: {doms}{extra}"
            )
        if analysis["max_lifetime_days"]:
            parts.append(
                f"longest tracker lifetime {analysis['max_lifetime_days']} days"
                + (f", {analysis['long_lived_count']} trackers persist beyond 180 days"
                   if analysis["long_lived_count"] else "")
            )
    else:
        parts.append("no tracking cookies set before consent")

    parts.append("cookie banner present" if banner else "no cookie banner")
    parts.append("privacy/cookie policy linked" if policy else "no privacy policy linked")

    form_sentence = build_form_sentence(form_gap or {"has_data_form": False}, embed)
    if form_sentence:
        parts.append(form_sentence)
    return "; ".join(parts) + "."


async def load(context, url, wait=SETTLE_MS, with_status=False):
    """
    Navigate and return (page, html, extracted) -- plus the main response's
    HTTP status as a fourth item when with_status. Caller closes the page.
    """
    page = await context.new_page()
    response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    try:
        await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
    except Exception:
        pass  # busy pages never go idle; the settle below still gives trackers time
    await page.wait_for_timeout(wait)
    html = await page.content()
    data = await page.evaluate(EXTRACT_JS)
    if with_status:
        return page, html, data, (response.status if response else None)
    return page, html, data


def homepage_urls(domain, www_fallback=False):
    """
    The URLs tried for a homepage, in order: https, then http. With
    www_fallback, then https://www. and http://www. too -- some sites
    (bisleri.com, biba.in) only answer on www, and the bare domain does not
    even resolve.
    """
    urls = [f"https://{domain}", f"http://{domain}"]
    if www_fallback and not domain.startswith("www."):
        urls += [f"https://www.{domain}", f"http://www.{domain}"]
    return urls


async def scrape_one(browser, company, use_llm, report_mode=False):
    """
    Everything for one company in one browser context. The context is fresh and
    nothing is ever clicked, so whatever cookies exist afterwards were set
    without consent -- that is the measurement.

    report_mode=True is for the one-page DPDP reports (report_build.py) and
    changes nothing on the default path. It loads the homepage only -- no
    subpages, no model -- and adds: http_status; scan_status 'ok' or
    'unscannable' (HTTP >= 400, a bot wall, or a parked page) with
    unscannable_reason; a vendor label on every cookie record;
    form_privacy_link; third_party_tracking_domains. If the bare domain does
    not load at all, it also tries www. (homepage_urls).

    cmp_evidence (script_src / dom / text) is measured on every path: the
    GapScore treats a CMP seen only in page text as no CMP, and scores a real
    one normally while tracking cookies still fire.
    """
    domain = company["resolved_domain"]
    name = company["company_name"]

    context = await browser.new_context(
        user_agent=USER_AGENT, viewport={"width": 1366, "height": 900},
        ignore_https_errors=True, locale="en-IN",
    )
    try:
        loaded = None
        for attempt_url in homepage_urls(domain, www_fallback=report_mode):
            try:
                loaded = await load(context, attempt_url, with_status=report_mode)
                break
            except Exception as exc:
                load_error = exc
        if loaded is None:
            raise load_error            # the last URL's error, as before
        page, html, data = loaded[:3]
        http_status = loaded[3] if report_mode else None
        # Which known CMP banner elements / globals are live -- the GapScore
        # depends on whether a detected CMP is really installed.
        cmp_dom_hits = await page.evaluate(fingerprints.CMP_DOM_JS,
                                           fingerprints.all_cmp_dom_markers())

        final_url = page.url
        title = await page.title()
        meta_desc = await page.evaluate(
            "() => (document.querySelector('meta[name=description]')||{}).content || ''"
        )
        scripts = await page.evaluate(
            "() => Array.from(document.querySelectorAll('script[src]')).map(s => s.src)"
        )
        forms = await page.evaluate(FORM_JS)
        shadow_html = await page.evaluate(SHADOW_JS)
        # Contact pages carry the real enquiry form far more often than the
        # homepage does, so their forms are merged in below.
        cookies = await context.cookies()
        await page.close()

        blob = "\n".join([html, shadow_html, " ".join(scripts)])
        main_text = clean_text(data.get("main"))
        footer_text = clean_text(data.get("footer"), 1200)

        # --- deterministic layer -------------------------------------------
        cmp_name = fingerprints.detect_cmp(blob)
        banner = fingerprints.detect_banner(blob)
        policy = fingerprints.detect_privacy_policy(blob)
        tech = fingerprints.detect_tech(blob)
        has_cmp = cmp_name is not None
        cmp_evidence = fingerprints.cmp_evidence(cmp_name, scripts, cmp_dom_hits)

        # Cookies are classified against the domain we actually LANDED on, not
        # the one we requested: sites redirect (ronaldweboffset.com serves from
        # ronaldindia.com), and comparing against the wrong domain marks every
        # first-party cookie as third-party.
        final_domain = urlparse(final_url).netloc.lower().removeprefix("www.") or domain
        analysis = fingerprints.analyse_cookies(cookies, final_domain)
        form_gap = fingerprints.form_gap(forms)
        form_embed = fingerprints.detect_form_embed(blob)
        gap_score = fingerprints.consent_score(
            has_cmp, analysis, banner, policy, form_gap, cmp_evidence)

        result = {
            "cin": company["cin"], "company_name": name, "domain": domain,
            "final_url": final_url,
            "page_title": title,
            "has_cmp": has_cmp, "cmp_name": cmp_name, "cmp_evidence": cmp_evidence,
            "final_domain": final_domain,
            "pre_consent_cookies": analysis["records"],
            "cookies_total": analysis["cookies_total"],
            "tracking_cookie_count": analysis["tracking_count"],
            "third_party_domains": analysis["third_party_domains"],
            "third_party_count": analysis["third_party_count"],
            "max_cookie_lifetime_days": analysis["max_lifetime_days"],
            "has_cookie_banner": banner, "has_privacy_policy": policy,
            "consent_gap_summary": build_consent_summary(
                cmp_name, analysis, banner, policy, form_gap, form_embed),
            "consent_gap_score": gap_score,
            "has_data_form": form_gap["has_data_form"],
            "form_field_types": form_gap["field_categories"],
            "form_consent_checkbox": form_gap["consent_checkbox"],
            "form_third_party_hosts": form_gap["external_hosts"],
            "form_embed_provider": form_embed,
            "cookie_categories": analysis["by_category"],
            **tech,
            "footer_address": None, "footer_phone": None,
            "founder_name_site": None, "company_tagline": None,
            "notable_clients": [], "notable_open_roles": [], "open_roles_count": None,
            "recent_blog_title": None, "recent_blog_date": None,
            "team_headcount_site": None,
            "subpages": {},
        }

        phone = re.search(r"(?:\+91[\s-]?)?\b\d{5}[\s-]?\d{5}\b|\b0\d{2,4}[\s-]?\d{6,8}\b", footer_text)
        if phone:
            result["footer_phone"] = phone.group(0).strip()
        if footer_text:
            result["footer_address"] = footer_text[:300]

        if report_mode:
            for r in analysis["records"]:
                r["vendor"] = fingerprints.cookie_vendor(r["name"], r["domain"], r["category"])
            if http_status is not None and http_status >= 400:
                reason = f"homepage returned HTTP {http_status}"
            elif fingerprints.is_blocked(title, main_text, http_status):
                reason = "homepage is a bot check or challenge page"
            elif fingerprints.is_parked(title, main_text, final_domain):
                reason = "homepage is parked, a placeholder or unfinished"
            else:
                reason = None
            result.update(
                http_status=http_status,
                scan_status="unscannable" if reason else "ok",
                unscannable_reason=reason,
                form_privacy_link=form_gap["privacy_link"],
                third_party_tracking_domains=analysis["third_party_domains"],
            )
            return result, None   # homepage only: no subpages, no model

        # --- subpages -------------------------------------------------------
        subpages = pick_subpages(final_url, data.get("links", []))
        result["subpages"] = subpages

        careers_text = blog_text = team_text = ""
        for kind, sub_url in subpages.items():
            try:
                sub_page, _, sub_data = await load(context, sub_url, wait=1200)
                text = clean_text(sub_data.get("main"), 2500)
                await sub_page.close()
            except Exception:
                continue
            if kind == "careers":
                careers_text = text
            elif kind == "blog":
                blog_text = text
            elif kind == "team":
                team_text = text

        if careers_text:
            result["open_roles_count"] = count_roles(careers_text)

        # --- model layer: reports what the text says, nothing more ----------
        if use_llm:
            corpus = "\n\n".join(
                x for x in [f"Meta description: {meta_desc}" if meta_desc else "",
                            main_text, team_text] if x
            )
            soft = await asyncio.to_thread(
                llm.extract_soft_fields, name, corpus[:MAX_TEXT_CHARS]
            )
            result["company_tagline"] = soft["tagline"] or (meta_desc[:300] or None)
            result["notable_clients"] = soft["notable_clients"]
            result["founder_name_site"] = soft["founder_name"]
            # team_headcount_site retired: it came back null on essentially
            # every site, and LinkedIn (stage 04) is the real source.

            if careers_text:
                roles = await asyncio.to_thread(
                    llm.chat_json,
                    'Reply with ONLY a JSON object: {"roles": [job titles listed as '
                    'currently open on this careers page]}. Use only titles stated in '
                    "the text. If none are listed, return an empty array.",
                    f"Company: {name}\n\nCareers page text:\n{careers_text}",
                    300,
                )
                if roles and isinstance(roles.get("roles"), list):
                    titles = [str(r).strip()[:100] for r in roles["roles"] if str(r).strip()][:20]
                    result["notable_open_roles"] = titles
                    if titles:
                        result["open_roles_count"] = len(titles)

            if blog_text:
                post = await asyncio.to_thread(
                    llm.chat_json,
                    'Reply with ONLY a JSON object: {"title": most recent post title or '
                    'null, "date": its publication date as YYYY-MM-DD or null}. Use only '
                    "what the text states.",
                    f"Blog page text:\n{blog_text}",
                    120,
                )
                if post:
                    t, d = post.get("title"), post.get("date")
                    if isinstance(t, str) and t.strip():
                        result["recent_blog_title"] = t.strip()[:300]
                    if isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.strip()):
                        result["recent_blog_date"] = d.strip()

        return result, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
    finally:
        await context.close()


SCRAPE_FIELDS = (
    "has_cmp", "cmp_name", "pre_consent_cookies", "founder_name_site",
    "company_tagline", "notable_open_roles", "open_roles_count",
    "notable_clients", "recent_blog_title", "recent_blog_date",
    "tech_cms", "tech_ecommerce_platform", "tech_payment_gateway",
    "tech_analytics_tools", "tech_marketing_tools", "team_headcount_site",
    "footer_address", "footer_phone", "consent_gap_summary",
    "has_cookie_banner", "has_privacy_policy", "consent_gap_score",
    "final_domain", "cookies_total", "tracking_cookie_count",
    "third_party_domains", "third_party_count", "max_cookie_lifetime_days",
    "has_data_form", "form_field_types", "form_consent_checkbox",
    "form_third_party_hosts", "form_embed_provider",
)


async def run(companies, use_llm, dry_run, concurrency):
    results = []
    semaphore = asyncio.Semaphore(concurrency)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        async def worker(company):
            async with semaphore:
                data, err = await scrape_one(browser, company, use_llm)
                name = company["company_name"][:40]

                if err:
                    print(f"  FAIL  {name:<42} {err}")
                    if not dry_run:
                        await asyncio.to_thread(
                            db.mark_failed, company["cin"], "scrape", err
                        )
                    return {"cin": company["cin"], "company_name": company["company_name"],
                            "error": err}

                score = data["consent_gap_score"]
                if data["has_cmp"]:
                    tag, gate, note = "SKIP", "NOT A LEAD", data["cmp_name"]
                elif not fingerprints.is_lead_score(score):
                    tag, gate, note = "WEAK", "no gap", "no pre-consent trackers"
                else:
                    tag, gate = "OK  ", f"gap {score}"
                    note = (f"{data['tracking_cookie_count']}/{data['cookies_total']} "
                            f"tracking, {data['third_party_count']} 3p domains")
                print(f"  {tag}  {name:<42} {gate:<11} {note}")

                if not dry_run:
                    fields = {k: data[k] for k in SCRAPE_FIELDS}
                    # Write findings first, then close the gate. If this crashes
                    # in between, the row sits at 'scraped' with has_cmp=true --
                    # which stage 04's has_cmp = false filter still excludes.
                    await asyncio.to_thread(
                        db.advance_status, company["cin"], "scraped", **fields
                    )
                    if data["has_cmp"]:
                        await asyncio.to_thread(
                            db.mark_not_a_lead, company["cin"],
                            f"already has a CMP: {data['cmp_name']}",
                        )
                return data

        results = await asyncio.gather(*(worker(c) for c in companies))
        await browser.close()
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", help="write full results to this JSON file")
    ap.add_argument("--dry-run", action="store_true", help="scrape but write nothing to the DB")
    ap.add_argument("--no-llm", action="store_true", help="deterministic fields only")
    ap.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY)
    args = ap.parse_args()

    companies = db.get_pending("resolved", limit=args.limit)
    if not companies:
        print("nothing at status='resolved' -- run stage 02 first")
        return

    use_llm = not args.no_llm and llm.available()
    if not args.no_llm and not use_llm:
        print("warning: LLM server unreachable -- continuing with deterministic fields only")

    print(f"scraping {len(companies)} companies (llm={'on' if use_llm else 'off'}, "
          f"concurrency={args.concurrency})\n")

    results = asyncio.run(run(companies, use_llm, args.dry_run, args.concurrency))

    ok = [r for r in results if r and not r.get("error")]
    leads = [r for r in ok if not r["has_cmp"]
             and fingerprints.is_lead_score(r["consent_gap_score"])]
    weak = [r for r in ok if not r["has_cmp"]
            and not fingerprints.is_lead_score(r["consent_gap_score"])]
    cmp_hits = [r for r in ok if r["has_cmp"]]
    print(f"\nscraped {len(ok)}/{len(companies)} | {len(leads)} leads with a gap | "
          f"{len(weak)} no gap | {len(cmp_hits)} not_a_lead | "
          f"{len(results) - len(ok)} failed")
    if leads:
        top = sorted(leads, key=lambda r: -r["consent_gap_score"])[:5]
        print("\nstrongest gaps:")
        for r in top:
            print(f"  {r['consent_gap_score']:>3}  {r['company_name'][:40]:<42} {r['domain']}")

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"run_at": datetime.now(timezone.utc).isoformat(),
             "count": len(results), "results": results},
            indent=2, ensure_ascii=False,
        ))
        print(f"wrote {path}")

    if args.dry_run:
        print("dry run -- nothing written to the DB")
    else:
        print("status counts:", db.count_by_status())


if __name__ == "__main__":
    main()
