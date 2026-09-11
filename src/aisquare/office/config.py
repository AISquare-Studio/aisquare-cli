"""Office configuration: resolved once, injected everywhere, pure to build.

Two rules shape this module.

**It reads nothing by itself.** :meth:`OfficeConfig.from_mapping` takes a
mapping and a home; :meth:`OfficeConfig.from_environment` takes the environment
and a home resolver as *arguments*. Neither touches ``os.environ`` or the real
``~/.aisquare`` unless a caller hands them over, so a test configures an Office
by constructing one rather than by monkeypatching the process. There is
deliberately no ``.env`` loading: the CLI does not do it elsewhere, and a
serving process that silently absorbs a file of secrets is exactly the surprise
this codebase avoids.

**It holds no credentials.** The Team OS token and the platform workspace key
are never fields here. What a field may hold is the *name of the environment
variable* a credential is read from at the moment it is needed — the same shape
``core.config.ExplainabilityTarget.api_key_env`` already uses. A config object
therefore stays safe to log, to include in a diagnostic, and to hand to a
capability document.

The durations are the documented starting points from the Office plan
(500 ms observation, 15 s heartbeat, 2 s acknowledgement, 1 s prompt-close
observation, 3 s ordinary remote read). They are **configurable starting
points, not measured guarantees** — nothing here claims the machine can meet
them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_PORT: Final = 7373
"""The port ``asq office`` opens by default, per the accepted Office plan."""

ALLOWED_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})
"""The only hosts the server may bind.

Exactly the plan's ``ALLOWED_HOSTS``. ``::1`` is loopback too and is
deliberately *not* here: the accepted plan lists these two, and widening the
set is P15's call with the server in front of it, not a default that arrives
quietly from a config module.
"""

TEAM_OS_DEFAULT_PORT: Final = 4317
"""The Team OS peer's own default (``TEAMOS_PORT`` in its server)."""

MAX_CAPTURE_FPS: Final = 10
"""Visible shared terminal capture, at most 10 frames per second."""

MAX_QUEUE_LIMIT: Final = 4096
"""Hard ceiling for any per-subscriber queue, whatever the configuration says."""

ENV_PREFIX: Final = "AISQUARE_OFFICE_"

#: The allow-list: environment variable → field. Anything else in the
#: environment is ignored, because the ambient environment is not ours to
#: police — but a variable named here with an unusable value is an error that
#: names the variable, never its value.
ENV_FIELDS: Final[Mapping[str, str]] = {
    f"{ENV_PREFIX}HOST": "host",
    f"{ENV_PREFIX}PORT": "port",
    f"{ENV_PREFIX}WEB_ROOT": "web_root",
    f"{ENV_PREFIX}POLL_MS": "poll_interval_ms",
    f"{ENV_PREFIX}HEARTBEAT_S": "heartbeat_s",
    f"{ENV_PREFIX}ACTION_ACK_S": "action_ack_s",
    f"{ENV_PREFIX}PROMPT_CLOSE_S": "prompt_close_s",
    f"{ENV_PREFIX}REMOTE_TIMEOUT_S": "remote_timeout_s",
    f"{ENV_PREFIX}CAPTURE_FPS": "capture_fps",
    f"{ENV_PREFIX}QUEUE_MAX": "stream_queue_max",
    f"{ENV_PREFIX}SUBSCRIBERS_MAX": "max_subscribers",
    f"{ENV_PREFIX}PLATFORM_PROFILE": "platform_profile",
    # Team OS keeps its own vocabulary. Renaming these into AISQUARE_* would
    # claim the peer is ours to configure; it is a separate local process with
    # its own settings, and an operator who set TEAMOS_PORT means it.
    "TEAMOS_ENABLED": "team_os_enabled",
    "TEAMOS_HOST": "team_os_host",
    "TEAMOS_PORT": "team_os_port",
    "TEAMOS_TOKEN_ENV": "team_os_token_env",
}

_BOOL_FIELDS: Final = frozenset({"team_os_enabled"})
_INT_FIELDS: Final = frozenset(
    {
        "port",
        "poll_interval_ms",
        "capture_fps",
        "stream_queue_max",
        "max_subscribers",
        "team_os_port",
    }
)
_FLOAT_FIELDS: Final = frozenset(
    {"heartbeat_s", "action_ack_s", "prompt_close_s", "remote_timeout_s"}
)
_PATH_FIELDS: Final = frozenset({"web_root"})

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class OfficeConfigError(ValueError):
    """A configuration value is unusable.

    The message names the setting — and, when the value came from the
    environment, the variable — and never repeats the value itself. A bad value
    is sometimes a secret pasted into the wrong variable, and an error message
    is the one place a secret reliably ends up in a log.
    """


