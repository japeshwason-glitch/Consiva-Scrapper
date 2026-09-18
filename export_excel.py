"""
Excel export of everything the dashboard shows, for sharing outside the tool.

    python export_excel.py [--out data/exports/consiva_findings.xlsx]

One sheet, one row per company. Per-cookie detail stays in the dashboard --
it is 428 rows against 73 companies and nobody reads it in a spreadsheet.

LinkedIn employee count is deliberately excluded.
Cookie VALUES are never exported anywhere: they are live tracking identifiers
for real visitors and have no business leaving the database.
"""

import argparse
from pathlib import Path

import pandas as pd

import db
import fingerprints

# What someone actually uses when they open the file. Everything else is one
# click away in the dashboard, and the cookie detail is on its own sheet.
CORE_COLUMNS = [
    ("company_name",             "Company"),
    ("cin",                      "CIN"),
    ("final_domain",             "Website"),
    ("consent_gap_score",        "Gap score"),
    ("verdict",                  "Verdict"),
    ("contact_name",             "Contact"),
    ("contact_title",            "Contact title"),
    ("business_scale",           "Scale"),
    ("industry",                 "Industry"),
    ("tracking_cookie_count",    "Trackers pre-consent"),
    ("third_party_count",        "3rd parties"),
    ("max_cookie_lifetime_days", "Longest tracker (days)"),
    ("has_cookie_banner",        "Cookie banner"),
    ("form_summary",             "Collects via form"),
    ("consent_gap_summary",      "Consent finding"),
]

FULL_COLUMNS = [
    ("company_name",            "Company"),
    ("cin",                     "CIN"),
    ("final_domain",            "Website"),
    ("resolved_domain",         "Resolved domain"),
    ("consent_gap_score",       "Consent gap score"),
    ("verdict",                 "Verdict"),
    ("has_cmp",                 "Has CMP"),
    ("cmp_name",                "CMP vendor"),
    ("has_cookie_banner",       "Cookie banner"),
    ("has_privacy_policy",      "Privacy policy linked"),
    ("cookies_total",           "Cookies total"),
    ("tracking_cookie_count",   "Tracking cookies pre-consent"),
    ("third_party_count",       "Third-party domains"),
    ("third_party_domains",     "Third parties"),
    ("max_cookie_lifetime_days","Longest tracker (days)"),
    ("has_data_form",           "Collects data via form"),
    ("form_field_types",        "Form collects"),
    ("form_consent_checkbox",   "Form consent checkbox"),
    ("form_third_party_hosts",  "Form submits to"),
    ("form_embed_provider",     "Form provider"),
    ("tech_cms",                "CMS"),
    ("tech_ecommerce_platform", "Ecommerce"),
    ("tech_payment_gateway",    "Payment gateway"),
    ("tech_analytics_tools",    "Analytics"),
    ("tech_marketing_tools",    "Marketing/ad tech"),
    ("contact_name",            "Contact"),
    ("contact_title",           "Contact title"),
    ("contact_email",           "Contact email"),
    ("title_match_confidence",  "Contact confidence"),
    ("business_scale",          "Business scale"),
    ("scale_confidence",        "Scale confidence"),
    ("scale_basis",             "Scale basis"),
    ("linkedin_industry",       "Industry (LinkedIn)"),
    ("industrial_classification","Industry (MCA)"),
    ("linkedin_hq",             "HQ"),
    ("linkedin_url",            "LinkedIn"),
    ("paidup_capital_cr",       "Paid-up capital (Rs cr)"),
    ("registration_date",       "Registered"),
    ("reg_pin",                 "PIN"),
    ("company_tagline",         "Tagline"),
    ("consent_gap_summary",     "Consent finding"),
    ("resolve_evidence",        "Why this website matched"),
    ("status",                  "Pipeline status"),
]

def flatten(value):
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return value


def build(columns):
    rows = []
    for status in ("scraped", "linkedin_done", "classified",
                   "contact_done", "contact_failed", "exported", "not_a_lead"):
        for c in db.get_pending(status, limit=100000):
            site = c.get("final_domain") or c.get("resolved_domain")
            score = c.get("consent_gap_score")
            c = dict(c)
            c["verdict"] = ("Not a lead — has CMP" if c.get("has_cmp")
                            else "LEAD" if fingerprints.is_lead_score(score)
                            else "Not a lead — no gap found")
            cap = c.get("paidup_capital")
            c["paidup_capital_cr"] = round(float(cap) / 1e7, 2) if cap is not None else None
            c["industry"] = c.get("linkedin_industry") or c.get("industrial_classification")
            c["form_summary"] = (
                ", ".join(c.get("form_field_types") or []) + " (no consent checkbox)"
                if c.get("has_data_form") and not c.get("form_consent_checkbox")
                else ", ".join(c.get("form_field_types") or []) if c.get("has_data_form")
                else "")
            rows.append({label: flatten(c.get(key)) for key, label in columns})


    score_col = "Gap score" if "Gap score" in rows[0] else "Consent gap score"
    return pd.DataFrame(rows).sort_values(score_col, ascending=False)


def write(companies, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        companies.to_excel(writer, sheet_name="Companies", index=False)

        for sheet, frame in (("Companies", companies),):
            ws = writer.sheets[sheet]
            ws.freeze_panes = "B2"
            for i, col in enumerate(frame.columns, start=1):
                longest = max([len(str(col))] +
                              [len(str(v)) for v in frame[col].head(300)])
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(60, longest + 2)
            for cell in ws[1]:
                cell.font = cell.font.copy(bold=True)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/exports/consiva_findings.xlsx")
    ap.add_argument("--full", action="store_true",
                    help="every field the dashboard holds (43 columns) instead of the core 15")
    args = ap.parse_args()
    companies = build(FULL_COLUMNS if args.full else CORE_COLUMNS)
    path = write(companies, Path(args.out))
    print(f"wrote {path} — {len(companies)} rows x {len(companies.columns)} columns")
