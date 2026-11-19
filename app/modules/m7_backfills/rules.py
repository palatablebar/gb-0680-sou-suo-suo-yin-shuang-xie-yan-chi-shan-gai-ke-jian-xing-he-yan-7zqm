import hashlib,json
from decimal import Decimal
from app import windows
from app.errors import DomainError
from app.reasons import sources
def evaluate(rows,context):
    stages=[v for v in context.values() if isinstance(v,dict) and "metrics" in v]
    balances=[s["metrics"]["balanced"] for s in stages if "balanced" in s["metrics"]]
    if balances and not all(balances): raise DomainError("UNBALANCED","来源行未对平，不能归档")
    ids=[str(r.get("doc_id",r.get("_line",""))) for r in rows]
    if len(ids)!=len(set(ids)): raise DomainError("ARCHIVE_DUPLICATE","归档业务键重复")
    # 直接以 rows 调用时没有上游窗口字段，按同一口径就地补算，避免两套归窗逻辑。
    if any(not r.get("window") for r in rows):
        assigned=windows.assign([r for r in rows if r.get("source_at") or r.get("source_time")],(context.get("parameters") or {}).get("window_timezone"))
        rows=[{**r,**assigned.get(str(r.get("doc_id")),{})} for r in rows]
    # 归档沿用可见性重建的同一窗口口径；发现跨窗或漏窗即拒绝封存。
    check=windows.validate(rows)
    if check["duplicate_documents"] or check["duplicate_events"]: raise DomainError("WINDOW_SPLIT","同一文档或事件跨窗口重复")
    if check["mismatched"]: raise DomainError("WINDOW_SPLIT","文档窗口与事件业务日不一致")
    if check["unwindowed"]: raise DomainError("WINDOW_MISSING","存在未归窗文档，需带时区的事件时间")
    summary=windows.summarize(rows)
    encoded=json.dumps(rows,ensure_ascii=False,sort_keys=True,default=str).encode()
    return {"rows":rows,"quarantine":[],"metrics":{"records":len(rows),"source_lines":len(set(x for row in rows for x in sources(row))),"sha256":hashlib.sha256(encoded).hexdigest(),"ready":True,"differences":sum(r.get("status")=="差异" for r in rows),"windows":len(summary),"window_cross_day":sum(1 for r in rows if r.get("window_cross_day")),"window_rule":windows.WINDOW_RULE}}
