"""本地虚拟进度持久化（2026-10-17 新增，opt-in 最小安全方案）。

隐私与安全边界（硬约束）：
- **默认不写任何文件**：只有显式提供 `--progress <路径>` 或设置环境变量
  BURGER_QUEST_PROGRESS 时才启用；
- **零个人信息**：进度文件只包含经验值、成就 id、任务签名哈希（sha1）、
  门店编码（公开数据）与计数器——**不包含**用户输入的原始文本、位置、
  手机号、Token 或任何 MCP 响应内容；
- **损坏容忍**：JSON 损坏时备份为 `<路径>.corrupt-<时间戳>` 后以全新进度
  开始，绝不静默丢失也不崩溃；
- **版本迁移**：旧版本缺失字段以默认值补齐；未来版本（version 更大）只读
  能理解的字段，并提示；
- **原子写**：先写临时文件再 os.replace，避免半截文件；
- **容量上限**：签名/门店/卡片签名列表设上限，防止无限增长。

明确边界：进度只是**本机虚拟进度**，与麦当劳官方账户无关，不做任何同步，
也不宣称已同步（CLI 与 README 均有明示）。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .achievements import RunProgress, evaluate_run, level_for_xp
from .models import QuestResult

PROGRESS_VERSION = 2
PROGRESS_ENV = "BURGER_QUEST_PROGRESS"
MAX_SIGNATURES = 500       # 任务签名上限（超出丢弃最旧）
MAX_STORES = 100           # 门店编码上限
MAX_CARD_SIGNATURES = 200  # 卡片签名上限


@dataclass
class Progress:
    """本地虚拟进度（可 JSON 序列化；无个人信息）。"""

    xp: int = 0
    unlocked: dict = field(default_factory=dict)       # ach_id -> 首次解锁的 Unix 秒
    seen_signatures: list = field(default_factory=list)  # 已计经验的任务签名（去重）
    stores: list = field(default_factory=list)          # 已规划过的门店编码（去重）
    card_signatures: list = field(default_factory=list)  # 产出过分享卡的任务签名（去重）
    version: int = PROGRESS_VERSION

    # -- 视图 -----------------------------------------------------------------
    def unlocked_ids(self) -> set:
        return set(self.unlocked)

    def level(self) -> tuple[int, str]:
        return level_for_xp(self.xp)

    def snapshot_sets(self) -> tuple[set, set, set, set]:
        """(unlocked, seen_signatures, stores, card_signatures) 四份可变副本。"""
        return (
            set(self.unlocked),
            set(self.seen_signatures),
            set(self.stores),
            set(self.card_signatures),
        )

    # -- 序列化 ---------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "xp": self.xp,
            "unlocked": dict(self.unlocked),
            "seen_signatures": list(self.seen_signatures),
            "stores": list(self.stores),
            "card_signatures": list(self.card_signatures),
        }

    @classmethod
    def from_dict(cls, data: dict) -> tuple["Progress", list[str]]:
        """从字典恢复；返回 (progress, 迁移/警告说明)。"""
        notes: list[str] = []
        if not isinstance(data, dict):
            return cls(), ["存档内容不是对象，已重置"]
        version = data.get("version")
        if not isinstance(version, int):
            notes.append("存档缺少版本号，按旧版迁移（计分与成就保留，历史签名重置）")
            version = 0
        elif version > PROGRESS_VERSION:
            notes.append(f"存档版本（{version}）比当前（{PROGRESS_VERSION}）新：仅加载可识别字段")
        progress = cls(
            xp=int(data.get("xp") or 0),
            unlocked={str(k): int(v) for k, v in (data.get("unlocked") or {}).items() if isinstance(v, (int, float))},
            seen_signatures=[str(s) for s in (data.get("seen_signatures") or [])][-MAX_SIGNATURES:],
            stores=[str(s) for s in (data.get("stores") or [])][-MAX_STORES:],
            card_signatures=[str(s) for s in (data.get("card_signatures") or [])][-MAX_CARD_SIGNATURES:],
            version=PROGRESS_VERSION,
        )
        if version < PROGRESS_VERSION:
            notes.append("存档已迁移到当前版本")
        return progress, notes


def load_progress(path: str) -> tuple[Progress, list[str]]:
    """读取进度文件；不存在 -> 全新；损坏 -> 备份后全新。返回 (progress, 说明)。"""
    p = Path(path)
    if not p.exists():
        return Progress(), []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        backup = p.with_suffix(p.suffix + f".corrupt-{int(time.time())}")
        try:
            os.replace(p, backup)
            return Progress(), [f"存档损坏（{type(e).__name__}），已备份到 {backup.name} 并以全新进度开始"]
        except OSError:
            return Progress(), ["存档损坏且无法备份，以全新进度开始（不修改原文件）"]
    return Progress.from_dict(data)


def save_progress(path: str, progress: Progress) -> None:
    """原子写进度文件（临时文件 + os.replace）。"""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(progress.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def apply_progress(
    progress: Progress,
    result: QuestResult,
    demo: bool,
    used_json: bool = False,
    card_generated: bool = False,
) -> tuple[Progress, RunProgress, list[str]]:
    """把一次任务结算进进度：评估 -> 更新 -> 返回 (progress, RunProgress, 事件行)。

    事件行包含：新解锁成就、等级提升、重复挑战提示、迁移/损坏说明（调用方打印）。
    """
    unlocked, seen, stores, cards = progress.snapshot_sets()
    before_level, _before_title = progress.level()
    run = evaluate_run(
        result,
        demo=demo,
        unlocked=unlocked,
        seen_signatures=seen,
        stores_seen=stores,
        card_signatures=cards,
        used_json=used_json,
        card_generated=card_generated,
    )
    # 持久化更新（去重 + 容量上限，保留插入顺序）
    progress.xp += run.total_xp
    for u in run.unlocks:
        if not u.already:
            progress.unlocked[u.achievement.id] = int(time.time())
    if not run.repeated:
        if run.signature not in progress.seen_signatures:
            progress.seen_signatures.append(run.signature)
            progress.seen_signatures = progress.seen_signatures[-MAX_SIGNATURES:]
    for code in sorted(stores):
        if code not in progress.stores:
            progress.stores.append(code)
    progress.stores = progress.stores[-MAX_STORES:]
    for sig in sorted(cards):
        if sig not in progress.card_signatures:
            progress.card_signatures.append(sig)
    progress.card_signatures = progress.card_signatures[-MAX_CARD_SIGNATURES:]

    after_level, after_title = progress.level()
    events: list[str] = []
    if run.repeated:
        events.append("⏱️ 重复挑战：与历史记录相同，不计经验")
    for u in run.unlocks:
        events.append(u.line())
        if u.lore_line():
            events.append(u.lore_line())
    for chapter in run.chapters:
        events.append(f"📜 剧情推进 · {chapter.name}：{chapter.unlock_text}")
    if run.blocked:
        events.append(
            "🔒 需真实数据的成就未解锁（演示模式或查询失败不冒充实测）："
            + "、".join(a.name for a in run.blocked[:6])
            + ("…" if len(run.blocked) > 6 else "")
        )
    if after_level > before_level:
        events.append(f"🎉 等级提升：LV.{before_level} -> LV.{after_level} {after_title}")
    elif run.total_xp:
        events.append(f"💰 本次经验 +{run.total_xp}（累计 {progress.xp}，LV.{after_level} {after_title}）")
    else:
        events.append("💰 本次经验 +0（累计 %d，LV.%d %s）" % (progress.xp, after_level, after_title))
    return progress, run, events
