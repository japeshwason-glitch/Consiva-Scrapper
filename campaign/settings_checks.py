"""Config for stages 2 (free email checks) and 3 (domain). Overridable by AE_* environment variables."""
from pathlib import Path

from .settings import FREE_MAIL_DOMAINS, _env  # noqa: F401  (FREE_MAIL_DOMAINS re-exported for the checks)

# Local parts that name a function, not a person.
ROLE_LOCAL_PARTS = frozenset(p.strip() for p in _env(
    "AE_ROLE_LOCAL_PARTS",
    "info,contact,contactus,support,sales,hr,admin,administrator,office,hello,enquiry,enquiries,"
    "inquiry,careers,career,jobs,accounts,billing,finance,marketing,mail,webmaster,postmaster,"
    "noreply,no-reply,donotreply,help,team,service,services,feedback,press,media,reception,"
    "customercare,care,orders,abuse,security,legal,compliance,privacy").split(",") if p.strip())

DISPOSABLE_FILE = Path(_env("AE_DISPOSABLE_FILE", str(Path(__file__).parent / "data" / "disposable_domains.txt")))

MX_TIMEOUT_SECONDS = _env("AE_MX_TIMEOUT_SECONDS", 5, float)    # per lookup, wall clock
MX_CACHE_DAYS = _env("AE_MX_CACHE_DAYS", 14, int)               # a cached answer older than this is asked again

# Stage 3: with no Website, a domain is taken from the passing business contacts' emails when
# one domain covers at least this share of them.
DOMAIN_MAJORITY_SHARE = _env("AE_DOMAIN_MAJORITY_SHARE", 0.6, float)
