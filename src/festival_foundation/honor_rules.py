"""节日坚守人员评选的可冻结规则与合并/公开裁剪逻辑。

规则以版本形式发布：批次创建时绑定当前版本，未完成批次可显式刷新到新版本，
批次终局后规则版本锁定，因此规则变化只影响未完成批次。
"""

from __future__ import annotations

import re
from typing import Any

from .audit import digest

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 候选记录可以公开的全部字段；候选人的同意范围只能取其中子集。
PUBLIC_FIELDS = (
    "candidate_name",
    "organization",
    "department",
    "post_name",
    "event_date",
    "event_location",
    "deed_summary",
)

DEFAULT_PARAMS: dict[str, Any] = {
    # 一条事件至少需要多少个相互独立的事实来源才能核验达标
    "min_independent_sources": 2,
    # 核验人被分派后的核验期限（小时）
    "verify_hours": 48,
    # 每次及时补证后核验期限顺延的小时数
    "supplement_extension_hours": 24,
    # 批次开放的推荐时间窗（小时）
    "nomination_window_hours": 72,
    "allowed_public_fields": list(PUBLIC_FIELDS),
    # 敏感岗位在公开输出中必须遮罩的字段
    "sensitive_mask_fields": ["department", "post_name", "event_location"],
}

INT_KEYS = ("min_independent_sources", "verify_hours",
            "supplement_extension_hours", "nomination_window_hours")


def normalize_params(raw: dict[str, Any] | None) -> dict[str, Any]:
    """校验并补全一次规则发布的参数。"""

    raw = dict(raw or {})
    params = {key: raw.get(key, default) for key, default in DEFAULT_PARAMS.items()}
    for key in INT_KEYS:
        value = params[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{key} 必须是正整数")
    fields = params["allowed_public_fields"]
    if not isinstance(fields, list) or not fields or any(f not in PUBLIC_FIELDS for f in fields):
        raise ValueError("allowed_public_fields 必须是公开字段的非空子集")
    masked = params["sensitive_mask_fields"]
    if not isinstance(masked, list) or any(f not in PUBLIC_FIELDS for f in masked):
        raise ValueError("sensitive_mask_fields 必须是公开字段的子集")
    return params


def validate_event_date(value: str) -> str:
    value = str(value).strip()
    if not DATE.fullmatch(value):
        raise ValueError("日期必须使用 YYYY-MM-DD 格式")
    return value


def merge_key(*, candidate_id: str, event_date: str, event_location: str) -> str:
    """依据冻结规则生成同一值守事件的稳定合并键。

    同一候选人、同一值守日期、同一场所即视为同一事件，部门重复推荐会合并，
    但各方推荐作为独立贡献保留。
    """

    material = {
        "candidate_id": candidate_id.strip(),
        "event_date": validate_event_date(event_date),
        "event_location": event_location.strip(),
    }
    return digest(material)


def build_public_entry(*, candidate_name: str, organization: str, department: str,
                       post_name: str, event_date: str, event_location: str,
                       deed_summary: str, consent_scope: list[str],
                       sensitive: bool, params: dict[str, Any]) -> dict[str, str]:
    """按规则白名单、候选人同意范围和敏感岗位遮罩裁剪公开字段。"""

    full = {
        "candidate_name": candidate_name,
        "organization": organization,
        "department": department,
        "post_name": post_name,
        "event_date": event_date,
        "event_location": event_location,
        "deed_summary": deed_summary,
    }
    allowed = set(params["allowed_public_fields"])
    scope = set(consent_scope)
    masked = set(params["sensitive_mask_fields"]) if sensitive else set()
    return {key: full[key] for key in PUBLIC_FIELDS
            if key in allowed and key in scope and key not in masked}
