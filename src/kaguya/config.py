"""V2 配置系统 — 基于 TOML + dataclasses。

加载顺序（后者覆盖前者）：
  1. config/default.toml     — 基础默认值
  2. config/secrets.toml     — API Key（不入 git）
  3. config/persona.toml     — 人格设定
  4. data/user_mixin.toml    — 用户运行时配置（管理界面写入）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

import tomli_w


# ---------------------------------------------------------------------------
# 子配置块
# ---------------------------------------------------------------------------


@dataclass
class LLMModelConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.7
    max_tokens: int = 8192


@dataclass
class LLMConfig:
    primary: LLMModelConfig = field(default_factory=LLMModelConfig)
    summarizer: LLMModelConfig = field(default_factory=LLMModelConfig)
    agent: LLMModelConfig | None = None  # 子代理模型（可选，默认回退到 summarizer）


@dataclass
class MemoryConfig:
    working_memory_size: int = 50
    l1_max: int = 100
    l1_summarize_batch: int = 20
    l2_max: int = 50
    l2_summarize_batch: int = 10
    l3_max_tokens: int = 2000
    inject_l1_count: int = 10
    inject_l2_count: int = 5
    max_consciousness_logs: int = 200
    max_notes: int = 30
    max_note_length: int = 500
    inject_consciousness_count: int = 5


@dataclass
class ConsciousnessConfig:
    enabled: bool = True
    interval_minutes: int = 30
    jitter_minutes: int = 10
    quiet_hours: list[str] = field(default_factory=lambda: ["23:00", "07:00"])


@dataclass
class NotificationFilter:
    """通知过滤规则。pattern 为正则表达式，匹配 title 或 text。"""
    pattern: str = ""
    target: str = "any"  # "title" / "text" / "any"


@dataclass
class NotificationsConfig:
    poll_interval_seconds: int = 30
    watch_apps: list[str] = field(default_factory=list)
    ignore_apps: list[str] = field(default_factory=list)
    filters: list[NotificationFilter] = field(default_factory=list)


@dataclass
class AdminConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    password: str = ""


@dataclass
class PersonaConfig:
    name: str = "辉夜姬"
    description: str = ""
    personality: str = ""
    speaking_style: str = ""
    interests: list[str] = field(default_factory=list)
    guidelines_default: str = ""
    guidelines_chat: str = ""
    guidelines_notification: str = ""
    guidelines_heartbeat: str = ""

    def get_guidelines(self, kind: str) -> str:
        """获取拼接了 default 前缀的 guidelines。kind: chat/notification/heartbeat。"""
        specific = getattr(self, f"guidelines_{kind}", "").strip()
        default = self.guidelines_default.strip()
        parts = [p for p in (default, specific) if p]
        return "\n".join(parts)


@dataclass
class ImageConfig:
    enabled: bool = False
    model_generate: str = "wanx2.1-t2i-turbo"
    model_edit: str = "wanx2.1-imageedit"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""


@dataclass
class DesktopConfig:
    enabled: bool = True
    screenshot_scale: float = 0.5  # 截图缩放比例
    yolo_model_repo: str = "microsoft/OmniParser-v2.0"
    yolo_model_file: str = "icon_detect/model.pt"
    box_threshold: float = 0.05


@dataclass
class BrowserConfig:
    enabled: bool = False
    mode: str = "local"            # "cdp" | "local" | "cloud"
    cdp_url: str = ""              # CDP 模式: ws://... 或 http://...
    headless: bool = True
    browser_path: str = ""         # local 模式: 浏览器路径（留空自动下载）
    cloud_api_key: str = ""        # cloud 模式: browser-use cloud API key
    cloud_timeout: int = 15        # cloud 模式: 会话超时（分钟）


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    consciousness: ConsciousnessConfig = field(default_factory=ConsciousnessConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    desktop: DesktopConfig = field(default_factory=DesktopConfig)

    # 扩展配置段（[extensions.*]）
    extensions_raw: dict[str, Any] = field(default_factory=dict)

    # 运行时状态：指向 user_mixin.toml 的路径
    _mixin_path: str = ""


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------


def _merge(base: dict, override: dict) -> dict:
    """递归合并两个字典，override 优先。"""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _parse_llm_model(d: dict) -> LLMModelConfig:
    return LLMModelConfig(
        base_url=d.get("base_url", ""),
        model=d.get("model", ""),
        api_key=d.get("api_key", ""),
        temperature=float(d.get("temperature", 0.7)),
        max_tokens=int(d.get("max_tokens", 8192)),
    )


def _parse_filters(raw_list: list[dict]) -> list[NotificationFilter]:
    filters = []
    for item in raw_list:
        filters.append(NotificationFilter(
            pattern=item.get("pattern", ""),
            target=item.get("target", "any"),
        ))
    return filters


# ---------------------------------------------------------------------------
# user_mixin 写入
# ---------------------------------------------------------------------------


def save_user_mixin(cfg: AppConfig, section: str, data: dict) -> None:
    """将某个配置段写入 user_mixin.toml（仅更新指定段）。"""
    mixin_path = Path(cfg._mixin_path)
    existing = _load_toml(mixin_path)
    existing[section] = data
    mixin_path.parent.mkdir(parents=True, exist_ok=True)
    with open(mixin_path, "wb") as f:
        tomli_w.dump(existing, f)


# ---------------------------------------------------------------------------
# 主加载函数
# ---------------------------------------------------------------------------


def load_config(config_dir: str | Path | None = None, data_dir: str | Path | None = None) -> AppConfig:
    """从 config/ 和 data/ 目录加载配置。"""
    if config_dir is None:
        config_dir = Path(__file__).parent.parent.parent / "config"
    config_dir = Path(config_dir)

    if data_dir is None:
        data_dir = Path(__file__).parent.parent.parent / "data"
    data_dir = Path(data_dir)

    mixin_path = data_dir / "user_mixin.toml"

    raw: dict[str, Any] = {}
    for fname in ("default.toml", "secrets.toml", "persona.toml"):
        raw = _merge(raw, _load_toml(config_dir / fname))
    # user_mixin 最后加载，优先级最高
    raw = _merge(raw, _load_toml(mixin_path))

    cfg = AppConfig()
    cfg._mixin_path = str(mixin_path)

    # ── LLM ──────────────────────────────────────────────────────────────
    llm_raw = raw.get("llm", {})
    api_keys = raw.get("api_keys", {})

    primary_raw = dict(llm_raw.get("primary", {}))
    if not primary_raw.get("api_key") and api_keys.get("primary"):
        primary_raw["api_key"] = api_keys["primary"]
    cfg.llm.primary = _parse_llm_model(primary_raw)

    summarizer_raw = dict(llm_raw.get("summarizer", llm_raw.get("secondary", {})))
    if not summarizer_raw.get("api_key"):
        summarizer_raw["api_key"] = api_keys.get("summarizer", api_keys.get("secondary", ""))
    cfg.llm.summarizer = _parse_llm_model(summarizer_raw)

    # ── LLM Agent（可选，不配则回退到 summarizer）────────────────────────
    agent_raw = llm_raw.get("agent")
    if agent_raw:
        agent_dict = dict(agent_raw)
        if not agent_dict.get("api_key"):
            agent_dict["api_key"] = (
                api_keys.get("agent", "") or cfg.llm.summarizer.api_key
            )
        cfg.llm.agent = _parse_llm_model(agent_dict)

    # ── Memory ────────────────────────────────────────────────────────────
    mem_raw = raw.get("memory", {})
    cfg.memory = MemoryConfig(
        working_memory_size=int(mem_raw.get("working_memory_size", 50)),
        l1_max=int(mem_raw.get("l1_max", 100)),
        l1_summarize_batch=int(mem_raw.get("l1_summarize_batch", 20)),
        l2_max=int(mem_raw.get("l2_max", 50)),
        l2_summarize_batch=int(mem_raw.get("l2_summarize_batch", 10)),
        l3_max_tokens=int(mem_raw.get("l3_max_tokens", 2000)),
        inject_l1_count=int(mem_raw.get("inject_l1_count", 10)),
        inject_l2_count=int(mem_raw.get("inject_l2_count", 5)),
        max_consciousness_logs=int(mem_raw.get("max_consciousness_logs", 200)),
        max_notes=int(mem_raw.get("max_notes", 30)),
        max_note_length=int(mem_raw.get("max_note_length", 500)),
        inject_consciousness_count=int(mem_raw.get("inject_consciousness_count", 5)),
    )

    # ── Consciousness ─────────────────────────────────────────────────────
    con_raw = raw.get("consciousness", {})
    cfg.consciousness = ConsciousnessConfig(
        enabled=bool(con_raw.get("enabled", True)),
        interval_minutes=int(con_raw.get("interval_minutes", 30)),
        jitter_minutes=int(con_raw.get("jitter_minutes", 10)),
        quiet_hours=con_raw.get("quiet_hours", ["23:00", "07:00"]),
    )

    # ── Notifications ─────────────────────────────────────────────────────
    notif_raw = raw.get("notifications", {})
    cfg.notifications = NotificationsConfig(
        poll_interval_seconds=int(notif_raw.get("poll_interval_seconds", 30)),
        watch_apps=notif_raw.get("watch_apps", []),
        ignore_apps=notif_raw.get("ignore_apps", []),
        filters=_parse_filters(notif_raw.get("filters", [])),
    )

    # ── Admin ─────────────────────────────────────────────────────────────
    admin_raw = raw.get("admin", {})
    cfg.admin = AdminConfig(
        enabled=bool(admin_raw.get("enabled", True)),
        host=admin_raw.get("host", "127.0.0.1"),
        port=int(admin_raw.get("port", 8080)),
        password=admin_raw.get("password", ""),
    )

    # ── Image ─────────────────────────────────────────────────────────────
    img_raw = raw.get("image", {})
    img_api_key = img_raw.get("api_key", "") or api_keys.get("image", "")
    if not img_api_key:
        img_api_key = cfg.llm.primary.api_key  # fallback 到主模型 key
    cfg.image = ImageConfig(
        enabled=bool(img_raw.get("enabled", False)),
        model_generate=img_raw.get("model_generate", "wanx2.1-t2i-turbo"),
        model_edit=img_raw.get("model_edit", "wanx2.1-imageedit"),
        base_url=img_raw.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        api_key=img_api_key,
    )

    # ── Browser ────────────────────────────────────────────────────────────
    browser_raw = raw.get("browser", {})
    browser_api_key = browser_raw.get("cloud_api_key", "") or api_keys.get("browser_cloud", "")
    cfg.browser = BrowserConfig(
        enabled=bool(browser_raw.get("enabled", False)),
        mode=browser_raw.get("mode", "local"),
        cdp_url=browser_raw.get("cdp_url", ""),
        headless=bool(browser_raw.get("headless", True)),
        browser_path=browser_raw.get("browser_path", ""),
        cloud_api_key=browser_api_key,
        cloud_timeout=int(browser_raw.get("cloud_timeout", 15)),
    )

    # ── Desktop ─────────────────────────────────────────────────────────────
    desktop_raw = raw.get("desktop", {})
    cfg.desktop = DesktopConfig(
        enabled=bool(desktop_raw.get("enabled", True)),
        screenshot_scale=float(desktop_raw.get("screenshot_scale", 0.5)),
        yolo_model_repo=str(desktop_raw.get("yolo_model_repo", "microsoft/OmniParser-v2.0")),
        yolo_model_file=str(desktop_raw.get("yolo_model_file", "icon_detect/model.pt")),
        box_threshold=float(desktop_raw.get("box_threshold", 0.05)),
    )

    # ── Persona ───────────────────────────────────────────────────────────
    persona_raw = raw.get("persona", {})
    traits = persona_raw.get("traits", {})
    guidelines = persona_raw.get("guidelines", {})
    cfg.persona = PersonaConfig(
        name=persona_raw.get("name", "辉夜姬"),
        description=persona_raw.get("description", ""),
        personality=traits.get("personality", ""),
        speaking_style=traits.get("speaking_style", ""),
        interests=traits.get("interests", []),
        guidelines_default=guidelines.get("default", ""),
        guidelines_chat=guidelines.get("chat", ""),
        guidelines_notification=guidelines.get("notification", ""),
        guidelines_heartbeat=guidelines.get("heartbeat", ""),
    )

    # ── Extensions ─────────────────────────────────────────────────────────
    cfg.extensions_raw = raw.get("extensions", {})

    return cfg
