"""Registry wire shapes (T008)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coire_core.models.registry import (
    CapabilityProfile,
    ModelAddRequest,
    ModelUpdateRequest,
    Tag,
    is_valid_slug,
    slug_for,
)


class TestSlug:
    def test_repo_id_becomes_a_flat_store_key(self) -> None:
        assert slug_for("mlx-community/Qwen3.8-27B-4bit") == "mlx-community--Qwen3.8-27B-4bit"

    @pytest.mark.parametrize("bad", ["no-slash", "a/b/c", "../etc/passwd", "", "a/"])
    def test_non_repo_ids_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            slug_for(bad)

    @pytest.mark.parametrize("bad", ["../etc", "a/b", "", "a--b/../c", "/abs--x"])
    def test_only_platform_produced_slugs_validate(self, bad: str) -> None:
        """Every slug arriving from outside is checked before it is joined to a path."""
        assert not is_valid_slug(bad)

    def test_a_produced_slug_validates(self) -> None:
        assert is_valid_slug(slug_for("mlx-community/Qwen2.5-0.5B-Instruct-4bit"))


class TestPlacementPolicy:
    @pytest.mark.parametrize(
        "policy",
        ["single:auto", "single:coire-edge-a", "pinned:coire-edge-b", "sharded:tp", "sharded:pp"],
    )
    def test_valid_policies(self, policy: str) -> None:
        assert ModelAddRequest(repo_id="a/b", placement_policy=policy).placement_policy == policy

    @pytest.mark.parametrize(
        "policy", ["single:", "pinned:auto", "sharded:dp", "single:Coire-Edge-A", "auto", ""]
    )
    def test_invalid_policies_are_rejected(self, policy: str) -> None:
        with pytest.raises(ValidationError):
            ModelAddRequest(repo_id="a/b", placement_policy=policy)

    def test_default_is_single_auto(self) -> None:
        assert ModelAddRequest(repo_id="a/b").placement_policy == "single:auto"


class TestCuration:
    def test_verified_cannot_be_set_by_an_admin(self) -> None:
        """Only feature 017's evaluation sets it; the router keys write access on it."""
        with pytest.raises(ValidationError):
            ModelUpdateRequest(capability_profile={"verified": True})  # type: ignore[arg-type]

    def test_unknown_tag_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelUpdateRequest(tags=["not-a-tag"])  # type: ignore[list-item]

    def test_known_tags_pass(self) -> None:
        assert ModelUpdateRequest(tags=[Tag.CODING, Tag.REASONING]).tags == [
            Tag.CODING,
            Tag.REASONING,
        ]

    def test_absent_versus_null_chat_template_are_distinguishable(self) -> None:
        """`null` clears the override; absence leaves it alone. Both deserialise to None."""
        assert "chat_template" not in ModelUpdateRequest().model_fields_set
        assert "chat_template" in ModelUpdateRequest(chat_template=None).model_fields_set

    def test_oversized_chat_template_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelUpdateRequest(chat_template="x" * (64 * 1024 + 1))

    def test_profile_defaults_are_conservative(self) -> None:
        """A model claims no capability until someone says otherwise."""
        profile = CapabilityProfile()
        assert profile.tool_calling == "none"
        assert profile.verified is False
        assert profile.parallel_tools is False
