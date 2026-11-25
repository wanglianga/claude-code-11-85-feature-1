"""配送解释核对：用药品实物流、库存变动与签收链回答“集中购药能否由配送解释”。

五个相互独立、可逐案复算的部件：
1. 库存批次账（batch ledger）：按 药店×成分×批次 汇总进销存台账，逐笔计算流水余额，
   标出负库存（缺货）与台账缺口。
2. 调拨处方守恒器（conservation）：
   - 逐批守恒匹配：被解释交易（供药/配送出库）按“先入后出”向同批次进货/调拨/退回入库量匹配，
     证据强，但依赖台账质量；
   - 按日库存流量平衡：台账缺口时给覆盖区间 [保守可覆盖, 乐观上界]，覆盖广但只能给区间；
   - 同时核对调拨双边守恒、处方与供药数量守恒。
   配送/供药量超过当时可用库存时记为 unmatched，不能作为正常解释。
3. 配送路线时序对齐器（timeline）：进货/调拨→出库→签收的时间先后，倒挂即违规。
4. 签收证据质量器（signature quality）：签名核验 + 照片指纹去重；
   重复签收照片只计一次独立证据，重复项不独立加分。
5. 缺货反证器（shortage）：把负库存、超库存配送、时序倒挂等汇入案件包
   LogisticsFinding（open 待核），只汇集证据，不自动改变案件定性。

结论 overall_status：explains（可解释）/ partial（材料不足只能区间解释）/
cannot_explain（存在超库存配送或缺货，配送不能作正常解释）。
"""
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import (
    Delivery, InventoryLedger, LogisticsAnalysis, LogisticsFinding,
    Prescription, Settlement, SourceRecord, StockTransfer, Supply,
)

INFLOW_TYPES = ("purchase_in", "transfer_in", "return_in")
OUTFLOW_TYPES = ("sale_out", "transfer_out")


def _src_code(db, sid):
    if not sid:
        return None
    s = db.get(SourceRecord, sid)
    return s.code if s else None


def _d(x):
    if isinstance(x, datetime):
        return x.date()
    return x


# --------------------------------------------------------------- 1. 案件范围
def case_scope(db, case) -> dict:
    """收集案件锚定的结算/供药/签收/台账/调拨/处方集合。"""
    a = case.anchors or {}
    st_codes = list(a.get("settlement_codes", []))
    pharmacy_codes = set(a.get("pharmacies", []))
    ingredients = set()
    if a.get("ingredient"):
        ingredients.add(a["ingredient"])

    settlements = [db.query(Settlement).filter_by(code=c).first() for c in st_codes]
    for st in settlements:
        if st:
            pharmacy_codes.add(st.pharmacy_code)
    supplies = db.query(Supply).filter(Supply.settlement_code.in_(st_codes)).all() \
        if st_codes else []
    # 弱身份等无结算锚点的案件：回退到按参保人归集供药/配送链
    if not supplies and a.get("person_codes"):
        supplies = db.query(Supply).filter(
            Supply.person_code.in_(a["person_codes"])).all()
    supplies = sorted(supplies, key=lambda s: (s.supply_date, s.code))
    for s in supplies:
        pharmacy_codes.add(s.pharmacy_code)
        ingredients.add(s.ingredient)

    # 锚定结算的处方成分补全（与反证编排同口径，避免同店其他药品串扰）
    for c in st_codes:
        st = db.query(Settlement).filter_by(code=c).first()
        if st and st.rx_code:
            rx = db.query(Prescription).filter_by(code=st.rx_code).first()
            if rx:
                ingredients.update(i.ingredient for i in rx.items)

    sup_codes = [s.code for s in supplies]
    deliveries = db.query(Delivery).filter(Delivery.supply_code.in_(sup_codes)).all() \
        if sup_codes else []
    deliveries = sorted(deliveries, key=lambda d: (d.signed_at, d.code))

    led_q = db.query(InventoryLedger)
    if ingredients:
        led_q = led_q.filter(InventoryLedger.ingredient.in_(sorted(ingredients)))
    if pharmacy_codes:
        led_q = led_q.filter(InventoryLedger.pharmacy_code.in_(sorted(pharmacy_codes)))
    # 无成分/机构范围（如纯住院结算、无供药链的案件）：不得回全表，视为无台账
    ledgers = sorted(led_q.all(), key=lambda x: (x.pharmacy_code, x.ingredient,
                                                 x.batch_no, x.txn_date, x.id)) \
        if (ingredients and pharmacy_codes) else []
    transfers = [t for t in db.query(StockTransfer)
                 .filter(StockTransfer.ingredient.in_(sorted(ingredients))).all()
                 if t.from_pharmacy in pharmacy_codes or t.to_pharmacy in pharmacy_codes] \
        if ingredients else []
    return {"settlement_codes": st_codes, "pharmacies": sorted(pharmacy_codes),
            "ingredients": sorted(ingredients), "supplies": supplies,
            "deliveries": deliveries, "ledgers": ledgers, "transfers": transfers}


