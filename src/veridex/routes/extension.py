"""
The self-hosted channel for the browser extension.

``/extension`` is the install page, ``/extension/updates.xml`` the update manifest Chromium polls, and
``/extension/<file>`` the release files themselves.

Releases are signed CRX packages built by the extension repository (``make release``) and copied into
``veridex/static/extension``, so they ship with the image. Chromium installs them through a browser policy (or the
ungoogled-chromium flag) and keeps them updated from ``updates.xml``; no store is involved.
"""

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

from veridex.routes.pages import PAGE_HEADERS, STATIC_DIR

RELEASE_DIR = STATIC_DIR / "extension"
PUBLIC_BASE = "https://veridex.kroshtan.com/extension"
_GUPDATE_NS = "{http://www.google.com/update2/response}"

# The only files served, by exact name pattern: no paths, so nothing outside RELEASE_DIR can be reached.
_FILES: dict[str, tuple[re.Pattern[str], str, str]] = {
    "crx": (
        re.compile(r"veridex-\d+(\.\d+){1,3}\.crx"),
        "application/x-chrome-extension",
        "public, max-age=31536000, immutable",  # versioned names never change
    ),
    "zip": (
        re.compile(r"veridex-chrome-\d+(\.\d+){1,3}\.zip"),
        "application/zip",
        "public, max-age=31536000, immutable",
    ),
    "sums": (re.compile(r"SHA256SUMS"), "text/plain; charset=utf-8", "public, max-age=300"),
}

router = APIRouter(include_in_schema=False)


@dataclass(frozen=True)
class Release:
    """The release currently published in ``RELEASE_DIR``."""

    extension_id: str
    version: str
    crx: str
    zip: str | None
    checksums: str


def current_release(release_dir: Path | None = None) -> Release | None:
    """
    Read the published release from its update manifest and checksums.

    :param release_dir: Where releases live (``RELEASE_DIR`` by default).
    :return: The release, or ``None`` when nothing (valid) is published.
    """
    root = release_dir or RELEASE_DIR
    try:
        # Our own release manifest, written by the release script: not untrusted input.
        app = ET.parse(root / "updates.xml").getroot().find(f"{_GUPDATE_NS}app")  # noqa: S314
        check = app.find(f"{_GUPDATE_NS}updatecheck") if app is not None else None
        checksums = (root / "SHA256SUMS").read_text(encoding="utf-8")
    except OSError, ET.ParseError:
        return None
    if app is None or check is None:
        return None
    crx = check.get("codebase", "").rsplit("/", 1)[-1]
    version = check.get("version", "")
    if not _FILES["crx"][0].fullmatch(crx) or not (root / crx).is_file():
        return None
    zip_name = f"veridex-chrome-{version}.zip"
    return Release(
        extension_id=app.get("appid", ""),
        version=version,
        crx=crx,
        zip=zip_name if (root / zip_name).is_file() else None,
        checksums=checksums,
    )


def linux_policy_command(extension_id: str, policy_dir: str = "/etc/brave/policies/managed") -> str:
    """
    The one-line command that force-installs the extension in a Chromium browser on Linux.

    :param extension_id: The extension ID.
    :param policy_dir: The browser's managed-policy directory.
    :return: A shell command.
    """
    policy = f'{{"ExtensionInstallForcelist":["{extension_id};{PUBLIC_BASE}/updates.xml"]}}'
    return f"sudo mkdir -p {policy_dir} && echo '{policy}' | sudo tee {policy_dir}/veridex.json > /dev/null"


@router.get("/extension", response_class=HTMLResponse)
def install_page() -> HTMLResponse:
    """
    How to install the extension from this server, filled in for the current release.

    :return: The page.
    """
    template = (STATIC_DIR / "extension.html").read_text(encoding="utf-8")
    release = current_release()
    if release is None:
        values = {"release": "<p>No release is published yet.</p>"}
    else:
        downloads = f'<li><a href="/extension/{release.crx}">{release.crx}</a> (signed package)</li>'
        if release.zip:
            downloads += f'<li><a href="/extension/{release.zip}">{release.zip}</a> (for developer mode)</li>'
        values = {
            "release": (
                f"<p>Version <strong>{html.escape(release.version)}</strong>, extension ID "
                f"<code>{html.escape(release.extension_id)}</code>.</p><ul>{downloads}</ul>"
                f"<details><summary>SHA-256 checksums</summary><pre>{html.escape(release.checksums)}</pre></details>"
            )
        }
        values["brave"] = html.escape(linux_policy_command(release.extension_id))
        values["chromium"] = html.escape(linux_policy_command(release.extension_id, "/etc/chromium/policies/managed"))
        values["chrome"] = html.escape(linux_policy_command(release.extension_id, "/etc/opt/chrome/policies/managed"))
        values["crx_url"] = html.escape(f"{PUBLIC_BASE}/{release.crx}")
    for key in ("brave", "chromium", "chrome", "crx_url"):
        values.setdefault(key, "(no release yet)")
    page = template
    for key, value in values.items():
        page = page.replace(f"{{{{{key}}}}}", value)
    return HTMLResponse(page, headers=PAGE_HEADERS)


@router.get("/extension/updates.xml")
def update_manifest() -> Response:
    """
    The update manifest (Omaha ``gupdate`` XML) that installed copies poll every few hours.

    :return: The manifest.
    :raises HTTPException: 404 when no release is published.
    """
    path = RELEASE_DIR / "updates.xml"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return Response(path.read_bytes(), media_type="text/xml", headers={"Cache-Control": "public, max-age=300"})


@router.get("/extension/{name}")
def release_file(name: str) -> FileResponse:
    """
    A release file: the signed CRX, the developer-mode zip or the checksums.

    :param name: The file name (matched against a fixed set of patterns).
    :return: The file.
    :raises HTTPException: 404 for any other name or a missing file.
    """
    for pattern, media_type, cache in _FILES.values():
        if pattern.fullmatch(name):
            path = RELEASE_DIR / name
            if path.is_file():
                return FileResponse(
                    path, media_type=media_type, headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff"}
                )
            break
    raise HTTPException(status_code=404)
