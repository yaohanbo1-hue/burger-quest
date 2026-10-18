import pytest

from burger_quest.mcp_base import scene_for_mode
from burger_quest.mcp_mock import MockMCPClient


@pytest.fixture()
def client():
    return MockMCPClient()


@pytest.fixture()
def scene():
    return scene_for_mode("takeaway", "DEMO-001")


@pytest.fixture()
def menu(client, scene):
    """带名称与价格的菜单（等价于服务端「骨架+详情+价格发现」的结果）。"""
    return client.enriched_menu(scene)


@pytest.fixture()
def coupons(client, scene):
    return client.store_coupons(scene) + client.my_coupons()


@pytest.fixture()
def nutrition(client):
    return client.list_nutrition_foods()


@pytest.fixture(autouse=True)
def _tests_never_read_developer_env(monkeypatch):
    """测试预设环境 = 无 Token、不读开发者机器的真实 .env（与 CI 一致）。

    2026-10-18 背景：用户在仓库根配置了真实 .env 供网页端联调，随后 14 个
    「缺 Token 应明确失败」用例（test_cli/test_doctor/test_failure_matrix/
    test_fault_injection/test_http_client/test_mcp_healthcheck/test_security_audit/
    test_smoke）全部意外捡到真实 Token 而失败——CI 环境无 .env，此前一直全绿。
    修复是恢复测试预设环境，不是削弱断言：需要 Token 的用例自行 setenv 或
    用 mock 客户端。test_dotenv.py 显式传路径调用加载器，不受本 fixture 影响。
    """
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    from burger_quest import dotenv as _dotenv

    real_find = _dotenv.find_env_file

    def _guarded_find(start=None):
        # 无参 = 「从 cwd 向上自动发现」，测试中一律视为没有 .env；
        # 显式传路径（test_dotenv.py）走真实逻辑。
        return real_find(start) if start is not None else None

    monkeypatch.setattr(_dotenv, "find_env_file", _guarded_find)