# --------------------------------------------------------------- 2. 库存批次账
def build_batch_ledger(ledgers: list[InventoryLedger]) -> dict:
    """返回 {批次键: {rows: 逐笔流水(含running_balance/negative), totals, negative_count}}。"""
    groups: dict = defaultdict(list)
    for row in ledgers:
        key = (row.pharmacy_code, row.ingredient, row.batch_no)
        groups[key].append(row)
    out = {}
    for key, rows in groups.items():
        rows = sorted(rows, key=lambda r: (r.txn_date, r.id))
        running = 0.0
        enriched, negatives = [], 0
        totals = defaultdict(float)
        for r in rows:
            running += float(r.change_qty)
            totals[r.change_type] += abs(float(r.change_qty))
            neg = running < -1e-9
            if neg:
                negatives += 1
            enriched.append({
                "code": r.code, "pharmacy": r.pharmacy_code,
                "ingredient": r.ingredient, "batch": r.batch_no,
                "date": r.txn_date.isoformat(), "change_type": r.change_type,
                "change_qty": float(r.change_qty), "ref_no": r.ref_no,
                "counterpart": r.counterpart_org_code,
                "running_balance": round(running, 3), "negative": neg,
            })
        out[f"{key[0]}|{key[1]}|{key[2]}"] = {
            "pharmacy": key[0], "ingredient": key[1], "batch": key[2],
            "rows": enriched, "totals": dict(totals),
            "negative_count": negatives,
            "final_balance": round(running, 3),
        }
    return out


# --------------------------------------------------------------- 3. 守恒器
def _inflow_buckets(ledgers, pharmacy, ingredient, batch):
    """同店同成分同批次、可用于覆盖出库的入库桶（含到货日，先入先出）。"""
    buckets = []
    for r in ledgers:
        if (r.pharmacy_code == pharmacy and r.ingredient == ingredient
                and r.batch_no == batch and r.change_type in INFLOW_TYPES):
            buckets.append({"ref": r.ref_no, "date": r.txn_date,
                            "qty": abs(float(r.change_qty)), "remaining": abs(float(r.change_qty)),
                            "type": r.change_type, "code": r.code})
    return sorted(buckets, key=lambda b: (b["date"], b["code"]))


