from __future__ import annotations

import pytest
from pydantic import ValidationError

from insightagent.artifact_access import (
    ARTIFACT_REF_PATTERN,
    ArtifactArgs,
    assert_allowed_artifact_ref,
)
from insightagent.market import make_event_id


def test_artifact_args_reject_event_id():
    with pytest.raises(ValidationError):
        ArtifactArgs.model_validate({"ref": "000858-8b2582a854b6"})
    with pytest.raises(ValidationError):
        ArtifactArgs.model_validate({"ref": "event:000858:8b2582a854b6"})


def test_artifact_args_accept_sha256_ref():
    ref = "artifact://" + "a" * 64
    parsed = ArtifactArgs.model_validate({"ref": ref})
    assert parsed.ref == ref
    assert ARTIFACT_REF_PATTERN.startswith("^artifact://")


def test_whitelist_rejects_other_run_ref():
    allowed = "artifact://" + "a" * 64
    other = "artifact://" + "b" * 64
    with pytest.raises(PermissionError):
        assert_allowed_artifact_ref(other, allowed)
    assert_allowed_artifact_ref(allowed, allowed)


def test_event_id_uses_event_prefix():
    event_id = make_event_id("000858", "控股股东拟减持不超过 2% 股份")
    holder_id = make_event_id("000858", "减持", kind="holder")
    assert event_id.startswith("event:000858:")
    assert holder_id.startswith("event:000858:holder:")
    assert event_id.count(":") == 2
    assert holder_id.count(":") == 3
