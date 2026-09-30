# Implementation Plan: Populated Chat Migration Proof

**Branch**: `feat/014k-chat-migration-proof` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Use an opt-in PostgreSQL integration fixture with a unique temporary database. Alembic starts at revision 0014, then applies 0015. Insert a legacy model/user through SQL, add a chat conversation, and assert downgrade refuses without dropping data. Remove the content, downgrade to 0014, inspect retained older rows and removed columns, then re-upgrade and drop the disposable database. Use an explicit `COIRE_TEST_POSTGRES_DSN` as the server connection source; no service is started automatically.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | Migration test starts no model process or service. |
| III | Verifies registry backend and Chat table wire persistence. |
| IV | Tests owner FK and guarded content removal; only disposable DB. |
| V | Older models retain bare `mlx_lm` backend default. |
| VI | No runtime path changes. |
| VII | Automated populated upgrade/downgrade regression. |

No constitution exception or dependency.
