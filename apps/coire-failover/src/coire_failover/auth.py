"""Access verification is shared with node relay for independent admission enforcement."""

from coire_core.failover_identity import AccessIdentityError, verify_access_assertion

__all__ = ["AccessIdentityError", "verify_access_assertion"]
