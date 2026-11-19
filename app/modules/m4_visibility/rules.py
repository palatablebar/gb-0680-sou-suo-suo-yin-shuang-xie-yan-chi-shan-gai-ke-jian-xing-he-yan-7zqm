from collections import defaultdict
from app import windows
from app.reasons import sources
from app.values import number
import json
def same_document(a,b):return json.dumps(a,sort_keys=True)==json.dumps(b,sort_keys=True)
def evaluate(rows,context):
    grouped=defaultdict(dict)
    for row in rows:grouped[row["doc_id"]][row["side"]]=row
    # 窗口按文档裁决：取两端全部有效事件（含被替代版本）的可观测时刻，最晚者归窗。
    # 优先用 M3 的事件明细，缺失时退回全量事件或当前行，保证单独调用也可复算。
    events=(context.get("versions") or {}).get("rows") or (context.get("schema") or {}).get("rows") or rows
    flattened=[]
    for event in events:
        flattened.extend(event.get("_events") or [event])
    timezone=(context.get("parameters") or {}).get("window_timezone")
    assigned=windows.assign([e for e in flattened if str(e.get("doc_id")) in grouped],timezone)
    out=[]
    for doc,sides in sorted(grouped.items()):
        source=sides.get("source");target=sides.get("index");lines=[n for r in sides.values() for n in sources(r)]
        if source is None:state="ORPHAN_INDEX"
        elif target is None:state="MISSING_INDEX" if source["op"]=="upsert" else "DELETE_NOT_CONFIRMED"
        elif number(target["version"])>number(source["version"]):state="INDEX_AHEAD"
        elif number(target["version"])<number(source["version"]):state="STALE_INDEX"
        elif source["op"]!=target["op"]:state="OP_MISMATCH"
        elif source["op"]=="upsert" and not same_document(source.get("document"),target.get("document")):state="CONTENT_MISMATCH"
        else:state="CONSISTENT"
        anchor=source or target
        out.append({**anchor,"_sources":lines,"source_version":source["version"] if source else "—","index_version":target["version"] if target else "—","source_op":source["op"] if source else "—","index_op":target["op"] if target else "—","visible_at":target.get("visible_at") if target else None,"visibility_state":state,"expected":"1","reported":"1" if state=="CONSISTENT" else "0","document_equal":state=="CONSISTENT",**assigned.get(doc,{})})
    summary=windows.summarize(out)
    return {"rows":out,"quarantine":[],"metrics":{"documents":len(out),"consistent":sum(r["visibility_state"]=="CONSISTENT" for r in out),"missing":sum(r["visibility_state"] in {"MISSING_INDEX","DELETE_NOT_CONFIRMED"} for r in out),"window_count":len(summary),"window_cross_day":sum(1 for r in out if r.get("window_cross_day"))}}
