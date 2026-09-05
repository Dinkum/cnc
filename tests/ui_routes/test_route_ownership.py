from app.routes import ui as ui_routes


def test_ui_aggregator_contains_only_surface_owned_routes() -> None:
    endpoint_modules = {
        route.endpoint.__module__
        for route in ui_routes.router.routes
        if hasattr(route, "endpoint")
    }

    assert endpoint_modules == {
        "app.ui.routes.backup_mutations",
        "app.ui.routes.hardening_mutations",
        "app.ui.routes.input_mutations",
        "app.ui.routes.output_mutations",
        "app.ui.routes.pages",
        "app.ui.routes.reads",
        "app.ui.routes.settings_mutations",
    }
