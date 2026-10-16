"""Local TLS never serves the password form over the machine HTTP listener."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def configuration(source: str, hostname: str = "coire-core.lab") -> str:
    spec = importlib.util.spec_from_file_location("local_tls", ROOT / "scripts/coire-local-tls.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.configuration(source, hostname))


def test_only_https_serves_browser_pages_and_http_machine_paths_remain() -> None:
    source = (ROOT / "apps/coire-web/nginx/nginx.conf").read_text()
    rendered = configuration(source)
    plain, tls = rendered.split("listen 8081 ssl;", 1)
    assert "return 308 https://coire-core.lab:8443$request_uri;" in plain
    assert "try_files $uri" not in plain
    assert "try_files $uri $uri/ /index.html;" in tls
    assert "return 308" not in tls
    for route in (
        "location /v1/",
        "location /failover/",
        "location = /nginx-health",
        "location ~ ^/(health|ready|api)(/|$)",
    ):
        assert route in plain and route in tls


def test_unknown_browser_configuration_and_hostname_injection_fail_closed() -> None:
    source = (ROOT / "apps/coire-web/nginx/nginx.conf").read_text()
    with pytest.raises(ValueError):
        configuration(source.replace("try_files $uri $uri/ /index.html;", "return 200;"))
    with pytest.raises(ValueError):
        configuration(source, "coire-core.lab; injected")
