FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# msitools (msiinfo/msiextract) + wixl let central build a real Windows .msi for
# the agent on demand (central/msi_builder.py). Installed in its own early layer
# so it's cached across app-code changes.
# curl is here for the api container's /readyz healthcheck (compose), not for
# application code -- nothing in central shells out to it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends wixl msitools curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies in a layer of their own, so an app-code change doesn't re-resolve
# the whole stack -- `deploy/install.sh --update` runs `docker compose build`
# on-prem, sometimes over a slow link.
#
# The list is DERIVED from pyproject.toml and is never written out here. What
# used to occupy this position was a hand-maintained `||` fallback, three
# packages behind [project.dependencies]: authlib, cryptography and tzdata.
#
# Two things made that worse than it reads. First, the fallback was not a
# fallback: `pip install ".[postgres]"` with only pyproject.toml copied has
# never once succeeded -- [tool.setuptools] packages names central/ and
# printer_nanny_agent/, and setuptools exits with `error: package directory
# 'central' does not exist` before resolving anything -- so the drifted list was
# the only path that ever ran. Second, what covered for it was the `pip install
# -e .` below, which did resolve [project.dependencies] and pull the three in --
# and it was written `|| true`. So the image was correct only for as long as
# that step kept succeeding, and its failure was explicitly discarded: an index
# outage or a yanked release produced a green build with no SSO (authlib) and no
# credential decryption at all (cryptography). Hence a projection of pyproject
# rather than a second copy of it.
#
# tzdata is the quiet one and deserves an honest note: python:3.12-slim does
# currently carry Debian's tzdata (443 zones, measured), so zoneinfo resolves
# without the wheel today. But no installed package depends on it, so an
# upstream slimming pass drops it with no signal here -- and central.suppression
# degrades an unresolvable zone to UTC rather than raising, which would move
# every operator's quiet-hours window by hours in silence. Declared, so pinned.
COPY pyproject.toml /build/pyproject.toml
COPY scripts/pyproject_requirements.py /build/pyproject_requirements.py
RUN python /build/pyproject_requirements.py /build/pyproject.toml postgres > /build/requirements.txt \
    && pip install --no-cache-dir -r /build/requirements.txt \
    && rm -rf /build

COPY . .

# Editable on purpose. central/ carries its Jinja templates and its vendored
# static assets as ordinary files and nothing declares them as package-data, so
# a non-editable install would put a `central` in site-packages with no
# templates and no /static -- an image that starts and then 500s on every page.
#
# And deliberately not `|| true`. This step is what proves the declared
# dependency set is actually satisfied against pyproject itself, so swallowing
# its failure would hand back exactly the green-but-broken image the layout
# above exists to prevent. A dependency that cannot be installed must stop the
# build here, where someone is watching, rather than at an operator's SSO login.
RUN pip install --no-cache-dir -e ".[postgres]"

EXPOSE 8000
CMD ["uvicorn", "central.main:app", "--host", "0.0.0.0", "--port", "8000"]
