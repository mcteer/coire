"""Pooling TLS trust stores keeps HTTPX's verified trust boundary intact."""

import ssl
from pathlib import Path

import httpx
import pytest

from coire_core.net import _shared_http_ssl_context, shared_http_ssl_context


@pytest.mark.parametrize("trust_env", [False, True])
def test_shared_context_retains_httpx_verified_ca_policy(trust_env: bool) -> None:
    shared = shared_http_ssl_context(trust_env=trust_env)
    default = httpx.create_ssl_context(verify=True, trust_env=trust_env)
    assert shared is shared_http_ssl_context(trust_env=trust_env)
    assert shared.check_hostname is True
    assert shared.verify_mode == ssl.CERT_REQUIRED
    assert shared.get_ca_certs(binary_form=True) == default.get_ca_certs(binary_form=True)
    assert shared.cert_store_stats()["x509_ca"] > 0


def test_identical_default_trust_stores_are_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    assert shared_http_ssl_context() is shared_http_ssl_context(trust_env=False)


def test_environment_ca_override_stays_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = httpx.create_ssl_context(verify=True, trust_env=False).get_ca_certs(binary_form=True)[0]
    bundle = tmp_path / "root.pem"
    bundle.write_text(ssl.DER_cert_to_PEM_cert(root))
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    _shared_http_ssl_context.cache_clear()
    try:
        environment = shared_http_ssl_context(trust_env=True)
        explicit = shared_http_ssl_context(trust_env=False)
        assert environment is not explicit
        assert environment.check_hostname and environment.verify_mode == ssl.CERT_REQUIRED
        assert environment.get_ca_certs(binary_form=True) == [root]
        assert environment.get_ca_certs(binary_form=True) == httpx.create_ssl_context(
            verify=True, trust_env=True
        ).get_ca_certs(binary_form=True)
        assert explicit.get_ca_certs(binary_form=True) == httpx.create_ssl_context(
            verify=True, trust_env=False
        ).get_ca_certs(binary_form=True)
    finally:
        _shared_http_ssl_context.cache_clear()
