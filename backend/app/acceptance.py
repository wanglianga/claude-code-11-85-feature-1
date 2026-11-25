"""虚构网络验收断言（只读）。

对应题目验收剧本：
P1 双药店同日同成分重叠 + 停诊 + 同一给药期；规则与图模型同链；
P2/P3 弱身份不合并、不扩展；P4 调拨解释合法供药；P5 申诉退回关闭。
"""
from datetime import date

from sqlalchemy.orm import Session

from .models import (
    Appeal, Case, CaseJudgment, GraphEdge, IdentityEvidence, SourceRecord,
)
from .services import identity
from .services.expansion import expand, high_amount_extension
from .services.ranking import rank_cases


def _chk(key, name, passed, expected, actual, detail=""):
    return {"key": key, "name": name, "passed": bool(passed),
            "expected": expected, "actual": actual, "detail": detail}


def run_acceptance(db: Session) -> dict:
    checks = []

    # 1) 来源账 ----------------------------------------------------------
    src_total = db.query(SourceRecord).count()
    no_citation = db.query(SourceRecord).filter(
        (SourceRecord.citation.is_(None)) | (SourceRecord.citation == "")).count()
    old = db.query(SourceRecord).filter_by(code="SRC-ST5B").first()
    corr = db.query(SourceRecord).filter_by(code="SRC-ST5B-C2").first()
    checks.append(_chk(
        "source_ledger", "来源账：记录可引用且更正链完整",
        src_total >= 50 and no_citation == 0 and old and old.status == "corrected"
        and corr and corr.supersedes_id == old.id,
        ">=50 条来源、0 条缺引用、SRC-ST5B 被更正单接续",
        {"total": src_total, "missing_citation": no_citation,
         "old_status": old.status if old else None,
         "correction_linked": bool(corr and old and corr.supersedes_id == old.id)}))

    # 2) 身份解析分级 -----------------------------------------------------
    ie01 = db.query(IdentityEvidence).filter_by(code="IE-01").first()
    ie03 = db.query(IdentityEvidence).filter_by(code="IE-03").first()
    v, p2c, c2m = identity.cluster_map(db)
    p1_same = p2c.get("P1") == p2c.get("P1B")
    p23_sep = p2c.get("P2") != p2c.get("P3")
    checks.append(_chk(
        "identity_grading", "强证据成簇、弱电话候选不合并",
        ie01 and ie01.tier == "strong" and ie01.status == "auto" and p1_same
        and ie03 and ie03.tier == "weak" and ie03.status == "pending" and p23_sep,
        "P1/P1B 同强簇；P2/P3 异簇且 IE-03 pending",
        {"identity_version": v.version if v else None,
         "P1_cluster": p2c.get("P1"), "P1B_cluster": p2c.get("P1B"),
         "P2_cluster": p2c.get("P2"), "P3_cluster": p2c.get("P3"),
         "IE-03_status": ie03.status if ie03 else None}))

    # 3) P1 必要事实 ------------------------------------------------------
    c1 = db.query(Case).filter_by(code="C-001").first()
    j1 = db.query(CaseJudgment).filter_by(id=c1.current_judgment_id).first()
    rr1 = j1.rule_result
    facts = {f["key"]: f for f in rr1["necessary_facts"]}
    chain1 = rr1["support_chain"]
    required_uids = [
        "edge:identity_strong:IE-01",
        "edge:rx_settlement:RX1__ST1", "edge:rx_settlement:RX2__ST2",
        "edge:settle_org:ST1__PH1", "edge:settle_org:ST2__PH2",
        "edge:ingredient_overlap:ST1__ST2",
        "edge:schedule_conflict:RX1__SCH1",
    ]
    missing = [u for u in required_uids if u not in chain1]
    p1_facts_ok = (
        rr1["fired"]
        and facts["distinct_pharmacies"]["passed"]
        and facts["same_day"]["passed"]
        and facts["qty_overlap"]["value"] >= 28
        and facts["same_admin_period"]["passed"]
        and facts["schedule_conflict"]["passed"]
        and not missing
    )
    checks.append(_chk(
        "p1_chain", "P1：两药店同日同成分数量重叠+停诊+同一给药期，支持链完整",
        p1_facts_ok, "全部必要事实成立且 7 条核心边在链",
        {"fired": rr1["fired"],
         "overlap_qty": facts["qty_overlap"]["value"],
         "same_admin_period": facts["same_admin_period"]["passed"],
         "schedule": facts["schedule_conflict"]["passed"],
         "missing_uids": missing, "chain": chain1}))

    # 4) 规则与图模型同链 + 同目标排序（基线网络 P1 居首；物流案 C-009 因多渠道未排除获更高优先级）
    mr1 = j1.model_result or {}
    same_chain = mr1.get("trace_chain_uids") == chain1
    baseline_codes = {f"C-00{i}" for i in range(1, 9)}  # 原 8 案基线网络
    open_rows = [r for r in rank_cases(db, "MULTI_POINT", "model")
                 if r["status"] in ("pending", "reviewing", "new")]
    rule_rows = [r for r in rank_cases(db, "MULTI_POINT", "amount_completeness")
                 if r["status"] in ("pending", "reviewing", "new")]
    base_model = [r for r in open_rows if r["code"] in baseline_codes]
    base_rule = [r for r in rule_rows if r["code"] in baseline_codes]
    top_model = open_rows[0]["code"] if open_rows else None
    top_rule = rule_rows[0]["code"] if rule_rows else None
    c9_row = next((r for r in open_rows if r["code"] == "C-009"), None)
    rank_ok = (base_model and base_model[0]["code"] == "C-001"
               and base_rule and base_rule[0]["code"] == "C-001"
               and c9_row and (c9_row["model_score"] or 0) >= (mr1.get("calibrated_score") or 0))
    checks.append(_chk(
        "same_chain_and_rank", "规则与图模型在 P1 上展示相同支持链；基线网络 P1 居首，C-009 因物流未排除获最高模型优先级",
        same_chain and rank_ok and (mr1.get("calibrated_score") or 0) >= 55,
        "trace_chain_uids == support_chain；C-001 为基线 8 案第一；C-009 模型分≥C-001 且保持待核",
        {"same_chain": same_chain,
         "top_model_overall": top_model, "top_rule_overall": top_rule,
         "baseline_model_top": base_model[0]["code"] if base_model else None,
         "baseline_rule_top": base_rule[0]["code"] if base_rule else None,
         "c009_model_score": c9_row["model_score"] if c9_row else None,
         "c001_model_score": mr1.get("calibrated_score"),
         "rule_score": j1.rule_result.get("raw_score")}))

    # 5) P2/P3 弱身份：只给待核边，不扩展 --------------------------------
    exp23 = expand(db, "beneficiary", "P2")
    uids23 = {e["uid"] for e in exp23["edges"]}
    nodes23 = {n["uid"] for n in exp23["nodes"]}
    weak_ok = ("edge:identity_weak:IE-03" in uids23
               and "person:P3" in exp23["terminal_candidates"]
               and "settlement:ST4" not in nodes23
               and "rx:RX4" not in nodes23)
    checks.append(_chk(
        "p23_weak_no_expand", "P2/P3：弱电话候选只生成待核边，不沿 P3 继续扩展",
        weak_ok, "IE-03 在图；P3 为 terminal；ST4/RX4 不可达",
        {"weak_edge_present": "edge:identity_weak:IE-03" in uids23,
         "terminal": exp23["terminal_candidates"],
         "ST4_reached": "settlement:ST4" in nodes23,
         "RX4_reached": "rx:RX4" in nodes23}))

    # 6) P4 调拨解释 + 否定边不扩展 --------------------------------------
    c3 = db.query(Case).filter_by(code="C-003").first()
    j3 = db.query(CaseJudgment).filter_by(id=c3.current_judgment_id).first()
    inv = next((c for c in j3.counter_result.get("checks", [])
                if c["check_type"] == "inventory"), None)
    ov = db.query(GraphEdge).filter(
        GraphEdge.edge_type == "ingredient_overlap",
        GraphEdge.uid.like("%ST5A%ST5B%")).first()
    exp4 = expand(db, "claim", "ST5A")
    exp4_uids = {e["uid"] for e in exp4["edges"]}
    p4_ok = (c3.status == "excluded_legitimate" and inv
             and inv["status"] == "checked_explains" and "TR4" in inv["detail"]
             and ov and ov.status == "negated"
             and "edge:ingredient_overlap:ST5A__ST5B" not in exp4_uids)
    checks.append(_chk(
        "p4_transfer", "P4：调拨+红冲解释表面重复，重叠边否定且不再扩展",
        p4_ok, "C-003 excluded_legitimate；inventory=checked_explains(TR4)；overlap negated 且扩展不可见",
        {"status": c3.status, "inventory": inv and inv["status"],
         "overlap_status": ov and ov.status,
         "overlap_in_expansion": "edge:ingredient_overlap:ST5A__ST5B" in exp4_uids}))

    # 7) P5 申诉退回 -----------------------------------------------------
    app1 = db.query(Appeal).filter_by(code="APP1").first()
    c4 = db.query(Case).filter_by(code="C-004").first()
    j4 = db.query(CaseJudgment).filter_by(id=c4.current_judgment_id).first()
    ap = next((c for c in j4.counter_result.get("checks", [])
               if c["check_type"] == "appeal"), None)
    checks.append(_chk(
        "p5_appeal_return", "P5：申诉成立且确认药品退回，线索关闭",
        app1 and app1.result == "upheld" and app1.drug_returned
        and c4.status == "closed_returned" and ap and ap["status"] == "confirmed"
        and ap["effect"] == "close",
        "APP1 upheld/returned；C-004 closed_returned；appeal check confirmed/close",
        {"appeal": app1.result if app1 else None,
         "returned": app1.drug_returned if app1 else None,
         "case_status": c4.status, "check": ap and ap["status"]}))

    # 8) 默认受限扩展 ----------------------------------------------------
    exp1 = expand(db, "beneficiary", "P1")
    cutoff = date(2026, 3, 31)
    bad_dates = [e["uid"] for e in exp1["edges"] if e["event_date"]
                 and not (date.fromisoformat("2025-10-03")
                          <= date.fromisoformat(e["event_date"]) <= cutoff)]
    n1 = {n["uid"] for n in exp1["nodes"]}
    bounds_ok = (exp1["params"]["max_hops"] == 2 and exp1["params"]["window_days"] == 180
                 and not bad_dates and "schedule:SCH1" in n1
                 and "org:PH1" in n1 and "org:PH2" in n1)
    checks.append(_chk(
        "restricted_expansion", "默认扩展：同强簇、两跳、180 日，主链节点可达且无越窗边",
        bounds_ok, "hops=2/window=180；SCH1/PH1/PH2 可达；无越窗边",
        {"node_count": len(exp1["nodes"]), "edge_count": len(exp1["edges"]),
         "out_of_window": bad_dates}))

    # 9) 高金额跨机构扩展门槛 --------------------------------------------
    low = high_amount_extension(db, "C-001", freeze=False)
    high = high_amount_extension(db, "C-007", freeze=False)
    bed_reached = any(n["node_type"] == "bed" for n in high.get("nodes", []))
    checks.append(_chk(
        "high_amount_gate", "高金额跨机构扩展须模式显式授权：P1 被拒、C-007 授权并加入床位边",
        low["authorized"] is False and high["authorized"] is True and bed_reached,
        "C-001 金额不足拒绝；C-007 授权且 bed 节点可达",
        {"c001": low.get("reason"), "c007_authorized": high.get("authorized"),
         "added": high.get("extension", {}).get("added_edge_types"),
         "bed_reached": bed_reached}))

    # 10) 快照与不可变判断 ------------------------------------------------
    from .models import GraphSnapshot
    snap1 = db.query(GraphSnapshot).filter_by(snapshot_no=1).first()
    snap_ok = (snap1 and snap1.cutoff_date.isoformat() == "2026-03-31"
               and snap1.identity_version >= 1 and snap1.policy_versions
               and snap1.graph.get("nodes") and j1.version == 1
               and j1.snapshot_no == 1 and j1.disclaimer)
    checks.append(_chk(
        "snapshot_immutable", "快照 #1 冻结四要素，首份判断不可变且带免责声明",
        snap_ok, "cutoff/identity_version/edge_scope/policy_versions 齐全；judgment v1@snap1",
        {"cutoff": snap1.cutoff_date.isoformat() if snap1 else None,
         "identity_version": snap1.identity_version if snap1 else None,
         "policies": len(snap1.policy_versions) if snap1 else 0,
         "judgment_version": j1.version, "snapshot_no": j1.snapshot_no}))

    # 11~13) 配送解释/库存守恒专项
    run_logistics_acceptance(db, checks)

    passed = sum(1 for c in checks if c["passed"])
    return {"overall": passed == len(checks), "passed": passed,
            "total": len(checks), "checks": checks}


