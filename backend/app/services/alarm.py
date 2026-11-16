"""流告警规则业务规则：状态流转、字段校验与筛选口径都收在这里。

告警判定数据结果（alarmjudg）也收在本模块：同一监控指标并发命中多条规则时，
按告警级别做优先级判定，只生成一份判定结果；静默期内只记录不通知；高级别
规则仍在静默期而低级别规则可通知时，按升级策略生成升级结论。判定结果保留
唯一生效规则、升级原因和通知抑制结果，规则列表、规则详情、通知链路与日志
中心读取的都是同一份数据，写入侧保留来源标识。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "alarm"
JUDGMENT_MODULE = "alarmjudg"
LOG_MODULE = "log"
REQUIRED_FIELDS = ["规则编号", "监控指标", "告警条件"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
ACTION_RULES = {"启用告警": "草稿", "暂停告警": "已启用", "关闭告警": "已暂停"}
NEGATIVE_ACTIONS = []

# 告警级别优先级：P1 最高，未识别的级别排最后。
SEVERITY_ORDER = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
# 告警条件形如 latency_p99>800ms：指标名、比较符、阈值、单位都取自规则字段本身，
# 阈值、口径、规则和判定条件保持同一组业务边界，不引入第二份配置。
CONDITION_RE = re.compile(r"^\s*([\w.]+)\s*(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)\s*(ms|s|秒|分钟|%|个)?\s*$")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
SILENCE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(秒|s|分钟|m|小时|h)?")
COMPARATORS = {
    ">": lambda value, threshold: value > threshold,
    ">=": lambda value, threshold: value >= threshold,
    "<": lambda value, threshold: value < threshold,
    "<=": lambda value, threshold: value <= threshold,
    "==": lambda value, threshold: value == threshold,
    "!=": lambda value, threshold: value != threshold,
}


def _severity_rank(rule: dict[str, Any]) -> int:
    return SEVERITY_ORDER.get(str(rule.get("告警级别", "")).strip().upper(), 99)


def _parse_condition(text: Any) -> tuple[str, str, float] | None:
    match = CONDITION_RE.match(str(text or ""))
    if not match:
        return None
    metric, operator, threshold = match.group(1), match.group(2), float(match.group(3))
    return metric, operator, threshold


def _parse_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    match = NUMBER_RE.search(str(value or ""))
    return float(match.group(0)) if match else None


def _parse_silence_seconds(text: Any) -> int:
    match = SILENCE_RE.search(str(text or ""))
    if not match:
        return 0
    amount = float(match.group(1))
    unit = match.group(2) or "秒"
    if unit in ("分钟", "m"):
        amount *= 60
    elif unit in ("小时", "h"):
        amount *= 3600
    return int(amount)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _condition_hit(rule: dict[str, Any], metric: str, raw_value: Any) -> bool:
    condition = _parse_condition(rule.get("告警条件"))
    if condition is None:
        return False
    cond_metric, operator, threshold = condition
    if cond_metric != metric:
        return False
    value = _parse_number(raw_value)
    if value is None:
        return False
    return COMPARATORS[operator](value, threshold)


def _silence_remaining(rule: dict[str, Any], at: datetime) -> int:
    """规则在 at 时刻剩余的静默秒数；0 表示不在静默期。"""
    last_notified = _parse_time(rule.get("最后通知时间"))
    if last_notified is None:
        return 0
    silence = _parse_silence_seconds(rule.get("静默周期"))
    elapsed = (at - last_notified).total_seconds()
    remaining = int(silence - elapsed)
    return remaining if remaining > 0 else 0


class AlarmService:
    # ---------- 规则列表与详情：读取同一份判定结果 ----------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = rows[start:start + size]
        for row in page_rows:
            self._attach_latest_judgment(row)
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        self._attach_latest_judgment(entry)
        code = str(entry.get("规则编号", ""))
        related = [
            row for row in store.rows(JUDGMENT_MODULE)
            if row.get("唯一生效规则") == code or code in (row.get("命中规则") or [])
        ]
        related.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        entry["判定结果列表"] = related
        return entry

    def _attach_latest_judgment(self, rule: dict[str, Any]) -> None:
        code = str(rule.get("规则编号", ""))
        latest: dict[str, Any] | None = None
        for row in store.rows(JUDGMENT_MODULE):
            if row.get("唯一生效规则") == code or code in (row.get("命中规则") or []):
                if latest is None or int(row.get("id", 0)) > int(latest.get("id", 0)):
                    latest = row
        rule["最近判定编号"] = latest.get("判定编号", "") if latest else ""
        rule["最近通知抑制结果"] = latest.get("通知抑制结果", "") if latest else ""

    # ---------- 规则登记与状态流转 ----------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("告警级别", "通知渠道", "静默周期", "升级策略"):
            entry[field] = values.get(field) or ""
        entry["status"] = STATUS_ORDER[0]
        entry["规则状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["最后通知时间"] = ""
        entry["最近判定编号"] = ""
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"告警规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于流告警规则可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["规则状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"告警规则已{action}"

    # ---------- 告警判定：同一监控指标只生成一份判定结果 ----------
    def evaluate_metric(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        metric = str(values.get("监控指标") or "").strip()
        source = str(values.get("来源标识") or "").strip()
        raw_value = values.get("指标值")
        occurred_at = _parse_time(values.get("判定时间") or values.get("发生时间")) or datetime.now()
        if not metric:
            return None, "缺少监控指标，无法生成告警判定数据结果"
        if not source:
            return None, "缺少来源标识，告警判定数据结果必须可追溯"

        rules = store.rows(MODULE)
        same_metric = [rule for rule in rules if str(rule.get("监控指标", "")).strip() == metric]
        enabled = [rule for rule in same_metric if rule.get("status") == "已启用"]
        # 命中判定：阈值、口径、判定条件都取自规则本身的监控指标与告警条件字段。
        if raw_value in (None, ""):
            hits = list(enabled)
        else:
            hits = [rule for rule in enabled if _condition_hit(rule, metric, raw_value)]
        if not hits:
            return None, f"监控指标 {metric} 当前无启用规则命中，未生成告警判定数据结果"

        conflict_note = self._validate_conflicts(same_metric, hits)

        # 优先级判定：告警级别优先，同级按阈值更严格、规则编号兜底。
        def sort_key(rule: dict[str, Any]) -> tuple[int, float, str]:
            condition = _parse_condition(rule.get("告警条件"))
            threshold = condition[2] if condition else 0.0
            return (_severity_rank(rule), -threshold, str(rule.get("规则编号", "")))

        hits.sort(key=sort_key)
        effective = hits[0]
        effective_code = str(effective.get("规则编号", ""))
        effective_level = str(effective.get("告警级别", ""))

        remaining = _silence_remaining(effective, occurred_at)
        notify_rule: dict[str, Any] | None = None
        if remaining <= 0:
            escalation = f"无升级：{effective_code}({effective_level})优先级最高，直接生效"
            suppression = (
                f"可通知：静默期外命中，静默期{_parse_silence_seconds(effective.get('静默周期'))}秒自本次判定起生效"
            )
            notify_rule = effective
        else:
            target = self._escalation_target(effective, hits[1:], occurred_at)
            if target is not None:
                target_code = str(target.get("规则编号", ""))
                target_level = str(target.get("告警级别", ""))
                escalation = (
                    f"{effective_code}({effective_level})静默期内（剩余{remaining}秒），"
                    f"升级为{target_code}({target_level})通知"
                )
                suppression = f"升级通知：{effective_code}静默中只记录，由{target_code}代发一次"
                notify_rule = target
            else:
                escalation = f"{effective_code}({effective_level})静默期内（剩余{remaining}秒），无低级别规则可升级"
                suppression = "静默期内，只记录不通知"

        judgments = store.rows(JUDGMENT_MODULE)
        judgment_id = max((int(row.get("id", 0)) for row in judgments), default=0) + 1
        judgment = {
            "id": judgment_id,
            "status": "已判定",
            "pending": notify_rule is not None,
            "abnormal": False,
            "判定编号": f"alert-{judgment_id}",
            "监控指标": metric,
            "指标值": "" if raw_value is None else str(raw_value),
            "来源标识": source,
            "命中规则": [str(rule.get("规则编号", "")) for rule in hits],
            "冲突校验": conflict_note,
            "唯一生效规则": effective_code,
            "通知规则": str(notify_rule.get("规则编号", "")) if notify_rule is not None else "",
            "告警级别": effective_level,
            "告警条件": str(effective.get("告警条件", "")),
            "静默周期": str(effective.get("静默周期", "")),
            "升级原因": escalation,
            "通知抑制结果": suppression,
            "判定时间": occurred_at.isoformat(timespec="seconds"),
            "规则状态": str(effective.get("status", "")),
        }
        # 升级结果与规则状态一起落库：判定行与规则行在同一次写入里更新。
        judgments.append(judgment)
        if notify_rule is not None:
            notify_rule["最后通知时间"] = judgment["判定时间"]
        effective["最近判定编号"] = judgment["判定编号"]
        effective["规则状态"] = str(effective.get("status", ""))

        # 日志中心同步引用同一份判定结果，升级落库结论可追溯到同一判定编号。
        self._write_judgment_log(judgment)
        return judgment, f"告警判定 {judgment['判定编号']} 已生成，唯一生效规则 {effective_code}"

    def _validate_conflicts(self, same_metric: list[dict[str, Any]], hits: list[dict[str, Any]]) -> str:
        notes: list[str] = []
        skipped = [str(rule.get("规则编号", "")) for rule in same_metric if rule.get("status") != "已启用"]
        if skipped:
            notes.append(f"{'、'.join(skipped)}非启用状态，不参与判定")
        hit_codes = [str(rule.get("规则编号", "")) for rule in hits]
        if len(hits) > 1:
            notes.append(f"{'、'.join(hit_codes)}同时命中，按告警级别优先级判定唯一生效规则")
            by_level: dict[str, list[str]] = {}
            for rule in hits:
                by_level.setdefault(str(rule.get("告警级别", "")), []).append(str(rule.get("规则编号", "")))
            for level, codes in by_level.items():
                if len(codes) > 1:
                    notes.append(f"规则{'、'.join(codes)}告警级别同为{level}，按阈值与规则编号兜底")
        else:
            notes.append(f"仅{hit_codes[0]}一条启用规则命中，无冲突")
        return "；".join(notes)

    def _escalation_target(
        self,
        effective: dict[str, Any],
        lower_rules: list[dict[str, Any]],
        at: datetime,
    ) -> dict[str, Any] | None:
        """高级别规则静默中时，按升级策略找一条可通知的低级别规则。"""
        if "升级" not in str(effective.get("升级策略", "")):
            return None
        for rule in lower_rules:
            if _silence_remaining(rule, at) <= 0:
                return rule
        return None

    def _write_judgment_log(self, judgment: dict[str, Any]) -> None:
        rows = store.rows(LOG_MODULE)
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        seq = next_id
        rows.append({
            "id": next_id,
            "status": "已记录",
            "pending": True,
            "abnormal": False,
            "记录编号": f"LOG-{seq:04d}",
            "流作业名": judgment["来源标识"],
            "日志级别": judgment["告警级别"],
            "日志内容": (
                f"告警判定{judgment['判定编号']}：唯一生效规则{judgment['唯一生效规则']}，"
                f"升级原因{judgment['升级原因']}，通知抑制结果{judgment['通知抑制结果']}"
            ),
            "发生时间": judgment["判定时间"],
            "异常类型": "告警判定",
            "处理状态": "待处理",
            "记录状态": "已记录",
            "判定编号": judgment["判定编号"],
            "来源标识": judgment["来源标识"],
        })

    # ---------- 判定结果与通知链路：全链路只读同一份数据 ----------
    def list_determinations(
        self,
        *,
        source: str | None = None,
        metric: str | None = None,
        rule: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(JUDGMENT_MODULE))
        if source:
            rows = [row for row in rows if row.get("来源标识") == source]
        if metric:
            rows = [row for row in rows if row.get("监控指标") == metric]
        if rule:
            rows = [
                row for row in rows
                if row.get("唯一生效规则") == rule or rule in (row.get("命中规则") or [])
            ]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_determination(self, judge_id: str) -> dict[str, Any] | None:
        for row in store.rows(JUDGMENT_MODULE):
            if row.get("判定编号") == judge_id or str(row.get("id", "")) == judge_id:
                return row
        return None

    def list_notifications(self) -> list[dict[str, Any]]:
        """通知链路出口：只消费判定结果这一份数据，不自行重算规则。"""
        channels = {
            str(rule.get("规则编号", "")): str(rule.get("通知渠道", ""))
            for rule in store.rows(MODULE)
        }
        notifications: list[dict[str, Any]] = []
        for row in sorted(store.rows(JUDGMENT_MODULE), key=lambda item: int(item.get("id", 0))):
            suppression = str(row.get("通知抑制结果", ""))
            sendable = suppression.startswith(("可通知", "升级通知"))
            notify_rule = str(row.get("通知规则") or row.get("唯一生效规则") or "")
            notifications.append({
                "通知编号": f"notify-{row.get('id', 0)}",
                "判定编号": row.get("判定编号", ""),
                "唯一生效规则": row.get("唯一生效规则", ""),
                "通知规则": notify_rule,
                "通知渠道": channels.get(notify_rule, ""),
                "发送结果": "待发送" if sendable else "已抑制",
                "升级原因": row.get("升级原因", ""),
                "通知抑制结果": suppression,
                "来源标识": row.get("来源标识", ""),
                "判定时间": row.get("判定时间", ""),
            })
        return notifications
