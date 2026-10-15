"""Close positively exited fixture engines through the ordinary node stop route."""

from __future__ import annotations

import asyncio

import httpx

from coire_node.store import Store


async def close_exited_engines(client: httpx.AsyncClient, store: Store) -> None:
    response = await client.get("/node/engines")
    response.raise_for_status()
    engines = response.json()
    assert all(engine["state"] in {"failed", "stopped"} for engine in engines), (
        "fixture still owns a live or uncertain engine"
    )
    for engine in engines:
        if engine["state"] != "failed":
            continue
        assert engine["backend"] == "mlx_lm"
        manifest = await asyncio.to_thread(store.read_manifest, engine["slug"])
        assert manifest is not None
        if engine["target"] is not None:
            assert engine["target"]["base_manifest_sha256"] == manifest.sha256()
        stopped = await client.delete("/node/engines/" + engine["engine_id"])
        assert stopped.status_code == 202
    response = await client.get("/node/engines")
    response.raise_for_status()
    assert all(engine["state"] == "stopped" for engine in response.json())