def run_conservation(db, scope) -> dict:
    ledgers = scope["ledgers"]
    supplies = scope["supplies"]
    ledger_keys = {(l.pharmacy_code, l.ingredient) for l in ledgers}
    sale_refs = {(l.pharmacy_code, l.ingredient, l.ref_no)
                 for l in ledgers if l.change_type == "sale_out"}

    matches, gap_demands = [], []
    covered_total = uncovered_total = demand_total = 0.0
    over_stock = []
    for sp in supplies:
        demand = float(sp.qty)
        demand_total += demand
        has_ledger = (sp.pharmacy_code, sp.ingredient) in ledger_keys
        has_sale_row = (sp.pharmacy_code, sp.ingredient, sp.settlement_code) in sale_refs
        buckets = [b for b in _inflow_buckets(ledgers, sp.pharmacy_code,
                                              sp.ingredient, sp.batch_no)
                   if b["date"] <= sp.supply_date]
        need, used = demand, []
        for b in buckets:
            if need <= 0:
                break
            take = min(need, b["remaining"])
            if take > 0:
                b["remaining"] -= take
                need -= take
                used.append({"ref": b["ref"], "inflow_type": b["type"],
                             "inflow_date": b["date"].isoformat(),
                             "batch": sp.batch_no, "qty": round(take, 3)})
        matched = round(demand - need, 3)
        unmatched = round(need, 3)
        covered_total += matched
        gap = not has_ledger or not has_sale_row
        if gap:
            gap_demands.append(sp.code)
        if unmatched > 0 and not gap:
            # 台账齐全仍无法覆盖：配送/供药量超过当时同批次可用库存
            uncovered_total += unmatched
            over_stock.append({"supply": sp.code, "settlement": sp.settlement_code,
                               "pharmacy": sp.pharmacy_code, "batch": sp.batch_no,
                               "date": sp.supply_date.isoformat(),
                               "unmatched_qty": unmatched})
        elif gap:
            # 台账缺口：无法证伪也无法证实，归入区间上界而非负库存
            uncovered_total += unmatched
        matches.append({
            "supply": sp.code, "settlement": sp.settlement_code,
            "pharmacy": sp.pharmacy_code, "ingredient": sp.ingredient,
            "batch": sp.batch_no, "date": sp.supply_date.isoformat(),
            "demand_qty": demand, "matched_qty": matched,
            "unmatched_qty": unmatched, "matched_from": used,
            "ledger_complete": has_ledger and has_sale_row,
            # 台账缺口优先：账面即使能匹配，缺少出库行仍不能算已证实
            "status": ("ledger_gap" if gap else
                       "covered" if unmatched <= 0 else "over_stock"),
        })

    # 按日库存流量平衡（按 药店×成分×批次 独立累计，避免跨批次互相掩盖缺货）
    daily = defaultdict(lambda: {"inflow": 0.0, "outflow": 0.0})
    for r in ledgers:
        k = (r.pharmacy_code, r.ingredient, r.batch_no, r.txn_date.isoformat())
        if r.change_type in INFLOW_TYPES:
            daily[k]["inflow"] += abs(float(r.change_qty))
        else:
            daily[k]["outflow"] += abs(float(r.change_qty))
    daily_balance, running_map = [], defaultdict(float)
    demand_by_day = defaultdict(float)
    for sp in supplies:
        demand_by_day[(sp.pharmacy_code, sp.ingredient, sp.batch_no,
                       sp.supply_date.isoformat())] += float(sp.qty)
    all_keys = sorted(set(daily) | set(demand_by_day))
    negative_days = []
    for k in all_keys:
        batch_key = (k[0], k[1], k[2])
        running_map[batch_key] += daily[k]["inflow"] - daily[k]["outflow"]
        bal = round(running_map[batch_key], 3)
        if bal < -1e-9:
            negative_days.append({"pharmacy": k[0], "ingredient": k[1], "batch": k[2],
                                  "date": k[3], "balance": bal})
        daily_balance.append({"pharmacy": k[0], "ingredient": k[1], "batch": k[2],
                              "date": k[3],
                              "inflow": round(daily[k]["inflow"], 3),
                              "outflow": round(daily[k]["outflow"], 3),
                              "demand": round(demand_by_day.get(k, 0.0), 3),
                              "running_balance": bal})

    # 覆盖区间：保守下界=已证实匹配；台账缺口案的需求总量整笔只能给乐观上界
    gap_supplies = {m["supply"] for m in matches if m["status"] == "ledger_gap"}
    gap_demand = sum(m["demand_qty"] for m in matches if m["status"] == "ledger_gap")
    interval_only = bool(gap_supplies)
    lower = round(covered_total, 3)
    upper = round(covered_total + gap_demand, 3) if interval_only else lower

    # 调拨双边守恒
    transfer_checks = []
    for t in scope["transfers"]:
        out_row = next((l for l in ledgers
                        if l.pharmacy_code == t.from_pharmacy and l.ingredient == t.ingredient
                        and l.change_type == "transfer_out" and l.ref_no == t.code), None)
        in_row = next((l for l in ledgers
                       if l.pharmacy_code == t.to_pharmacy and l.ingredient == t.ingredient
                       and l.change_type == "transfer_in" and l.ref_no == t.code), None)
        qty_ok = bool(out_row and in_row
                      and abs(abs(float(out_row.change_qty)) - float(t.qty)) < 1e-9
                      and abs(abs(float(in_row.change_qty)) - float(t.qty)) < 1e-9)
        order_ok = t.ship_date <= t.receive_date
        transfer_checks.append({
            "transfer": t.code, "from": t.from_pharmacy, "to": t.to_pharmacy,
            "ingredient": t.ingredient, "qty": float(t.qty),
            "ship_date": t.ship_date.isoformat(), "receive_date": t.receive_date.isoformat(),
            "qty_conserved": qty_ok, "timeline_ok": order_ok,
            "status": "conserved" if qty_ok and order_ok else "mismatch"})

    # 处方-供药数量守恒
    rx_checks = []
    for sp in supplies:
        if not sp.rx_item_code:
            continue
        from ..models import PrescriptionItem
        item = db.query(PrescriptionItem).filter_by(code=sp.rx_item_code).first()
        if item:
            rx_checks.append({
                "supply": sp.code, "rx_item": item.code,
                "rx_qty": float(item.qty), "supply_qty": float(sp.qty),
                "conserved": abs(float(item.qty) - float(sp.qty)) < 1e-9})

    return {
        "method": "batch_first_daily_fallback",
        "method_note": "优先逐批守恒匹配（证据强、依赖台账质量）；台账缺口处改用按日库存流量平衡，"
                       "只能给出覆盖区间，不做单点结论。",
        "demand_total": round(demand_total, 3),
        "covered_total": round(covered_total, 3),
        "uncovered_total": round(uncovered_total, 3),
        "coverage_interval": [lower, upper],
        "interval_only": interval_only,
        "ledger_gap_supplies": sorted(set(gap_demands)),
        "unverified_gap_demand": round(gap_demand, 3),
        "matches": matches,
        "daily_balance": daily_balance,
        "negative_balance_days": negative_days,
        "over_stock": over_stock,
        "transfer_checks": transfer_checks,
        "rx_checks": rx_checks,
    }


