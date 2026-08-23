from __future__ import annotations

import re
from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field

from .persistence import FileArtifactStore

ARTIFACT_REF_PATTERN = r"^artifact://[a-f0-9]{64}$"
ARTIFACT_REF_RE = re.compile(ARTIFACT_REF_PATTERN)

ARTIFACT_TOOL_DESCRIPTION = (
    "Load this run's original snapshot file. "
    "ref must be exactly this run's artifact:// + 64 lowercase SHA-256 hex. "
    "Do not pass event_id, announcement id, citation id, or any other key."
)

ARTIFACT_REF_FIELD_DESCRIPTION = (
    "This run's artifact:// SHA-256 only. "
    "Format: artifact:// followed by 64 lowercase hex characters. "
    "event_id, announcement id, and citation id are not valid."
)


class ArtifactArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(
        description=ARTIFACT_REF_FIELD_DESCRIPTION,
        pattern=ARTIFACT_REF_PATTERN,
    )


class ArtifactOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    content: str


def assert_allowed_artifact_ref(ref: str, allowed_ref: str) -> None:
    if not ARTIFACT_REF_RE.fullmatch(ref):
        raise PermissionError(
            "Invalid artifact ref {!r}. Expected artifact:// + 64-char "
            "sha256. event_id, announcement id, and citation id are not "
            "valid here.".format(ref)
        )
    if not allowed_ref or ref != allowed_ref:
        raise PermissionError(
            "Artifact ref is not in this run. Allowed: {}".format(
                allowed_ref or "(none)"
            )
        )


async def load_whitelisted_artifact(
    store: FileArtifactStore,
    ref: str,
    allowed_ref: str,
) -> Dict[str, Any]:
    assert_allowed_artifact_ref(ref, allowed_ref)
    content = await store.get(ref)
    return {"ref": ref, "content": content}
