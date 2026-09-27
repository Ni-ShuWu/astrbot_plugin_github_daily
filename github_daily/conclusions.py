"""Rotating conclusion sentences for a check result.

Every status keeps its fixed conclusion as the first entry of its pool, so
turning the rotation on changes how often a sentence repeats, never which facts
are reported. All sentences are single-line and stay within the layout produced
by ``ContributionService.format_result``.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

#: The fixed conclusion per status, used while rotation is disabled.
DEFAULT_CONCLUSIONS: dict[str, str] = {
    "coding": "不是摸鱼，正在写代码。",
    "active": "有 GitHub 活动，但暂时不能确认在写代码。",
    "idle": "最近没有检测到公开活动，疑似摸鱼。",
}

#: Rotating pool per status. Each pool opens with the fixed conclusion and holds
#: two sentences for each tone: 中性 / 编译器 / 编程语言 / 开源 / 雌小鬼 / 轻嘲讽.
ROTATING_CONCLUSIONS: dict[str, tuple[str, ...]] = {
    "coding": (
        DEFAULT_CONCLUSIONS["coding"],
        "提交记录摆在这，摸鱼指控不成立。",
        "编译通过，警告暂时还没升级成错误。",
        "gcc 这次没骂人，说明括号至少是配平的。",
        "正在写 Rust，borrow checker 还在审他。",
        "这门语言他写得很顺，因为它就是 Python。",
        "已 push，CI 正在绿。",
        "upstream 又多了一个 commit，开源界因此前进了一毫米。",
        "哼…居然真的在写代码，杂鱼偶尔也会认真一次嘛。",
        "诶～主动提交了，杂鱼今天转性了？♡",
        "行，这次算你写了。别骄傲，有可能是 rebase 出来的。",
        "本次不算摸鱼，算摸完鱼之后的补救。",
    ),
    "active": (
        DEFAULT_CONCLUSIONS["active"],
        "有公开活动，但看不出是不是在写代码。",
        "编译中，暂无输出，也可能只是卡在 make 上。",
        "报了个 warning，还没到 error，先当作在写。",
        "像 JS：说做了点什么，但没人看得出做了什么。",
        "像 Perl：写完之后作者自己也看不懂了。",
        "有 diff，但 diff 全在 README 里。",
        "提交是提交了，提交的是 .gitignore。",
        "有动静哦～不过只是点了下 Star 吧，杂鱼♡",
        "在忙呢～忙成什么样就不知道了，反正不是代码♡",
        "活动是有的，代码是不一定的，摸鱼大概率是确定的。",
        "看出来在忙了，就是不知道在忙什么。",
    ),
    "idle": (
        DEFAULT_CONCLUSIONS["idle"],
        "404 Not Found：activity not found。",
        "gcc 等了他一整天，只等来一个 unused variable 警告。",
        "编译从未开始，所以也从未失败。",
        "一直没有输出，怀疑在等一个永远不会返回的 future。",
        "进程无输出，判定为 defunct。",
        "这仓库的 upstream 已经不太认识他了。",
        "连一个 star 都没点，开源界那点存在感也省了。",
        "诶～一点动静都没有，杂鱼果然是杂鱼♡",
        "全程无提交，杂鱼今天也在装死呢～♡",
        "本地无改动，远端无提交，人间蒸发。",
        "建议用 git commit --amend --no-edit 改一下人生。",
    ),
}


class ConclusionPicker:
    """Draw rotating conclusions from a shuffle bag.

    Drawing from a reshuffled bag rather than calling ``random.choice`` is what
    makes the wording feel like actual rotation: every sentence is used once
    before any of them repeats, and a reshuffle never hands back the sentence
    the previous draw just used.
    """

    def __init__(
        self,
        pools: Mapping[str, Sequence[str]] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        """Initialize the picker, optionally with custom pools or randomness."""
        self._pools = ROTATING_CONCLUSIONS if pools is None else pools
        self._rng = random.Random() if rng is None else rng
        self._bags: dict[str, list[str]] = {}
        self._last: dict[str, str] = {}

    def pick(self, status: str) -> str:
        """Return the next conclusion for ``status``.

        Unknown statuses and empty pools fall back to the fixed conclusion, so a
        caller never ends up without a sentence to print.
        """
        pool = tuple(self._pools.get(status) or ())
        if not pool:
            return DEFAULT_CONCLUSIONS.get(status, DEFAULT_CONCLUSIONS["idle"])
        bag = self._bags.get(status)
        if not bag:
            bag = list(pool)
            self._rng.shuffle(bag)
            previous = self._last.get(status)
            # ``pick`` draws from the end, so a fresh bag must not end with the
            # sentence the previous draw already used.
            if previous is not None and len(bag) > 1 and bag[-1] == previous:
                index = self._rng.randrange(len(bag) - 1)
                bag[-1], bag[index] = bag[index], bag[-1]
            self._bags[status] = bag
        chosen = bag.pop()
        self._last[status] = chosen
        return chosen