# --------------------------------------------------------------- 4. 配送时序对齐
def run_timeline(scope) -> dict:
    supplies = {s.code: s for s in scope["supplies"]}
    ledgers = scope["ledgers"]
    events, violations = [], []

    # 批次首次到货 → 出库 → 签收
    first_inflow = {}
    for r in ledgers:
        if r.change_type not in INFLOW_TYPES:
            continue
        key = (r.pharmacy_code, r.ingredient, r.batch_no)
        if key not in first_inflow or r.txn_date < first_inflow[key]:
            first_inflow[key] = r.txn_date

    for dl in scope["deliveries"]:
        sp = supplies.get(dl.supply_code)
        if not sp:
            continue
        inflow_day = first_inflow.get((sp.pharmacy_code, sp.ingredient, sp.batch_no))
        chain = [
            {"stage": "批次到货", "at": inflow_day.isoformat() if inflow_day else None},
            {"stage": "供药出库", "at": sp.supply_date.isoformat()},
            {"stage": "配送签收", "at": dl.signed_at.isoformat()},
        ]
        item_violations = []
        if inflow_day and inflow_day > sp.supply_date:
            item_violations.append("outflow_before_inflow")
        if _d(dl.signed_at) < sp.supply_date:
            item_violations.append("sign_before_supply")
        events.append({"delivery": dl.code, "supply": sp.code,
                       "pharmacy": sp.pharmacy_code, "address": dl.address,
                       "chain": chain, "violations": item_violations})
        for v in item_violations:
            violations.append({"delivery": dl.code, "supply": sp.code, "type": v,
                               "detail": {"signed_at": dl.signed_at.isoformat(),
                                          "supply_date": sp.supply_date.isoformat(),
                                          "inflow_date": inflow_day.isoformat() if inflow_day else None}})

    # 调拨时序
    for t in scope["transfers"]:
        if t.ship_date > t.receive_date:
            violations.append({"delivery": None, "supply": None,
                               "type": "transfer_order_inverted",
                               "detail": {"transfer": t.code,
                                          "ship_date": t.ship_date.isoformat(),
                                          "receive_date": t.receive_date.isoformat()}})
    return {"events": events, "violations": violations}


