"""
Predicts which job titles are worth hunting at a company, for leads where the
contact provider returned nobody.

Roughly 8 in 10 of our leads arrive without a name, and sales sources those
through Lusha. Handing Lusha "Chief Information Security Officer" for a
five-person MSME spends a credit on a role that does not exist there. Scale is
therefore the primary signal -- it decides which roles a company HAS -- and
industry only reorders what is already plausible.

    import personas
    personas.predict(scale="MSME", industry="Trading", cin="U51909DL...")
    -> ["Managing Director", "Director", "Proprietor"]

Titles are returned as Lusha would search them, not as internal shorthand.
"""

# Base plausibility by company scale. The numbers are a ranking device, not a
# measurement -- what matters is the order they produce.
#
# The shape of each tier is the point:
#   Enterprise has specialist compliance and security functions
#   SMB has a decision maker plus whoever actually runs IT
#   Startup has a founder who is still the technical decision maker
#   MSME has an owner, and no one else with authority
BY_SCALE = {
    "Enterprise": {
        "Chief Information Officer": 95, "Chief Technology Officer": 92,
        "Data Protection Officer": 90, "Head of Compliance": 86,
        "Chief Information Security Officer": 84, "Head of Legal": 80,
        "IT Head": 74, "Head of Marketing": 66, "Company Secretary": 64,
        "Managing Director": 60,
    },
    "SMB": {
        "Managing Director": 92, "IT Head": 88, "Director": 84,
        "Chief Technology Officer": 78, "Head of Marketing": 74,
        "Head of Operations": 68, "Company Secretary": 60,
        "Head of Compliance": 54,
    },
    "Startup": {
        "Founder": 95, "Chief Technology Officer": 90, "Co-Founder": 86,
        "Chief Executive Officer": 84, "Head of Marketing": 74,
        "Head of Growth": 68, "Head of Product": 64, "Director": 60,
    },
    "MSME": {
        "Managing Director": 92, "Director": 88, "Proprietor": 82,
        "Partner": 78, "Owner": 74, "Marketing Manager": 38,
        "IT Manager": 36, "Founder": 56,
    },
    # Mostly LLPs, where paid-up capital is not filed and scale is unknowable
    # from the registry. Their statutory roles are partners, not directors.
    "Unknown": {
        "Designated Partner": 92, "Partner": 88, "Managing Director": 76,
        "Director": 72, "Founder": 68, "Proprietor": 60,
    },
}

# Industry reorders what scale has already deemed plausible. Mirrors the
# persona weighting the contact stage uses to RANK real titles, so the titles
# we hunt and the titles we prefer when found agree with each other.
BY_INDUSTRY = {
    "Finance": {"Chief Information Officer": 25, "Head of Compliance": 25,
                "Company Secretary": 20, "Data Protection Officer": 18,
                "Chief Information Security Officer": 18, "Head of Legal": 15,
                "Founder": -15, "Chief Executive Officer": -15},
    "Insurance": {"Head of Compliance": 25, "Chief Information Officer": 22,
                  "Data Protection Officer": 20, "Head of Legal": 15,
                  "Founder": -15, "Chief Executive Officer": -15},
    "Business Services": {"Chief Technology Officer": 22, "IT Head": 14,
                          "Head of Product": 10, "Founder": -6},
    "Transport, storage and Communications": {
        "Chief Information Officer": 14, "IT Head": 12,
        "Head of Operations": 10},
    "Community, personal and Social Services": {
        "Head of Operations": 12, "Head of Marketing": 8},
    "Manufacturing": {"Managing Director": 12, "Director": 10,
                      "Head of Operations": 8, "IT Head": 6,
                      "Chief Technology Officer": -8},
    "Real Estate": {"Managing Director": 12, "Director": 10,
                    "Head of Marketing": 8, "Chief Technology Officer": -10},
    "Construction": {"Managing Director": 12, "Director": 10,
                     "Head of Operations": 8, "Chief Technology Officer": -10},
    "Trading": {"Proprietor": 12, "Partner": 10, "Managing Director": 8,
                "Owner": 8, "Chief Technology Officer": -15, "IT Head": -10},
}

