"""
Report PDFs in Cloudflare R2 (S3-compatible API, boto3).

The bucket is connected to the custom domain REPORT_PUBLIC_BASE_URL, and the
URL path equals the object key, so a report's public URL is simply
    REPORT_PUBLIC_BASE_URL + "/" + key
Keys are random (r/<32 hex>.pdf): a report URL cannot be guessed from a
company name or enumerated from another report's URL.
"""
import re
import secrets
from functools import lru_cache

import boto3
from botocore.config import Config

import config


@lru_cache(maxsize=1)
def client():
    missing = [k for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                           "R2_REPORTS_BUCKET") if not getattr(config, k)]
    if missing:
        raise RuntimeError(f"missing in .env.docker: {', '.join(missing)}")
    return boto3.client(
        "s3", endpoint_url=config.R2_ENDPOINT, region_name="auto",
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        config=Config(retries={"max_attempts": 3, "mode": "standard"},
                      connect_timeout=15, read_timeout=60))


def public_url(key):
    return f"{config.REPORT_PUBLIC_BASE_URL}/{key}"


def _safe_filename_part(domain):
    return re.sub(r"[^A-Za-z0-9.-]", "-", domain or "site")[:80]


def upload_report(pdf_bytes, domain):
    """Uploads one report PDF; returns (object_key, public_url)."""
    key = f"r/{secrets.token_hex(16)}.pdf"
    client().put_object(
        Bucket=config.R2_REPORTS_BUCKET, Key=key, Body=pdf_bytes,
        ContentType="application/pdf",
        ContentDisposition=(f'inline; filename="Consiva-DPDP-Readiness-'
                            f'{_safe_filename_part(domain)}.pdf"'))
    return key, public_url(key)


def put_text(key, text):
    client().put_object(Bucket=config.R2_REPORTS_BUCKET, Key=key,
                        Body=text.encode(), ContentType="text/plain")


def delete(key):
    client().delete_object(Bucket=config.R2_REPORTS_BUCKET, Key=key)
