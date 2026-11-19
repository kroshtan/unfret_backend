"""
The Android app's verified links (Android App Links): ``/.well-known/assetlinks.json`` and the path it opens.

"Use on your phone" on ``/my-checks`` shows a QR code for ``<site>/pass#pass=<pass>`` (:data:`PHONE_LINK_PATH`). With
Unfret for Android installed, Android opens that link in the app, because the app declares it (an intent filter with
``android:autoVerify="true"`` for ``https://unfret.net/pass``) and this site's ``assetlinks.json`` names the app: its
package and its release signing certificate. Anywhere else (no app, an iPhone, a computer) the link opens ``/pass`` in
the browser, which forwards to ``/check`` with the fragment intact.

Constants, not settings: the package name is the app's ``applicationId`` (``unfret_android/app/build.gradle.kts``)
and the certificate is :data:`unfret.android_release.SIGNING_CERT_SHA256`, the same for every release
(``unfret_android/docs/release-cert.sha256``). Both are public and the same on dev and production, so dev serves the
same file. The app has no separate dev build: a debug build has the same package, signed with a throwaway key made
in each checkout, so no debug certificate is listed (a debug build's link is approved by hand with ``adb``).
"""

from typing import Any

from unfret.android_release import SIGNING_CERT_SHA256, fingerprint

PACKAGE_NAME = "net.unfret.android"
RELATION = "delegate_permission/common.handle_all_urls"
PHONE_LINK_PATH = "/pass"


def statements() -> list[dict[str, Any]]:
    """
    The Digital Asset Links statements this site makes: the app may open its links.

    :return: The contents of ``/.well-known/assetlinks.json``.
    """
    return [
        {
            "relation": [RELATION],
            "target": {
                "namespace": "android_app",
                "package_name": PACKAGE_NAME,
                "sha256_cert_fingerprints": [fingerprint(SIGNING_CERT_SHA256)],
            },
        }
    ]
