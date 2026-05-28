from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import BaseModel, Field, model_validator


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class StorageSettings(BaseModel):
    codepilot_home: str = "~/codepilot"


class LLMProviderSettings(BaseModel):
    label: str
    models: list[str] = Field(default_factory=list)
    litellm_model_prefix: str = ""

    @model_validator(mode="after")
    def validate_models(self) -> "LLMProviderSettings":
        if not self.models:
            raise ValueError("LLM provider 至少需要配置一个 model")
        return self


class ActivatedLLMProvider(BaseModel):
    provider: str
    label: str
    models: list[str]
    litellm_model_prefix: str = ""
    required_env_vars: list[str] = Field(default_factory=list)


class LLMRuntimeSettings(BaseModel):
    activated_providers: dict[str, ActivatedLLMProvider] = Field(default_factory=dict)


class LLMSettings(BaseModel):
    providers: dict[str, LLMProviderSettings] = Field(default_factory=dict)
    max_tokens: int = 4096
    temperature: float = 0
    stream: bool = True


class AgentSettings(BaseModel):
    default_agent_name: str = "build"
    max_loop_iterations: int = 50


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


class ToolSettings(BaseModel):
    default_timeout_seconds: int = 120
    default_can_parallel: bool = False
    default_requires_approval: bool = False
    default_error_policy: str = "return_tool_result"


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
            models=list(provider_settings.models),
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