# --------------------------------------------------------------- 5. 签收证据质量
def run_signature_quality(scope) -> dict:
    seen_photos = set()
    items, dup_groups = [], defaultdict(list)
    for dl in scope["deliveries"]:
        dup_groups[dl.photo_hash].append(dl.code)
    for dl in scope["deliveries"]:
        is_dup = bool(dl.photo_hash) and dl.photo_hash in seen_photos
        if dl.photo_hash:
            seen_photos.add(dl.photo_hash)
        if not dl.photo_hash:
            weight, reason = 0.0, "无签收照片指纹，不计独立证据"
        elif is_dup:
            weight, reason = 0.0, "重复签收照片，不得独立加分"
        elif not dl.signature_match:
            weight, reason = 0.0, "签名/身份核验不匹配"
        else:
            weight, reason = 1.0, "本人签收且照片独立"
        items.append({
            "delivery": dl.code, "supply": dl.supply_code, "person": dl.person_code,
            "receiver": dl.receiver_name, "signed_at": dl.signed_at.isoformat(),
            "signature_match": dl.signature_match, "photo_hash": dl.photo_hash,
            "evidence_weight": weight, "quality_note": reason,
            "evidence_note": dl.evidence_note,
        })
    duplicate_groups = [{"photo_hash": h, "deliveries": codes}
                        for h, codes in dup_groups.items() if h and len(codes) > 1]
    independent = sum(1 for i in items if i["evidence_weight"] >= 1.0)
    total = len(items)
    return {
        "items": items,
        "independent_evidence": independent,
        "total_deliveries": total,
        "duplicate_photo_groups": duplicate_groups,
        "duplicate_count": sum(len(g["deliveries"]) - 1 for g in duplicate_groups),
        "mismatch_count": sum(1 for d in scope["deliveries"] if not d.signature_match),
        "score": round(independent / total, 3) if total else 0.0,
        "rule_note": "同一照片指纹在多份签收单中出现时只计一次独立证据，重复项不独立加分；"
                     "签名核验不匹配同样不计分。",
    }


