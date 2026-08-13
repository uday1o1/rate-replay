import importlib


def test_modular_monolith_package_boundaries_import() -> None:
    for package in (
        "ratereplay_api",
        "ratereplay_worker",
        "ratereplay_domain",
        "ratereplay_ingestion",
        "ratereplay_tariffs",
        "ratereplay_optimizer",
        "ratereplay_persistence",
        "ratereplay_reports",
    ):
        assert importlib.import_module(package).__name__ == package
