import pytest

from app import windows
from app.config import WINDOW_TIMEZONE
from app.pipeline import calculate
from app.modules.m4_visibility.rules import evaluate as visibility
from app.modules.m7_backfills.rules import evaluate as archive

HEADER = "event_id,doc_id,version,op,source_time,visible_time,side,payload,reported,note\n"


def row(event_id, doc_id, side, source, visible, version="1", op="upsert", payload='{"n":"x"}', reported="1"):
    return {
        "event_id": str(event_id),
        "doc_id": str(doc_id),
        "version": version,
        "op": op,
        "source_at": source,
        "visible_at": visible,
        "side": side,
        "document": None if op == "delete" else {"n": "x"},
        "reported": reported,
        "_sources": [int(event_id)],
    }


def test_cross_day_visibility_lands_in_visible_day():
    """昨夜发生、今晨才可见：文档归入可见当日，结论不漏。"""
    events = [
        row(1, "d1", "source", "2026-11-15T15:00:00Z", None),   # 上海 11-15 23:00
        row(2, "d1", "index", "2026-11-15T15:00:00Z", "2026-11-15T17:00:00Z"),  # 11-16 01:00
    ]
    assigned = windows.assign(events)
    assert assigned["d1"]["window"] == "2026-11-16"
    assert assigned["d1"]["window_cross_day"] is True
    assert assigned["d1"]["window_event_windows"] == ["2026-11-15", "2026-11-16"]


def test_late_old_event_stays_in_its_own_day():
    """迟到旧事件仍落回原业务日，不会在到达日再计一次。"""
    events = [
        row(1, "d1", "source", "2026-11-15T02:00:00Z", None),   # 11-15 10:00
        row(2, "d1", "source", "2026-11-15T03:00:00Z", None),   # 11-15 11:00
    ]
    assigned = windows.assign(events)
    assert assigned["d1"]["window"] == "2026-11-15"
    assert assigned["d1"]["window_event_windows"] == ["2026-11-15"]


def test_document_assigned_once_across_windows():
    """同一文档的跨日事件整体随文档归一个窗口，事件各归自己的业务日。"""
    rows = [
        row(1, "d1", "source", "2026-11-15T15:00:00Z", None),
        row(2, "d1", "source", "2026-11-15T16:00:00Z", None),   # 11-16 00:00
    ]
    assigned = windows.assign(rows)
    visible = visibility([{**r, "source_at": r["source_at"]} for r in rows], {"parameters": {}})
    assert len(visible["rows"]) == 1
    assert visible["rows"][0]["window"] == assigned["d1"]["window"] == "2026-11-16"


def test_rerun_same_batch_keeps_windows_and_conclusions():
    raw = (HEADER
           + "1,d1,1,upsert,2026-11-15T15:00:00Z,-,source,\"{\"\"n\"\":\"\"x\"\"}\",1,源\n"
           + "2,d1,1,upsert,2026-11-15T15:00:00Z,2026-11-15T17:00:00Z,index,\"{\"\"n\"\":\"\"x\"\"}\",1,跨日可见\n"
           + "3,d2,1,upsert,2026-11-15T16:00:00Z,-,source,\"{\"\"n\"\":\"\"y\"\"}\",1,次日\n").encode()
    first = calculate(raw)
    second = calculate(raw)
    assert first["run_id"] == second["run_id"]
    assert first["windows"] == second["windows"]
    assert [r["visibility_state"] for r in first["stages"][3]["rows"]] == \
           [r["visibility_state"] for r in second["stages"][3]["rows"]]


def test_window_basis_and_sources_traceable():
    events = [
        row(7, "d1", "source", "2026-11-15T15:00:00Z", None),
        row(9, "d1", "index", "2026-11-15T15:00:00Z", "2026-11-15T17:00:00Z"),
    ]
    assigned = windows.assign(events)
    assert assigned["d1"]["window_basis"].startswith("visible_time:index:9@")
    assert assigned["d1"]["window_sources"] == [7, 9]
    assert {e["event_id"]: e["source_line"] for e in assigned["d1"]["window_events"]} == {"7": 7, "9": 9}
    assert all(e["observable_field"] in {"source_time", "visible_time"} for e in assigned["d1"]["window_events"])