# --------------------------------------------------------------- 6. 缺货反证 → 案件包
def _finding_seed(scope, conservation, timeline, sig) -> list[dict]:
    """把分析中发现的反证素材转成案件包条目（不落库，由持久化层统一编号）。"""
    seeds = []
    for neg in conservation["negative_balance_days"]:
        seeds.append({
            "severity": "critical", "title": "库存流水出现负余额（缺货仍出库）",
            "detail": f"{neg['pharmacy']} {neg['ingredient']} 在 {neg['date']} "
                      f"日终库存余额为 {neg['balance']:g}，出库量超过同期入库累计，药品实物来源不足。",
            "ingredient": neg["ingredient"], "pharmacy": neg["pharmacy"],
            "shortage_qty": abs(neg["balance"]), "finding_key": f"neg:{neg['pharmacy']}:{neg['date']}"})
    for os_ in conservation["over_stock"]:
        seeds.append({
            "severity": "critical", "title": "配送/供药量超过当时可用库存，不能作正常解释",
            "detail": f"{os_['pharmacy']} 批次 {os_['batch']} 在 {os_['date']} 的供药 {os_['supply']} "
                      f"有 {os_['unmatched_qty']:g} 最小单位无同批次在库量可覆盖（先入先出匹配后仍缺）。",
            "ingredient": None, "pharmacy": os_["pharmacy"],
            "shortage_qty": os_["unmatched_qty"],
            "finding_key": f"overstock:{os_['supply']}"})
    for v in timeline["violations"]:
        seeds.append({
            "severity": "warning", "title": f"配送时序倒挂：{v['type']}",
            "detail": f"单据 {v['delivery'] or v['detail'].get('transfer')} 时序 {v['detail']}，"
                      "签收链不能证明药品实物流向。",
            "ingredient": None, "pharmacy": None, "shortage_qty": 0,
            "finding_key": f"timeline:{v['type']}:{v['delivery'] or v['detail'].get('transfer')}"})
    for g in sig["duplicate_photo_groups"]:
        seeds.append({
            "severity": "warning", "title": "重复签收照片，未形成独立签收证据",
            "detail": f"照片 {g['photo_hash']} 被 {len(g['deliveries'])} 份签收单复用"
                      f"（{ '、'.join(g['deliveries']) }），只计一次独立证据，重复项不加分。",
            "ingredient": None, "pharmacy": None, "shortage_qty": 0,
            "finding_key": f"dupphoto:{g['photo_hash']}"})
    for it in sig["items"]:
        if not it["signature_match"]:
            seeds.append({
                "severity": "warning", "title": "签收签名/身份核验不匹配",
                "detail": f"签收单 {it['delivery']} 签名核验不匹配，代收真实性需人工核验。",
                "ingredient": None, "pharmacy": None, "shortage_qty": 0,
                "finding_key": f"mismatch:{it['delivery']}"})
    return seeds


def _refresh_findings(db, case_code: str, seeds: list[dict], source_codes: list[str]):
    """开放条目随分析刷新；已人工裁定（accepted/dismissed）的条目与编码保留。

    重新生成 open 条目时优先复用同标题条目的历史编码，保证重算前后案件包编号稳定可追溯。
    """
    existing = db.query(LogisticsFinding).filter_by(case_code=case_code).all()
    open_code_by_title = {f.title: f.code for f in existing if f.status == "open"}
    decided_titles = {f.title for f in existing if f.status != "open"}
    used_codes = {f.code for f in existing}
    db.query(LogisticsFinding).filter_by(case_code=case_code, status="open").delete()
    db.flush()
    seq = 0
    out = []
    for s in seeds:
        if s["title"] in decided_titles:  # 同名条目已人工裁定，不重复开单
            continue
        if s["title"] in open_code_by_title:
            code = open_code_by_title[s["title"]]  # 复用历史编码
        else:
            seq += 1
            code = f"LF-{case_code}-{seq:02d}"
            while code in used_codes:
                seq += 1
                code = f"LF-{case_code}-{seq:02d}"
        used_codes.add(code)
        row = LogisticsFinding(
            case_code=case_code, code=code, severity=s["severity"], title=s["title"],
            detail=s["detail"], ingredient=s["ingredient"], pharmacy_code=s["pharmacy"],
            shortage_qty=s["shortage_qty"], source_codes=source_codes, status="open")
        db.add(row)
        out.append(row)
    db.flush()
    return db.query(LogisticsFinding).filter_by(case_code=case_code) \
        .order_by(LogisticsFinding.code).all()


# --------------------------------------------------------------- 7. 总装与持久化
def _overall(conservation, timeline, sig, scope) -> tuple[str, bool]:
    hard_shortage = bool(conservation["over_stock"]
                         or conservation["negative_balance_days"])
    if hard_shortage:
        return "cannot_explain", False
    soft_defects = bool(timeline["violations"]
                        or sig["independent_evidence"] < sig["total_deliveries"])
    if conservation["interval_only"] or soft_defects:
        return "partial", False
    if conservation["uncovered_total"] > 0:
        return "partial", False
    return "explains", True


