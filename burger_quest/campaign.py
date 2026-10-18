"""campaign-calendar 返回解析（官方返回 Markdown 文本，非 JSON 信封）。

2026-10-18 真实联调实测结构：

    ### 当前时间：2026-10-18 11:39:42

    ### 活动列表：

    #### 2026年10月18日 今日

    -   **活动标题**：超值9.9元早餐两件套陪你开工啦😋
        **活动内容介绍**：早八的快乐……
        （可多行）
        **活动图片介绍**：
        <img src="https://cms-cdn.mcd.cn/..." alt="" height="300" width="auto">

    #### 2026年10月19日     ← 未来日期（无「今日」标记）

设计原则：
- **只解析官方字段**：标题/内容/图片/日期由官方文本给出；不派生奖品、优惠金额、资格。
- 状态只依据「今日」标记与日期比较（today 取官方「当前时间」，缺省用本机日期）。
- 任何格式变化都返回已识别的部分，绝不编造活动。
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

from .models import Campaign

_TIME_RE = re.compile(r"^###\s*当前时间[：:]\s*(\d{4})-(\d{1,2})-(\d{1,2})")
_DATE_RE = re.compile(r"^####\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*(今日)?")
_TITLE_RE = re.compile(r"^\s*-\s*\*\*活动标题\*\*\s*[：:]\s*(.+?)\s*$")
_CONTENT_RE = re.compile(r"^\s*\*\*活动内容介绍\*\*\s*[：:]\s*(.*)$")
_IMAGE_RE = re.compile(r"^\s*\*\*活动图片介绍\*\*\s*[：:]?\s*$")
_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"')


def parse_campaign_text(text: str, today: Optional[_dt.date] = None) -> list[Campaign]:
    """把官方 Markdown 文本解析为 Campaign 列表（保持官方顺序）。

    无法识别的行一律跳过——只输出有明确标题的活动。
    """
    if not text or "活动列表" not in text and "活动标题" not in text:
        return []

    official_today = today
    m = _TIME_RE.search(text)
    if m:
        try:
            official_today = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            official_today = today
    if official_today is None:
        official_today = _dt.date.today()

    campaigns: list[Campaign] = []
    cur_date: Optional[_dt.date] = None
    cur_is_today = False
    title: Optional[str] = None
    content_lines: list[str] = []
    image_url = ""
    in_content = False
    in_image = False

    def flush() -> None:
        nonlocal title, content_lines, image_url, in_content, in_image
        if title and cur_date is not None:
            status = "live" if cur_is_today else ("past" if cur_date < official_today else "upcoming")
            campaigns.append(
                Campaign(
                    date=cur_date.isoformat(),
                    title=title.strip(),
                    content="\n".join(line for line in content_lines if line.strip()).strip(),
                    image_url=image_url,
                    status=status,
                )
            )
        title = None
        content_lines = []
        image_url = ""
        in_content = False
        in_image = False

    for raw in text.splitlines():
        line = raw.rstrip()
        dm = _DATE_RE.match(line)
        if dm:
            flush()
            try:
                cur_date = _dt.date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            except ValueError:
                cur_date = None
            cur_is_today = bool(dm.group(4))
            continue
        tm = _TITLE_RE.match(line)
        if tm:
            flush()
            title = tm.group(1)
            continue
        cm = _CONTENT_RE.match(line)
        if cm and title:
            in_content, in_image = True, False
            if cm.group(1).strip():
                content_lines.append(cm.group(1).strip())
            continue
        if _IMAGE_RE.match(line) and title:
            in_image, in_content = True, False
            continue
        img = _IMG_SRC_RE.search(line)
        if img and title:
            image_url = img.group(1)
            continue
        if title and in_content and line.strip():
            content_lines.append(line.strip())

    flush()
    return campaigns


def summarize(campaigns: list[Campaign]) -> dict:
    """按状态计数（供 CLI/Web 展示；不做任何派生结论）。"""
    counts = {"live": 0, "upcoming": 0, "past": 0}
    for c in campaigns:
        if c.status in counts:
            counts[c.status] += 1
    return counts
