"""
Unfret Dev on ``dev.unfret.net/android``: the Android app's test build of main, never on production.

The app repository's CI builds Unfret Dev on every push to main (``net.unfret.android.dev``, "Unfret Dev", talking to
dev.unfret.net only), signs it with the dev key (never the release key) and publishes it as the forge's rolling
pre-release ``dev``, with ``dev.json``: the build's version, CI run, commit, size, SHA-256 and signing certificate.
With ``UNFRET_ANDROID_DEV_BUILDS`` on (dev only, ``config/deploy.dev.yml``), ``/android`` reads that file (cached for a
few minutes) and offers the build below the beta. A file that doesn't pass :func:`parse` (another package, another
key, a malformed field) shows nothing, and so does a forge that can't be reached. Production's setting is off, and
then nothing is fetched or shown.
"""

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from unfret.android_release import MAX_APK_BYTES, REPOSITORY, SIGNING_CERT_SHA256
from unfret.config import settings

FACTS_URL = f"{REPOSITORY}/releases/download/dev/dev.json"
PACKAGE = "net.unfret.android.dev"
# The SHA-256 fingerprint of the dev key's certificate (the app repository's docs/dev-cert.sha256). Never the release
# key's: a build signed with that is not shown here.
DEV_CERT_SHA256 = "a8e505ef95f9e56f8e14a180eff7f549684e56a22b34edccd18ffb06b4d1beda"
CACHE_S = 300.0

_VERSION_RE = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(?:-[0-9A-Za-z.]{1,40})?-dev\.[0-9]{1,9}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_BUILT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

_cache: dict[str, tuple[float, DevBuild | None]] = {}


@dataclass(frozen=True)
class DevBuild:
    """The latest Unfret Dev build."""

    version: str
    run: int
    commit: str
    size: int
    sha256: str
    built: str

    @property
    def apk(self) -> str:
        """
        The APK's file name.

        :return: ``unfret-dev-<version>.apk``.
        """
        return f"unfret-dev-{self.version}.apk"

    @property
    def apk_url(self) -> str:
        """
        Where the APK is downloaded from: the rolling ``dev`` pre-release on git.kroshtan.com.

        :return: The address.
        """
        return f"{REPOSITORY}/releases/download/dev/{self.apk}"

    @property
    def release_url(self) -> str:
        """
        The rolling ``dev`` pre-release.

        :return: The address.
        """
        return f"{REPOSITORY}/releases/tag/dev"

    @property
    def sums_url(self) -> str:
        """
        The dev pre-release's SHA256SUMS.

        :return: The address.
        """
        return f"{REPOSITORY}/releases/download/dev/SHA256SUMS"

    @property
    def commit_url(self) -> str:
        """
        The commit it was built from.

        :return: The address.
        """
        return f"{REPOSITORY}/commit/{self.commit}"


def parse(data: Any) -> DevBuild | None:
    """
    Unfret Dev's facts, when they check out.

    :param data: ``dev.json``, decoded.
    :return: The build, or ``None`` when anything is missing, malformed, another package, or signed with another key
        (the release key included).
    """
    if not isinstance(data, dict):
        return None
    version, commit, digest, cert = (
        data.get("version"),
        data.get("commit"),
        data.get("sha256"),
        data.get("cert_sha256"),
    )
    run, size, built = data.get("run"), data.get("size"), data.get("built")
    if (
        data.get("package") != PACKAGE
        or not isinstance(version, str)
        or not _VERSION_RE.fullmatch(version)
        or data.get("apk") != f"unfret-dev-{version}.apk"
        or not isinstance(commit, str)
        or not _COMMIT_RE.fullmatch(commit)
        or not isinstance(digest, str)
        or not _SHA256_RE.fullmatch(digest)
        or cert != DEV_CERT_SHA256
        or cert == SIGNING_CERT_SHA256
        or not isinstance(run, int)
        or isinstance(run, bool)
        or run < 1
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= MAX_APK_BYTES
        or not isinstance(built, str)
        or not _BUILT_RE.fullmatch(built)
    ):
        return None
    return DevBuild(version=version, run=run, commit=commit, size=size, sha256=digest, built=built)


async def latest(client: httpx.AsyncClient | None = None) -> DevBuild | None:
    """
    The latest Unfret Dev build, on dev only.

    :param client: The HTTP client (a test's); a short-lived one otherwise.
    :return: The build, or ``None`` when dev builds are off (production), none is published, or the forge can't be
        reached. Cached for :data:`CACHE_S` seconds, failures included.
    """
    if not settings.android_dev_builds:
        return None
    now = time.monotonic()
    cached = _cache.get(FACTS_URL)
    if cached is not None and now - cached[0] < CACHE_S:
        return cached[1]
    build: DevBuild | None = None
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as own:
                response = await own.get(FACTS_URL)
        else:
            response = await client.get(FACTS_URL)
        if response.status_code == 200:
            build = parse(response.json())
    except httpx.HTTPError, ValueError:
        build = None
    _cache[FACTS_URL] = (now, build)
    return build


def forget() -> None:
    """Drop the cached build (tests)."""
    _cache.clear()
