"""
配置加载系统：从 TOML 文件加载项目配置。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from loguru import logger

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


# 项目根目录（从 src/kaguya/config.py 往上三层）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"


@dataclass
class LLMModelConfig:
    """单个 LLM 模型的配置"""

    provider: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o"
    api_key: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    dimensions: Optional[int] = None  # Embedding 模型专用


@dataclass
class LLMConfig:
    """LLM 配置（主模型 + 次级模型 + Embedding）"""

    primary: LLMModelConfig = field(default_factory=LLMModelConfig)
    secondary: LLMModelConfig = field(default_factory=lambda: LLMModelConfig(
        model="gpt-4o-mini", temperature=0.3, max_tokens=2048
    ))
    embedding: LLMModelConfig = field(default_factory=lambda: LLMModelConfig(
        model="text-embedding-3-small", dimensions=1024
    ))


@dataclass
class MemoryConfig:
    """记忆系统配置"""

    short_term_limit: int = 10
    vectorize_threshold: int = 10
    log_hours: int = 24
    retrieval_top_k: int = 5


@dataclass
class ConsciousnessConfig:
    """主动意识配置"""

    enabled: bool = True
    heartbeat_interval_minutes: int = 30
    jitter_seconds: int = 300
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "08:00"


@dataclass
class BrowserConfig:
    """浏览器配置"""

    mode: str = "local"  # local / cloud / cdp
    chrome_path: str = ""
    cloud_proxy_country: str = "us"
    cdp_url: str = ""
    headless: bool = True


@dataclass
class AdminConfig:
    """管理面板配置"""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class PersonaConfig:
    """人格配置"""

    name: str = "辉夜姬"
    age: int = 16
    origin: str = "月球"
    personality: str = ""
    tone: str = ""
    emoji_frequency: str = "moderate"
    speech_examples: list[str] = field(default_factory=list)
    likes: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    refuse_topics: list[str] = field(default_factory=list)
    max_messages_per_proactive_wake: int = 2


@dataclass
class AppConfig:
    """应用总配置"""

    llm: LLMConfig = field(default_factory=LLMConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    consciousness: ConsciousnessConfig = field(default_factory=ConsciousnessConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)


def _deep_get(d: dict, *keys: str, default: Any = None) -> Any:
    """从嵌套字典中安全地获取值"""
    for key in keys:
        if isinstance(d, dict):
            d = d.get(key, default)
        else:
            return default
    return d


def _load_toml(path: Path) -> dict:
    """加载 TOML 文件"""
    if not path.exists():
        logger.warning(f"配置文件不存在: {path}")
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config(
    config_dir: Optional[Path] = None,
) -> AppConfig:
    """
    加载完整的应用配置。

    加载顺序：
    1. config/default.toml — 默认配置
    2. config/secrets.toml — API Keys（可选）
    3. config/persona.toml — 人格定义
    """
    config_dir = config_dir or CONFIG_DIR

    # 1. 加载默认配置
    defaults = _load_toml(config_dir / "default.toml")

    # 2. 加载 secrets
    secrets = _load_toml(config_dir / "secrets.toml")

    # 3. 加载 persona
    persona_data = _load_toml(config_dir / "persona.toml")

    # 构建 LLM 配置
    llm_config = LLMConfig(
        primary=LLMModelConfig(
            provider=_deep_get(defaults, "llm", "primary", "provider", default="openai"),
            base_url=_deep_get(defaults, "llm", "primary", "base_url", default="https://api.openai.com/v1"),
            model=_deep_get(defaults, "llm", "primary", "model", default="gpt-4o"),
            api_key=_deep_get(secrets, "api_keys", "primary", default=""),
            temperature=_deep_get(defaults, "llm", "primary", "temperature", default=0.7),
            max_tokens=_deep_get(defaults, "llm", "primary", "max_tokens", default=4096),
        ),
        secondary=LLMModelConfig(
            provider=_deep_get(defaults, "llm", "secondary", "provider", default="openai"),
            base_url=_deep_get(defaults, "llm", "secondary", "base_url", default="https://api.openai.com/v1"),
            model=_deep_get(defaults, "llm", "secondary", "model", default="gpt-4o-mini"),
            api_key=_deep_get(secrets, "api_keys", "secondary", default=""),
            temperature=_deep_get(defaults, "llm", "secondary", "temperature", default=0.3),
            max_tokens=_deep_get(defaults, "llm", "secondary", "max_tokens", default=2048),
        ),
        embedding=LLMModelConfig(
            provider=_deep_get(defaults, "llm", "embedding", "provider", default="openai"),
            base_url=_deep_get(defaults, "llm", "embedding", "base_url", default="https://api.openai.com/v1"),
            model=_deep_get(defaults, "llm", "embedding", "model", default="text-embedding-3-small"),
            api_key=_deep_get(secrets, "api_keys", "embedding", default=""),
            dimensions=_deep_get(defaults, "llm", "embedding", "dimensions", default=1024),
        ),
    )

    # 构建 Persona 配置
    persona_config = PersonaConfig(
        name=_deep_get(persona_data, "identity", "name", default="辉夜姬"),
        age=_deep_get(persona_data, "identity", "age", default=16),
        origin=_deep_get(persona_data, "identity", "origin", default="月球"),
        personality=_deep_get(persona_data, "identity", "personality", default=""),
        tone=_deep_get(persona_data, "speech_style", "tone", default=""),
        emoji_frequency=_deep_get(persona_data, "speech_style", "emoji_frequency", default="moderate"),
        speech_examples=_deep_get(persona_data, "speech_style", "examples", default=[]),
        likes=_deep_get(persona_data, "preferences", "likes", default=[]),
        dislikes=_deep_get(persona_data, "preferences", "dislikes", default=[]),
        refuse_topics=_deep_get(persona_data, "boundaries", "refuse_topics", default=[]),
        max_messages_per_proactive_wake=_deep_get(
            persona_data, "boundaries", "max_messages_per_proactive_wake", default=2
        ),
    )

    config = AppConfig(
        llm=llm_config,
        memory=MemoryConfig(
            short_term_limit=_deep_get(defaults, "memory", "short_term_limit", default=10),
            vectorize_threshold=_deep_get(defaults, "memory", "vectorize_threshold", default=10),
            log_hours=_deep_get(defaults, "memory", "log_hours", default=24),
            retrieval_top_k=_deep_get(defaults, "memory", "retrieval_top_k", default=5),
        ),
        consciousness=ConsciousnessConfig(
            enabled=_deep_get(defaults, "consciousness", "enabled", default=True),
            heartbeat_interval_minutes=_deep_get(
                defaults, "consciousness", "heartbeat_interval_minutes", default=30
            ),
            jitter_seconds=_deep_get(defaults, "consciousness", "jitter_seconds", default=300),
            quiet_hours_start=_deep_get(defaults, "consciousness", "quiet_hours_start", default="23:00"),
            quiet_hours_end=_deep_get(defaults, "consciousness", "quiet_hours_end", default="08:00"),
        ),
        browser=BrowserConfig(
            mode=_deep_get(defaults, "browser", "mode", default="local"),
            chrome_path=_deep_get(defaults, "browser", "chrome_path", default=""),
            cloud_proxy_country=_deep_get(defaults, "browser", "cloud_proxy_country", default="us"),
            cdp_url=_deep_get(defaults, "browser", "cdp_url", default=""),
            headless=_deep_get(defaults, "browser", "headless", default=True),
        ),
        admin=AdminConfig(
            enabled=_deep_get(defaults, "admin", "enabled", default=True),
            host=_deep_get(defaults, "admin", "host", default="127.0.0.1"),
            port=_deep_get(defaults, "admin", "port", default=8080),
        ),
        persona=persona_config,
    )

    logger.info(f"配置加载完成: 主模型={config.llm.primary.model}, 次级模型={config.llm.secondary.model}")
    return config