def run_logistics_acceptance(db: Session, checks: list):
    """配送解释/库存守恒专项断言（五件套）。"""
    from .models import LogisticsAnalysis, LogisticsFinding

    def latest(code):
        return db.query(LogisticsAnalysis).filter_by(case_code=code) \
            .order_by(LogisticsAnalysis.version.desc()).first()

    # 11) C-009：配送量超库存 → cannot_explain，缺货入案件包且案件不被自动定性
    c9 = db.query(Case).filter_by(code="C-009").first()
    a9 = latest("C-009")
    d9 = a9.detail if a9 else {}
    cons9 = d9.get("conservation", {})
    over_codes = {x["supply"] for x in cons9.get("over_stock", [])}
    open_findings = db.query(LogisticsFinding).filter_by(case_code="C-009").all()
    open_codes = {f.code for f in open_findings if f.status == "open"}
    auto_untouched = c9.status == "reviewing"  # 分析与案件包不改案件定性
    ok9 = (a9 and a9.overall_status == "cannot_explain"
           and a9.explanation_capable is False
           and "SUP-N3" in over_codes
           and any(x["pharmacy"] == "PH6" and x["date"] == "2026-03-10"
                   and x["balance"] < 0 for x in cons9.get("negative_balance_days", []))
           and len(open_codes) >= 3 and auto_untouched)
    checks.append(_chk(
        "logistics_over_stock",
        "C-009：配送量超同批次库存不能作正常解释，负库存/缺货入案件包且不自动定性",
        ok9,
        "cannot_explain；SUP-N3 超库存；存在负库存日；案件包 open；案件状态仍 reviewing",
        {"status": a9.overall_status if a9 else None,
         "over_stock": sorted(over_codes),
         "negative_days": cons9.get("negative_balance_days"),
         "finding_codes": sorted(open_codes), "case_status": c9.status,
         "coverage_interval": cons9.get("coverage_interval")}))

    # 12) 重复签收照片不独立加分 + 时序倒挂被捕获
    sig9 = d9.get("signature_quality", {})
    dup = {g["photo_hash"]: g["deliveries"] for g in sig9.get("duplicate_photo_groups", [])}
    dup_weights = {i["delivery"]: i["evidence_weight"]
                   for i in sig9.get("items", []) if i["delivery"] in ("DL-N1", "DL-N2")}
    tl9 = d9.get("timeline", {})
    tl_types = {v["type"] for v in tl9.get("violations", [])}
    ok12 = (dup.get("PHOTO-DUP-7777") == ["DL-N1", "DL-N2"]
            and dup_weights == {"DL-N1": 1.0, "DL-N2": 0.0}
            and sig9.get("independent_evidence") == 2
            and "sign_before_supply" in tl_types)
    checks.append(_chk(
        "logistics_photo_timeline",
        "重复签收照片只计一次独立证据（不独立加分），签收早于出库的时序倒挂被捕获",
        ok12,
        "DL-N2 复用照片权重 0；独立证据 2/4；sign_before_supply 入违规",
        {"dup_groups": dup, "weights": dup_weights,
         "independent": sig9.get("independent_evidence"),
         "total": sig9.get("total_deliveries"),
         "timeline_violations": sorted(tl_types)}))

    # 13) 对照：C-003 调拨双边守恒可解释；C-010 进货充足逐批守恒、独立签收
    a3 = latest("C-003")
    tr_checks = {t["transfer"]: t for t in
                 a3.detail.get("conservation", {}).get("transfer_checks", [])} if a3 else {}
    a10 = latest("C-010")
    d10 = a10.detail if a10 else {}
    sig10 = d10.get("signature_quality", {})
    ok13 = (a3 and a3.overall_status == "explains"
            and tr_checks.get("TR4", {}).get("qty_conserved") is True
            and tr_checks.get("TR4", {}).get("timeline_ok") is True
            and a10 and a10.overall_status == "explains"
            and a10.explanation_capable is True
            and d10.get("conservation", {}).get("uncovered_total", 1) == 0
            and sig10.get("independent_evidence") == 1
            and sig10.get("duplicate_photo_groups") == [])
    checks.append(_chk(
        "logistics_conserved_controls",
        "C-003 调拨双边守恒、C-010 进货充足逐批守恒，均判定可由配送/调拨正常解释",
        ok13,
        "C-003 explains 且 TR4 守恒；C-010 explains、无未覆盖量、签收独立",
        {"c003_status": a3.overall_status if a3 else None,
         "tr4": tr_checks.get("TR4"),
         "c010_status": a10.overall_status if a10 else None,
         "c010_uncovered": d10.get("conservation", {}).get("uncovered_total"),
         "c010_signature": {"independent": sig10.get("independent_evidence"),
                            "dups": sig10.get("duplicate_photo_groups")}}))
