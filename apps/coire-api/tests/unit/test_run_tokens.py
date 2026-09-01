from __future__ import annotations

from coire_api.run_tokens import RUN_TOKEN_PATTERN, hasher, token_material, verify_material


def test_run_token_has_256_bit_secret_and_hash_verifies() -> None:
    prefix, secret, presented = token_material()
    assert len(prefix) == 12
    assert len(secret) == 43
    assert RUN_TOKEN_PATTERN.fullmatch(presented)
    digest = hasher.hash(secret)
    assert verify_material(digest, secret)
    assert not verify_material(digest, "x" * 43)
    assert secret not in digest


def test_run_token_pattern_rejects_api_keys_and_malformed_values() -> None:
    assert RUN_TOKEN_PATTERN.fullmatch("coire_abcdefghijkl_" + "x" * 43) is None
    assert RUN_TOKEN_PATTERN.fullmatch("coire_run_short_" + "x" * 43) is None
