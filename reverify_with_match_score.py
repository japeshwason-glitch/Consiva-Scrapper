"""
Re-checks domains accepted before the match-score resolver existed.

Those rows were accepted on string similarity alone, which let through
marvel.com for MARVEL KING LIMITED and gaurav.com for GAURAV SPINNERS -- and
because wrong domains are usually big tracker-heavy sites, they scored HIGHEST
on consent_gap_score and sit at the top of any list sorted by severity.

Rows the scorer rejects are moved to 'resolve_failed'; their scrape findings
stay on the row for audit.
"""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, urllib3
import db, fingerprints, llm

urllib3.disable_warnings()
UA={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}
ACCEPT = 60

def fetch(domain, attempts=3):
    """
    Patient fetch. The first pass used a 4s/6s budget tuned for scanning
    thousands of hosts and left 51 rows undetermined -- small Indian SME hosts
    are slow and intermittent, not absent. Here correctness matters more than
    throughput, so each host gets several tries with a real timeout.
    """
    import time
    for attempt in range(attempts):
        for scheme in ("https://","http://"):
            try:
                r=requests.get(f"{scheme}{domain}",headers=UA,timeout=(8,20),
                               allow_redirects=True,verify=False)
                if r.text and r.text.strip(): return r.text
            except requests.RequestException: continue
        if attempt < attempts-1: time.sleep(2)
    return None

def page(html):
    m=re.search(r"<title[^>]*>(.*?)</title>",html,re.I|re.S)
    title=re.sub(r"\s+"," ",m.group(1)).strip() if m else ""
    b=re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>"," ",html)
    b=re.sub(r"(?s)<[^>]+>"," ",b)
    return title, re.sub(r"\s+"," ",b).strip()[:4000]

def check(c):
    dom=c.get("final_domain") or c.get("resolved_domain")
    html=fetch(dom)
    if not html: return None,"unreachable now — left as is"
    title,body=page(html)
    if fingerprints.is_parked(title,body,dom): return False,f"parked/placeholder: {title[:60]!r}"
    score,why=llm.match_score(c["company_name"],title,body,
                              industry=c.get("industrial_classification"))
    if score is None: return None,"verifier unavailable"
    return (score>=ACCEPT), f"score {score}: {why[:90]}"

targets=[]
for status in ("scraped","not_a_lead","linkedin_done","classified","contact_done","contact_failed"):
    targets += [c for c in db.get_pending(status, limit=2000)
                if c.get("resolved_domain") and c.get("resolve_verified_by") is None]
print(f"re-verifying {len(targets)} rows accepted before the match-score resolver\n")

kept=demoted=unknown=0
with ThreadPoolExecutor(max_workers=4) as pool:
    futures={pool.submit(check,c):c for c in targets}
    for f in as_completed(futures):
        c=futures[f]; name=c["company_name"][:38]
        dom=(c.get("final_domain") or c.get("resolved_domain"))[:26]
        try: ok,why=f.result()
        except Exception as e: ok,why=None,f"{type(e).__name__}"
        if ok is True:
            kept+=1
            db.advance_status(c["cin"], c["status"], resolve_verified_by="llm-match-score")
        elif ok is False:
            demoted+=1
            print(f"  DEMOTE  {name:<40} {dom:<28} {why[:56]}",flush=True)
            db.advance_status(c["cin"],"resolve_failed",
                              resolve_verified_by="llm-rejected",
                              last_error=f"re-verified against match_score: {why[:280]}")
        else:
            unknown+=1
            print(f"  ?       {name:<40} {dom:<28} {why[:56]}",flush=True)
print(f"\nkept {kept}, demoted {demoted}, undetermined {unknown}")
print("status counts:", db.count_by_status())
