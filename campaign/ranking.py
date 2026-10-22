"""
Stage 7: contact ranking. reported -> contact_ranked | held_no_contact (or left at `reported`).

For each `reported` company: take the contacts that passed the stage 2 free checks, classify every
job title (rules first, then the model, cached by normalised title), rank the contacts, store rank and
rank_reason on every contact, and decide the company. Ranking is code, not the model:

    sort by  relevance (high first)  ->  position on the company-size priority list (settings_titles)
             ->  seniority (high first)  ->  contact order

An unclassified title (the model answered badly twice) ranks after every classified one; a contact with
no title ranks after that. The company is held_no_contact when its best contact is below RELEVANCE_MIN, or
is unclassified or untitled.

If the model server cannot be reached, a company that needs it is left at `reported` with nothing written
for it, and tried again next run; that is never treated as "no contact". After the first failure in a run
no further calls are made, so a down server costs one timeout, not one per company.
"""
import psycopg2.extras

from . import settings, settings_titles as st, status, title_llm, titles


class Deferred(Exception):
    """This company needs the model and the model is not available; leave it at `reported`."""


_TALLY_KEY = {"rule": "titles_rule", "llm": "titles_llm", "unclassified": "titles_unclassified",
              "none": "titles_none"}
UNCLASSIFIED_RELEVANCE, NO_TITLE_RELEVANCE = -1, -2     # sort values: below every real relevance


def _cached(cur, norm, provider, model):
    """A model answer is reused only when provider, model AND prompt_version all match."""
    cur.execute("""SELECT function, seniority, relevance FROM ae_title_cache
                   WHERE normalized_title = %s AND source = 'llm' AND provider = %s AND model = %s
                     AND prompt_version = %s""", (norm, provider, model, st.PROMPT_VERSION))
    row = cur.fetchone()
    if row is None:
        return None
    # ae_title_cache stores seniority and relevance as text (migration 008); they are ints here.
    return {"function": row["function"], "seniority": int(row["seniority"]), "relevance": int(row["relevance"])}


def _cache_put(cur, norm, function, seniority, relevance, source, version, provider=None, model=None):
    """
    Upserts one cache row. Each (title, source, provider, model) has its own row (migration 010's unique
    index, so the conflict target is that expression index); rule rows are provider-less.
    """
    cur.execute("""INSERT INTO ae_title_cache (normalized_title, function, seniority, relevance, source,
                                               prompt_version, provider, model)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (normalized_title, source, COALESCE(provider, ''), COALESCE(model, ''))
                   DO UPDATE SET function = EXCLUDED.function, seniority = EXCLUDED.seniority,
                       relevance = EXCLUDED.relevance, prompt_version = EXCLUDED.prompt_version""",
                (norm, function, str(seniority), str(relevance), source, version, provider, model))


class _Run:
    """Per-run state: counters and whether the model has already failed."""

    def __init__(self, dry_run, classify):
        self.dry_run = dry_run
        self.classify = classify or title_llm.classify_title
        self.llm_down = False
        self.no_api_key = False
        self.provider, self.model = st.active_provider()      # also rejects an unknown provider, loudly
        self.counts = {"provider": self.provider, "model": self.model, "companies_claimed": 0, "contact_ranked": 0, "held_no_contact": 0,
                       "deferred_llm_unavailable": 0, "deferred_no_api_key": 0,
                       "companies_pending_llm": 0, "titles_rule": 0, "titles_llm": 0,
                       "titles_unclassified": 0, "titles_none": 0, "llm_calls": 0, "cache_hits": 0,
                       "would_need_llm": []}

    def classify_title(self, cur, title):
        """(source, function, seniority, relevance, rule_label) for one raw title."""
        norm = titles.normalize_title(title)
        if not norm:
            return ("none", None, None, None, None)
        rule = titles.classify_by_rules(title)
        if rule:
            if not self.dry_run:
                _cache_put(cur, norm, rule["function"], rule["seniority"], rule["relevance"],
                           "rule", st.RULES_VERSION)
            return ("rule", rule["function"], rule["seniority"], rule["relevance"], rule["rule"])
        hit = _cached(cur, norm, self.provider, self.model)
        if hit:
            self.counts["cache_hits"] += 1
            return ("llm", hit["function"], hit["seniority"], hit["relevance"], None)
        if self.dry_run:
            if title.strip() not in self.counts["would_need_llm"]:
                self.counts["would_need_llm"].append(title.strip())
            return ("pending_llm", None, None, None, None)
        if self.llm_down:
            raise Deferred("the model server is down")
        try:
            out = self.classify(title)
            self.counts["llm_calls"] += 1
        except title_llm.ProviderNotConfigured:      # no key: nothing was sent, so no call is counted
            self.llm_down = self.no_api_key = True
            raise Deferred("the provider has no API key")
        except title_llm.ServerUnavailable:
            self.counts["llm_calls"] += 1
            self.llm_down = True
            raise Deferred("the model server is unavailable")
        if out is None:
            return ("unclassified", None, None, None, None)
        _cache_put(cur, norm, out["f"], out["s"], out["r"], "llm", st.PROMPT_VERSION,
                   self.provider, self.model)
        return ("llm", out["f"], out["s"], out["r"], None)


