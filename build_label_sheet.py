"""
Builds a hand-labelling sheet for domain resolution.

Ground truth so far came from the verifier itself, which makes every metric
circular -- KEONA INTERNATIONAL was labelled 'wrong' by the old 8B model while
being demonstrably correct. This sheet carries NO machine verdict, so labelling
is not anchored by what the system already decided.
"""
import csv, re, sys, concurrent.futures as cf
import requests, urllib3
import config, psycopg2, location

urllib3.disable_warnings()
UA={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}

def fetch(domain):
    for scheme in ("https://","http://"):
        try:
            r=requests.get(f"{scheme}{domain}",headers=UA,timeout=(5,12),
                           allow_redirects=True,verify=False)
            if r.status_code<400 and r.text: return r.text, r.url
        except requests.RequestException: continue
    return None, None

def page(html):
    t=re.search(r"<title[^>]*>(.*?)</title>",html,re.I|re.S)
    title=re.sub(r"\s+"," ",t.group(1)).strip() if t else ""
    b=re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>"," ",html)
    b=re.sub(r"(?s)<[^>]+>"," ",b)
    body=re.sub(r"\s+"," ",b).strip()
    return title, body

c=psycopg2.connect(config.DATABASE_URL); cur=c.cursor()
cur.execute("""
    SELECT cin,company_name,resolved_domain,registered_address,
           industrial_classification,resolve_confidence,status
    FROM companies
    WHERE resolved_domain IS NOT NULL
      AND status IN ('resolve_failed','scraped','not_a_lead')
    ORDER BY resolve_confidence NULLS LAST""")
cols=[d[0] for d in cur.description]
rows=[dict(zip(cols,r)) for r in cur.fetchall()]
print(f"{len(rows)} resolved domains to label", file=sys.stderr)

def build(i_row):
    i,row=i_row
    html,final=fetch(row["resolved_domain"])
    if not html:
        title,snippet,final="(unreachable now)","",""
    else:
        title,body=page(html)
        snippet=body[:150]
    loc=location.parse(row["registered_address"])
    where=", ".join([t for t in (loc["locations"] or [])[:2]] +
                    [loc["state"] or ""]).strip(", ")
    return {
        "id": i,
        "company_name": row["company_name"],
        "mca_industry": (row["industrial_classification"] or "")[:60],
        "mca_location": f"{where} {loc['pin'] or ''}".strip(),
        "domain": row["resolved_domain"],
        "landed_on": (final or "").replace("https://","").replace("http://","").rstrip("/")[:40],
        "page_title": title[:110],
        "page_snippet": snippet.replace("\n"," ")[:150],
        "CORRECT_y_n": "",
    }

with cf.ThreadPoolExecutor(max_workers=10) as pool:
    out=list(pool.map(build, enumerate(rows,1)))

path="data/exports/domain_labels.csv"
with open(path,"w",newline="",encoding="utf-8-sig") as f:
    w=csv.DictWriter(f,fieldnames=list(out[0]))
    w.writeheader(); w.writerows(out)
print(f"wrote {path} ({len(out)} rows)", file=sys.stderr)

# key file, kept separate so the sheet itself carries no machine verdict
with open("data/exports/.domain_labels_key.csv","w",newline="",encoding="utf-8") as f:
    w=csv.writer(f); w.writerow(["id","cin","domain","machine_verdict","confidence"])
    for rec,row in zip(out,rows):
        w.writerow([rec["id"],row["cin"],row["resolved_domain"],
                    "reject" if row["status"]=="resolve_failed" else "accept",
                    row["resolve_confidence"]])
