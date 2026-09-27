"""Run the failover ASGI application."""

import uvicorn

uvicorn.run("coire_failover.app:app", host="0.0.0.0", port=8004)
