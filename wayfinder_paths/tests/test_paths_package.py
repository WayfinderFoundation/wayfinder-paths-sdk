import importlib


def test_paths_package_imports_cleanly() -> None:
    paths_pkg = importlib.import_module("wayfinder_paths.paths")

    assert paths_pkg.__all__ == ["PathBuilder", "PathManifest", "PathsApiClient"]
