"""Guard the administrative mutation-to-audit invariant."""

import inspect

from coire_api.routes import admin_ledger, admin_models, admin_nodes, admin_sharding, instances


def test_pre_identity_admin_mutation_modules_use_typed_principal_audits() -> None:
    for module in (admin_ledger, admin_models, admin_nodes, admin_sharding, instances):
        source = inspect.getsource(module)
        assert "write_principal_audit(" in source, module.__name__


def test_identity_mutations_write_audit_events() -> None:
    from coire_api.identity import entitlements, keys, users

    expected = {
        users: {"user.create", "user.update", "user.deactivate"},
        keys: {"api_key.create", "api_key.rotate", "api_key.revoke"},
        entitlements: {"entitlement.grant", "entitlement.revoke"},
    }
    for module, actions in expected.items():
        source = inspect.getsource(module)
        assert actions <= {action for action in actions if action in source}
