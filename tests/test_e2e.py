import csv,io
from app.pipeline import execute,load_sample
from app import store
def test_full_sample_flow(client):
    r=client.post("/api/runs",json={"sample":"dirty.csv"});assert r.status_code==200
    p=r.get_json();assert len(p["stages"])==7
    assert p["stages"][5]["metrics"]["balanced"] is True
def test_idempotent_real_input(sample):
    a=execute(sample);b=execute(sample)
    assert a["run_id"]==b["run_id"] and len(store.history())==1
def test_stale_review_409(client):
    "TRAP-26 陈旧复核不得覆盖新版本"
    p=client.post("/api/runs",json={"sample":"dirty.csv"}).get_json();u="/api/runs/"+p["run_id"]+"/review"
    assert client.post(u,json={"version":1,"decision":"已复核"}).status_code==200
    assert client.post(u,json={"version":1,"decision":"退回复核"}).status_code==409
def test_export_has_manifest_and_quarantine(client):
    p=client.post("/api/runs",json={"sample":"dirty.csv"}).get_json()
    r=client.get("/api/runs/"+p["run_id"]+"/archive")
    rows=list(csv.reader(io.StringIO(r.data.decode("utf-8-sig"))))
    assert rows[0][1]=="input_sha256" and any(x[3]=="quarantine" for x in rows[1:])
def test_failed_import_is_atomic(client):
    assert client.post("/api/runs",json={"csv":"bad"}).status_code==422
    assert store.history()==[]
def test_module_entrances(client):
    from app.config import MODULES
    for slug,name in MODULES:
        assert client.get("/api/"+slug).status_code==200
        assert client.get("/"+slug).status_code==200
def test_missing_run_404(client): assert client.get("/api/runs/missing").status_code==404

def test_cutover_windows_are_stable_and_exported(client):
    "跨日关账：页面、接口与归档同一窗口口径"
    p=client.post("/api/runs",json={"sample":"cutover.csv"}).get_json()
    again=client.post("/api/runs",json={"sample":"cutover.csv"}).get_json()
    assert p["run_id"]==again["run_id"] and len(store.history())==1
    windows=p["windows"];assert [w["window"] for w in windows]==["2026-11-15","2026-11-16"]
    # 每篇文档只settle一次、每个事件只归一个业务日
    docs=p["stages"][3]["rows"];assert len({r["doc_id"] for r in docs})==len(docs)
    event_windows=p["stages"][3]["metrics"]["window_count"]
    assert event_windows==len(windows)
    runs=client.get("/api/windows").get_json()
    assert sum(w["events"] for w in runs["windows"])==len({e["event_id"] for r in docs for e in r["window_events"]})
    assert runs["conflicting_events"]==[] and runs["conflicting_documents"]==[]
    run_view=client.get("/api/runs/"+p["run_id"]+"/windows").get_json()
    assert run_view["windows"]==windows
    assert run_view["check"]["duplicate_events"]==[] and run_view["check"]["mismatched"]==[]
    rows=list(csv.reader(io.StringIO(client.get("/api/runs/"+p["run_id"]+"/archive").data.decode("utf-8-sig"))))
    assert rows[0][4]=="window" and rows[0][9]=="window_basis"
    exported=sorted(r[4] for r in rows[1:] if r[3]=="window")
    assert exported==[w["window"] for w in windows]
    accepted=[r for r in rows[1:] if r[3]=="accepted"]
    assert len(accepted)==len(docs) and all(r[4] and r[9] for r in accepted)
    html=client.get("/visibility").data.decode()
    for w in windows: assert w["window"] in html

def test_window_timezone_is_part_of_batch_identity(client):
    "业务日地区是口径的一部分：换时区得到新批次与新窗口，不覆盖旧结论"
    a=client.post("/api/runs",json={"sample":"cutover.csv"}).get_json()
    b=client.post("/api/runs",json={"sample":"cutover.csv","parameters":{"window_timezone":"UTC"}}).get_json()
    assert a["run_id"]!=b["run_id"]
    assert [w["window"] for w in a["windows"]]==["2026-11-15","2026-11-16"]
    # UTC 下 17:00 仍属当日，跨日可见不再跨日，窗口合并为一天
    assert [w["window"] for w in b["windows"]]==["2026-11-15"]
    assert b["parameters"]["window_timezone"]=="UTC"
