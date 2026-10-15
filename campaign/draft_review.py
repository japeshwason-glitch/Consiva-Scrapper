"""
output/draft_review.html: every draft as the recipient would see it, with the company, band, score, the
findings it was built from and which slots came from the model, a retry or the fallback. A review aid
only: it reads ae_drafts and writes one HTML file; it touches nothing else.
"""
import json
from datetime import datetime
from pathlib import Path

import jinja2

import db

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "output" / "draft_review.html"

PAGE = jinja2.Environment(autoescape=True).from_string("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Draft review</title>
<style>
 body { font: 15px/1.5 system-ui, sans-serif; margin: 0; background: #f4f5f7; color: #1a1d21; }
 header { padding: 16px 24px; background: #fff; border-bottom: 1px solid #d8dbe0; }
 .banner { background: #fff4d6; border: 1px solid #e6c15a; padding: 8px 12px; border-radius: 6px; margin-top: 8px; }
 main { max-width: 1100px; margin: 0 auto; padding: 16px 24px 48px; }
 article { background: #fff; border: 1px solid #d8dbe0; border-radius: 8px; margin: 20px 0; padding: 16px 20px; }
 h2 { margin: 0 0 4px; font-size: 18px; } .meta { color: #5b6270; font-size: 13px; }
 .grid { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 20px; margin-top: 12px; }
 .email { border: 1px solid #c9ced6; border-radius: 6px; padding: 12px 16px; background: #fff; }
 .subject { font-weight: 600; margin-bottom: 8px; } .to { color: #5b6270; font-size: 13px; margin-bottom: 8px; }
 .tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; margin-right: 4px; }
 .model { background: #dff3e4; } .retry { background: #fff0c2; } .fallback { background: #fbdcdc; }
 .ready { background: #dff3e4; } .risky { background: #fbdcdc; }
 pre { white-space: pre-wrap; background: #f4f5f7; padding: 8px; border-radius: 6px; font-size: 12px; overflow-x: auto; }
 table { border-collapse: collapse; font-size: 13px; } td, th { padding: 2px 10px 2px 0; text-align: left; vertical-align: top; }
 @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
</style></head><body>
<header><h1 style="margin:0;font-size:20px">Draft review: {{ drafts|length }} draft(s)</h1>
<div class="meta">Generated {{ now }}. Nothing here has been sent or written to the CRM.</div>
<div class="banner"><strong>The template wording is a placeholder</strong>, not approved by the CEO or the sales team.
Slot colours: <span class="tag model">model</span><span class="tag retry">retry</span><span class="tag fallback">fallback</span></div></header>
<main>
{% for d in drafts %}
<article>
 <h2>{{ d.company_name }} <span class="meta">{{ d.domain }}</span></h2>
 <div class="meta">company {{ d.company_id }}{% if d.is_test %} (test cohort){% endif %} · band {{ d.band }} · score {{ d.score }} ·
   {{ d.gap_count }} gap(s) · <span class="tag {{ d.draft_status }}">{{ d.draft_status }}</span> ·
   template {{ d.template_version }} · prompt {{ d.prompt_version }} · {{ d.provider }}/{{ d.model }} ·
   tokens in {{ d.input_tokens }} / out {{ d.output_tokens }} · attempts {{ d.attempts }}</div>
 <div class="grid">
  <div>
   <div class="email">
    <div class="subject">{{ d.subject }}</div>
    <div class="to">To: {{ d.to_email }}</div>
    {{ d.body_html|safe }}
   </div>
   <details><summary>Plain-text version</summary><pre>{{ d.plain_text }}</pre></details>
  </div>
  <div>
   <table>
    <tr><th>opening_line</th><td><span class="tag {{ d.slot_source.opening_line }}">{{ d.slot_source.opening_line }}</span></td></tr>
    <tr><th>finding_sentence</th><td><span class="tag {{ d.slot_source.finding_sentence }}">{{ d.slot_source.finding_sentence }}</span></td></tr>
    <tr><th>impact_sentence</th><td><span class="tag {{ d.slot_source.impact_sentence }}">{{ d.slot_source.impact_sentence }}</span></td></tr>
    <tr><th>contact function</th><td>{{ d.contact_function or 'none' }}</td></tr>
   </table>
   <details><summary>Validation failures per attempt</summary><pre>{{ d.validation_log_text }}</pre></details>
   <details open><summary>Findings the draft was built from</summary><pre>{{ d.findings_text }}</pre></details>
  </div>
 </div>
</article>
{% else %}<p>No drafts yet.</p>{% endfor %}
</main></body></html>""")


def write_review(path=None, only_test=None):
    """Writes the review page; returns its path."""
    path = Path(path or DEFAULT_PATH)
    where, params = "TRUE", []
    if only_test is not None:
        where, params = "a.is_test = %s", [only_test]
    with db.get_cursor() as cur:
        cur.execute(f"""SELECT d.*, a.company_name, a.is_test, a.score, a.gap_count
                        FROM ae_drafts d JOIN ae_companies a USING (company_id) WHERE {where}
                        ORDER BY d.band, a.company_id""", params)
        drafts = cur.fetchall()
    for d in drafts:
        d["findings_text"] = json.dumps(d["findings"], indent=1, ensure_ascii=False)
        d["validation_log_text"] = json.dumps(d["validation_log"], indent=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PAGE.render(drafts=drafts, now=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")))
    return path
