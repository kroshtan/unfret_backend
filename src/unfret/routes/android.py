"""
``/android``: the Android app, Google Play first, then the beta APK, and Unfret Dev on dev only.

Three channels (Floris, 2026-11-25): Google Play first ("Get it on Google Play", the
stable app); below it the beta, the signed APK "for testers and phones without Google Play" (the same app and key, so
it installs over the Play version and Play takes over again once its version is newer); and on dev only, Unfret Dev,
the test build of main (:mod:`unfret.android_dev`), which production never shows.

What the app does, the APK with its version and size, the APK's SHA-256 and the signing certificate's
fingerprint, how to install it and turn "Install unknown apps" off again, how updates work, its permissions and its
privacy. The release comes from :mod:`unfret.android_release`; until one is published the page says the download is
coming soon and offers no file. A beta (a pre-release version, ``0.1.0-beta.1``) is labelled "Beta" next to its
version, with one sentence on what that means and where to say what doesn't work.

Not linked from the home page, the menu or the footer yet, and ``noindex``: reachable by its address until the app is
listed alongside the other ways to get Unfret.

``/.well-known/assetlinks.json`` lets the installed app open this site's ``/pass`` links
(:mod:`unfret.android_app_links`).
"""

import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from unfret import android_app_links, android_dev, android_release
from unfret.android_dev import DevBuild
from unfret.android_release import AndroidRelease
from unfret.config import settings
from unfret.routes.pages import PAGE_HEADERS, template, versioned

HEADERS = {**PAGE_HEADERS, "X-Robots-Tag": "noindex"}

router = APIRouter(include_in_schema=False)

PLAY_URL = "https://play.google.com/store/apps/details?id=net.unfret.android"

# The APK's heading: the beta channel's, or for a stable version on the same channel, without the word.
BETA_HEADING = "Beta: for testers and phones without Google Play"
APK_HEADING = "The APK: for testers and phones without Google Play"
BETA_INTRO = """<p>The same app as on Google Play, as an APK from Unfret's own code forge, sometimes a little ahead of
        it. It installs over the Play version, and Play takes over again (and updates it) once Play's version is
        newer.</p>"""

COMING_SOON = """<section class="panel notice apk-download" aria-labelledby="download">
      <h2 id="download">Download coming soon</h2>
      <p>The signed app isn't published yet. Until it is, check links from your phone with the
        <a href="/check">link checker</a>: it installs from your phone's browser and shows up in the share sheet
        too.</p>
      <p class="buttons"><a class="button primary large" href="/check">Open the link checker</a></p>
    </section>"""


BETA_LABEL = '<span class="beta">Beta</span>'
BETA_NOTE = """<p class="beta-note">This is a beta, an early version: things may still change. Tell us what doesn't
        work at <a href="mailto:hello@unfret.net">hello@unfret.net</a>.</p>"""


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
    label = f" {BETA_LABEL}" if release.prerelease else ""
    note = f"\n      {BETA_NOTE}" if release.prerelease else ""
    return f"""<section class="panel apk-download" aria-labelledby="download">
      <h2 id="download">{BETA_HEADING if release.prerelease else APK_HEADING}</h2>
      {BETA_INTRO}{note}
      <p class="buttons"><a class="button primary large" href="{url}" type="application/vnd.android.package-archive"
        download>Download Unfret {version} (APK, {size})</a></p>
      <dl class="facts">
        <dt>Version</dt>
        <dd>{version}{label}, published {_long_date(release.published)}</dd>
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


def dev_panel(build: DevBuild) -> str:
    """
    Unfret Dev's download and facts: on dev only.

    :param build: The latest dev build.
    :return: The panel's HTML.
    """
    version = html.escape(build.version)
    size = android_release.megabytes(build.size)
    return f"""<section class="panel apk-download apk-dev" aria-labelledby="dev">
      <h2 id="dev">Unfret Dev: test builds (dev.unfret.net only)</h2>
      <p>A test build of every change to the app, for testers. It is its own app, "Unfret Dev", next to Unfret: it
        talks to dev.unfret.net only and is signed with a separate dev key, so it never updates or replaces Unfret.</p>
      <p class="buttons"><a class="button large" href="{html.escape(build.apk_url)}"
        type="application/vnd.android.package-archive" download>Download Unfret Dev {version} (APK, {size})</a></p>
      <dl class="facts hashes">
        <dt>Version</dt>
        <dd>{version} (CI run {build.run}), built {html.escape(build.built)}</dd>
        <dt>Commit</dt>
        <dd><a href="{html.escape(build.commit_url)}" rel="noopener"
          ><code>{html.escape(build.commit[:12])}</code></a></dd>
        <dt>{html.escape(build.apk)} (SHA-256)</dt>
        <dd><code class="hash">{html.escape(build.sha256)}</code></dd>
        <dt>Signing certificate (dev key, SHA-256)</dt>
        <dd><code class="hash">{html.escape(android_release.fingerprint(android_dev.DEV_CERT_SHA256))}</code></dd>
        <dt>From</dt>
        <dd><a href="{html.escape(build.release_url)}" rel="noopener">the rolling dev pre-release</a>
          (<a href="{html.escape(build.sums_url)}" rel="noopener">SHA256SUMS</a>)</dd>
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
async def android_page() -> HTMLResponse:
    """
    Unfret for Android: Google Play first, then the beta APK, and Unfret Dev on dev only.

    The beta is "coming soon" while none is published.

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
    dev_build = await android_dev.latest() if settings.android_dev_builds else None
    fields = {
        "play_url": html.escape(PLAY_URL),
        "download": download,
        "dev": dev_panel(dev_build) if dev_build else "",
        "apk_hash": apk_hash,
        "cert_fingerprint": html.escape(android_release.fingerprint(android_release.SIGNING_CERT_SHA256)),
        "permissions": permission_items(permissions),
        "min_android": html.escape(android_release.android_version(min_sdk)),
    }
    page = template("android.html")
    for name, value in fields.items():
        page = page.replace("{{" + name + "}}", value)
    return HTMLResponse(versioned(page), headers=HEADERS)


@router.get("/.well-known/assetlinks.json")
def asset_links() -> JSONResponse:
    """
    The Digital Asset Links file that verifies the app's App Links: Unfret for Android may open ``/pass`` links.

    Android's verifier wants a 200 with ``application/json`` and no redirect, so the file is served as it is, at this
    address only, on dev as on production.

    :return: :func:`unfret.android_app_links.statements`, as JSON.
    """
    return JSONResponse(
        android_app_links.statements(),
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"},
    )
