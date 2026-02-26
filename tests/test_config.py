"""测试配置加载。"""

from kaguya.config import load_config, AppConfig


def test_load_default_config(tmp_path):
    """没有配置文件时应返回默认值。"""
    cfg = load_config(config_dir=tmp_path)
    assert isinstance(cfg, AppConfig)
    assert cfg.persona.name == "辉夜姬"
    assert cfg.memory.working_memory_size == 50
    assert cfg.desktop.enabled is True


def test_load_with_toml(tmp_path):
    """从 TOML 文件加载覆盖默认值。"""
    (tmp_path / "default.toml").write_bytes(
        b'[memory]\nworking_memory_size = 20\n'
    )
    cfg = load_config(config_dir=tmp_path)
    assert cfg.memory.working_memory_size == 20
    assert cfg.memory.l1_max == 100  # 其余保持默认


def test_api_key_injection(tmp_path):
    """secrets.toml 中的 api_keys 应注入到 llm 配置。"""
    (tmp_path / "default.toml").write_bytes(
        b'[llm.primary]\nbase_url = "https://example.com"\nmodel = "gpt-4"\n'
    )
    (tmp_path / "secrets.toml").write_bytes(
        b'[api_keys]\nprimary = "sk-test-key"\n'
    )
    cfg = load_config(config_dir=tmp_path)
    assert cfg.llm.primary.api_key == "sk-test-key"
