from unfret.routes.check import _failure_site


def _fetch_the_shop() -> None:
    raise RuntimeError("Client error for url 'https://shop.example/secret-product'")


def test_the_failure_site_names_frames_but_never_the_message() -> None:
    try:
        _fetch_the_shop()
    except RuntimeError as exc:
        site = _failure_site(exc)
    assert site[-1].endswith(":_fetch_the_shop")
    assert all("shop.example" not in frame for frame in site)
    assert all(frame.count(":") == 2 for frame in site)
