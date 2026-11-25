"""配送/库存守恒分析路由：批次账、守恒匹配、时序、签收质量与缺货反证案件包。

所有结论均为核查建议：缺货反证只进入案件包（open），由人工 accepted/dismissed，
不自动改变案件定性；分析可重算并保留历史版本。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Case, LogisticsFinding
from ..security import can_access_case, current_user
from ..services import logistics as lg

router = APIRouter(prefix="/api/logistics", tags=["logistics"],
                   dependencies=[Depends(current_user)])


def _visible_cases(db, user):
    cases = db.query(Case).order_by(Case.code).all()
    return [c for c in cases
            if can_access_case(user, c.region or "", c.org_codes or [])]


def _require_case(db, code, user) -> Case:
    case = db.query(Case).filter_by(code=code).first()
    if not case:
        raise HTTPException(404, "案件不存在")
    if not can_access_case(user, case.region or "", case.org_codes or []):
        raise HTTPException(403, "您的数据权限不能查看该案件")
    return case


@router.get("/overview")
def overview(db: Session = Depends(get_db), user=Depends(current_user)):
    cases = _visible_cases(db, user)
    data = lg.overview(db, cases)
    data["status_labels"] = {
        "explains": "可由配送正常解释",
        "partial": "材料不足/部分缺陷，仅区间解释",
        "cannot_explain": "配送量超库存，不能作正常解释",
        "no_logistics_record": "无物流材料",
    }
    return data


@router.get("/cases/{code}")
def case_logistics(code: str, db: Session = Depends(get_db),
                   user=Depends(current_user)):
    case = _require_case(db, code, user)
    data = lg.get_latest_analysis(db, code)
    if not data:
        # 尚未持久化（如无物流材料或新案件）：即时试算但不落库
        data = lg.build_case_logistics(db, case, persist=False, user=user.username)
    return data


@router.post("/cases/{code}/rerun")
def rerun(code: str, db: Session = Depends(get_db), user=Depends(current_user)):
    case = _require_case(db, code, user)
    data = lg.build_case_logistics(db, case, persist=True, user=user.username)
    if not data.get("version"):
        raise HTTPException(400, "该案件缺少进销存/供药/签收材料，无法形成守恒分析")
    return data


@router.get("/findings")
def findings(db: Session = Depends(get_db), user=Depends(current_user)):
    cases = _visible_cases(db, user)
    allowed = {c.code for c in cases}
    rows = db.query(LogisticsFinding).order_by(
        LogisticsFinding.case_code, LogisticsFinding.code).all()
    return {"items": [{
        "code": f.code, "case_code": f.case_code, "severity": f.severity,
        "title": f.title, "detail": f.detail, "ingredient": f.ingredient,
        "pharmacy_code": f.pharmacy_code,
        "shortage_qty": float(f.shortage_qty or 0),
        "source_codes": f.source_codes or [], "status": f.status,
        "decided_by": f.decided_by,
        "decided_at": f.decided_at.isoformat() if f.decided_at else None,
        "decision_note": f.decision_note,
    } for f in rows if f.case_code in allowed]}


class FindingIn(BaseModel):
    decision: str  # accepted / dismissed
    note: str | None = None


@router.post("/findings/{code}/adjudicate")
def adjudicate(code: str, body: FindingIn, db: Session = Depends(get_db),
               user=Depends(current_user)):
    row = db.query(LogisticsFinding).filter_by(code=code).first()
    if not row:
        raise HTTPException(404, "缺货反证条目不存在")
    _require_case(db, row.case_code, user)
    try:
        row = lg.adjudicate_finding(db, code, body.decision, body.note, user.username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "code": row.code, "status": row.status,
            "decided_by": row.decided_by,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "note": "缺货反证裁定只影响案件包条目，不自动改变案件定性；案件状态仍须人工在案件页提交。"}
