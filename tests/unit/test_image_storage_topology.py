"""Image storage and upload limits must not broaden the rest of the platform."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/compose/compose.yaml"
NGINX = ROOT / "apps/coire-web/nginx/nginx.conf"
API_IMAGE = ROOT / "apps/coire-api/docker/api.Dockerfile"


def test_private_blob_volume_is_api_only_and_admission_defaults_off() -> None:
    config = yaml.safe_load(COMPOSE.read_text())
    services = config["services"]
    assert "coire-blobs" in config["volumes"]
    assert "coire-blobs:/opt/coire/blobs" in services["coire-api"]["volumes"]
    assert "--chown=65532:65532 /volume/blobs /opt/coire/blobs" in API_IMAGE.read_text()
    for name, service in services.items():
        if name != "coire-api":
            assert not any("coire-blobs:" in volume for volume in service.get("volumes", [])), name
    env = services["coire-api"]["environment"]
    assert env["IMAGE_ENABLED"] == "${COIRE_IMAGE_ENABLED:-false}"
    assert (
        env["IMAGE_GENERATION_INPUT_MAX_BYTES"]
        == "${COIRE_IMAGE_GENERATION_INPUT_MAX_BYTES:-10485760}"
    )
    assert env["IMAGE_RECIPE_INPUT_MAX_BYTES"] == "${COIRE_IMAGE_RECIPE_INPUT_MAX_BYTES:-67108864}"
    assert (
        env["IMAGE_RECIPE_METADATA_MAX_BYTES"] == "${COIRE_IMAGE_RECIPE_METADATA_MAX_BYTES:-65536}"
    )
    worker = services["coire-file-worker"]
    assert "image-files" in worker["profiles"]
    assert "coire-chat-originals:/opt/coire/chat/originals:ro" in worker["volumes"]


def test_image_ingress_limits_are_route_specific() -> None:
    nginx = NGINX.read_text()
    assert "location = /api/v1/image-inputs" in nginx
    assert "client_max_body_size 65m;" in nginx
    assert 'location ~ "^/internal/images/[0-9A-HJKMNP-TV-Z]{26}/outputs/[0-3]$"' in nginx
    assert "client_max_body_size 64m;" in nginx
    assert "location ~ ^/api/v1/chat/conversations/[0-9a-fA-F-]+/files$" in nginx
    assert "client_max_body_size 11m;" in nginx
