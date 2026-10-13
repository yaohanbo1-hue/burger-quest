import pytest

from burger_quest.mcp_mock import MockMCPClient


@pytest.fixture()
def client():
    return MockMCPClient()


@pytest.fixture()
def menu(client):
    return client.query_meals("DEMO-001")


@pytest.fixture()
def coupons(client):
    return client.store_coupons("DEMO-001") + client.my_coupons()


@pytest.fixture()
def nutrition(client):
    return client.list_nutrition_foods()
