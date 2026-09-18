# Playwright's own image: it ships Chromium plus every system library the
# browser needs. Building on python:slim means chasing ~40 apt packages by hand.
# noble, not jammy: jammy ships Python 3.10 and several pinned dependencies
# (rapidfuzz 3.14.6, pandas 3.x) require >=3.11. noble gives 3.12, matching the
# development machine.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
# tzdata, or the TZ environment variable has no zone database to resolve
# against and the container silently stays on UTC.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The browser is already in the base image; this just confirms the binary the
# pinned playwright version expects is present.
RUN python -c "from playwright.sync_api import sync_playwright; \
    p=sync_playwright().start(); print('chromium:', p.chromium.executable_path); p.stop()"

ENV PYTHONUNBUFFERED=1
EXPOSE 8012

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8012"]
