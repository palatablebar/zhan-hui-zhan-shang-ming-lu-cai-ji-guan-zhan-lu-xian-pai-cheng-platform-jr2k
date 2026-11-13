"""路线排程控制器：发起排程、查询运行与路线明细、影响核对与增量重排。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, route_run_service, visit_service


def create_run(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    run = route_run_service.create_and_execute(
        db,
        params=payload,
        operator_email=principal["email"],
        trigger="manual",
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="route.create",
        entity="route_run",
        entity_id=run.id,
        detail={"run_no": run.run_no, "priorities": (run.params or {}).get("priorities")},
        ip=request.client.host if request.client else None,
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="route.complete",
        entity="route_run",
        entity_id=run.id,
        detail={"status": run.status, "visits": (run.metrics or {}).get("visit_count"), "walk_m": (run.metrics or {}).get("total_walk_m")},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    data = route_run_service.serialize(run)
    data["visits"] = visit_service.list_visits(db, run_id=run.id, limit=400, role=principal["role"])["items"]
    return data


def list_runs(db: Session, **filters) -> dict:
    return route_run_service.list_runs(db, **filters)


def get_run(db: Session, run_id: int, role: str) -> dict:
    run = route_run_service.get_run(db, run_id)
    data = route_run_service.serialize(run)
    data["visits"] = visit_service.list_visits(db, run_id=run.id, limit=400, role=role)["items"]
    return data


def get_current_run(db: Session, role: str) -> dict:
    """对外可见的路线版本（最近一次成功/部分成功）；失败的尝试不会顶掉它。"""
    run = route_run_service.get_visible_run(db)
    data = route_run_service.serialize(run)
    data["visits"] = visit_service.list_visits(db, run_id=run.id, limit=400, role=role)["items"]
    return data


def get_impact(db: Session, run_id: int) -> dict:
    run = route_run_service.get_run(db, run_id)
    return route_run_service.impact_preview(db, run)


def replan(db: Session, run_id: int, principal: dict, request: Request) -> dict:
    base_run = route_run_service.get_run(db, run_id)
    run = route_run_service.execute_replan(db, base_run, operator_email=principal["email"])
    reconcile = (run.metrics or {}).get("reconcile") or {}
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="route.replan",
        entity="route_run",
        entity_id=run.id,
        detail={
            "run_no": run.run_no,
            "base_run_id": base_run.id,
            "status": run.status,
            "invalidated": reconcile.get("invalidated"),
            "carried": reconcile.get("carried"),
            "pool": reconcile.get("pool"),
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    data = route_run_service.serialize(run)
    data["visits"] = visit_service.list_visits(db, run_id=run.id, limit=400, role=principal["role"])["items"]
    return data
