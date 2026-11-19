import csv,io,json
from app import windows
def build_archive(run):
    stream=io.StringIO(newline="");writer=csv.writer(stream)
    writer.writerow(["run_id","input_sha256","rule_version","kind","window","window_timezone","window_start","window_end","window_settlement_at","window_basis","window_event_windows","window_cross_day","source_lines","reason","evidence"])
    meta=run["manifest"];rows=run["stages"][-1]["rows"]
    # 页面与归档共用 app.windows 的同一份汇总，窗口号、时区、边界和结算时刻一致。
    summary=run.get("windows") or windows.summarize(rows)
    def window_cells(row):
        return [row.get("window",""),row.get("window_timezone",""),row.get("window_start",""),row.get("window_end",""),row.get("window_settlement_at",""),row.get("window_basis",""),json.dumps(row.get("window_event_windows",[]),ensure_ascii=False),row.get("window_cross_day","")]
    for entry in summary:
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"window",entry["window"],entry["timezone"],entry["start"],entry["end"],"","rule:%s;documents:%s;events:%s"%(entry["rule"],entry["documents"],entry["events"]),"",entry.get("cross_day_documents",0),"","",""])
    for row in rows:
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"accepted",*window_cells(row),json.dumps(row.get("_sources",[])),row.get("status",""),json.dumps(row,ensure_ascii=False)])
    for row in run["stages"][-1].get("cumulative_quarantine",[]):
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"quarantine","","","","","","","","",json.dumps(row.get("_sources",[])),row.get("reason",""),json.dumps(row,ensure_ascii=False)])
    return "﻿"+stream.getvalue()
