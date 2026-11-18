"""
``/android``: the Android app, for a direct install of its signed APK (Google Play comes later).

What the app does, the download with its version and size, the APK's SHA-256 and the signing certificate's
fingerprint, how to install it and turn "Install unknown apps" off again, how updates work, its permissions and its
privacy. The release comes from :mod:`unfret.android_release`; until one is published the page says the download is
coming soon and offers no file.

Not linked from the home page, the menu or the footer yet, and ``noindex``: reachable by its address until the app is
listed alongside the other ways to get Unfret.
"""

import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from unfret import android_release
from unfret.android_release import AndroidRelease
from unfret.routes.pages import PAGE_HEADERS, template, versioned

HEADERS = {**PAGE_HEADERS, "X-Robots-Tag": "noindex"}

router = APIRouter(include_in_schema=False)

COMING_SOON = """<section class="panel notice apk-download" aria-labelledby="download">
      <h2 id="download">Download coming soon</h2>
      <p>The signed app isn't published yet. Until it is, check links from your phone with the
        <a href="/check">link checker</a>: it installs from your phone's browser and shows up in the share sheet
        too.</p>
      <p class="buttons"><a class="button primary large" href="/check">Open the link checker</a></p>
    </section>"""


def _long_date(day: android_release.date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def download_panel(release: AndroidRelease) -> str:
    """
    The download button and the release's facts.

    :param release: The published release.
    :return: The panel's HTML.
    """
    url = html.escape(release.apk_url)
    version = html.escape(release.version)
    size = android_release.megabytes(release.size)
    return f"""<section class="panel apk-download" aria-labelledby="download">
      <h2 id="download">Download</h2>
      <p class="buttons"><a class="button primary large" href="{url}" type="application/vnd.android.package-archive"
        download>Download Unfret {version} (APK, {size})</a></p>
      <dl class="facts">
        <dt>Version</dt>
        <dd>{version}, published {_long_date(release.published)}</dd>
        <dt>Size</dt>
        <dd>{size} ({release.size:,} bytes)</dd>
        <dt>Needs</dt>
        <dd>{html.escape(android_release.android_version(release.min_sdk))} or newer</dd>
        <dt>From</dt>
        <dd>Unfret's own code forge, over HTTPS:
          <a href="{html.escape(release.release_url)}" rel="noopener">release {html.escape(release.tag)}</a>
          (<a href="{html.escape(release.sums_url)}" rel="noopener">SHA256SUMS</a>)</dd>
      </dl>
    </section>"""


def permission_items(permissions: tuple[str, ...]) -> str:
    """
    The app's permissions as list items, each with what it is for.

    :param permissions: The permissions' names, as in the APK's manifest.
    :return: ``<li>`` elements.
    """
    items = []
    for name in permissions:
        known = android_release.PERMISSIONS.get(name)
        code = f"<code>{html.escape(name)}</code>"
        if known:
            title, why = known
            items.append(f"<li><strong>{html.escape(title)}</strong> ({code}): {html.escape(why)}</li>")
        else:
            items.append(f"<li>{code}</li>")
    return "\n      ".join(items)


@router.get("/android", response_class=HTMLResponse)
def android_page() -> HTMLResponse:
    """
    Unfret for Android: download and install the signed APK, or "coming soon" while none is published.

    :return: The page.
    """
    release = android_release.load()
    if release:
        download = download_panel(release)
        apk_hash = (
            f"<dt>{html.escape(release.apk)} (SHA-256)</dt>\n"
            f'      <dd><code class="hash">{html.escape(release.sha256)}</code></dd>'
        )
        permissions, min_sdk = release.permissions, release.min_sdk
    else:
        download = COMING_SOON
        apk_hash = "<dt>The APK (SHA-256)</dt>\n      <dd>Listed here with each release.</dd>"
        permissions, min_sdk = android_release.DEFAULT_PERMISSIONS, android_release.DEFAULT_MIN_SDK
    fields = {
        "download": download,
        "apk_hash": apk_hash,
        "cert_fingerprint": html.escape(android_release.fingerprint(android_release.SIGNING_CERT_SHA256)),
        "permissions": permission_items(permissions),
        "min_android": html.escape(android_release.android_version(min_sdk)),
    }
    page = template("android.html")
    for name, value in fields.items():
        page = page.replace("{{" + name + "}}", value)
    return HTMLResponse(versioned(page), headers=HEADERS)