def _sort_key(entry, size):
    src = entry["source"]
    if src == "none":
        return (-NO_TITLE_RELEVANCE, 0, 0, entry["order"])
    if src == "unclassified":
        return (-UNCLASSIFIED_RELEVANCE, 0, 0, entry["order"])
    priority = st.PRIORITY_LISTS[size]
    pos = priority.index(entry["function"]) if entry["function"] in priority else len(priority)
    return (-entry["relevance"], pos, -entry["seniority"], entry["order"])


def _reason(entry, size, scale):
    src = entry["source"]
    if src == "none":
        return "no job title; ranked after every contact that has one"
    if src == "unclassified":
        return ("unclassified title: neither the rules nor the model could place it; "
                "ranked after every classified contact")
    priority = st.PRIORITY_LISTS[size]
    where = (f"priority #{priority.index(entry['function']) + 1} on the {size} list"
             if entry["function"] in priority else f"not on the {size} priority list")
    via = f"rule: {entry['rule']}" if src == "rule" else f"llm ({st.active_provider()[0]})"
    return (f"{entry['function']}, seniority {entry['seniority']}, relevance {entry['relevance']} "
            f"via {via}; {size} company ({scale or 'size unknown'}), {where}")


def rank_contacts(entries, size):
    """entries: dicts with source, function, seniority, relevance, order. Returns them best first."""
    return sorted(entries, key=lambda e: _sort_key(e, size))


def _process(run, cur, company):
    cid, scale = company["company_id"], company["company_scale"]
    size = st.size_class(scale)
    cur.execute("SELECT * FROM ae_contacts WHERE company_id = %s ORDER BY id", (cid,))
    contacts = [dict(r) for r in cur.fetchall()]
    passing = [c for c in contacts if c["check_passed"] is True]
    failing = [c for c in contacts if c["check_passed"] is not True]

    entries, tally = [], {"rule": 0, "llm": 0, "unclassified": 0, "none": 0, "pending_llm": 0}
    for order, c in enumerate(passing):
        source, function, seniority, relevance, rule = run.classify_title(cur, c["job_title"])
        tally[source] += 1
        entries.append({"contact": c, "source": source, "function": function, "seniority": seniority,
                        "relevance": relevance, "rule": rule, "order": order})
    if tally["pending_llm"]:        # dry run only: the outcome depends on a model call we are not making
        run.counts["companies_pending_llm"] += 1
        for src in ("rule", "llm", "unclassified", "none"):
            run.counts[_TALLY_KEY[src]] += tally[src]
        return
    ranked = rank_contacts(entries, size)

    if not ranked:
        new_status, reason = "held_no_contact", "no contact passed the free checks"
    else:
        best = ranked[0]
        if best["source"] == "none":
            new_status, reason = "held_no_contact", "no job title on any usable contact"
        elif best["source"] == "unclassified":
            new_status, reason = "held_no_contact", "only unclassified titles: no contact could be placed"
        elif best["relevance"] < st.RELEVANCE_MIN:
            new_status = "held_no_contact"
            reason = (f"best contact has relevance {best['relevance']} "
                      f"({best['function']}, seniority {best['seniority']}), below the minimum {st.RELEVANCE_MIN}")
        else:
            new_status = "contact_ranked"
            reason = (f"best contact: {best['contact']['job_title']!r} ({best['function']}, seniority "
                      f"{best['seniority']}, relevance {best['relevance']}, via {best['source']}); {size} company")
    if (new_status == "held_no_contact" and ranked and cid >= settings.LEAD_ID_BASE and settings.LEAD_SELF_CONTACT):
        new_status = "contact_ranked"
        reason = (f"lead: the person who contacted us is the contact, whatever the title "
                  f"({ranked[0]['contact']['job_title']!r})")
    run.counts[new_status] += 1
    for src in ("rule", "llm", "unclassified", "none"):
        run.counts[_TALLY_KEY[src]] += tally[src]
    if run.dry_run:
        return
    for rank, e in enumerate(ranked, start=1):
        cur.execute("""UPDATE ae_contacts SET rank = %s, rank_reason = %s, title_function = %s,
                           title_seniority = %s, title_relevance = %s, title_source = %s WHERE id = %s""",
                    (rank, _reason(e, size, scale), e["function"], e["seniority"], e["relevance"],
                     e["source"], e["contact"]["id"]))
    for c in failing:
        cur.execute("UPDATE ae_contacts SET rank = NULL, rank_reason = %s WHERE id = %s",
                    (f"not ranked: failed the free checks ({c['check_reason'] or 'not checked'})", c["id"]))
    status.set_status(cur, cid, new_status, reason)


def run(limit=None, dry_run=False, only_test=None, classify=None):
    """One pass over `reported` companies. Returns the counts dict (see _Run.counts)."""
    from . import express
    if express.active():                  # a held-out express batch: the CRM's primary contact, no ranking
        return express.rank(limit=limit, dry_run=dry_run, only_test=only_test)
    state = _Run(dry_run, classify)
    with status.claim("reported", limit, is_test=only_test) as (cur, companies):
        for company in companies:
            state.counts["companies_claimed"] += 1
            try:
                _process(state, cur, company)
            except Deferred:
                # Nothing was written for this company and no outcome was counted for it.
                if not dry_run:
                    state.counts["deferred_no_api_key" if state.no_api_key else "deferred_llm_unavailable"] += 1
    return state.counts