def test_timezone_is_configurable_and_bad_zone_rejected():
    from app.errors import DomainError
    events = [row(1, "d1", "source", "2026-11-15T16:30:00Z", None)]
    assert windows.assign(events, "Asia/Shanghai")["d1"]["window"] == "2026-11-16"
    assert windows.assign(events, "UTC")["d1"]["window"] == "2026-11-15"
    with pytest.raises(DomainError) as error:
        windows.assign(events, "Mars/Olympus")
    assert error.value.code == "BAD_TIMEZONE"


def test_summary_counts_each_event_once():
    events = [
        row(1, "d1", "source", "2026-11-15T15:00:00Z", None),
        row(2, "d1", "index", "2026-11-15T15:00:00Z", "2026-11-15T17:00:00Z"),
        row(3, "d2", "source", "2026-11-15T17:00:00Z", None),
    ]
    assigned = windows.assign(events)
    rows = [{"doc_id": "d1", **assigned["d1"]}, {"doc_id": "d2", **assigned["d2"]}]
    summary = windows.summarize(rows)
    counts = {item["window"]: item["events"] for item in summary}
    total = sum(counts.values())
    assert total == 3
    assert counts == {"2026-11-15": 1, "2026-11-16": 2}


def test_archive_rejects_split_windows():
    from app.errors import DomainError
    doc = {"doc_id": "d1", "window": "2026-11-16", "window_timezone": WINDOW_TIMEZONE,
           "window_start": "2026-11-16T00:00:00+08:00", "window_end": "2026-11-17T00:00:00+08:00",
           "window_events": [{"event_id": "1", "event_window": "2026-11-15"}], "_sources": [1]}
    with pytest.raises(DomainError) as error:
        archive([doc], {})
    assert error.value.code == "WINDOW_SPLIT"


def test_ledger_deduplicates_events_across_runs():
    events = [row(1, "d1", "source", "2026-11-15T17:00:00Z", None)]
    assigned = windows.assign(events)
    doc = {"doc_id": "d1", **assigned["d1"]}
    run_a = {"run_id": "a", "stages": [{}, {}, {}, {}, {}, {}, {"rows": [doc]}]}
    run_b = {"run_id": "b", "stages": [{}, {}, {}, {}, {}, {}, {"rows": [doc]}]}
    ledger = windows.ledger([run_a, run_b])
    assert ledger["events"] == 1
    assert ledger["documents"] == 1
    assert ledger["conflicting_events"] == []
    assert [item["window"] for item in ledger["windows"]] == ["2026-11-16"]


def test_ledger_flags_event_appearing_in_two_windows():
    """同一事件被重放到两个窗口时，台账以冲突暴露，而不是各计一次。"""
    first = {"doc_id": "d1", **windows.assign([row(1, "d1", "source", "2026-11-15T17:00:00Z", None)])["d1"]}
    second = {"doc_id": "d1", **windows.assign([row(1, "d1", "source", "2026-11-14T17:00:00Z", None)])["d1"]}
    runs = [{"run_id": "a", "stages": [{}, {}, {}, {}, {}, {}, {"rows": [first]}]},
            {"run_id": "b", "stages": [{}, {}, {}, {}, {}, {}, {"rows": [second]}]}]
    ledger = windows.ledger(runs)
    assert ledger["events"] == 1
    assert ledger["conflicting_events"] == [{"event_id": "1", "windows": ["2026-11-15", "2026-11-16"]}]
    assert ledger["conflicting_documents"] == [{"doc_id": "d1", "windows": ["2026-11-15", "2026-11-16"]}]


def test_rehydrate_legacy_run_without_window_fields():
    """旧批次可补算同一口径的窗口，不改写库内原始载荷。"""
    rows = [row(1, "d1", "source", "2026-11-15T15:00:00Z", None),
            row(2, "d1", "index", "2026-11-15T15:00:00Z", "2026-11-15T17:00:00Z")]
    stages = [{}, {"rows": rows}, {}, {"rows": [{"doc_id": "d1", "_sources": [1, 2]}]},
              {"rows": [{"doc_id": "d1"}]}, {}, {"rows": [{"doc_id": "d1"}]}]
    legacy = {"run_id": "old", "parameters": {}, "stages": stages}
    fixed = windows.rehydrate(legacy)
    assert fixed["stages"][3]["rows"][0]["window"] == "2026-11-16"
    assert fixed["stages"][6]["rows"][0]["window"] == "2026-11-16"
    summary = {item["window"]: item for item in fixed["windows"]}
    assert summary["2026-11-16"]["documents"] == 1
    assert summary["2026-11-15"]["events"] == 1 and summary["2026-11-16"]["events"] == 1
    assert "window" not in legacy["stages"][3]["rows"][0]
