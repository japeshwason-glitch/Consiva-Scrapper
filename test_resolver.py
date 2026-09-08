"""
Measures the multi-axis decision rule against the labelled set produced by
today's re-verification: 37 domains confirmed WRONG (demoted) and 17 confirmed
RIGHT (accepted by the verifier at low fuzzy confidence).

Ground truth is imperfect -- the 17 positives were labelled by the same model
that adjudicates ties -- but the 37 negatives were eyeballed and are solid.
"""
import re, sys, concurrent.futures as cf
import requests, urllib3
import config, psycopg2, location, resolver

urllib3.disable_warnings()
UA={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}

def fetch(domain):
    for scheme in ("https://","http://"):
        try:
            r=requests.get(f"{scheme}{domain}",headers=UA,timeout=(5,12),
                           allow_redirects=True,verify=False)
            if r.status_code<400 and r.text: return r.text
        except requests.RequestException: continue
    return None

def page(html):
    t=re.search(r"<title[^>]*>(.*?)</title>",html,re.I|re.S)
    title=re.sub(r"\s+"," ",t.group(1)).strip() if t else ""
    b=re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>"," ",html)
    b=re.sub(r"(?s)<[^>]+>"," ",b)
    return title, re.sub(r"\s+"," ",b).strip()[:6000]

def evaluate(row, expected):
    html=fetch(row["resolved_domain"])
    if not html: return None
    title,body=page(html)
    loc=location.parse(row["registered_address"])
    terms=resolver.industry_terms(row["industrial_classification"])
    cand={"title":title,"body":body,"tier":resolver.TIER_STRONG,
          "domain":row["resolved_domain"]}
    # infer the tier the way generation would have
    words=[w for w in resolver.clean_name(row["company_name"]).split()
           if w not in resolver.STOPWORDS]
    slug=re.sub(r"\..*$","",row["resolved_domain"]).replace("-","")
    if words and slug==words[0].lower(): cand["tier"]=resolver.TIER_WEAK
    resolver.score_candidate(cand,row,loc,terms)
    verdict,reason=resolver.decide(cand,row)
    if verdict is None:
        verdict=resolver.adjudicate(cand,row,loc); reason=f"LLM tiebreak -> {verdict} | {reason}"
    return {"row":row,"cand":cand,"verdict":bool(verdict),"reason":reason,"expected":expected}

c=psycopg2.connect(config.DATABASE_URL)
cur=c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if False else c.cursor()
cur.execute("""SELECT cin,company_name,resolved_domain,registered_address,
                      industrial_classification,state_code,resolve_confidence,status
               FROM companies
               WHERE (status='resolve_failed' AND resolved_domain IS NOT NULL)
                  OR (status='scraped' AND resolve_confidence<85)""")
cols=[d[0] for d in cur.description]
rows=[dict(zip(cols,r)) for r in cur.fetchall()]
cases=[(r, r["status"]=="scraped") for r in rows]   # scraped(<85) = known good
print(f"labelled set: {sum(1 for _,e in cases if e)} positives, "
      f"{sum(1 for _,e in cases if not e)} negatives\n")

with cf.ThreadPoolExecutor(max_workers=8) as pool:
    results=[r for r in pool.map(lambda a: evaluate(*a), cases) if r]

tp=sum(1 for r in results if r["expected"] and r["verdict"])
fn=sum(1 for r in results if r["expected"] and not r["verdict"])
tn=sum(1 for r in results if not r["expected"] and not r["verdict"])
fp=sum(1 for r in results if not r["expected"] and r["verdict"])
print(f"reachable now: {len(results)} of {len(cases)}\n")
print(f"  true  positives (kept a good domain)  : {tp}")
print(f"  false negatives (lost a good domain)  : {fn}")
print(f"  true  negatives (rejected a bad one)  : {tn}")
print(f"  FALSE POSITIVES (accepted a bad one)  : {fp}   <-- the costly error")
if tp+fp: print(f"\n  precision: {tp/(tp+fp)*100:.1f}%")
if tp+fn: print(f"  recall   : {tp/(tp+fn)*100:.1f}%")

print("\n--- false positives (would waste Apollo credits) ---")
for r in results:
    if not r["expected"] and r["verdict"]:
        print(f"  {r['row']['company_name'][:34]:<36} {r['cand']['domain']:<24} {r['reason'][:70]}")
print("\n--- false negatives (good domains we would lose) ---")
for r in results:
    if r["expected"] and not r["verdict"]:
        print(f"  {r['row']['company_name'][:34]:<36} {r['cand']['domain']:<24} {r['reason'][:70]}")
