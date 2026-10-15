"""
The email templates (templates/email/band_a..d.html.j2) and the plain fallbacks for the three model slots.

Everything a recipient reads is either in a template file (fixed: subject, structure, the report link and
its anchor text, the call to action, the closing) or one of the two slots, opening_line and
finding_sentence, which are validated (draft_validate) before they get here. Jinja autoescape is on, so a
slot cannot add markup even if validation were skipped. The wording in the template files is a
PLACEHOLDER until the CEO and sales team approve it; template_version says so.
"""
import hashlib
import html
import re
from collections import namedtuple

import jinja2

from . import settings_drafts as cfg

BANDS = "ABCD"
Rendered = namedtuple("Rendered", "subject html text template_version")

_env = jinja2.Environment(autoescape=True, undefined=jinja2.StrictUndefined, keep_trailing_newline=False)


def _path(band):
    if band not in BANDS or len(band) != 1:
        raise ValueError(f"no template for band {band!r}")
    return cfg.TEMPLATE_DIR / f"band_{band.lower()}.html.j2"


def _source(band):
    return _path(band).read_text()


def _label(source):
    m = re.search(r'set template_version = "([^"]+)"', source)
    return m.group(1) if m else "unversioned"


def template_version(band):
    """The label in the file plus a short hash of its content: editing the template changes the version."""
    source = _source(band)
    return f"{_label(source)}-{hashlib.sha256(source.encode()).hexdigest()[:8]}"


def crm_version(version):
    """AE_DraftVersion is nvarchar(20): the template label without its content hash, e.g. 'B-0.1-placeholder'."""
    return re.sub(r"-[0-9a-f]{8}$", "", version)[:20]


def html_to_text(markup):
    """The plain-text twin of a rendered email: paragraphs, line breaks, links as `text (url)`."""
    text = re.sub(r'<a href="([^"]+)"[^>]*>(.*?)</a>', lambda m: f"{m.group(2)} ({m.group(1)})", markup, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<li>", "\u2022 ", text)
    text = re.sub(r"</li>\s*", "\n", text)
    text = re.sub(r"</ul>", "\n", text)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip() + "\n"


def render(band, opening_line, finding_sentence, impact_sentence, first_name, report_url, domain, facts=None):
    """
    Subject, HTML, plain text and version for one draft. The slots are plain text, escaped on the way in.
    `facts` are the company's own numbers for the fixed template: company, n_tracking, no_banner, n_third.
    """
    template = _env.from_string(_source(band))
    facts = facts or {}
    context = dict(opening_line=opening_line, finding_sentence=finding_sentence, impact_sentence=impact_sentence,
                   first_name=(first_name or "").strip() or None, report_url=report_url, domain=domain,
                   company=facts.get("company") or domain, n_tracking=facts.get("n_tracking", 0),
                   no_banner=facts.get("no_banner", False), n_third=facts.get("n_third", 0),
                   unsubscribe_url=cfg.UNSUBSCRIBE_URL or None)
    body = template.render(context).strip()
    subject = template.make_module(context).subject
    return Rendered(subject, body, html_to_text(body), template_version(band))


# --- plain fallbacks, written in code ----------------------------------------------------------------
_FINDING_FALLBACKS = {
    "G5": "Your consent tool is in place, but some tracking cookies are still set before a visitor chooses anything.",
    "G1": "Some tracking cookies are set on your homepage before a visitor has made any choice.",
    "G4": "Your site collects details through a form, but I could not find a privacy notice linked from the homepage.",
}
_FINDING_DEFAULT = "A few things on your homepage's consent set-up looked worth a second look."


def fallback_slots(findings):
    """The three sentences used when the model's are rejected twice. They state nothing the findings lack."""
    opening = f"I took a quick look at the consent set-up on {findings['domain']}."
    top = (findings.get("top_finding") or {}).get("code")
    return {"opening_line": opening, "finding_sentence": _FINDING_FALLBACKS.get(top, _FINDING_DEFAULT),
            "impact_sentence": "It is a small detail on the page, but one visitors notice more and more."}
