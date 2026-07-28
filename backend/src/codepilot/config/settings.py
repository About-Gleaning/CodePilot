from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class StorageSettings(BaseModel):
    codepilot_home: str = "~/codepilot"


ReasoningEffortValue = Literal["none", "minimal", "low", "medium", "high", "xhigh"]
ThinkingBooleanValue = Literal["on", "off"]
ThinkingValue = ReasoningEffortValue | ThinkingBooleanValue


class ThinkingSettings(BaseModel):
    kind: Literal["reasoning_effort", "extra_body_boolean"]
    allowed_values: list[ThinkingValue] = Field(default_factory=list)
    default_value: ThinkingValue
    extra_body_key: str | None = None

    @model_validator(mode="after")
    def validate_thinking_values(self) -> "ThinkingSettings":
        if not self.allowed_values:
            raise ValueError("thinking.allowed_values 不能为空")
        if self.default_value not in self.allowed_values:
            raise ValueError("thinking.default_value 必须属于 allowed_values")
        if self.kind == "reasoning_effort":
            invalid = [value for value in self.allowed_values if value in {"on", "off"}]
            if invalid:
                raise ValueError("reasoning_effort 只能使用 none/minimal/low/medium/high/xhigh")
            if self.extra_body_key:
                raise ValueError("reasoning_effort 不需要配置 extra_body_key")
        if self.kind == "extra_body_boolean":
            invalid = [value for value in self.allowed_values if value not in {"on", "off"}]
            if invalid:
                raise ValueError("extra_body_boolean 只能使用 on/off")
            if not self.extra_body_key:
                raise ValueError("extra_body_boolean 必须配置 extra_body_key")
        return self


class LLMModelSettings(BaseModel):
    id: str
    thinking: ThinkingSettings | None = None