# Titles that mean the same person. Returning two of these as separate targets
# wastes one of the three slots.
SYNONYMS = (
    ("Founder", "Co-Founder", "Chief Executive Officer", "Proprietor", "Owner"),
    ("Partner", "Designated Partner"),
    ("Chief Information Officer", "IT Head", "IT Manager"),
    ("Head of Compliance", "Head of Legal", "Company Secretary"),
    ("Chief Technology Officer",),
)

# Functional roles that exist at almost any company of any shape. Used only to
# top up when the entity gate has left fewer than three distinct groups -- an
# LLP in a sector with no specialist roles otherwise returns two.
FALLBACK = ("Head of Operations", "Marketing Manager", "IT Manager",
            "Head of Marketing", "General Manager", "Business Head")

# Industry arrives in two vocabularies: MCA's NIC classification strings
# ("Business Services", "Manufacturing (Textiles)") and LinkedIn's own
# ("Financial Services", "Information Technology & Services"). Both have to
# reach the same rules, or a BFSI company gets generic treatment purely
# because of which source described it. Ordered: first match wins, and
# "services" alone is never a signal.
INDUSTRY_ALIASES = (
    (("insur",), "Insurance"),
    (("financ", "banking", "bank", "bfsi", "nbfc", "capital market",
      "investment", "lending"), "Finance"),
    (("business services", "software", "information technology",
      "it services", "saas", "computer", "internet", "technology &"),
     "Business Services"),
    (("trading", "wholesale", "retail", "import", "export", "distribut"),
     "Trading"),
    (("manufactur", "industr", "engineering", "chemical", "textile",
      "plastic", "metal", "machinery", "pharma", "food stuff"), "Manufacturing"),
    (("real estate", "renting", "property", "infrastructure"), "Real Estate"),
    (("construction", "builder", "contracting"), "Construction"),
    (("transport", "logistics", "communication", "shipping", "warehous"),
     "Transport, storage and Communications"),
    (("community", "social service", "personal service", "hospitality",
      "education", "healthcare", "hospital"),
     "Community, personal and Social Services"),
)


def normalise_industry(industry):
    """An incoming industry string mapped onto a rule key, or None."""
    if not industry:
        return None
    low = str(industry).lower()
    for needles, canonical in INDUSTRY_ALIASES:
        if any(n in low for n in needles):
            return canonical
    return None


SLOTS = 3


def is_llp(cin):
    """LLPs file an LLPIN like AAE-5648, not a 21-character CIN."""
    return bool(cin) and "-" in str(cin)


def predict(scale=None, industry=None, cin=None, slots=SLOTS):
    """
    The job titles worth hunting at this company, best first.

    scale is the primary signal, industry reorders it, and an LLP gets its
    statutory partner roles rather than directorships.
    """
    base = dict(BY_SCALE.get(scale or "", BY_SCALE["Unknown"]))

    for title, adjust in (BY_INDUSTRY.get(normalise_industry(industry)) or {}).items():
        if title in base:
            base[title] += adjust
        elif adjust > 0:
            # Industry can introduce a title the scale tier omitted, but only
            # as a weak candidate -- scale still decides what is plausible.
            base[title] = 50 + adjust

    # Entity type is a hard gate, not a nudge. A Private Limited company has
    # Directors and no Partners; an LLP has Partners and no Directors. Asking
    # Lusha for a Proprietor at a Pvt Ltd spends a credit on a role that
    # legally cannot exist there.
    PARTNER_ROLES = ("Designated Partner", "Partner", "Proprietor", "Owner")
    DIRECTOR_ROLES = ("Managing Director", "Director")
    if is_llp(cin):
        for title in ("Designated Partner", "Partner"):
            base[title] = base.get(title, 70) + 25
        for title in DIRECTOR_ROLES + ("Proprietor", "Owner"):
            base.pop(title, None)   # sole-trader terms; an LLP has partners
    elif cin:
        for title in PARTNER_ROLES:
            base.pop(title, None)

    ranked = sorted(base.items(), key=lambda kv: (-kv[1], kv[0]))

    chosen, used_groups = [], []

    def take(title):
        group = next((g for g in SYNONYMS if title in g), (title,))
        if group in used_groups or title in chosen:
            return False
        chosen.append(title)
        used_groups.append(group)
        return True

    for title, _score in ranked:
        if take(title) and len(chosen) == slots:
            return chosen
    for title in FALLBACK:
        if take(title) and len(chosen) == slots:
            break
    return chosen
