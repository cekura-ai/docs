import os
from typing import Optional, List, Set
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv

load_dotenv()


# Tools that are blocked by default because of high blast radius.
# Each has a dedicated opt-in env flag so ops teams can enable them explicitly.
DEFAULT_DANGEROUS_TOOLS: Set[str] = {"projects_destroy"}

# Skill-gate rollout ladder. `off` ships inert (no schema change, no evaluation).
# `shadow` logs would-block volume; `warn` appends a nudge but still proceeds;
# `enforce` HOLDS a gated write with no valid ack (deny + ask-the-user recovery).
# `strict` currently uses `enforce` behavior (tag-redaction hardening deferred).
VALID_SKILL_GATE_MODES = ("off", "shadow", "warn", "enforce", "strict")


class MCPServerConfig(BaseModel):
    base_url: str = Field(default_factory=lambda: os.getenv("CEKURA_BASE_URL", "https://api.cekura.ai"))
    openapi_spec_path: str = Field(default_factory=lambda: os.getenv("CEKURA_OPENAPI_SPEC_PATH", "../openapi.json"))
    max_tools: Optional[int] = Field(default_factory=lambda: _parse_int_env("CEKURA_MAX_TOOLS"))
    expose_project_destroy: bool = Field(default_factory=lambda: _parse_bool_env("CEKURA_EXPOSE_PROJECT_DESTROY", False))
    blocked_tools: List[str] = Field(default_factory=lambda: _parse_list_env("CEKURA_BLOCKED_TOOLS") or [])
    max_examples_per_tool: int = Field(default_factory=lambda: _parse_int_env("CEKURA_MAX_EXAMPLES_PER_TOOL") or 2)
    skill_gate_mode: str = Field(default_factory=lambda: _parse_gate_mode_env())

    def resolve_blocked_tools(self) -> Set[str]:
        """Final set of tool names to suppress at registration."""
        blocked = set(self.blocked_tools)
        if not self.expose_project_destroy:
            blocked.add("projects_destroy")
        return blocked

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v):
        if not v.startswith(("http://", "https://")):
            raise ValueError("CEKURA_BASE_URL must start with http:// or https://")
        return v.rstrip("/")

    @field_validator("openapi_spec_path")
    @classmethod
    def validate_spec_path(cls, v):
        if not os.path.exists(v):
            raise ValueError(f"OpenAPI spec file not found: {v}")
        return v


def _parse_list_env(key: str) -> Optional[List[str]]:
    value = os.getenv(key)
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_int_env(key: str) -> Optional[int]:
    value = os.getenv(key)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got: {value}")


def _parse_bool_env(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _parse_gate_mode_env() -> str:
    """Resolve CEKURA_SKILL_GATE_MODE; unknown/absent values fall back to `off`."""
    raw = (os.getenv("CEKURA_SKILL_GATE_MODE") or "off").strip().lower()
    return raw if raw in VALID_SKILL_GATE_MODES else "off"


def load_config() -> MCPServerConfig:
    try:
        config = MCPServerConfig()
        return config
    except Exception as e:
        raise RuntimeError(f"Configuration error: {e}")
