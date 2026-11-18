"""
The Android app's direct download, shown on ``/android``: the latest signed release APK and the facts to verify it.

The app repository's "Release APK" workflow builds a release APK, signs it with Unfret's release key, and attaches
``unfret-<version>.apk``, ``SHA256SUMS`` and ``release.json`` (these facts) to its Forgejo release ``v<version>``.
``python -m unfret.android_release v<version>`` (``make android-release TAG=v<version>``) reads that release,
downloads the APK, checks its size and SHA-256 against ``release.json``, and writes the facts to
``static/android-release.json``; a pull request publishes them with the next deploy.

The page links to the APK on git.kroshtan.com: the address is built here from the version, never read from a file.
A release is shown only when its facts pass :func:`parse`, signed with :data:`SIGNING_CERT_SHA256`; with no file, or a
file that doesn't pass, the page says the download is coming soon. Nothing about a visitor is involved.

A version with a pre-release suffix (``0.1.0-beta.1``, tag ``v0.1.0-beta.1``) is a beta: its ``release.json`` says
``"prerelease": true`` (the workflow publishes it as a Forgejo pre-release), and the page labels it "Beta". The flag
must agree with the version; a ``release.json`` without it is a stable release.
"""

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

RELEASE_PATH = Path(__file__).resolve().parent / "static" / "android-release.json"

REPOSITORY = "https://git.kroshtan.com/kroshtan/unfret_android"
RELEASES_API = "https://git.kroshtan.com/api/v1/repos/kroshtan/unfret_android/releases"

# The SHA-256 fingerprint of the certificate Unfret's release key signs the app with (RSA 4096, valid until 2054):
# the same for every version, so each update installs over the one before. apksigner prints it like this.
SIGNING_CERT_SHA256 = "3e71ef6b76faba49419722ae998a424b38a7e905c6a4484a37f8a96fe45f6634"

# What each permission the app's APK asks for is for, in plain words. The release workflow refuses an APK that asks
# for any other, so a new permission comes with its explanation here.
PERMISSIONS = {
    "android.permission.INTERNET": (
        "Internet",
        (
            "to open the page you share, over your own connection, and to send it to unfret.net when you tap Check "
            "this page."
        ),
    ),
    "net.unfret.android.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION": (
        "The app's own internal messages",
        (
            "a permission only Unfret itself holds, added by Android's support library so that messages within the "
            "app stay inside it. It gives access to nothing on your phone."
        ),
    ),
}
# What version 0.1.0 asks for (aapt2 dump permissions), shown while no release is published.
DEFAULT_PERMISSIONS = (
    "android.permission.INTERNET",
    "net.unfret.android.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION",
)
DEFAULT_MIN_SDK = 26

ANDROID_VERSIONS = {
    24: "7.0", 25: "7.1", 26: "8.0", 27: "8.1", 28: "9", 29: "10", 30: "11", 31: "12", 32: "12L", 33: "13",
    34: "14", 35: "15", 36: "16",
}  # fmt: skip

# major.minor.patch with an optional pre-release suffix (``-beta.1``), the same pattern the release workflow accepts.
_VERSION_RE = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(?:-[0-9A-Za-z]{1,16}(?:\.[0-9A-Za-z]{1,16}){0,3})?")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_PERMISSION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+")
MAX_APK_BYTES = 100_000_000


@dataclass(frozen=True)
class AndroidRelease:
    """A published, signed release APK of the app."""

    version: str
    version_code: int
    commit: str
    size: int
    sha256: str
    cert_sha256: str
    min_sdk: int
    permissions: tuple[str, ...]
    published: date
    prerelease: bool = False

    @property
    def tag(self) -> str:
        """
        The release's tag.

        :return: ``v<version>``.
        """
        return f"v{self.version}"

    @property
    def apk(self) -> str:
        """
        The APK's file name.

        :return: ``unfret-<version>.apk``.
        """
        return f"unfret-{self.version}.apk"

    @property
    def apk_url(self) -> str:
        """
        Where the APK is downloaded from: the release's asset on git.kroshtan.com.

        :return: The address.
        """
        return f"{REPOSITORY}/releases/download/{self.tag}/{self.apk}"

    @property
    def sums_url(self) -> str:
        """
        The release's ``SHA256SUMS``.

        :return: The address.
        """
        return f"{REPOSITORY}/releases/download/{self.tag}/SHA256SUMS"

    @property
    def release_url(self) -> str:
        """
        The release's page.

        :return: The address.
        """
        return f"{REPOSITORY}/releases/tag/{self.tag}"

    def as_json(self) -> dict[str, Any]:
        """
        The facts as ``android-release.json`` keeps them.

        :return: The JSON object.
        """
        return {
            "version": self.version,
            "version_code": self.version_code,
            "commit": self.commit,
            "size": self.size,
            "sha256": self.sha256,
            "cert_sha256": self.cert_sha256,
            "min_sdk": self.min_sdk,
            "permissions": list(self.permissions),
            "published": self.published.isoformat(),
            "prerelease": self.prerelease,
        }