class OfficeConfig(BaseModel):
    """Everything the Office server needs, resolved and validated.

    Frozen and strict: a component that could edit the configuration it was
    handed is a component whose behaviour cannot be reproduced from its inputs,
    and an unknown key is a typo that would otherwise be silently ignored — the
    CLI's own ``config.toml`` keeps unknown keys on purpose, for forward
    compatibility across builds, but that reasoning does not transfer to a
    serving process's own settings.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    home: Path
    """The resolved AISQUARE home. Injected, never read from the environment by
    this model — ``core.paths.aisquare_home`` is the caller's business."""

    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = DEFAULT_PORT

    web_root: Path | None = None
    """Where the built frontend lives. An explicit path object or nothing: a
    string would let a relative fragment become a path resolved against
    whatever directory the server happened to start in."""

    poll_interval_ms: Annotated[int, Field(ge=50, le=60_000)] = 500
    heartbeat_s: Annotated[float, Field(gt=0, le=600)] = 15.0
    action_ack_s: Annotated[float, Field(gt=0, le=120)] = 2.0
    prompt_close_s: Annotated[float, Field(gt=0, le=60)] = 1.0
    remote_timeout_s: Annotated[float, Field(gt=0, le=120)] = 3.0

    capture_fps: Annotated[int, Field(ge=1, le=MAX_CAPTURE_FPS)] = MAX_CAPTURE_FPS
    stream_queue_max: Annotated[int, Field(ge=1, le=MAX_QUEUE_LIMIT)] = 256
    """Per-subscriber bound. A slow consumer is reset, never allowed to grow a
    queue until it becomes the server's memory problem."""
    max_subscribers: Annotated[int, Field(ge=1, le=1024)] = 64

    team_os_enabled: bool = False
    team_os_host: str = "127.0.0.1"
    team_os_port: Annotated[int, Field(ge=1, le=65535)] = TEAM_OS_DEFAULT_PORT
    team_os_token_env: str | None = None
    """The *name* of the variable holding the peer's token, never the token."""

    platform_profile: str | None = None
    """Which configured platform profile P12 resolves a binding from. Not a URL,
    not a key, not a workspace: a reference into configuration the CLI already
    owns."""

    @field_validator("host", "team_os_host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        if value not in ALLOWED_HOSTS:
            allowed = ", ".join(sorted(ALLOWED_HOSTS))
            raise ValueError(f"must be a loopback host ({allowed}); Office never binds publicly")
        return value

    @field_validator("web_root")
    @classmethod
    def _absolute_web_root(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        if not value.is_absolute():
            raise ValueError("must be an absolute path, so it cannot follow the process's cwd")
        return value

    @property
    def sidecar_path(self) -> Path:
        """The Office-owned SQLite sidecar, separate from the CLI's ``context.db``.

        Under the resolved home so one Office per home has one store, and named
        rather than derived at each call site so P02 and P06 cannot disagree.
        """
        return self.home / "office" / "observations.sqlite3"

    @property
    def base_url(self) -> str:
        """The loopback URL the browser is pointed at (without the token)."""
        return f"http://{self.host}:{self.port}"

    @property
    def team_os_base_url(self) -> str:
        """The peer's loopback base URL. Meaningful only when enabled."""
        return f"http://{self.team_os_host}:{self.team_os_port}"

    @classmethod
    def from_mapping(cls, values: Mapping[str, object], *, home: Path) -> OfficeConfig:
        """Build from already-typed values. Pure, and strict about unknown keys."""
        unknown = sorted(set(values) - set(cls.model_fields) - {"home"})
        if unknown:
            raise OfficeConfigError(
                f"unknown Office configuration key(s): {', '.join(unknown)}. "
                f"Known keys: {', '.join(sorted(cls.model_fields))}"
            )
        payload = dict(values)
        payload["home"] = home
        try:
            return cls.model_validate(payload)
        except ValidationError as exc:
            raise OfficeConfigError(_first_problem(exc)) from exc

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str],
        *,
        home: Path | None = None,
        home_resolver: Callable[[], Path] | None = None,
    ) -> OfficeConfig:
        """Build from an environment mapping, reading only the allow-list.

        ``home`` wins; otherwise ``home_resolver`` is called. Both are injected
        so a test never needs the real home, and so this function has no
        opinion about ``AISQUARE_HOME`` — resolving that is
        ``core.paths.aisquare_home``'s job and its rules (including the
        Windows-mount caveat recorded there) stay in one place.
        """
        if home is None:
            if home_resolver is None:
                raise OfficeConfigError("from_environment needs either home or home_resolver")
            home = home_resolver()

        values: dict[str, object] = {}
        for variable, name in ENV_FIELDS.items():
            raw = env.get(variable)
            if raw is None:
                continue
            values[name] = _coerce(variable, name, raw)
        return cls.from_mapping(values, home=home)


def _coerce(variable: str, name: str, raw: str) -> object:
    """One environment string as the field's type, or an error naming the variable.

    The raw text never appears in the message: a value in the wrong variable is
    sometimes a token, and this is the moment it would otherwise be written to
    a log that outlives the mistake.
    """
    text = raw.strip()
    if name in _BOOL_FIELDS:
        lowered = text.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise OfficeConfigError(f"{variable} must be one of 1/0, true/false, yes/no, on/off")
    if name in _INT_FIELDS:
        try:
            return int(text)
        except ValueError as exc:
            raise OfficeConfigError(f"{variable} must be a whole number") from exc
    if name in _FLOAT_FIELDS:
        try:
            return float(text)
        except ValueError as exc:
            raise OfficeConfigError(f"{variable} must be a number of seconds") from exc
    if name in _PATH_FIELDS:
        if not text:
            raise OfficeConfigError(f"{variable} must be a path, or be unset")
        return Path(text)
    if not text:
        raise OfficeConfigError(f"{variable} must not be empty; unset it instead")
    return text


def _first_problem(exc: ValidationError) -> str:
    """One actionable sentence from a validation failure, naming the setting.

    Pydantic's own message embeds the offending input. It is rebuilt here from
    the location and the reason alone so that a value which should never be
    written down is not written down.
    """
    errors: list[Any] = list(exc.errors())
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return "invalid Office configuration"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "configuration"
    variable = next((name for name, field in ENV_FIELDS.items() if field == location), None)
    named = f"{location} (set by {variable})" if variable else location
    return f"Office configuration: {named} {first.get('msg', 'is invalid')}"
