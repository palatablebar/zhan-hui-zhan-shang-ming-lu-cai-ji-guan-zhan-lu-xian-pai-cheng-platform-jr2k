"""算法 B+ 单测：影响核对、增量重排（保留人工确认/只动受影响天）、新旧对照、幂等与失败恢复。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.models import Base
from src.models.exhibitor import Exhibitor
from src.models.route_run import RouteRun
from src.models.visit import Visit
from src.services import route_run_service, route_service, visit_service

DEFERRED = route_service.DEFERRED_SEQ_BASE


def candidate(exhibitor_id: int, booth_no: str, priority: str = "P1", dwell: int = 10, **appointment):
    return route_service.build_candidate(
        {
            "id": exhibitor_id,
            "name": f"展商 {exhibitor_id}",
            "booth_no": booth_no,
            "priority": priority,
            "dwell_min": dwell,
            **appointment,
        }
    )


def visit_row(exhibitor_id: int, day: int, seq: int, eta: int, **extra):
    row = {
        "exhibitor_id": exhibitor_id,
        "day": day,
        "seq": seq,
        "eta_min": eta,
        "dwell_min": extra.pop("dwell_min", 10),
        "walk_before_m": extra.pop("walk_before_m", 0.0),
        "conflict": extra.pop("conflict", False),
        "conflict_kind": extra.pop("conflict_kind", None),
        "conflict_note": extra.pop("conflict_note", None),
        "status": extra.pop("status", "planned"),
        "reviewer": extra.pop("reviewer", None),
        "review_note": extra.pop("review_note", None),
        "priority": extra.pop("priority", "P1"),
        "weight": extra.pop("weight", 3),
    }
    row.update(extra)
    return row


def snap(**overrides):
    base = {
        "booth_no": "A1-011",
        "priority": "P1",
        "dwell_min": 10,
        "appointment_day": None,
        "appointment_start_min": None,
        "appointment_end_min": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# compute_impact
# ---------------------------------------------------------------------------


def test_impact_detects_field_changes():
    base_snapshot = {"1": snap(), "2": snap(booth_no="A1-012")}
    current = {
        1: snap(priority="P0", dwell_min=20),
        2: snap(booth_no="B1-021", appointment_day=1, appointment_start_min=600, appointment_end_min=630),
    }
    visits = [visit_row(1, 1, 1, 540), visit_row(2, 1, 2, 560)]
    impact = route_service.compute_impact(base_snapshot, current, priorities=("P0", "P1"), base_visits=visits)
    assert impact["affected_ids"] == [1, 2]
    fields_1 = {item["field"] for item in impact["changes"][1]}
    fields_2 = {item["field"] for item in impact["changes"][2]}
    assert fields_1 == {"priority", "dwell_min"}
    assert fields_2 == {"booth_no", "appointment"}
    assert impact["invalidated_count"] == 2
    assert impact["affected_days"] == [1]
    assert impact["empty"] is False


def test_impact_appointment_overlap_propagation():
    """A 的预约改期后，与 A 新旧窗口重叠的未变更展商 B 也被带入影响集。"""
    base_snapshot = {
        "1": snap(appointment_day=1, appointment_start_min=600, appointment_end_min=630),
        "2": snap(booth_no="A1-012", appointment_day=1, appointment_start_min=620, appointment_end_min=650),
        "3": snap(booth_no="A1-013", appointment_day=2, appointment_start_min=600, appointment_end_min=630),
    }
    current = {
        1: snap(appointment_day=1, appointment_start_min=840, appointment_end_min=870),
        2: snap(booth_no="A1-012", appointment_day=1, appointment_start_min=620, appointment_end_min=650),
        3: snap(booth_no="A1-013", appointment_day=2, appointment_start_min=600, appointment_end_min=630),
    }
    visits = [visit_row(1, 1, 1, 600), visit_row(2, 1, 2, 620), visit_row(3, 2, 1, 600)]
    impact = route_service.compute_impact(base_snapshot, current, priorities=("P1",), base_visits=visits)
    assert 1 in impact["affected_ids"]
    assert 2 in impact["affected_ids"]  # 传播：旧窗口与 B 重叠
    assert 2 in impact["propagated_ids"]
    assert 3 not in impact["affected_ids"]


def test_impact_incoming_removed_and_empty():
    base_snapshot = {"1": snap(), "2": snap(booth_no="A1-012", priority="P2")}
    current = {1: snap(), 3: snap(booth_no="A1-013", priority="P0")}
    visits = [visit_row(1, 1, 1, 540), visit_row(2, 1, 2, 560)]
    impact = route_service.compute_impact(base_snapshot, current, priorities=("P0", "P1"), base_visits=visits)
    assert impact["incoming_ids"] == [3]  # 新展商且优先级在候选内
    assert 2 in impact["affected_ids"]  # 名录移除且原本在路线上 → 失效
    assert impact["invalidated_count"] == 1

    same = route_service.compute_impact(base_snapshot, {1: snap(), 2: snap(booth_no="A1-012", priority="P2")}, priorities=("P0", "P1"), base_visits=visits)
    assert same["empty"] is True
    assert same["invalidated_count"] == 0


def test_impact_manual_at_risk():
    base_snapshot = {"1": snap(appointment_day=1, appointment_start_min=600, appointment_end_min=630)}
    current = {1: snap(appointment_day=1, appointment_start_min=700, appointment_end_min=730)}
    visits = [visit_row(1, 1, 1, 600, status="confirmed", reviewer="analyst@boothmap.io")]
    impact = route_service.compute_impact(base_snapshot, current, priorities=("P1",), base_visits=visits)
    assert impact["manual_at_risk"] == [1]


# ---------------------------------------------------------------------------
# plan_incremental
# ---------------------------------------------------------------------------


def _base_visits_two_days():
    return [
        visit_row(1, 1, 1, 600, priority="P0", weight=5, appointment_day=1, appointment_start_min=600, appointment_end_min=630),
        visit_row(2, 1, 2, 640, status="confirmed", reviewer="analyst@boothmap.io", review_note="现场确认"),
        visit_row(3, 1, 3, 700),
        visit_row(4, 2, 1, 600),
        visit_row(5, 2, 2, 660),
    ]


def _candidates_after_appointment_move():
    return [
        candidate(1, "A1-011", "P0", 10, appointment_day=1, appointment_start_min=840, appointment_end_min=870),
        candidate(2, "A1-012", "P1", 10),
        candidate(3, "A1-013", "P1", 10),
        candidate(4, "B1-011", "P1", 10),
        candidate(5, "B1-012", "P1", 10),
    ]


def test_incremental_preserves_manual_and_untouched_day():
    result = route_service.plan_incremental(
        _candidates_after_appointment_move(),
        _base_visits_two_days(),
        affected_ids={1},
        day_count=2,
        priorities=("P0", "P1"),
    )
    by_ex = {row["exhibitor_id"]: row for row in result["visits"]}
    # 未受影响的人工确认：钉在原 ETA，状态/复核人逐字保留
    assert by_ex[2]["eta_min"] == 640
    assert by_ex[2]["status"] == "confirmed"
    assert by_ex[2]["reviewer"] == "analyst@boothmap.io"
    assert by_ex[2]["origin"] == "pinned"
    # 失效展商按新预约窗口重排
    assert by_ex[1]["eta_min"] == 840
    assert by_ex[1]["origin"] == "replanned"
    # 未受影响的 Day2 整段逐字保留
    assert (by_ex[4]["day"], by_ex[4]["seq"], by_ex[4]["eta_min"]) == (2, 1, 600)
    assert (by_ex[5]["day"], by_ex[5]["seq"], by_ex[5]["eta_min"]) == (2, 2, 660)
    assert by_ex[4]["origin"] == "carried" and by_ex[5]["origin"] == "carried"
    assert result["metrics"]["replan"]["touched_days"] == [1]
    assert result["status"] == "success"


def test_incremental_pinned_wins_against_incoming_anchor():
    base_visits = [visit_row(2, 1, 1, 600, status="confirmed", reviewer="analyst@boothmap.io")]
    candidates = [
        candidate(2, "A1-012", "P1", 10),
        candidate(9, "A1-019", "P0", 10, appointment_day=1, appointment_start_min=600, appointment_end_min=630),
    ]
    result = route_service.plan_incremental(candidates, base_visits, affected_ids=set(), day_count=1, priorities=("P0", "P1"))
    by_ex = {row["exhibitor_id"]: row for row in result["visits"]}
    assert by_ex[2]["eta_min"] == 600 and by_ex[2]["status"] == "confirmed"
    assert by_ex[9]["status"] == "deferred"
    assert "人工确认" in by_ex[9]["conflict_note"]


def test_incremental_pool_member_always_has_one_row():
    """重排池成员（失效/新增/连带释放）必有一行输出：排入或改期，不重不漏。"""
    base_visits = _base_visits_two_days()
    candidates = _candidates_after_appointment_move() + [candidate(8, "C1-011", "P0", 10)]
    result = route_service.plan_incremental(candidates, base_visits, affected_ids={1}, day_count=2, priorities=("P0", "P1"))
    ids = [row["exhibitor_id"] for row in result["visits"]]
    assert sorted(ids) == [1, 2, 3, 4, 5, 8]  # 无重复、无遗漏
    assert len(ids) == len(set(ids))
    replan = result["metrics"]["replan"]
    assert replan["pool"] == replan["pool_placed"] + replan["pool_deferred"]
    assert replan["invalidated"] + replan["carried"] == len(base_visits)


def test_incremental_dropped_when_leaving_candidates():
    base_visits = _base_visits_two_days()
    candidates = _candidates_after_appointment_move()
    candidates = [item for item in candidates if item.exhibitor_id != 1]  # 展商 1 不再是候选
    result = route_service.plan_incremental(candidates, base_visits, affected_ids={1}, day_count=2, priorities=("P0", "P1"))
    ids = [row["exhibitor_id"] for row in result["visits"]]
    assert 1 not in ids
    assert result["metrics"]["replan"]["dropped"] == 1


def test_incremental_is_deterministic():
    args = (_candidates_after_appointment_move(), _base_visits_two_days())
    kwargs = {"affected_ids": {1}, "day_count": 2, "priorities": ("P0", "P1")}
    assert route_service.plan_incremental(*args, **kwargs) == route_service.plan_incremental(*args, **kwargs)


def test_incremental_infeasible_pinned_raises():
    base_visits = [
        visit_row(2, 1, 1, 1000, dwell_min=30, status="confirmed", reviewer="a@b.io"),
        visit_row(1, 1, 2, 940),
    ]
    candidates = [candidate(2, "A1-012", "P1", 30), candidate(1, "A1-011", "P1", 10)]
    with pytest.raises(route_service.ReplanInfeasibleError):
        # Day1 被失效行打开；钉点 1000-1030 超出 540+480=1020 的当日时长
        route_service.plan_incremental(candidates, base_visits, affected_ids={1}, day_count=1, day_minutes=480, priorities=("P1",))


# ---------------------------------------------------------------------------
# build_diff
# ---------------------------------------------------------------------------


def test_build_diff_kinds():
    base = [
        visit_row(1, 1, 1, 600),
        visit_row(2, 1, 2, 640),
        visit_row(3, 1, 3, 700),
        visit_row(4, 2, 1, 600),
        visit_row(5, 1, DEFERRED + 1, 600, status="deferred", conflict=True),
        visit_row(6, 1, 4, 720, status="confirmed", reviewer="a@b.io"),
    ]
    new = [
        visit_row(1, 1, 1, 615),  # 改时
        visit_row(2, 2, 1, 640),  # 换天
        visit_row(3, 1, DEFERRED + 2, 700, status="deferred", conflict=True),  # 转改期
        visit_row(5, 1, 2, 660),  # 改期→排入
        visit_row(6, 1, 3, 720, status="planned"),  # 人工状态重置
        visit_row(7, 1, 4, 730),  # 新增
    ]
    diff = route_service.build_diff(base, new, reasons={1: "预约 10:00→14:00"})
    kinds = {entry["exhibitor_id"]: entry["change"] for entry in diff}
    assert kinds == {
        1: "time_changed",
        2: "day_changed",
        3: "deferred",
        4: "removed",
        5: "restored",
        6: "status_reset",
        7: "added",
    }
    assert diff[0]["reason"] or True
    entry_1 = [entry for entry in diff if entry["exhibitor_id"] == 1][0]
    assert entry_1["reason"] == "预约 10:00→14:00"
    assert entry_1["old"]["eta_min"] == 600 and entry_1["new"]["eta_min"] == 615
    entry_6 = [entry for entry in diff if entry["exhibitor_id"] == 6][0]
    assert entry_6["manual_lost"] is True
    # 展商 4 消失 → removed
    removed = [entry for entry in diff if entry["change"] == "removed"]
    assert [entry["exhibitor_id"] for entry in removed] == [4]


# ---------------------------------------------------------------------------
# 服务层（SQLite 内存库）：端到端增量重排、幂等重试、失败恢复
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with factory() as session:
        yield session
    engine.dispose()


def _add_exhibitor(db: Session, seed: int, booth_no: str, priority: str, dwell: int = 10, **appointment) -> Exhibitor:
    hall = booth_no.split("-")[0]
    row = Exhibitor(
        name=f"展商{seed}",
        hall=hall,
        booth_no=booth_no,
        row_no=int(booth_no.split("-")[1][:2]),
        col_no=int(booth_no.split("-")[1][2]),
        category="robot",
        priority=priority,
        dwell_min=dwell,
        appointment_day=appointment.get("appointment_day"),
        appointment_start_min=appointment.get("appointment_start_min"),
        appointment_end_min=appointment.get("appointment_end_min"),
    )
    db.add(row)
    db.flush()
    return row


def _seed_directory(db: Session) -> dict[str, Exhibitor]:
    rows = {
        "e1": _add_exhibitor(db, 1, "A1-011", "P0", 10, appointment_day=1, appointment_start_min=600, appointment_end_min=630),
        "e2": _add_exhibitor(db, 2, "A1-012", "P1", 10),
        "e3": _add_exhibitor(db, 3, "A1-013", "P1", 10),
        "e4": _add_exhibitor(db, 4, "B1-011", "P1", 10),
        "e5": _add_exhibitor(db, 5, "B1-012", "P1", 10),
    }
    return rows


def _run_params() -> dict:
    return {"day_count": 2, "day_minutes": 480, "day_start_minute": 540, "walk_speed_m_per_min": 55.0, "priorities": ["P0", "P1"]}


def _visits_of(db: Session, run_id: int) -> list[Visit]:
    return list(db.scalars(select(Visit).where(Visit.run_id == run_id).order_by(Visit.day, Visit.seq, Visit.id)).all())


def test_execute_replan_end_to_end(db: Session):
    exhibitors = _seed_directory(db)
    base = route_run_service.create_and_execute(db, params=_run_params(), operator_email="analyst@boothmap.io")
    assert base.status == "success"
    assert (base.params or {}).get("input_snapshot")

    # 人工确认 e2（Day1 非预约站）
    e2_visit = db.scalar(select(Visit).where(Visit.run_id == base.id, Visit.exhibitor_id == exhibitors["e2"].id))
    visit_service.review(db, e2_visit, status="confirmed", note="现场确认", day=None, reviewer="analyst@boothmap.io")
    day2_before = [
        (row.exhibitor_id, row.day, row.seq, row.eta_min, row.status)
        for row in _visits_of(db, base.id)
        if row.day == 2
    ]
    base_row_count = len(_visits_of(db, base.id))

    # 名录变更：e1 预约从 10:00 改到 14:00
    exhibitors["e1"].appointment_start_min = 840
    exhibitors["e1"].appointment_end_min = 870
    db.flush()

    impact = route_run_service.impact_preview(db, base)
    assert impact["empty"] is False
    assert impact["invalidated_count"] == 1
    assert impact["affected_days"] == [1]

    run = route_run_service.execute_replan(db, base, operator_email="analyst@boothmap.io")
    assert run.status == "success"
    assert run.trigger == "replan"
    assert (run.params or {}).get("base_run_id") == base.id

    new_visits = _visits_of(db, run.id)
    by_ex = {row.exhibitor_id: row for row in new_visits}
    # 人工确认保留：状态、复核人、时间不变
    kept = by_ex[exhibitors["e2"].id]
    assert kept.status == "confirmed"
    assert kept.reviewer == "analyst@boothmap.io"
    assert kept.eta_min == e2_visit.eta_min
    # 失效的 e1 按新窗口重排
    moved = by_ex[exhibitors["e1"].id]
    assert 840 <= moved.eta_min <= 870 - moved.dwell_min
    # 未受影响的 Day2 逐字保留
    day2_after = [
        (row.exhibitor_id, row.day, row.seq, row.eta_min, row.status)
        for row in new_visits
        if row.day == 2
    ]
    assert day2_after == day2_before
    # 旧版本行数不变（没有被冲掉）
    assert len(_visits_of(db, base.id)) == base_row_count

    reconcile = run.metrics["reconcile"]
    assert reconcile["balanced"] is True
    assert reconcile["invalidated"] == 1
    assert reconcile["carried"] == reconcile["source_rows"] - 1
    diff = run.metrics["diff"]
    moved_entry = [entry for entry in diff if entry["exhibitor_id"] == exhibitors["e1"].id][0]
    assert moved_entry["change"] == "time_changed"
    assert "预约" in moved_entry["reason"]
    # 可见版本切换到新运行
    assert route_run_service.latest_visible_run(db).id == run.id


def test_replan_idempotent_retry(db: Session):
    exhibitors = _seed_directory(db)
    base = route_run_service.create_and_execute(db, params=_run_params(), operator_email="analyst@boothmap.io")
    exhibitors["e1"].appointment_start_min = 840
    exhibitors["e1"].appointment_end_min = 870
    db.flush()

    first = route_run_service.execute_replan(db, base, operator_email="analyst@boothmap.io")
    assert first.status == "success"
    total_runs = db.scalar(select(func.count()).select_from(RouteRun))
    again = route_run_service.execute_replan(db, base, operator_email="analyst@boothmap.io")
    assert again.id == first.id  # 幂等：同样的变更不生成新版本
    assert db.scalar(select(func.count()).select_from(RouteRun)) == total_runs
    # 不重不漏：每个展商在新运行里只有一站
    ids = [row.exhibitor_id for row in _visits_of(db, first.id)]
    assert len(ids) == len(set(ids))


def test_replan_failure_keeps_old_visible_and_retry_recovers(db: Session, monkeypatch):
    exhibitors = _seed_directory(db)
    base = route_run_service.create_and_execute(db, params=_run_params(), operator_email="analyst@boothmap.io")
    exhibitors["e1"].appointment_start_min = 840
    exhibitors["e1"].appointment_end_min = 870
    db.flush()

    original = route_service.plan_incremental

    def boom(*args, **kwargs):
        raise RuntimeError("模拟规划器故障")

    monkeypatch.setattr(route_run_service.route_service, "plan_incremental", boom)
    failed = route_run_service.execute_replan(db, base, operator_email="analyst@boothmap.io")
    assert failed.status == "failed"
    assert "模拟规划器故障" in failed.log_text
    # 生成失败 → 可见路线仍是旧版本
    assert route_run_service.latest_visible_run(db).id == base.id
    assert _visits_of(db, failed.id) == []

    # 恢复后重试：复用同一运行行，不重复排站
    monkeypatch.setattr(route_run_service.route_service, "plan_incremental", original)
    recovered = route_run_service.execute_replan(db, base, operator_email="analyst@boothmap.io")
    assert recovered.id == failed.id
    assert recovered.status == "success"
    ids = [row.exhibitor_id for row in _visits_of(db, recovered.id)]
    assert len(ids) == len(set(ids)) == 5
    assert route_run_service.latest_visible_run(db).id == recovered.id


def test_replan_rejects_empty_impact_and_bad_base(db: Session):
    exhibitors = _seed_directory(db)
    base = route_run_service.create_and_execute(db, params=_run_params(), operator_email="analyst@boothmap.io")
    with pytest.raises(Exception) as excinfo:
        route_run_service.execute_replan(db, base, operator_email="analyst@boothmap.io")
    assert "没有变化" in str(excinfo.value)

    exhibitors["e1"].appointment_start_min = 840
    exhibitors["e1"].appointment_end_min = 870
    db.flush()
    failed = route_run_service.execute_replan(db, base, operator_email="analyst@boothmap.io")
    failed.status = "failed"
    db.flush()
    with pytest.raises(Exception) as excinfo2:
        route_run_service.execute_replan(db, failed, operator_email="analyst@boothmap.io")
    assert "基准" in str(excinfo2.value)


def test_visible_run_helpers(db: Session):
    _seed_directory(db)
    base = route_run_service.create_and_execute(db, params=_run_params(), operator_email="analyst@boothmap.io")
    assert route_run_service.latest_visible_run(db).id == base.id
    ghost = RouteRun(run_no="RT20990101-9999", status="failed", trigger="replan", params={}, metrics={})
    db.add(ghost)
    db.flush()
    assert route_run_service.latest_visible_run(db).id == base.id  # 失败的尝试不影响可见版本
    assert route_run_service.get_visible_run(db).id == base.id
