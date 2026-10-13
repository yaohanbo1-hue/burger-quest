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
