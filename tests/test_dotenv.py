"""安全 .env 加载器测试（2026-10-16 Windows 支持新增）。"""
import os

import pytest

from burger_quest import dotenv


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例前清掉可能污染的环境变量。"""
    for key in ("MCD_MCP_TOKEN", "BQ_TEST_A", "BQ_TEST_B"):
        monkeypatch.delenv(key, raising=False)
    yield


def test_parse_basic_and_comments():
    text = (
        "# 注释行\n"
        "\n"
        "BQ_TEST_A=value1\n"
        "export BQ_TEST_B=value2\n"
        "畸形行没有等号\n"
        "KEY WITH SPACE=nope\n"
    )
    parsed = dotenv._parse_env_text(text)
    assert parsed == {"BQ_TEST_A": "value1", "BQ_TEST_B": "value2"}


def test_parse_strips_matching_quotes():
    text = 'A="double"\nB=\'single\'\nC="mismatched\'\n'
    parsed = dotenv._parse_env_text(text)
    assert parsed["A"] == "double"
    assert parsed["B"] == "single"
    assert parsed["C"] == '"mismatched\''  # 不匹配引号不剥


def test_load_dotenv_applies_without_override(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MCD_MCP_TOKEN=from-file\nBQ_TEST_A=file-a\n", encoding="utf-8")
    # 进程环境变量优先：已存在的绝不覆盖
    monkeypatch.setenv("MCD_MCP_TOKEN", "from-process")
    applied = dotenv.load_dotenv(tmp_path)
    assert "BQ_TEST_A" in applied and "MCD_MCP_TOKEN" not in applied
    assert os.environ["MCD_MCP_TOKEN"] == "from-process"
    assert os.environ["BQ_TEST_A"] == "file-a"


def test_load_dotenv_missing_file_is_silent(tmp_path):
    assert dotenv.load_dotenv(tmp_path) == {}
    assert dotenv.find_env_file(tmp_path) is None


def test_find_env_file_uplevel(tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (tmp_path / ".env").write_text("X=1", encoding="utf-8")
    found = dotenv.find_env_file(nested)
    assert found == (tmp_path / ".env")


def test_find_env_file_respects_max_uplevel(tmp_path):
    """超过 MAX_UPLEVEL 的 .env 不读（避免误读无关项目配置）。"""
    deep = tmp_path
    for i in range(dotenv.MAX_UPLEVEL + 3):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    (tmp_path / ".env").write_text("X=1", encoding="utf-8")
    assert dotenv.find_env_file(deep) is None