def build_case_logistics(db: Session, case, persist: bool = True,
                         user: str = "system") -> dict:
    scope = case_scope(db, case)
    if not (scope["supplies"] or scope["ledgers"] or scope["deliveries"]):
        # 无任何进销存/供药/签收材料：不形成分析记录，解释空间仍在
        return {"version": None, "overall_status": "no_logistics_record",
                "explanation_capable": False, "demand_qty": 0, "covered_qty": 0,
                "uncovered_qty": 0, "coverage_interval": [0, 0], "interval_only": False,
                "shortage_qty": 0, "ledger_gap_count": 0, "timeline_violations": 0,
                "duplicate_photo_groups": 0, "open_findings": 0,
                "detail": {"case_code": case.code,
                           "scope_note": "窗口内未查到进销存台账、供药单或配送签收单，无法核对实物流。"}}
    batch_ledger = build_batch_ledger(scope["ledgers"])
    conservation = run_conservation(db, scope)
    timeline = run_timeline(scope)
    sig = run_signature_quality(scope)

    source_codes = sorted({c for c in (
        [_src_code(db, x.source_id) for x in scope["ledgers"]]
        + [_src_code(db, x.source_id) for x in scope["supplies"]]
        + [_src_code(db, x.source_id) for x in scope["deliveries"]]
        + [_src_code(db, t.source_id) for t in scope["transfers"]]) if c})

    seeds = _finding_seed(scope, conservation, timeline, sig)
    shortage_qty = round(sum(s["shortage_qty"] for s in seeds), 3)
    findings = _refresh_findings(db, case.code, seeds, source_codes) if persist else []
    finding_dicts = [{
        "code": f.code, "severity": f.severity, "title": f.title, "detail": f.detail,
        "ingredient": f.ingredient, "pharmacy": f.pharmacy_code,
        "shortage_qty": float(f.shortage_qty or 0), "source_codes": f.source_codes or [],
        "status": f.status, "decided_by": f.decided_by,
        "decided_at": f.decided_at.isoformat() if f.decided_at else None,
        "decision_note": f.decision_note,
    } for f in findings]

    overall, capable = _overall(conservation, timeline, sig, scope)
    detail = {
        "case_code": case.code,
        "scope": {"settlement_codes": scope["settlement_codes"],
                  "pharmacies": scope["pharmacies"],
                  "ingredients": scope["ingredients"],
                  "supply_count": len(scope["supplies"]),
                  "delivery_count": len(scope["deliveries"]),
                  "ledger_row_count": len(scope["ledgers"])},
        "batch_ledger": list(batch_ledger.values()),
        "conservation": conservation,
        "timeline": timeline,
        "signature_quality": sig,
        "shortage_findings": finding_dicts,
        "rules": [
            "逐批守恒匹配证据强但依赖库存台账质量；台账缺口改用按日流量平衡，只给覆盖区间。",
            "配送/供药量超过当时可用库存时不能作为正常解释（cannot_explain）。",
            "重复签收照片不得独立加分；签名核验不匹配不计独立证据。",
            "缺货反证进入案件包并保持 open，由人工裁定，不自动改变案件定性。",
        ],
    }
    summary = {
        "overall_status": overall,
        "explanation_capable": capable,
        "demand_qty": conservation["demand_total"],
        "covered_qty": conservation["covered_total"],
        "uncovered_qty": conservation["uncovered_total"],
        "coverage_interval": conservation["coverage_interval"],
        "interval_only": conservation["interval_only"],
        "shortage_qty": shortage_qty,
        "ledger_gap_count": len(conservation["ledger_gap_supplies"]),
        "timeline_violations": len(timeline["violations"]),
        "duplicate_photo_groups": len(sig["duplicate_photo_groups"]),
        "open_findings": sum(1 for f in finding_dicts if f["status"] == "open"),
    }
    detail["summary"] = summary

    if not persist:
        return {"version": None, **summary, "detail": detail}

    last = db.query(LogisticsAnalysis).filter_by(case_code=case.code) \
        .order_by(LogisticsAnalysis.version.desc()).first()
    version = 1 if not last else last.version + 1
    row = LogisticsAnalysis(
        case_code=case.code, version=version,
        method_profile="daily_hybrid" if conservation["interval_only"] else "batch",
        overall_status=overall, explanation_capable=capable,
        demand_qty=summary["demand_qty"], covered_qty=summary["covered_qty"],
        uncovered_qty=summary["uncovered_qty"], shortage_qty=shortage_qty,
        ledger_gap_count=summary["ledger_gap_count"],
        timeline_violations=summary["timeline_violations"],
        duplicate_photo_groups=summary["duplicate_photo_groups"],
        created_by=user, detail=detail)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "version": row.version, **summary,
            "created_at": row.created_at.isoformat(), "detail": detail}


