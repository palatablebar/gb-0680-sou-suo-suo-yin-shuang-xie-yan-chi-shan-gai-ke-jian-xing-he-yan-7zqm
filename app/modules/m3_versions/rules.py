from collections import defaultdict
import json
from app.reasons import reject,sources
from app.values import number
def signature(row):return json.dumps([row["op"],row.get("document")],sort_keys=True,ensure_ascii=False)
def event(row):
    """事件瘦身副本：只保留归窗与追溯需要的字段。"""
    line=min(sources(row)) if sources(row) else row.get("_line")
    return {"event_id":str(row.get("event_id","")),"doc_id":str(row.get("doc_id","")),
            "side":row.get("side",""),"version":row.get("version",""),"op":row.get("op",""),
            "source_at":row.get("source_at"),"source_time":row.get("source_time"),
            "visible_at":row.get("visible_at"),"visible_time":row.get("visible_time"),
            "_line":line,"_sources":[line] if line is not None else []}
def evaluate(rows,context):
    grouped=defaultdict(list);good=[];bad=[]
    for row in rows:grouped[(row["doc_id"],row["side"],row["version"])].append(row)
    by_doc=defaultdict(list);blocked=set()
    for key,events in sorted(grouped.items()):
        if len(set(signature(e) for e in events))>1:
            bad.extend(reject(e,"SAME_VERSION_CONFLICT","同文档同版本内容冲突") for e in events);blocked.add((key[0],key[1]));continue
        first=sorted(events,key=lambda e:e["event_id"])[0]
        by_doc[(key[0],key[1])].append({**first,"_sources":[n for e in events for n in sources(e)],"version_duplicate_count":len(events)-1})
    for (doc,side),events in sorted(by_doc.items()):
        if (doc,side) in blocked:
            bad.extend(reject(e,"UNRESOLVED_VERSION","当前文档存在版本冲突") for e in events);continue
        winner=max(events,key=lambda e:number(e["version"]))
        # 保留该端全部有效事件（含被替代版本），供 M4 按可观测结算日归窗与追溯。
        history=sorted((event(e) for e in events),key=lambda e:(e["event_id"],e["version"]))
        good.append({**winner,"_sources":[n for e in events for n in sources(e)],"superseded_versions":sorted([e["version"] for e in events if e is not winner],key=number),"tombstone":winner["op"]=="delete","_events":history})
    return {"rows":good,"quarantine":bad,"metrics":{"states":len(good),"tombstones":sum(r["tombstone"] for r in good),"conflict_rows":len(bad)}}
