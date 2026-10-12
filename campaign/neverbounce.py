"""
NeverBounce v4 client (single check + account info) in three modes, chosen by make_client():

  off   -> None (the stage does nothing)
  mock  -> MockClient: configurable verdicts, no network, spends nothing
  live  -> LiveClient: the real API, key from the environment

Failures are split in two so the stage never mistakes an outage for a verdict:
  TransientError   timeout, connection error, HTTP 429 / 5xx, temp_unavail, throttle_triggered,
                   an unparseable or unexpected reply. Nothing may be concluded; retry later.
  NeverBounceError permanent: no key, auth_failure, a rejected request. Retrying will not help.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from . import settings_verify

VERDICTS = ("valid", "invalid", "disposable", "catchall", "unknown")


class NeverBounceError(Exception):
    """Permanent: misconfiguration or a rejected request."""


class TransientError(Exception):
    """Temporary: nothing was learned about the address."""


class MockClient:
    """Verdicts from a table, never the network. `calls` records every address checked."""
    live = False

    def __init__(self, verdicts=None, default="valid"):
        self.verdicts = {k.lower(): v for k, v in (verdicts or {}).items()}
        self.default = default
        self.calls = []

    def check(self, email):
        self.calls.append(email)
        email = email.lower()
        domain = email.rsplit("@", 1)[-1]
        return self.verdicts.get(email) or self.verdicts.get(domain) or self.default

    def credits_remaining(self):
        return None            # unlimited: there is no account behind a mock


class LiveClient:
    """The real API. Every call costs a credit on the NeverBounce side when it succeeds."""
    live = True

    def __init__(self, api_key, base_url=None, timeout=None):
        if not api_key:
            raise NeverBounceError("NEVERBOUNCE_API_KEY is not set")
        self._key = api_key
        self.base = (base_url or settings_verify.API_BASE).rstrip("/")
        self.timeout = timeout or settings_verify.HTTP_TIMEOUT_S
        self.calls = []

    def _get(self, path, **params):
        url = f"{self.base}/{path}?" + urllib.parse.urlencode({"key": self._key, **params})
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise TransientError(f"NeverBounce HTTP {exc.code}") from None
            raise NeverBounceError(f"NeverBounce rejected the request: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise TransientError(f"NeverBounce unreachable: {exc}") from None
        except ValueError:
            raise TransientError("NeverBounce returned something that is not JSON") from None
        status = body.get("status")
        if status == "success":
            return body
        if status in ("auth_failure", "bad_referrer", "invalid_request"):
            raise NeverBounceError(f"NeverBounce refused the request: {status}")
        raise TransientError(f"NeverBounce status {status!r}")          # temp_unavail, throttle_triggered, ...

    def check(self, email):
        """One verdict from the single-check endpoint: valid / invalid / disposable / catchall / unknown."""
        self.calls.append(email)
        result = self._get("single/check", email=email).get("result")
        if result not in VERDICTS:
            raise TransientError(f"NeverBounce returned an unexpected result {result!r}")
        return result

    def credits_remaining(self):
        """Paid plus free credits left on the account."""
        info = self._get("account/info").get("credits_info") or {}
        return int(info.get("paid_credits_remaining") or 0) + int(info.get("free_credits_remaining") or 0)


def make_client(mode):
    """The client for a mode. Live without a key raises before any call is made."""
    if mode == "off":
        return None
    if mode == "mock":
        return MockClient(settings_verify.MOCK_VERDICTS, default=settings_verify.MOCK_DEFAULT)
    if mode == "live":
        key = settings_verify.api_key()
        if not key:
            raise NeverBounceError("live mode needs NEVERBOUNCE_API_KEY in the environment; it is not set")
        return LiveClient(key)
    raise ValueError(f"unknown NeverBounce mode {mode!r} (expected one of {settings_verify.MODES})")