def get_latest_analysis(db: Session, case_code: str) -> dict | None:
    row = db.query(LogisticsAnalysis).filter_by(case_code=case_code) \
        .order_by(LogisticsAnalysis.version.desc()).first()
    if not row:
        return None
    return {"id": row.id, "version": row.version,
            "created_at": row.created_at.isoformat(),
            "method_profile": row.method_profile,
            "overall_status": row.overall_status,
            "explanation_capable": row.explanation_capable,
            "demand_qty": float(row.demand_qty or 0),
            "covered_qty": float(row.covered_qty or 0),
            "uncovered_qty": float(row.uncovered_qty or 0),
            "shortage_qty": float(row.shortage_qty or 0),
            "ledger_gap_count": row.ledger_gap_count,
            "timeline_violations": row.timeline_violations,
            "duplicate_photo_groups": row.duplicate_photo_groups,
            "detail": row.detail}


def overview(db: Session, cases: list) -> dict:
    rows = db.query(LogisticsAnalysis).all()
    latest = {}
    for r in rows:  # 同案取最新版本
        if r.case_code not in latest or r.version > latest[r.case_code].version:
            latest[r.case_code] = r
    by_status = defaultdict(int)
    for r in latest.values():
        by_status[r.overall_status] += 1
    findings = db.query(LogisticsFinding).all()
    return {
        "analyzed_cases": len(latest),
        "total_cases": len(cases),
        "by_status": dict(by_status),
        "open_findings": sum(1 for f in findings if f.status == "open"),
        "accepted_findings": sum(1 for f in findings if f.status == "accepted"),
        "dismissed_findings": sum(1 for f in findings if f.status == "dismissed"),
        "total_shortage_qty": round(sum(float(f.shortage_qty or 0) for f in findings), 3),
        "cases": [{
            "case_code": c.code, "title": c.title, "status": c.status,
            "logistics": (None if c.code not in latest else {
                "version": latest[c.code].version,
                "overall_status": latest[c.code].overall_status,
                "explanation_capable": latest[c.code].explanation_capable,
                "covered_qty": float(latest[c.code].covered_qty or 0),
                "demand_qty": float(latest[c.code].demand_qty or 0),
                "uncovered_qty": float(latest[c.code].uncovered_qty or 0),
                "shortage_qty": float(latest[c.code].shortage_qty or 0),
            })} for c in cases],
    }


def adjudicate_finding(db: Session, code: str, status: str, note: str | None,
                       user: str) -> LogisticsFinding:
    if status not in ("accepted", "dismissed"):
        raise ValueError("裁定状态只能是 accepted 或 dismissed")
    f = db.query(LogisticsFinding).filter_by(code=code).first()
    if not f:
        raise ValueError("缺货反证条目不存在")
    f.status = status
    f.decided_by = user
    f.decided_at = datetime.utcnow()
    f.decision_note = note
    db.commit()
    db.refresh(f)
    return f
