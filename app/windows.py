"""核验窗口裁决。

同一批双写事件可能在凌晨关账时跨业务日重放：事件昨夜发生、今晨才在索引可见，
或今天发生却随昨天的批次文件补交。按“发生日”归窗会漏掉跨日才可见的结论，按
“文件到达日”归窗会把迟到的旧事件在两天各计一次。

本模块给出唯一口径——**文档可观测结算日**：

* 以文档为单位，取该文档全部有效事件的可观测时刻；有 ``visible_time`` 用可见
  时刻，否则用 ``source_time``。最晚的可观测时刻所在业务日，即该文档的核验窗口。
* 窗口只由事件自身时间戳推导，不读文件到达日、批次标签或运行时刻，所以跨日才
  可见的结果落进可见当日，不会漏；迟到的旧事件仍落回它自己的业务日。
* 每篇文档只归一个窗口，文档内所有事件整体随文档归窗；同一事件因此不会跨窗重
  复计入。事件各自的业务日仍保存在 ``window_event_windows``，便于追回差异。
* 同一输入、同一参数得到同一批窗口；重跑同一批（run_id 相同，落库只保留一条）
  不会新增窗口。
"""

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import WINDOW_TIMEZONE
from app.errors import DomainError
from app.values import instant

WINDOW_RULE = "observable-settlement-v1"
WINDOW_FIELDS = (
    "window", "window_timezone", "window_start", "window_end",
    "window_settlement_at", "window_basis", "window_event_windows",
    "window_cross_day", "window_events", "window_sources",
)
_MISSING = (None, "", "-", "NULL")


