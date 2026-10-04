"""API package smoke test (Task 3); route tests arrive with Task 4."""


def test_api_package_imports() -> None:
    import apps.api

    assert apps.api.__doc__
