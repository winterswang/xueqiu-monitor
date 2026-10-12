"""per-stock 的 opencli 时间预算。

## 为什么需要

opencli 单次调用最坏要等 ≈87 秒：插件层限速器的「取锁 2s + 每 30 次一次 45–75s 长停」
加上一次真实执行（见 `xueqiu-crawler/opencli-adapters/lib/ratelimit.js`）。

而一只股票要发**多次** opencli 调用 —— 预取（讨论 + 公告，讨论空还会 sleep 30s 再重试）、
资讯 1 次、回复正文最多 `replies_per_stock`(5) 次。这些**都不在**
`crawler.timeout_seconds` 的覆盖范围内：那个超时只包住 Playwright 回退，opencli 预取跑在
它前面、资讯与回复跑在后面的循环里。

2026-10-12 把单次超时从 20/60/90 统一抬到 120s（`xueqiu-analyzer-skill`）之后，单只股票
最坏的 opencli 时间从 ~700s 涨到 ~1100s；一组 8 只串行时会顶穿 1 小时的 cron 窗口，
与下一组重叠（那正是分组作业最怕的形态）。

## 做法

给「一个**循环**阶段」一个墙钟死线，阶段内每次 opencli 调用**前**问一句；超了就跳过剩下的
（并记日志），而不是让某个卡住的调用把整组拖过窗口。

只约束多调用的循环（预取、回复）；**单次调用不需要预算** —— 它自己就有 120s 超时兜底。

⚠️ **守卫是「调用前检查」，所以每个阶段的实际上界是 `预算 + 最多一次调用`**（检查通过后
发起的那次调用会跑完；重试路径还要多算它那个 30s sleep）。默认值下的真实上界因此是：

    预取(120 + 120) + 资讯(120) + 回复(120 + 120) ≈ 600s/只

典型情况远低于此（多数调用只等 0–12s，长停每 30 次才一次），所以**这是把「无界」收敛成
「有界」**，不是「保证 8 只一定待在 1 小时内」。要更紧就把 `opencli_budget_seconds` 调小 ——
但它必须 ≥90s，否则一遇长停（最坏 87s）就会把整段跳光、反而丢数据。

`seconds <= 0` 表示不限（保持旧行为，便于回滚）。
"""

from __future__ import annotations

import time

# 每个循环阶段的默认预算。取 120s 的理由：要能容下**一次**长停（最坏 87s）加正常工作量，
# 否则一遇到长停就把整个阶段跳光、反而丢数据；同时 120×2 加资讯那 120s 仍能把每只股票
# 的 opencli 上界压在 360s 以内。
DEFAULT_OPENCLI_BUDGET_SECONDS = 120


class OpencliBudget:
    """一个 opencli 阶段的墙钟预算。"""

    def __init__(self, seconds: float | None) -> None:
        self.seconds = float(seconds or 0)
        self._started = time.monotonic()
        self._deadline = self._started + self.seconds if self.seconds > 0 else None

    @property
    def limited(self) -> bool:
        """是否设了上限（`seconds <= 0` 时不限，退化为恒不超时）。"""
        return self._deadline is not None

    def spent(self) -> float:
        """本阶段已花掉的墙钟秒数。"""
        return time.monotonic() - self._started

    def remaining(self) -> float | None:
        """剩余秒数；不限时返回 None。"""
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    def expired(self) -> bool:
        """预算是否已用尽。不限时恒为 False。"""
        return self._deadline is not None and time.monotonic() >= self._deadline
