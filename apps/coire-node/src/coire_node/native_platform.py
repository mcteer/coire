"""Runtime platform predicates keep native code type checked on Linux CI too."""


def is_darwin(platform_name: str) -> bool:
    # Mypy treats direct sys.platform comparisons as compile-time branches.
    # Keep native refusal at runtime while checking the retained Darwin body on
    # both platforms, without disabling strict unreachable-code diagnostics.
    return platform_name == "darwin"
