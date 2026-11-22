"""
Where the clients are, for ``/developers`` and ``/llms.txt``: a Python client, and plain HTTP for every other language.

Only the Python client (``clients/python``, ``unfret-client``) is published, to PyPI, by the manual "Publish clients"
workflow. The TypeScript client (``clients/typescript``) is a reference to copy, not a package; other languages get
``/developers/http``.
"""

# Flip to True once unfret-client is on PyPI (https://pypi.org/project/unfret-client/), in the same change that
# notes it in the CHANGELOG (and drops the source install from clients/python/README.md). Until then the pages say
# how to install it from the source instead.
PYTHON_CLIENT_ON_PYPI = True

PYTHON_CLIENT_SOURCE = (
    "unfret-client @ git+https://git.kroshtan.com/kroshtan/unfret_backend.git#subdirectory=clients/python"
)
TYPESCRIPT_CLIENT_URL = "https://git.kroshtan.com/kroshtan/unfret_backend/src/branch/main/clients/typescript"
TYPESCRIPT_CLIENT_MIRROR = "https://github.com/kroshtan/unfret_backend/tree/main/clients/typescript"
# Copy-paste examples (Python and TypeScript): a pass, x402 on Solana, x402 on Base.
EXAMPLES_URL = "https://git.kroshtan.com/kroshtan/unfret_backend/src/branch/main/examples"
EXAMPLES_MIRROR = "https://github.com/kroshtan/unfret_backend/tree/main/examples"


def python_install_note() -> str:
    """
    Where ``pip install unfret-client`` gets the client from, in one sentence (Markdown, for ``/llms.txt``).

    :return: The sentence: from PyPI, or, before the first release there, from the source.
    """
    if PYTHON_CLIENT_ON_PYPI:
        return "`pip install unfret-client` (from PyPI)."
    return (
        "`pip install unfret-client`, from PyPI once its first release is out (it comes with Unfret's next "
        f'release); until then, from the source: `pip install "{PYTHON_CLIENT_SOURCE}"`.'
    )