def zone(name):
    """解析业务日时区；默认取配置的关账地区。"""
    try:
        return ZoneInfo(name or WINDOW_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise DomainError("BAD_TIMEZONE", "核验窗口时区无效")


def business_day(moment, tz):
    """返回 moment 所在业务日，区间为 [start, end)。"""
    local = moment.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return {
        "window": start.date().isoformat(),
        "timezone": str(tz),
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def day_bounds(label, tz):
    """按业务日标签补出 [start, end)，供事件所属窗口的清单使用。"""
    try:
        day = datetime.strptime(label, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return {
        "window": label,
        "timezone": str(tz),
        "start": start.isoformat(),
        "end": (start + timedelta(days=1)).isoformat(),
    }


def _value(row, *keys):
    for key in keys:
        value = row.get(key)
        if value not in _MISSING:
            return value
    return None


def observable(row):
    """事件可观测时刻及依据字段；索引可见优先于事件发生。"""
    value = _value(row, "visible_at", "visible_time")
    if value is not None:
        return instant(value), "visible_time"
    value = _value(row, "source_at", "source_time")
    if value is not None:
        return instant(value), "source_time"
    return None, None


def _line(row):
    lines = row.get("_sources") or []
    if lines:
        return min(lines)
    return row.get("_line")


def _event_id(row):
    return str(row.get("event_id", ""))


def _dedupe(events):
    """同一 event_id 在同一文档内只保留一条，避免重复计入。"""
    seen = {}
    for row in events:
        seen.setdefault(_event_id(row), row)
    return list(seen.values())


def assign(events, timezone=None):
    """按文档裁决核验窗口，返回 doc_id -> 窗口字段。"""
    tz = zone(timezone)
    buckets = defaultdict(list)
    for row in events:
        doc_id = row.get("doc_id")
        if doc_id in _MISSING:
            continue
        moment, field = observable(row)
        if moment is None:
            continue
        buckets[str(doc_id)].append({
            "moment": moment,
            "field": field,
            "event_id": _event_id(row),
            "side": row.get("side", ""),
            "source_time": _value(row, "source_time", "source_at"),
            "visible_time": _value(row, "visible_time", "visible_at"),
            "source_line": _line(row),
        })

    assigned = {}
    for doc_id, items in buckets.items():
        items = _dedupe(items)
        items.sort(key=lambda item: (item["moment"], item["event_id"]))
        latest = items[-1]
        day = business_day(latest["moment"], tz)
        event_windows = sorted({business_day(item["moment"], tz)["window"]
                                for item in items})
        evidence = []
        for item in items:
            local_day = business_day(item["moment"], tz)
            evidence.append({
                "event_id": item["event_id"],
                "side": item["side"],
                "source_time": item["source_time"],
                "visible_time": item["visible_time"],
                "observable_field": item["field"],
                "observable_at": item["moment"].astimezone(tz).isoformat(),
                "event_window": local_day["window"],
                "source_line": item["source_line"],
            })
        assigned[doc_id] = {
            "window": day["window"],
            "window_timezone": day["timezone"],
            "window_start": day["start"],
            "window_end": day["end"],
            "window_settlement_at": latest["moment"].astimezone(tz).isoformat(),
            "window_basis": "%s:%s:%s@%s" % (
                latest["field"], latest["side"], latest["event_id"],
                latest["moment"].astimezone(tz).isoformat()),
            "window_event_windows": event_windows,
            "window_cross_day": len(event_windows) > 1,
            "window_events": evidence,
            "window_sources": sorted({item["source_line"] for item in items
                                      if item["source_line"] is not None}),
        }
    return assigned


def summarize(rows):
    """把文档级结果汇总成窗口清单；页面与归档共用这一份口径。

    文档计入其结算窗口（最晚可观测事件所在日）；事件计入它自己的业务日。
    两者都只计一次，同一事件在清单里只出现于一个窗口。
    """
    groups = {}

    def bucket(label, row):
        group = groups.get(label)
        if group is None:
            bounds = day_bounds(label, zone(row.get("window_timezone"))) or {}
            group = {
                "window": label,
                "timezone": row.get("window_timezone", WINDOW_TIMEZONE),
                "start": bounds.get("start", row.get("window_start", "")),
                "end": bounds.get("end", row.get("window_end", "")),
                "rule": WINDOW_RULE,
                "documents": set(),
                "events": set(),
                "event_documents": set(),
                "cross_day_documents": 0,
            }
            groups[label] = group
        return group

    for row in rows:
        doc_label = row.get("window")
        if not doc_label:
            continue
        group = bucket(doc_label, row)
        group["documents"].add(str(row.get("doc_id")))
        if row.get("window_cross_day"):
            group["cross_day_documents"] += 1
        for event in row.get("window_events") or []:
            event_id = str(event.get("event_id") or "")
            target = bucket(event.get("event_window") or doc_label, row)
            if event_id:
                target["events"].add(event_id)
            target["event_documents"].add(str(row.get("doc_id")))
    return [{
        "window": group["window"],
        "timezone": group["timezone"],
        "start": group["start"],
        "end": group["end"],
        "rule": group["rule"],
        "documents": len(group["documents"]),
        "events": len(group["events"]),
        "event_documents": len(group["event_documents"]),
        "cross_day_documents": group["cross_day_documents"],
    } for group in sorted(groups.values(), key=lambda item: item["window"])]


def validate(rows):
    """校验一次批次：每篇文档只归一个结算窗口，每个事件只有一个业务日。

    另核每篇文档的结算窗口必须等于其事件业务日的最晚者（跨日时文档随最晚
    事件归窗），防止窗口与事件依据自相矛盾。
    """
    event_windows = defaultdict(set)
    doc_windows = defaultdict(set)
    unwindowed = []
    mismatched = []
    for row in rows:
        doc_id = str(row.get("doc_id"))
        label = row.get("window")
        if not label:
            unwindowed.append(doc_id)
            continue
        doc_windows[doc_id].add(label)
        days = []
        for event in row.get("window_events") or []:
            event_id = str(event.get("event_id") or "")
            event_label = event.get("event_window") or label
            if event_id:
                event_windows[event_id].add(event_label)
            days.append(event_label)
        if days and max(days) != label:
            mismatched.append({"doc_id": doc_id, "window": label, "latest_event_window": max(days)})
    return {
        "documents": len(doc_windows),
        "events": len(event_windows),
        "unwindowed": sorted(unwindowed),
        "duplicate_documents": sorted(doc for doc, labels in doc_windows.items()
                                      if len(labels) > 1),
        "duplicate_events": sorted(event for event, labels in event_windows.items()
                                   if len(labels) > 1),
        "mismatched": mismatched,
        "windows": sorted({label for labels in doc_windows.values() for label in labels}
                          | {label for labels in event_windows.values() for label in labels}),
    }


def rehydrate(run):
    """为旧规则批次补算文档级窗口字段，不改写库内原始载荷。

    只在全部文档都能归窗时补算，避免旧批次因缺字段被误判为漏窗。
    """
    stages = run.get("stages") or []
    if len(stages) < 4:
        return run
    rows = stages[3].get("rows") or []
    if not rows or all(row.get("window") for row in rows):
        return run
    docs = {str(row.get("doc_id")) for row in rows}
    events = (stages[1].get("rows") if len(stages) > 1 else None) or rows
    assigned = assign([event for event in events
                       if str(event.get("doc_id")) in docs],
                      (run.get("parameters") or {}).get("window_timezone"))
    if docs - set(assigned):
        return run
    out = dict(run)
    out["stages"] = list(stages)
    for index in range(3, min(7, len(stages))):
        stage = dict(stages[index])
        stage["rows"] = [{**row, **assigned.get(str(row.get("doc_id")), {})}
                         for row in stage.get("rows") or []]
        out["stages"][index] = stage
    out["windows"] = summarize(out["stages"][3]["rows"])
    return out


def ledger(runs):
    """跨批次窗口台账。

    事件按 event_id、文档按 doc_id 全局去重：最先出现的窗口拥有它，后来的批次
    不再重复计数。窗口只在其确实拥有文档或事件时入账，因此重跑同一批（run_id
    相同，落库只有一条）不会新增窗口，跨窗重放也不会把同一事件算两次。
    """
    first_event = {}
    first_doc = {}
    meta = {}
    windows = set()
    runs_by_window = defaultdict(set)
    conflicting_events = []
    conflicting_documents = []

    def note(label, row):
        meta.setdefault(label, {
            "window": label,
            "timezone": row.get("window_timezone", WINDOW_TIMEZONE),
            "start": (day_bounds(label, zone(row.get("window_timezone"))) or {}).get(
                "start", row.get("window_start", "")),
            "end": (day_bounds(label, zone(row.get("window_timezone"))) or {}).get(
                "end", row.get("window_end", "")),
            "rule": WINDOW_RULE,
        })

    for run in runs:
        stages = run.get("stages") or []
        if not stages:
            continue
        for row in stages[-1].get("rows", []):
            label = row.get("window")
            if not label:
                continue
            note(label, row)
            doc_id = str(row.get("doc_id"))
            known = first_doc.get(doc_id)
            if known is not None and known != label:
                conflicting_documents.append({"doc_id": doc_id,
                                              "windows": sorted({known, label})})
            else:
                first_doc[doc_id] = label
                windows.add(label)
                runs_by_window[label].add(run.get("run_id"))
            for event in row.get("window_events") or []:
                event_id = str(event.get("event_id") or "")
                if not event_id:
                    continue
                event_label = event.get("event_window") or label
                note(event_label, row)
                known = first_event.get(event_id)
                if known is not None and known != event_label:
                    conflicting_events.append({"event_id": event_id,
                                               "windows": sorted({known, event_label})})
                else:
                    first_event[event_id] = event_label
                    windows.add(event_label)
                    runs_by_window[event_label].add(run.get("run_id"))

    out = []
    for label in sorted(windows):
        out.append({
            **meta[label],
            "documents": sum(1 for value in first_doc.values() if value == label),
            "events": sum(1 for value in first_event.values() if value == label),
            "runs": len(runs_by_window[label]),
        })
    return {
        "rule": WINDOW_RULE,
        "timezone": (out[0]["timezone"] if out else WINDOW_TIMEZONE),
        "windows": out,
        "documents": len(first_doc),
        "events": len(first_event),
        "conflicting_documents": conflicting_documents,
        "conflicting_events": conflicting_events,
    }