class LLMProviderSettings(BaseModel):
    label: str
    models: list[LLMModelSettings] = Field(default_factory=list)
    litellm_model_prefix: str = ""

    @field_validator("models", mode="before")
    @classmethod
    def normalize_models(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, str):
                normalized.append({"id": item})
                continue
            normalized.append(item)
        return normalized

    @model_validator(mode="after")
    def validate_models(self) -> "LLMProviderSettings":
        if not self.models:
            raise ValueError("LLM provider 至少需要配置一个 model")
        ids = [model.id for model in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("LLM provider 下的 model id 不能重复")
        return self


class ActivatedLLMProvider(BaseModel):
    provider: str
    label: str
    models: list[str]
    model_settings: dict[str, LLMModelSettings] = Field(default_factory=dict)
    litellm_model_prefix: str = ""
    required_env_vars: list[str] = Field(default_factory=list)


class LLMRuntimeSettings(BaseModel):
    activated_providers: dict[str, ActivatedLLMProvider] = Field(default_factory=dict)


class LLMSettings(BaseModel):
    providers: dict[str, LLMProviderSettings] = Field(default_factory=dict)
    max_tokens: int = 4096
    stream: bool = True
    log_requests: bool = False
    title_provider: str = "qwen"
    title_model: str = "qwen3.5-flash"


class AgentSettings(BaseModel):
    default_agent_name: str = "build"
    max_loop_iterations: int = 50
    subagent_max_loop_iterations: int = 8


class ContextModelThresholdSettings(BaseModel):
    trigger_tokens: int | None = 120000
    trigger_ratio: float | None = None
    context_window_tokens: int | None = None


class LLMSummaryCompressionSettings(BaseModel):
    enabled: bool = True
    summary_max_tokens: int = 2048


class ToolResultPlaceholderSettings(BaseModel):
    enabled: bool = True
    keep_latest_tool_results: int = 5


class ContextCompressionStrategiesSettings(BaseModel):
    llm_summary: LLMSummaryCompressionSettings = Field(default_factory=LLMSummaryCompressionSettings)
    tool_result_placeholder: ToolResultPlaceholderSettings = Field(default_factory=ToolResultPlaceholderSettings)


class ContextSettings(BaseModel):
    compression_enabled: bool = False
    model_thresholds: dict[str, ContextModelThresholdSettings] = Field(
        default_factory=lambda: {"default": ContextModelThresholdSettings()}
    )
    latest_rounds_to_keep: int = 3
    strategies: ContextCompressionStrategiesSettings = Field(default_factory=ContextCompressionStrategiesSettings)
    # 兼容旧配置字段，加载后仍统一由 model_thresholds.default 驱动。
    compression_trigger_tokens: int = 120000
    compressed_context_target_tokens: int = 30000

    @model_validator(mode="after")
    def apply_legacy_threshold(self) -> "ContextSettings":
        if "default" not in self.model_thresholds:
            self.model_thresholds["default"] = ContextModelThresholdSettings(
                trigger_tokens=self.compression_trigger_tokens
            )
        return self


class BashToolSettings(BaseModel):
    approval_mode: str = Field(default="all", pattern="^(all|allowlist|none)$")
    allowlist: list[list[str]] = Field(default_factory=list)
    blacklist: list[list[str]] = Field(default_factory=list)
    readonly_allowlist: list[list[str]] = Field(
        default_factory=lambda: [
            ["pwd"],
            ["ls"],
            ["find"],
            ["rg"],
            ["cat"],
            ["sed"],
            ["head"],
            ["tail"],
            ["wc"],
            ["git", "status"],
            ["git", "diff"],
            ["git", "log"],
            ["git", "show"],
        ]
    )
    max_output_chars: int = 50_000


class ToolSettings(BaseModel):
    default_timeout_seconds: int = 120
    default_can_parallel: bool = False
    default_requires_approval: bool = False
    default_error_policy: str = "return_tool_result"
    bash: BashToolSettings = Field(default_factory=BashToolSettings)


class McpServerBaseSettings(BaseModel):
    enabled: bool = True
    requires_approval: bool = True
    timeout_seconds: int = Field(default=120, gt=0)
    max_output_chars: int = Field(default=50_000, gt=0)


class McpStdioServerSettings(McpServerBaseSettings):
    transport: Literal["stdio"]
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env_from_process: dict[str, str] = Field(default_factory=dict)


class McpStreamableHttpServerSettings(McpServerBaseSettings):
    transport: Literal["streamable_http"]
    url: str = Field(min_length=1)
    headers_from_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MCP Streamable HTTP URL 必须是有效的 HTTP(S) 地址")
        if parsed.username or parsed.password:
            raise ValueError("MCP URL 禁止内嵌认证信息，请使用 headers_from_env")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("MCP Streamable HTTP 仅允许 HTTPS，回环地址除外")
        return value


McpServerSettings = Annotated[
    McpStdioServerSettings | McpStreamableHttpServerSettings,
    Field(discriminator="transport"),
]


class McpSettings(BaseModel):
    servers: dict[str, McpServerSettings] = Field(default_factory=dict)

    @field_validator("servers")
    @classmethod
    def validate_server_names(cls, value: dict[str, McpServerSettings]) -> dict[str, McpServerSettings]:
        invalid = [name for name in value if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name)]
        if invalid:
            raise ValueError(f"MCP server 名称非法：{', '.join(invalid)}")
        return value


class HookPluginPromptConfig(BaseModel):
    role: str = "system"
    content: str


class HookPluginDefinition(BaseModel):
    hook_id: str
    hook_type: str
    plugin_type: str
    order: int = 100
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class HooksSettings(BaseModel):
    enabled: bool = True
    default_timeout_seconds: int = 30
    plugins: list[HookPluginDefinition] = Field(default_factory=list)


class MemorySettings(BaseModel):
    type: str = "jsonl"


class SSESettings(BaseModel):
    heartbeat_seconds: int = 15
    replay_on_connect: bool = True


class HumanInTheLoopSettings(BaseModel):
    enabled: bool = True


class LoggingSettings(BaseModel):
    level: str = "INFO"
    format: str = "json"
    redact_secrets: bool = True


