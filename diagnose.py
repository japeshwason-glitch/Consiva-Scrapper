"""
One-shot health check for a pipeline that is running but producing nothing.

    docker compose exec scheduler python diagnose.py

Read-only. Touches no data, starts no long job. Written after a live incident
where stage 02 attempted 396 companies a cycle for two days and wrote nothing:
every counter said zero, and the scheduler only prints a stage's last three
lines, so the per-company verdicts that would have explained it were never
visible.
"""
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "stages"))
from importlib import import_module

import config
import db
import fingerprints
import llm

_s2 = import_module("02_resolve_domain")


def rule(title):
    print(f"\n=== {title} " + "=" * max(0, 58 - len(title)))


def main():
    rule("queue")
    counts = db.count_by_status()
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<18} {v:>9,}")

    rule("model")
    t = time.time()
    score, reason = llm.match_score("TATA MOTORS LIMITED", "Tata Motors",
                                    "Indian automotive manufacturer.")
    print(f"  {config.LLM_BASE_URL}")
    print(f"  {time.time()-t:.1f}s  score={score}  {str(reason)[:60]}")

    rule("DNS: does a nonexistent name still return NXDOMAIN?")
    # If a junk hostname resolves, every candidate we invent looks real, every
    # company gets a fetched page, and nothing ever reaches a clean verdict.
    junk = ["zz-no-such-company-xyz.com", "asdkjhasd12345.co.in",
            "qqq-not-a-real-domain.in"]
    resolved_junk = 0
    for h in junk:
        try:
            ip = socket.getaddrinfo(h, None)[0][4][0]
            resolved_junk += 1
            print(f"  {h:<30} RESOLVED to {ip}   <-- wildcard DNS")
        except socket.gaierror as e:
            print(f"  {h:<30} {e.strerror}")
    if resolved_junk:
        print("  !! junk names are resolving. Every invented candidate will be")
        print("     fetched, so no company can ever be judged 'no site found'.")

    rule("fetch: a known-good site")
    for h in ("tatamotors.com", "haryanaplast.com"):
        got = _s2.fetch(h)
        if not got:
            print(f"  {h:<22} NO RESPONSE")
            continue
        url, title, body, status = (list(got) + [200])[:4]
        print(f"  {h:<22} HTTP {status} blocked={fingerprints.is_blocked(title, body, status)}"
              f"  {title[:40]!r}")

    rule("the real thing: resolve 3 pending companies, verdict by verdict")
    where = "industrial_classification NOT LIKE 'Manufacturing%'"
    rows = db.get_pending("discovered", limit=3, extra_where=where)
    if not rows:
        print("  nothing pending -- the queue filter matches no rows")
        return
    import location
    for c in rows:
        t = time.time()
        winner, reason, examined = _s2.resolver.resolve(
            c, _s2.fetch, loc=location.parse(c.get("registered_address")))
        verdict = ("RESOLVED " + winner["domain"]) if winner else reason[:64]
        print(f"\n  {c['company_name'][:44]}")
        print(f"    {time.time()-t:5.1f}s  {verdict}")
        print(f"    candidates examined: {len(examined)}")
        for e in examined[:4]:
            tag = "BLOCKED" if e.get("blocked") else f"score {e.get('score')}"
            print(f"      {e['domain']:<34} {tag}  {str(e.get('reason'))[:44]}")


if __name__ == "__main__":
    main()
