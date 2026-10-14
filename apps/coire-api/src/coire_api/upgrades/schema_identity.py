"""Read-only, bounded identity of the deployed PostgreSQL schema, excluding row data."""

import hashlib
import json

from opentelemetry import trace
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import SchemaHead

# Definitions include defaults, constraint expressions and routine bodies, but never
# table contents, credentials or sequence counters. They stay private to this digest.
_STRUCTURE = """
SELECT kind, identity, CASE WHEN octet_length(definition) <= 65536
 THEN definition ELSE NULL END AS definition FROM (
 SELECT 'column' AS kind, c.relname || '.' || a.attname AS identity,
   json_build_array(c.relkind, a.attnum, format_type(a.atttypid, a.atttypmod),
     a.attnotnull, a.attidentity, a.attgenerated,
     pg_get_expr(d.adbin, d.adrelid))::text AS definition
 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 JOIN pg_attribute a ON a.attrelid = c.oid
 LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
 WHERE n.nspname = current_schema() AND a.attnum > 0 AND NOT a.attisdropped
 UNION ALL
 SELECT 'relation', c.relname, json_build_array(c.relkind, c.relpersistence)::text
 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema()
 UNION ALL
 SELECT 'constraint', c.relname || '.' || k.conname, pg_get_constraintdef(k.oid, false)
 FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
 JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema()
 UNION ALL
 SELECT 'index', c.relname, pg_get_indexdef(c.oid)
 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema() AND c.relkind IN ('i', 'I')
 UNION ALL
 SELECT 'view', c.relname, pg_get_viewdef(c.oid, false)
 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema() AND c.relkind IN ('v', 'm')
 UNION ALL
 SELECT 'routine', p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
   pg_get_functiondef(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = current_schema() AND p.prokind IN ('f', 'p')
 UNION ALL
 SELECT 'trigger', c.relname || '.' || t.tgname, pg_get_triggerdef(t.oid, false)
 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
 JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema() AND NOT t.tgisinternal
 UNION ALL
 SELECT 'enum', t.typname || '.' || e.enumsortorder::text, e.enumlabel
 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
 JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = current_schema()
) objects ORDER BY kind COLLATE "C", identity COLLATE "C", definition COLLATE "C"
LIMIT 32769
"""


async def inspect_schema(session: AsyncSession) -> tuple[str, str]:
    """Return the sole Alembic head and a deterministic structural SHA-256."""
    heads = (await session.execute(text("SELECT version_num FROM alembic_version LIMIT 2"))).all()
    if len(heads) != 1:
        raise UpgradeUnavailable("Database schema must have exactly one known revision")
    try:
        head = TypeAdapter(SchemaHead).validate_python(heads[0][0])
    except ValidationError as exc:
        raise UpgradeUnavailable("Database schema revision is invalid") from exc
    digest = hashlib.sha256(b"[")
    size, count = 2, 0
    with trace.get_tracer("coire.api.upgrades").start_as_current_span(
        "coire.api.upgrade_schema_identity"
    ):
        result = await session.stream(text(_STRUCTURE))
        try:
            async for row in result:
                count += 1
                if count > 32768 or row[2] is None:
                    raise UpgradeUnavailable("Database schema inventory exceeded its bound")
                encoded = json.dumps(tuple(row), ensure_ascii=True, separators=(",", ":")).encode(
                    "ascii"
                )
                size += len(encoded) + (1 if count > 1 else 0)
                if size > 4 * 1024 * 1024:
                    raise UpgradeUnavailable("Database schema definitions exceeded their bound")
                if count > 1:
                    digest.update(b",")
                digest.update(encoded)
        finally:
            await result.close()
    digest.update(b"]")
    return head, digest.hexdigest()