class AppSettings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    llm_runtime: LLMRuntimeSettings = Field(default_factory=LLMRuntimeSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    hooks: HooksSettings = Field(default_factory=HooksSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    sse: SSESettings = Field(default_factory=SSESettings)
    human_in_the_loop: HumanInTheLoopSettings = Field(default_factory=HumanInTheLoopSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _provider_env_requirements() -> dict[str, tuple[str, ...]]:
    return {
        "openai": ("OPENAI_API_KEY",),
        "qwen": ("QWEN_API_KEY", "QWEN_BASE_URL"),
        "deepseek": ("DEEPSEEK_API_KEY",),
    }


def build_llm_runtime_settings(
    llm_settings: LLMSettings,
    environ: Mapping[str, str] | None = None,
) -> LLMRuntimeSettings:
    source_env = environ or os.environ
    activated_providers: dict[str, ActivatedLLMProvider] = {}

    for provider, provider_settings in llm_settings.providers.items():
        required_env_vars = list(_provider_env_requirements().get(provider, ()))
        if required_env_vars and any(not source_env.get(key) for key in required_env_vars):
            continue
        if not required_env_vars:
            continue
        activated_providers[provider] = ActivatedLLMProvider(
            provider=provider,
            label=provider_settings.label,
            models=[model.id for model in provider_settings.models],
            model_settings={model.id: model for model in provider_settings.models},
            litellm_model_prefix=provider_settings.litellm_model_prefix,
            required_env_vars=required_env_vars,
        )

    return LLMRuntimeSettings(activated_providers=activated_providers)


def resolve_llm_selection(
    settings: AppSettings,
    requested_provider: str | None,
    requested_model: str | None,
) -> tuple[ActivatedLLMProvider, str]:
    runtime = settings.llm_runtime
    if not runtime.activated_providers:
        raise ValueError("当前没有已激活的 LLM 厂商，请先检查 backend/.env 配置")

    provider = requested_provider
    if not provider:
        raise ValueError("必须显式传入 provider")

    activated_provider = runtime.activated_providers.get(provider)
    if activated_provider is None:
        raise ValueError(f"provider `{provider}` 未激活或不存在")

    model = requested_model
    if not model:
        raise ValueError("必须显式传入 model")
    if model not in activated_provider.models:
        raise ValueError(f"model `{model}` 不属于 provider `{provider}` 的已配置模型")

    return activated_provider, model


def resolve_thinking_value(
    settings: AppSettings,
    provider: str,
    model: str,
    metadata: Mapping[str, Any],
) -> str | None:
    """解析并校验用户选择的模型思考档位。

    thinking_enabled 是旧布尔协议：true 映射到模型默认值，false 表示不传参数。
    thinking_value 是新协议：只有模型声明支持该值时才允许进入 LLM 调用链。
    """
    activated_provider = settings.llm_runtime.activated_providers.get(provider)
    if activated_provider is None:
        raise ValueError(f"provider `{provider}` 未激活或不存在")
    model_settings = activated_provider.model_settings.get(model)
    thinking_settings = model_settings.thinking if model_settings else None

    raw_value = metadata.get("thinking_value")
    if raw_value is None:
        if metadata.get("thinking_enabled") is True and thinking_settings:
            return str(thinking_settings.default_value)
        return None
    if raw_value == "":
        return None
    if not isinstance(raw_value, str):
        raise ValueError("metadata.thinking_value 必须是字符串")
    if thinking_settings is None:
        raise ValueError(f"model `{model}` 未配置 thinking 能力，不能设置 thinking_value")
    if raw_value not in thinking_settings.allowed_values:
        allowed = ", ".join(str(value) for value in thinking_settings.allowed_values)
        raise ValueError(f"thinking_value `{raw_value}` 不属于 model `{model}` 的可选值：{allowed}")
    return raw_value


def load_settings(
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppSettings:
    if config_path is None or not config_path.exists():
        raise ValueError("未找到 backend/config.yaml，无法启动 CodePilot")

    settings = AppSettings()
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    merged = _merge_dict(settings.model_dump(), loaded)
    resolved_settings = AppSettings.model_validate(merged)
    runtime = build_llm_runtime_settings(resolved_settings.llm, environ=environ)
    return resolved_settings.model_copy(update={"llm_runtime": runtime})
