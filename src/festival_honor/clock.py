"""荣誉核验模块使用的可替换时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta


class MutableClock:
    """可在测试与验收中推进的 UTC 时钟。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("初始时间必须包含时区")
        self._value = value.astimezone()

    def now(self) -> datetime:
        return self._value.astimezone()

    def advance(self, **kwargs: float) -> None:
        self._value = self._value + timedelta(**kwargs)
