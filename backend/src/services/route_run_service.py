"""路线排程服务：加载名录 → 调用算法 B → 落库 RouteRun + Visit；变更后影响核对与增量重排。"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CHANGE_FIELD_LABELS,
    PRIORITY_DWELL_MIN,
    PRIORITY_WEIGHT,
    ROUTE_STATUS_LABELS,
    SHOW_DAY_DATES,
    TRIGGER_LABELS,
)
from src.models.exhibitor import Exhibitor
from src.models.route_run import RouteRun
from src.models.visit import Visit
from src.services import booth_service, route_service
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc, local_now, show_day_utc, utcnow

VISIBLE_STATUSES = ("success", "partial")


def serialize(run: RouteRun) -> dict:
    params = {key: value for key, value in (run.params or {}).items() if key != "input_snapshot"}
    return {
        "id": run.id,
        "run_no": run.run_no,
        "day_count": run.day_count,
        "status": run.status,
        "status_label": ROUTE_STATUS_LABELS.get(run.status, run.status),
        "trigger": run.trigger,
        "trigger_label": TRIGGER_LABELS.get(run.trigger, run.trigger),
        "base_run_id": params.get("base_run_id"),
        "operator_email": run.operator_email,
        "params": params,
        "metrics": run.metrics or {},
        "log_text": run.log_text,
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
    }


def next_run_no(db: Session) -> str:
    prefix = f"RT{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(RouteRun).where(RouteRun.run_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:04d}"


def load_candidates(db: Session, priorities: list[str]) -> tuple[list[route_service.Candidate], int]:
    rows = db.scalars(select(Exhibitor).order_by(Exhibitor.id)).all()
    candidates: list[route_service.Candidate] = []
    invalid = 0
    for row in rows:
        if row.priority not in priorities:
            continue
        try:
            candidates.append(
                route_service.build_candidate(
                    {
                        "id": row.id,
                        "name": row.name,
                        "booth_no": row.booth_no,
                        "priority": row.priority,
                        "dwell_min": row.dwell_min or PRIORITY_DWELL_MIN.get(row.priority, 8),
                        "appointment_day": row.appointment_day,
                        "appointment_start_min": row.appointment_start_min,
                        "appointment_end_min": row.appointment_end_min,
                    }
                )
            )
        except booth_service.BoothFormatError:
            invalid += 1
    return candidates, invalid


def snapshot_exhibitors(db: Session) -> dict[str, dict]:
    """全部展商排程输入的快照（键为字符串化的展商 id，便于 JSON 存储）。"""
    rows = db.scalars(select(Exhibitor).order_by(Exhibitor.id)).all()
    return {
        str(row.id): route_service.snapshot_of_row(
            {
                "booth_no": row.booth_no,
                "priority": row.priority,
                "dwell_min": row.dwell_min or PRIORITY_DWELL_MIN.get(row.priority, 8),
                "appointment_day": row.appointment_day,
                "appointment_start_min": row.appointment_start_min,
                "appointment_end_min": row.appointment_end_min,
            }
        )
        for row in rows
    }


def execute_run(db: Session, run: RouteRun, *, priorities: list[str], invalid_rows: int = 0) -> RouteRun:
    run.status = "running"
    params = run.params or {}
    snapshot = snapshot_exhibitors(db)
    candidates, invalid = load_candidates(db, priorities)
    try:
        result = route_service.plan_routes(
            candidates,
            day_count=int(params.get("day_count") or run.day_count or 2),
            day_minutes=int(params.get("day_minutes") or 480),
            day_start_minute=int(params.get("day_start_minute") or 540),
            speed=float(params.get("walk_speed_m_per_min") or 55.0),
            priorities=priorities,
            max_visits=params.get("max_visits"),
        )
        db.execute(delete(Visit).where(Visit.run_id == run.id))
        for row in result["visits"]:
            eta_at = show_day_utc(row["day"], row["eta_min"]) if row["day"] in SHOW_DAY_DATES else None
            db.add(
                Visit(
                    run_id=run.id,
                    exhibitor_id=row["exhibitor_id"],
                    day=row["day"],
                    seq=row["seq"],
                    eta_min=row["eta_min"],
                    eta_at=eta_at,
                    dwell_min=row["dwell_min"],
                    walk_before_m=row["walk_before_m"],
                    conflict=bool(row["conflict"]),
                    conflict_kind=row["conflict_kind"],
                    conflict_note=row["conflict_note"],
                    status=row["status"],
                )
            )
        run.metrics = result["metrics"]
        run.log_text = result["log"]
        run.status = result["status"]
        run.finished_at = utcnow()
        run.params = {
            **params,
            "priorities": priorities,
            "invalid_rows": invalid + invalid_rows,
            "input_snapshot": snapshot,
        }
    except Exception as exc:  # noqa: BLE001 - 运行失败要留痕
        run.status = "failed"
        run.log_text = f"排程失败：{exc}"
        run.metrics = {"error": str(exc)[:300]}
        run.finished_at = utcnow()
    db.flush()
    return run


def create_and_execute(
    db: Session,
    *,
    params: dict,
    operator_email: str,
    trigger: str = "manual",
    seed_key: str | None = None,
) -> RouteRun:
    if seed_key:
        existing = db.scalar(select(RouteRun).where(RouteRun.seed_key == seed_key))
        if existing is not None:
            return existing
    priorities = [str(item).upper() for item in (params.get("priorities") or ["P0", "P1"])]
    run = RouteRun(
        run_no=next_run_no(db),
        day_count=int(params.get("day_count") or 2),
        status="running",
        operator_email=operator_email,
        trigger=trigger,
        params={**params, "priorities": priorities},
        seed_key=seed_key,
    )
    db.add(run)
    db.flush()
    return execute_run(db, run, priorities=priorities)


def list_runs(db: Session, *, status: str | None, limit: int, offset: int) -> dict:
    stmt = select(RouteRun)
    count_stmt = select(func.count()).select_from(RouteRun)
    if status:
        stmt = stmt.where(RouteRun.status == status)
        count_stmt = count_stmt.where(RouteRun.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(RouteRun.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [serialize(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def get_run(db: Session, run_id: int) -> RouteRun:
    run = db.get(RouteRun, run_id)
    if run is None:
        raise AppError("NOT_FOUND", 404, {"entity": "route_run", "id": run_id})
    return run


def latest_visible_run(db: Session) -> RouteRun | None:
    """当前对外可见的路线版本：最近一次成功/部分成功的运行（失败的尝试不会顶掉旧路线）。"""
    return db.scalar(
        select(RouteRun).where(RouteRun.status.in_(VISIBLE_STATUSES)).order_by(RouteRun.id.desc()).limit(1)
    )


def get_visible_run(db: Session) -> RouteRun:
    run = latest_visible_run(db)
    if run is None:
        raise AppError("NOT_FOUND", 404, {"entity": "route_run", "id": "current"})
    return run


def _exhibitor_weights(db: Session) -> dict[int, tuple[str, int]]:
    rows = db.scalars(select(Exhibitor)).all()
    return {row.id: (row.priority, PRIORITY_WEIGHT.get(row.priority, 0)) for row in rows}


def _visit_rows(db: Session, run_id: int, weights: dict[int, tuple[str, int]]) -> list[dict]:
    rows = db.scalars(select(Visit).where(Visit.run_id == run_id).order_by(Visit.day, Visit.seq, Visit.id)).all()
    return [
        {
            "id": row.id,
            "exhibitor_id": row.exhibitor_id,
            "day": row.day,
            "seq": row.seq,
            "eta_min": row.eta_min,
            "dwell_min": row.dwell_min,
            "walk_before_m": float(row.walk_before_m or 0),
            "conflict": bool(row.conflict),
            "conflict_kind": row.conflict_kind,
            "conflict_note": row.conflict_note,
            "status": row.status,
            "reviewer": row.reviewer,
            "review_note": row.review_note,
            "priority": weights.get(row.exhibitor_id, (None, 0))[0],
            "weight": weights.get(row.exhibitor_id, (None, 0))[1],
        }
        for row in rows
    ]


def _impact(db: Session, run: RouteRun) -> dict:
    """影响核对（内部版）：基准版本的输入快照 vs 当前名录。"""
    params = run.params or {}
    snapshot = params.get("input_snapshot")
    if not snapshot:
        raise AppError("CONFLICT", 409, {"run_id": run.id}, "该版本缺少输入快照，无法做影响核对，请先重新全量排程")
    priorities = [str(item).upper() for item in (params.get("priorities") or ["P0", "P1"])]
    weights = _exhibitor_weights(db)
    base_visits = _visit_rows(db, run.id, weights)
    current = snapshot_exhibitors(db)
    impact = route_service.compute_impact(snapshot, current, priorities=priorities, base_visits=base_visits)
    impact["base_visits"] = base_visits
    impact["priorities"] = priorities
    return impact


def impact_preview(db: Session, run: RouteRun) -> dict:
    """影响核对（接口版）：字段级变更、失效到访、新入候选、受影响的人工确认。"""
    impact = _impact(db, run)
    exhibitors = {row.id: row for row in db.scalars(select(Exhibitor)).all()}

    def name_of(ex_id: int) -> str:
        row = exhibitors.get(ex_id)
        return row.name if row else f"#{ex_id}"

    removed = set(impact["removed_ids"])
    propagated = set(impact["propagated_ids"])
    changes = []
    for ex_id in impact["affected_ids"]:
        fields = [
            {**change, "label": CHANGE_FIELD_LABELS.get(change["field"], change["field"])}
            for change in impact["changes"].get(ex_id, [])
        ]
        if ex_id in removed:
            fields = [{"field": "removed", "label": "名录", "old": "在册", "new": "已移除"}]
        changes.append(
            {
                "exhibitor_id": ex_id,
                "exhibitor_name": name_of(ex_id),
                "propagated": ex_id in propagated,
                "fields": fields,
            }
        )
    return {
        "run_id": run.id,
        "run_no": run.run_no,
        "empty": impact["empty"],
        "affected_days": impact["affected_days"],
        "invalidated_count": impact["invalidated_count"],
        "changes": changes,
        "incoming": [
            {"exhibitor_id": ex_id, "exhibitor_name": name_of(ex_id)} for ex_id in impact["new_candidate_ids"]
        ],
        "manual_at_risk": [
            {"exhibitor_id": ex_id, "exhibitor_name": name_of(ex_id)} for ex_id in impact["manual_at_risk"]
        ],
    }


def _replan_fingerprint(base_run: RouteRun, snapshot: dict[str, dict], priorities: list[str]) -> str:
    params = base_run.params or {}
    payload = {
        "base_run_id": base_run.id,
        "snapshot": snapshot,
        "priorities": priorities,
        "day_count": params.get("day_count") or base_run.day_count or 2,
        "day_minutes": params.get("day_minutes") or 480,
        "day_start_minute": params.get("day_start_minute") or 540,
        "walk_speed_m_per_min": params.get("walk_speed_m_per_min") or 55.0,
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _diff_reasons(impact: dict) -> dict[int, str]:
    reasons: dict[int, str] = {}
    manual_risk = set(impact["manual_at_risk"])
    for ex_id, fields in impact["changes"].items():
        parts = [f"{CHANGE_FIELD_LABELS.get(item['field'], item['field'])} {item['old']}→{item['new']}" for item in fields]
        if ex_id in manual_risk:
            parts.append("人工确认需复核")
        reasons[ex_id] = "；".join(parts) or "名录变更"
    for ex_id in impact["propagated_ids"]:
        reasons[ex_id] = "预约与变更展商时段重叠，连带重排"
    for ex_id in impact["new_candidate_ids"]:
        reasons[ex_id] = "新入候选展商"
    for ex_id in impact["removed_ids"]:
        reasons[ex_id] = "已从名录移除"
    return reasons


def execute_replan(db: Session, base_run: RouteRun, *, operator_email: str) -> RouteRun:
    """增量重排：只重算受影响展商与受影响天；失败留痕且旧版本仍是对外可见路线。

    幂等：同一基准版本 + 同一名录状态的指纹相同，重试复用同一运行行；
    失败的运行重试时先清空其到访再重算，不会重复排站。
    """
    if base_run.status not in VISIBLE_STATUSES:
        raise AppError("CONFLICT", 409, {"run_id": base_run.id}, "只有成功/部分成功的版本才能作为增量重排基准")
    impact = _impact(db, base_run)
    if impact["empty"]:
        raise AppError("CONFLICT", 409, {"run_id": base_run.id}, "名录相对该版本没有变化，无需重排")

    base_params = base_run.params or {}
    priorities = impact["priorities"]
    snapshot = snapshot_exhibitors(db)
    seed_key = f"replan-{base_run.id}-{_replan_fingerprint(base_run, snapshot, priorities)}"
    existing = db.scalar(select(RouteRun).where(RouteRun.seed_key == seed_key))
    if existing is not None and existing.status in VISIBLE_STATUSES:
        return existing  # 幂等：同样的变更重试直接返回已生成的版本

    run = existing or RouteRun(run_no=next_run_no(db), day_count=base_run.day_count, trigger="replan", seed_key=seed_key)
    run.status = "running"
    run.operator_email = operator_email
    run.started_at = utcnow()
    run.finished_at = None
    params = {
        key: base_params.get(key)
        for key in ("day_count", "day_minutes", "day_start_minute", "walk_speed_m_per_min", "max_visits")
        if base_params.get(key) is not None
    }
    params.update(
        {
            "priorities": priorities,
            "mode": "incremental",
            "base_run_id": base_run.id,
            "base_run_no": base_run.run_no,
            "affected_exhibitor_ids": impact["affected_ids"],
            "affected_days": impact["affected_days"],
        }
    )
    run.params = params
    run.day_count = int(params.get("day_count") or base_run.day_count or 2)
    db.add(run)
    db.flush()

    try:
        # 纯读 + 纯函数规划：抛错时会话里没有未落库的到访行，旧版本不受影响
        candidates, invalid = load_candidates(db, priorities)
        result = route_service.plan_incremental(
            candidates,
            impact["base_visits"],
            affected_ids=impact["affected_ids"],
            incoming_ids=impact["new_candidate_ids"],
            day_count=run.day_count,
            day_minutes=int(params.get("day_minutes") or 480),
            day_start_minute=int(params.get("day_start_minute") or 540),
            speed=float(params.get("walk_speed_m_per_min") or 55.0),
            priorities=priorities,
            max_visits=params.get("max_visits"),
        )
        reasons = _diff_reasons(impact)
        diff = route_service.build_diff(impact["base_visits"], result["visits"], reasons=reasons)
        exhibitor_map = {row.id: row for row in db.scalars(select(Exhibitor)).all()}
        for entry in diff:
            exhibitor = exhibitor_map.get(entry["exhibitor_id"])
            entry["exhibitor_name"] = exhibitor.name if exhibitor else f"#{entry['exhibitor_id']}"
            entry["booth_no"] = exhibitor.booth_no if exhibitor else ""
    except Exception as exc:  # noqa: BLE001 - 失败留痕，旧版本仍是可见路线
        db.execute(delete(Visit).where(Visit.run_id == run.id))  # 清掉复用失败行的残留，绝不重复排站
        run.status = "failed"
        run.log_text = f"增量重排失败：{exc}"
        run.metrics = {"error": str(exc)[:300], "base_run_id": base_run.id}
        run.finished_at = utcnow()
        db.flush()
        return run

    db.execute(delete(Visit).where(Visit.run_id == run.id))
    for row in result["visits"]:
        eta_at = show_day_utc(row["day"], row["eta_min"]) if row["day"] in SHOW_DAY_DATES else None
        db.add(
            Visit(
                run_id=run.id,
                exhibitor_id=row["exhibitor_id"],
                day=row["day"],
                seq=row["seq"],
                eta_min=row["eta_min"],
                eta_at=eta_at,
                dwell_min=row["dwell_min"],
                walk_before_m=row["walk_before_m"],
                conflict=bool(row["conflict"]),
                conflict_kind=row["conflict_kind"],
                conflict_note=row["conflict_note"],
                status=row["status"],
                reviewer=row.get("reviewer"),
                review_note=row.get("review_note"),
            )
        )
    replan = result["metrics"]["replan"]
    base_visits = impact["base_visits"]
    out_rows = result["visits"]
    source_scheduled = sum(1 for row in base_visits if row["seq"] < 9000 and row["status"] != "deferred")
    out_scheduled = sum(1 for row in out_rows if row["seq"] < 9000 and row["status"] != "deferred")
    by_day_before: dict[int, int] = {}
    for row in base_visits:
        if row["seq"] < 9000 and row["status"] != "deferred":
            by_day_before[row["day"]] = by_day_before.get(row["day"], 0) + 1
    by_day_after = {item["day"]: item["visits"] for item in result["metrics"]["by_day"]}
    carried_out = sum(1 for row in out_rows if row["origin"] in ("carried", "pinned", "kept_deferred"))
    pool_out = sum(1 for row in out_rows if row["origin"] in ("released", "replanned", "incoming"))
    reconcile = {
        "source_run_id": base_run.id,
        "source_run_no": base_run.run_no,
        "source_rows": len(base_visits),
        "source_scheduled": source_scheduled,
        "invalidated": replan["invalidated"],
        "carried": replan["carried"],
        "carried_manual": replan["carried_manual"],
        "released": replan["released"],
        "incoming": replan["incoming"],
        "pool": replan["pool"],
        "dropped": replan["dropped"],
        "output_rows": len(out_rows),
        "output_scheduled": out_scheduled,
        "output_deferred": len(out_rows) - out_scheduled,
        "by_day": [
            {"day": day, "before": by_day_before.get(day, 0), "after": by_day_after.get(day, 0)}
            for day in sorted(set(by_day_before) | set(by_day_after))
        ],
        "touched_days": replan["touched_days"],
    }
    reconcile["balanced"] = (
        reconcile["source_rows"] == reconcile["invalidated"] + reconcile["carried"]
        and reconcile["output_rows"] == carried_out + pool_out
        and reconcile["carried"] == carried_out + replan["released"]
        and reconcile["pool"] == pool_out
    )
    run.metrics = {**result["metrics"], "diff": diff, "reconcile": reconcile}
    run.log_text = result["log"]
    run.status = result["status"]
    run.params = {**params, "invalid_rows": invalid, "input_snapshot": snapshot}
    run.finished_at = utcnow()
    db.flush()
    return run
