"""Describe image SSE framing and its typed data payload without claiming JSON transport."""

from __future__ import annotations

from fastapi import FastAPI


def install_image_event_openapi(app: FastAPI) -> None:
    original = app.openapi

    def rendered() -> dict[str, object]:
        schema = original()
        response = schema["paths"]["/api/v1/images/{job_id}/events"]["get"]["responses"]["200"]
        response["content"] = {
            "text/event-stream": {
                "schema": {"type": "string"},
                "x-coire-event-schema": {"$ref": "#/components/schemas/ImageJobEvent"},
            }
        }
        return schema

    app.openapi = rendered  # type: ignore[method-assign]
