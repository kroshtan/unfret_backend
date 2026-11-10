"""No test leaves state on the app for the next (tests/conftest.py puts it back); unit tests run after integration's."""

from unfret.main import app


def test_the_app_starts_every_test_without_a_pool_graphs_or_services() -> None:
    assert (app.db_pool, app.graph, app.quick_graph, app.x402, app.credits, app.fiat) == (None,) * 6