def parse(data: Any) -> AndroidRelease | None:
    """
    The release described by ``data``, if it is complete and signed with Unfret's release key.

    :param data: The decoded ``release.json``.
    :return: The release, or ``None`` when anything is missing, malformed or signed with another key, or when
        ``prerelease`` doesn't match the version.
    """
    if not isinstance(data, dict):
        return None
    try:
        release = AndroidRelease(
            version=str(data["version"]),
            version_code=data["version_code"],
            commit=str(data["commit"]),
            size=data["size"],
            sha256=str(data["sha256"]),
            cert_sha256=str(data["cert_sha256"]).replace(":", "").lower(),
            min_sdk=data["min_sdk"],
            permissions=tuple(data["permissions"]),
            published=date.fromisoformat(str(data["published"])),
            prerelease=data.get("prerelease", False),
        )
    except KeyError, TypeError, ValueError:
        return None
    integers = (release.version_code, release.size, release.min_sdk)
    if not all(type(value) is int and value > 0 for value in integers):
        return None
    if (
        not _VERSION_RE.fullmatch(release.version)
        # A beta says so, and only a version with a pre-release suffix is one.
        or type(release.prerelease) is not bool
        or release.prerelease != ("-" in release.version)
        or not _COMMIT_RE.fullmatch(release.commit)
        or not _SHA256_RE.fullmatch(release.sha256)
        or release.cert_sha256 != SIGNING_CERT_SHA256
        or release.size > MAX_APK_BYTES
        or not all(isinstance(p, str) and _PERMISSION_RE.fullmatch(p) for p in release.permissions)
    ):
        return None
    return release


def load(path: Path | None = None) -> AndroidRelease | None:
    """
    The release the page offers.

    :param path: The facts file (``static/android-release.json`` by default).
    :return: The release, or ``None`` while none (valid) is published.
    """
    try:
        data = json.loads((path or RELEASE_PATH).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return parse(data)


def fingerprint(hex_digest: str) -> str:
    """
    A SHA-256 fingerprint the way keytool and most verifying apps show it.

    :param hex_digest: 64 hex characters.
    :return: Upper-case byte pairs separated by colons.
    """
    return ":".join(hex_digest[i : i + 2] for i in range(0, len(hex_digest), 2)).upper()


def android_version(min_sdk: int) -> str:
    """
    The Android version an API level stands for.

    :param min_sdk: The API level.
    :return: "Android 8.0", or "Android API level N" for a level not in :data:`ANDROID_VERSIONS`.
    """
    name = ANDROID_VERSIONS.get(min_sdk)
    return f"Android {name}" if name else f"Android API level {min_sdk}"


def megabytes(size: int) -> str:
    """
    A file size as Android's Files app shows it.

    :param size: Bytes.
    :return: "3.4 MB" (decimal megabytes).
    """
    return f"{size / 1_000_000:.1f} MB"


def fetch(tag: str, client: httpx.Client) -> AndroidRelease:
    """
    Read a published release's facts from the forge and check them against the APK itself.

    :param tag: The release tag, ``v<version>``.
    :param client: The HTTP client.
    :return: The release.
    :raises ValueError: When the release, its facts or its APK don't match.
    """
    response = client.get(f"{RELEASES_API}/tags/{tag}")
    response.raise_for_status()
    assets = {asset["name"]: asset["browser_download_url"] for asset in response.json().get("assets", [])}
    if "release.json" not in assets:
        raise ValueError(f"{tag} has no release.json: attach it with the Release APK workflow")
    release = parse(client.get(assets["release.json"]).raise_for_status().json())
    if release is None:
        raise ValueError(f"{tag}'s release.json is incomplete, malformed or not signed with Unfret's release key")
    if release.tag != tag or release.apk not in assets:
        raise ValueError(f"{tag}'s release.json describes {release.tag}, or {release.apk} isn't attached")
    apk = client.get(release.apk_url).raise_for_status().content
    if len(apk) != release.size or hashlib.sha256(apk).hexdigest() != release.sha256:
        raise ValueError(f"{release.apk_url} doesn't match release.json (size or SHA-256)")
    return release


def write(release: AndroidRelease, path: Path | None = None) -> None:
    """
    Keep a release's facts for the page.

    :param release: The release.
    :param path: The facts file (``static/android-release.json`` by default).
    """
    text = json.dumps(release.as_json(), indent=2) + "\n"
    (path or RELEASE_PATH).write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - a network command, run by hand
    """
    ``python -m unfret.android_release v0.1.0`` (or ``v0.1.0-beta.1``): put that release on ``/android``.

    Review the diff, then commit.

    :param argv: The command line.
    """
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("tag", help="the release tag, e.g. v0.1.0 or v0.1.0-beta.1")
    tag = parser.parse_args(argv).tag
    headers = {"User-Agent": "Unfret Android release importer (+https://unfret.net)"}
    with httpx.Client(timeout=120, headers=headers, follow_redirects=True) as client:
        try:
            release = fetch(tag, client)
        except (httpx.HTTPError, ValueError) as error:
            sys.exit(f"error: {error}")
    write(release)
    print(  # noqa: T201 - command-line tool
        f"{RELEASE_PATH.name}: Unfret {release.version}{' (beta)' if release.prerelease else ''}"
        f" ({megabytes(release.size)}, sha256 {release.sha256})"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
