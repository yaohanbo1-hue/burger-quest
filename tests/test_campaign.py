"""campaign-calendar Markdown 解析与降级行为测试（离线，无需 Token）。"""
import datetime as dt

from burger_quest.campaign import parse_campaign_text, summarize

SAMPLE = """### 当前时间：2026-10-18 11:39:42

### 活动列表：


#### 2026年10月18日 今日


-   **活动标题**：超值9.9元早餐两件套
    **活动内容介绍**：早八的快乐，一堡一咖已就位
10月8日至10月21日
    **活动图片介绍**：
    <img src="https://cms-cdn.mcd.cn/img/a.jpg" alt="" height="300" width="auto">


-   **活动标题**：韩式风味新品
    **活动内容介绍**：龙焰鸡腿堡三件套 26.9 元起
    **活动图片介绍**：
    <img src="https://cms-cdn.mcd.cn/img/b.jpg" alt="">


#### 2026年10月19日


-   **活动标题**：明日活动
    **活动内容介绍**：预告内容
    **活动图片介绍**：


#### 2026年10月01日


-   **活动标题**：往期活动
    **活动内容介绍**：已结束
"""


def test_parses_title_content_image_and_status():
    items = parse_campaign_text(SAMPLE)
    assert len(items) == 4
    assert items[0].date == "2026-10-18" and items[0].status == "live"
    assert items[0].title == "超值9.9元早餐两件套"
    assert "10月8日至10月21日" in items[0].content
    assert items[0].image_url.endswith("a.jpg")
    assert items[1].title == "韩式风味新品"
    assert items[2].status == "upcoming" and items[2].date == "2026-10-19"
    assert items[3].status == "past" and items[3].date == "2026-10-01"


def test_summarize_counts():
    items = parse_campaign_text(SAMPLE)
    # 「今日」段有两条活动 → live 2；10-19 未来 1；10-01 往期 1
    assert summarize(items) == {"live": 2, "upcoming": 1, "past": 1}


def test_empty_or_unrelated_text_returns_empty():
    assert parse_campaign_text("") == []
    assert parse_campaign_text("### 服务不可用") == []
    assert parse_campaign_text("随便一段没有活动的文本") == []


def test_without_official_time_uses_given_today():
    """官方「当前时间」缺失时，用调用方给定的 today 判断状态。"""
    text = SAMPLE.replace("### 当前时间：2026-10-18 11:39:42\n", "")
    items = parse_campaign_text(text, today=dt.date(2026, 10, 18))
    assert items[0].status == "live"  # 「今日」标记优先
    assert items[2].status == "upcoming"
    assert items[3].status == "past"
