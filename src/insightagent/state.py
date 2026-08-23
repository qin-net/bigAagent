from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, Optional, Protocol

from .contracts import AgentState, StatePatch, utc_now


class StateConflictError(RuntimeError):
    pass


class InvalidStatePatchError(ValueError):
    pass


class StateStore(Protocol):
    async def load_or_create(self, **kwargs: Any) -> AgentState:
        ...

    async def get(self, session_id: str) -> AgentState:
        ...

    async def save(
        self, state: AgentState, *, expected_version: Optional[int] = None
    ) -> AgentState:
        ...

    async def apply_patch(
        self, session_id: str, patch: StatePatch
    ) -> AgentState:
        ...

    async def history(self, session_id: str) -> list[AgentState]:
        ...


class InMemoryStateStore:
    """Versioned state store suitable for tests and the first runtime slice."""

    DEFAULT_MUTABLE_ROOTS = frozenset(
        {"business_context", "private_memory", "meta", "checkpoint"}
    )

    def __init__(self, mutable_roots: Optional[Iterable[str]] = None) -> None:
        self._states: Dict[str, AgentState] = {}
        self._history: Dict[str, list[AgentState]] = {}
        self._lock = asyncio.Lock()
        self._mutable_roots = frozenset(
            mutable_roots or self.DEFAULT_MUTABLE_ROOTS
        )

    async def load_or_create(
        self,
        *,
        agent_name: str,
        session_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        stock_code: Optional[str] = None,
        thesis_id: Optional[str] = None,
        business_context: Optional[Dict[str, Any]] = None,
    ) -> AgentState:
        async with self._lock:
            if session_id and session_id in self._states:
                return self._states[session_id].model_copy(deep=True)

            state = AgentState(
                session_id=session_id or AgentState(agent_name=agent_name).session_id,
                parent_session_id=parent_session_id,
                agent_name=agent_name,
                stock_code=stock_code,
                thesis_id=thesis_id,
                business_context=business_context or {},
            )
            self._states[state.session_id] = state.model_copy(deep=True)
            self._history[state.session_id] = [state.model_copy(deep=True)]
            return state

    async def get(self, session_id: str) -> AgentState:
        async with self._lock:
            try:
                return self._states[session_id].model_copy(deep=True)
            except KeyError as error:
                raise KeyError("Unknown session: {}".format(session_id)) from error

    async def save(
        self, state: AgentState, *, expected_version: Optional[int] = None
    ) -> AgentState:
        async with self._lock:
            current = self._states.get(state.session_id)
            if current is None:
                if expected_version not in (None, -1):
                    raise StateConflictError("State does not exist")
                persisted = state.model_copy(deep=True)
            else:
                expected = state.version if expected_version is None else expected_version
                if current.version != expected:
                    raise StateConflictError(
                        "Expected version {}, found {}".format(
                            expected, current.version
                        )
                    )
                persisted = state.model_copy(
                    update={
                        "version": current.version + 1,
                        "updated_at": utc_now(),
                    },
                    deep=True,
                )

            self._states[persisted.session_id] = persisted.model_copy(deep=True)
            self._history.setdefault(persisted.session_id, []).append(
                persisted.model_copy(deep=True)
            )
            return persisted

    async def apply_patch(
        self, session_id: str, patch: StatePatch
    ) -> AgentState:
        async with self._lock:
            current = self._states.get(session_id)
            if current is None:
                raise KeyError("Unknown session: {}".format(session_id))
            if current.version != patch.base_version:
                raise StateConflictError(
                    "Patch based on {}, current version is {}".format(
                        patch.base_version, current.version
                    )
                )

            persisted = apply_patch_to_state(
                current, patch, self._mutable_roots
            )
            self._states[session_id] = persisted.model_copy(deep=True)
            self._history[session_id].append(persisted.model_copy(deep=True))
            return persisted

    async def history(self, session_id: str) -> list[AgentState]:
        async with self._lock:
            return [
                state.model_copy(deep=True)
                for state in self._history.get(session_id, [])
            ]

    def _validate_path(self, path: str) -> None:
        root = path.split(".", 1)[0]
        if root not in self._mutable_roots:
            raise InvalidStatePatchError(
                "State path is not mutable: {}".format(path)
            )


def _set_path(root: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cursor: Dict[str, Any] = root
    for part in parts[:-1]:
        child = cursor.get(part)
        if child is None:
            child = {}
            cursor[part] = child
        if not isinstance(child, dict):
            raise InvalidStatePatchError(
                "Cannot descend into non-object path: {}".format(path)
            )
        cursor = child
    cursor[parts[-1]] = value


def _get_or_create_list(root: Dict[str, Any], path: str) -> list[Any]:
    parts = path.split(".")
    cursor: Dict[str, Any] = root
    for part in parts[:-1]:
        child = cursor.get(part)
        if child is None:
            child = {}
            cursor[part] = child
        if not isinstance(child, dict):
            raise InvalidStatePatchError(
                "Cannot descend into non-object path: {}".format(path)
            )
        cursor = child

    value = cursor.get(parts[-1])
    if value is None:
        value = []
        cursor[parts[-1]] = value
    if not isinstance(value, list):
        raise InvalidStatePatchError(
            "State path is not a list: {}".format(path)
        )
    return value


def drop_immutable_patch_paths(
    patch: StatePatch,
    mutable_roots: Iterable[str] = InMemoryStateStore.DEFAULT_MUTABLE_ROOTS,
) -> tuple[StatePatch, list[str]]:
    """Keep report-side patches from crashing the run.

    Model submit_final may stuff report fields into state_patch. Those paths
    are not memory. Drop them; legal private_memory writes still apply.
    """
    allowed = frozenset(mutable_roots)
    dropped: list[str] = []

    def allowed_path(path: str) -> bool:
        root = path.split(".", 1)[0] if path else ""
        if root in allowed:
            return True
        dropped.append(path)
        return False

    return (
        patch.model_copy(
            update={
                "set": {
                    path: value
                    for path, value in patch.set.items()
                    if allowed_path(path)
                },
                "append": {
                    path: values
                    for path, values in patch.append.items()
                    if allowed_path(path)
                },
                "remove": {
                    path: values
                    for path, values in patch.remove.items()
                    if allowed_path(path)
                },
            }
        ),
        dropped,
    )


def apply_patch_to_state(
    current: AgentState,
    patch: StatePatch,
    mutable_roots: Iterable[str] = InMemoryStateStore.DEFAULT_MUTABLE_ROOTS,
) -> AgentState:
    allowed = frozenset(mutable_roots)

    def validate_path(path: str) -> None:
        root = path.split(".", 1)[0]
        if root not in allowed:
            raise InvalidStatePatchError(
                "State path is not mutable: {}".format(path)
            )

    data = current.model_dump(mode="python")
    for path, value in patch.set.items():
        validate_path(path)
        _set_path(data, path, value)
    for path, values in patch.append.items():
        validate_path(path)
        target = _get_or_create_list(data, path)
        target.extend(values)
    for path, values in patch.remove.items():
        validate_path(path)
        target = _get_or_create_list(data, path)
        target[:] = [item for item in target if item not in values]

    data["version"] = current.version + 1
    data["updated_at"] = utc_now()
    return AgentState.model_validate(data)
