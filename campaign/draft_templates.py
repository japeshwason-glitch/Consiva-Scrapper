"""
The email template (templates/email/consent_v2.html.j2, one for every band; the four older band templates are
in templates/email/archive_v1/) and the plain fallbacks for the three model slots.

Everything a recipient reads is in the template file (fixed: subject, structure, the buttons and their links) plus the
company's own numbers, which render() turns into the bullets. The model slots (opening_line and finding_sentence) are no
longer used by this template; render() still accepts them so the model path can come back for a template that has them.
Jinja autoescape is on, so nothing passed in can add markup.
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


TEMPLATE_FILE = "consent_v2.html.j2"


def _path(band):
    if band not in BANDS or len(band) != 1:
        raise ValueError(f"no template for band {band!r}")
    return cfg.TEMPLATE_DIR / TEMPLATE_FILE        # one template for every band; the band only labels the draft


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
    text = re.sub(r"</table>", "\n\n", text)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip() + "\n"


def possessive(name):
    """'OpenXcell' -> 'OpenXcell’s', 'CodesClue Technologies' -> 'CodesClue Technologies’' (a name ending in s takes no extra s)."""
    name = (name or "").rstrip()
    return name + ("\u2019" if name[-1:].lower() == "s" else "\u2019s")


class NothingToList(ValueError):
    """The company has no finding the template can list (no tracking, no third parties, no banner or policy gap)."""


def _bullets(facts):
    """
    What the scan identified, as plain strings: the tracking-tool and third-party counts when they are above zero;
    when both are zero, the finding that made it a lead (no cookie banner, or no published privacy notice).
    """
    n_tracking, n_third = facts.get("n_tracking", 0), facts.get("n_third", 0)
    out = []
    if n_tracking:
        out.append(f"{n_tracking} tracking tool{'' if n_tracking == 1 else 's'} active before visitor consent")
    if n_third:
        out.append(f"{n_third} third-party domain{'' if n_third == 1 else 's'} contacted before consent")
    if not out and facts.get("no_banner"):
        out.append("No visible cookie consent banner on your website")
    if not out and facts.get("no_policy"):
        out.append("No privacy notice published on your website")
    return out


def render(band, opening_line, finding_sentence, impact_sentence, first_name, report_url, domain, facts=None):
    """
    Subject, HTML, plain text and version for one draft. The slots are plain text, escaped on the way in.
    `facts` are the company's own numbers for the fixed template: company, n_tracking, no_banner, n_third, no_policy.
    Raises NothingToList when there is nothing true to list.
    """
    template = _env.from_string(_source(band))
    facts = facts or {}
    # A directory import can leave the company's own name in the first-name field; that is not a person to address.
    if first_name and first_name.strip().casefold() == (facts.get("company") or "").strip().casefold():
        first_name = None
    bullets = _bullets(facts)
    if not bullets:
        raise NothingToList("nothing to list: no tracking, no third parties, no banner or policy gap")
    context = dict(bullets=bullets, opening_line=opening_line, finding_sentence=finding_sentence, impact_sentence=impact_sentence,
                   first_name=(first_name or "").strip() or None, report_url=report_url, domain=domain,
                   company=facts.get("company") or domain, company_possessive=possessive(facts.get("company") or domain),
                   n_tracking=facts.get("n_tracking", 0),
                   no_banner=facts.get("no_banner", False), n_third=facts.get("n_third", 0),
                   unsubscribe_url=cfg.UNSUBSCRIBE_URL or None)
    body = template.render(context).strip()
    subject = html.unescape(str(template.make_module(context).subject))     # a plain-text header: no entities
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
