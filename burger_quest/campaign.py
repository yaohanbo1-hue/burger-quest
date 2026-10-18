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
from dataclasses import dataclass, field
from typing import Optional

from .models import Campaign

_TIME_RE = re.compile(r"^###\s*当前时间[：:]\s*(\d{4})-(\d{1,2})-(\d{1,2})")
_DATE_RE = re.compile(r"^####\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*(今日)?")
_TITLE_RE = re.compile(r"^\s*-\s*\*\*活动标题\*\*\s*[：:]\s*(.+?)\s*$")
_CONTENT_RE = re.compile(r"^\s*\*\*活动内容介绍\*\*\s*[：:]\s*(.*)$")
_IMAGE_RE = re.compile(r"^\s*\*\*活动图片介绍\*\*\s*[：:]?\s*$")
_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"')
# 「官方明示无活动」的精确措辞（不是泛化的「暂无」——后者可能出现在别处）
_EMPTY_HINTS = ("暂无活动", "无活动", "没有活动", "暂无任何活动")


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


@dataclass(frozen=True)
class CampaignParseResult:
    """活动文本解析结果：显式区分「有效空」与「无法识别」。

    state:
      ok         — 识别到官方结构且有活动（可附带 reason 说明未识别条目）
      empty      — 识别到官方结构、无活动，且文本明确表示没有活动
      parse_error— 无法确认活动列表是否完整（无结构标志/结构在但解析不出活动）
    注意：**不能**把 parse_campaign_text() 返回空列表直接当成有效空结果。
    """

    state: str
    campaigns: list[Campaign] = field(default_factory=list)
    reason: str = ""


def parse_campaign_result(text: str, today: Optional[_dt.date] = None) -> CampaignParseResult:
    """解析官方 Markdown，并给出显式状态（ok / empty / parse_error）。"""
    raw = text or ""
    campaigns = parse_campaign_text(raw, today)

    has_structure = bool(_DATE_RE.search(raw) or "活动列表" in raw)
    if not has_structure:
        return CampaignParseResult(
            state="parse_error",
            campaigns=campaigns,
            reason="未检测到官方活动结构（无日期段、也无「活动列表」标题）",
        )
    if campaigns:
        reason = ""
        declared = raw.count("**活动标题**")
        if declared > len(campaigns):
            reason = f"{declared - len(campaigns)} 条活动标题未能解析（可能格式变化），仅保留已识别条目"
        return CampaignParseResult(state="ok", campaigns=campaigns, reason=reason)
    if any(h in raw for h in _EMPTY_HINTS):
        return CampaignParseResult(state="empty", campaigns=[], reason="官方明示：当前没有活动")
    return CampaignParseResult(
        state="parse_error",
        campaigns=[],
        reason="检测到活动结构但未解析出任何活动（可能官方格式已变化），无法确认列表是否完整",
    )


def summarize(campaigns: list[Campaign]) -> dict:
    """按状态计数（供 CLI/Web 展示；不做任何派生结论）。"""
    counts = {"live": 0, "upcoming": 0, "past": 0}
    for c in campaigns:
        if c.status in counts:
            counts[c.status] += 1
    return counts
