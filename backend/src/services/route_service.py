"""算法 B：带时间窗的两日观展路线排程（纯函数，可单测）。

口径（README §10）：
- 每天可用 480 分钟（默认 09:00 入场），权重 P0=5 / P1=3 / P2=1；
- 预约时段是硬窗：到访必须落在 [appointment_start, appointment_end] 内；
- 先按预约锚点落位，再按「权重 / 插入增量距离」贪心插入其余展商，最后 2-opt 改进馆内顺序；
- 预约窗口重叠 → 冲突双方都打标记，只保留权重高者，另一个转「改期」；
- 输出：两日路线（含 ETA / 步行增量）、总步行、覆盖权重、冲突与超时项。

增量重排（算法 B+，README §10 末尾）：
- `snapshot_of_row` / `compute_impact` 对比运行时的输入快照与当前名录，找出受影响展商；
- `plan_incremental` 只重排受影响天：未受影响的人工确认站钉成固定锚点（pinned），
  未受影响的天整段逐字保留，失效/新增/连带释放的候选进入重排池；
- `build_diff` 输出新旧对照（改时/换天/新增/移出/转改期/改期→排入），供页面标注。

不依赖数据库，也不做任何网络访问；同样输入两次结果逐字一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from src.constants.enums import (
    CONFLICT_KIND_LABELS,
    PRIORITY_WEIGHT,
    VISIT_STATUS_LABELS,
)
from src.services import booth_service

DEFAULT_DAY_MINUTES = 480
DEFAULT_DAY_START_MINUTE = 540  # 09:00
DEFAULT_SPEED_M_PER_MIN = 55.0
DEFAULT_PRIORITIES = ("P0", "P1")
MIN_INSERT_DELTA_M = 10.0
DEFERRED_SEQ_BASE = 9000
PINNED_WINDOW_SLACK_MIN = 1  # 钉点窗口的取整冗余：基准 ETA 是浮点取整，窗口放宽 1 分钟保证原链可行

_WEIGHT = PRIORITY_WEIGHT


@dataclass
class Candidate:
    exhibitor_id: int
    name: str
    hall: str
    booth_no: str
    priority: str
    dwell_min: int
    weight: int
    position: booth_service.BoothPosition
    appt_day: int | None = None
    appt_start: int | None = None
    appt_end: int | None = None
    conflict: bool = False
    conflict_kind: str | None = None
    conflict_note: str | None = None
    status: str = "planned"
    pinned: bool = False  # 人工确认站：窗口被钉死在原 ETA，冲突/超时取舍时不可被丢弃

    @property
    def is_anchor(self) -> bool:
        return self.appt_day is not None and self.appt_start is not None


class ReplanInfeasibleError(RuntimeError):
    """增量重排不可行（如人工确认站在当日时长内排不下）——调用方应失败留痕并保留旧版本。"""


def format_minute(minute: int) -> str:
    """把「当天第几分钟」转成 HH:MM。"""
    minute = int(round(minute))
    return f"{minute // 60:02d}:{minute % 60:02d}"


def build_candidate(row: dict, *, default_dwell: int | None = None) -> Candidate:
    """把一行展商数据转成排程候选。展位号非法会抛 BoothFormatError。"""
    position = booth_service.parse_booth_no(row.get("booth_no", ""))
    priority = str(row.get("priority") or "P2").upper()
    if priority not in _WEIGHT:
        raise ValueError(f"未知优先级：{priority}")
    dwell = row.get("dwell_min")
    if dwell is None:
        dwell = default_dwell if default_dwell is not None else 8
    return Candidate(
        exhibitor_id=int(row["id"]),
        name=str(row.get("name") or ""),
        hall=position.hall,
        booth_no=position.booth_no,
        priority=priority,
        dwell_min=int(dwell),
        weight=_WEIGHT[priority],
        position=position,
        appt_day=_optional_int(row.get("appointment_day")),
        appt_start=_optional_int(row.get("appointment_start_min")),
        appt_end=_optional_int(row.get("appointment_end_min")),
    )


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


@dataclass
class _Timeline:
    feasible: bool
    eta: list[int] = field(default_factory=list)
    walk_m: float = 0.0
    legs: list[float] = field(default_factory=list)
    end_min: int = 0
    conflict_flags: list[bool] = field(default_factory=list)


def _travel(previous: Candidate | None, current: Candidate, speed: float) -> float:
    if previous is None:
        return booth_service.gate_distance(current.position) / speed
    return booth_service.walking_distance(previous.position, current.position) / speed


def evaluate_order(
    order: list[Candidate],
    day: int,
    *,
    day_start_minute: int = DEFAULT_DAY_START_MINUTE,
    day_minutes: int = DEFAULT_DAY_MINUTES,
    speed: float = DEFAULT_SPEED_M_PER_MIN,
) -> _Timeline:
    """按给定顺序走一遍，返回 ETA / 步行距离；硬窗或当日时长不满足则不可行。"""
    clock = float(day_start_minute)
    previous: Candidate | None = None
    total_walk = 0.0
    etas: list[int] = []
    legs: list[float] = []
    for item in order:
        if previous is None:
            distance = booth_service.gate_distance(item.position)
        else:
            distance = booth_service.walking_distance(previous.position, item.position)
        travel = distance / speed
        arrival = clock + travel
        start = arrival
        if item.is_anchor and item.appt_day == day:
            window_start = float(item.appt_start or 0)
            window_end = float(item.appt_end if item.appt_end is not None else window_start + item.dwell_min)
            if start < window_start:
                start = window_start
            if start + item.dwell_min > window_end + 1e-6:
                return _Timeline(feasible=False)
        end = start + item.dwell_min
        total_walk += distance
        legs.append(round(distance, 3))
        etas.append(int(round(start)))
        clock = end
        previous = item
    if clock > day_start_minute + day_minutes + 1e-6:
        return _Timeline(feasible=False)
    return _Timeline(
        feasible=True,
        eta=etas,
        walk_m=round(total_walk, 3),
        legs=legs,
        end_min=int(round(clock)),
    )


def _anchor_clusters(anchors: list[Candidate]) -> list[list[Candidate]]:
    ordered = sorted(anchors, key=lambda item: (item.appt_start or 0, -item.weight, item.exhibitor_id))
    clusters: list[list[Candidate]] = []
    cluster_end = -1.0
    for anchor in ordered:
        start = float(anchor.appt_start or 0)
        if clusters and start < cluster_end - 1e-6:
            clusters[-1].append(anchor)
            cluster_end = max(cluster_end, float(anchor.appt_end or start + anchor.dwell_min))
        else:
            clusters.append([anchor])
            cluster_end = float(anchor.appt_end or start + anchor.dwell_min)
    return clusters


def _resolve_anchor_conflicts(candidates: list[Candidate], day_count: int) -> tuple[dict[int, list[Candidate]], list[Candidate]]:
    """把预约展商按天落位，窗口重叠的只留权重高者，并给双方打冲突标记。"""
    anchors_by_day: dict[int, list[Candidate]] = {day: [] for day in range(1, day_count + 1)}
    deferred: list[Candidate] = []
    for candidate in candidates:
        if not candidate.is_anchor:
            continue
        if candidate.appt_day not in anchors_by_day:
            candidate.conflict = True
            candidate.conflict_kind = "overtime"
            candidate.conflict_note = f"预约在展期第 {candidate.appt_day} 天，超出本次 {day_count} 天排程"
            candidate.status = "deferred"
            deferred.append(candidate)
            continue
        anchors_by_day[candidate.appt_day].append(candidate)

    for day, anchors in anchors_by_day.items():
        accepted: list[Candidate] = []
        for cluster in _anchor_clusters(anchors):
            pinned_members = [item for item in cluster if item.pinned]
            if pinned_members:
                # 人工确认站永远保留；与其重叠的预约一律转改期
                accepted.extend(pinned_members)
                for loser in [item for item in cluster if not item.pinned]:
                    loser.conflict = True
                    loser.conflict_kind = "appointment"
                    loser.conflict_note = "与人工确认到访时段重叠，保留已确认站，本条转为改期"
                    loser.status = "deferred"
                    deferred.append(loser)
                continue
            if len(cluster) == 1:
                accepted.append(cluster[0])
                continue
            winner = sorted(cluster, key=lambda item: (-item.weight, item.appt_start or 0, item.exhibitor_id))[0]
            others = [item for item in cluster if item is not winner]
            winner.conflict = True
            winner.conflict_kind = "appointment"
            winner.conflict_note = "与 " + "、".join(f"{item.name}({item.booth_no})" for item in others) + " 的预约时段重叠，保留权重高者"
            accepted.append(winner)
            for loser in others:
                loser.conflict = True
                loser.conflict_kind = "appointment"
                loser.conflict_note = f"与 {winner.name}({winner.booth_no}) 预约时段重叠，权重较低转为改期"
                loser.status = "deferred"
                deferred.append(loser)
        accepted.sort(key=lambda item: (item.appt_start or 0, item.exhibitor_id))
        anchors_by_day[day] = accepted
    return anchors_by_day, deferred


def _drop_infeasible_anchor(
    order: list[Candidate],
    day: int,
    ctx: dict,
    deferred: list[Candidate],
) -> tuple[list[Candidate], _Timeline | None]:
    """预约锚点本身超出当日时长时，逐个丢弃权重最低者直到可行；人工确认站（pinned）不可丢弃。"""
    result = evaluate_order(order, day, **ctx)
    while result is not None and not result.feasible and any(item.is_anchor and not item.pinned for item in order):
        droppable = [item for item in order if item.is_anchor and not item.pinned]
        victim = sorted(droppable, key=lambda item: (item.weight, item.exhibitor_id))[0]
        victim.conflict = True
        victim.conflict_kind = "overtime"
        victim.conflict_note = f"预约窗口 {format_minute(victim.appt_start or 0)} 导致当日超出 {ctx['day_minutes']} 分钟"
        victim.status = "deferred"
        deferred.append(victim)
        order = [item for item in order if item is not victim]
        result = evaluate_order(order, day, **ctx)
    if result is not None and not result.feasible and any(item.pinned for item in order):
        raise ReplanInfeasibleError(f"Day{day} 人工确认站在 {ctx['day_minutes']} 分钟时长内排不下")
    return order, result


def _greedy_fill(
    order: list[Candidate],
    day: int,
    ctx: dict,
    fillers: list[Candidate],
) -> tuple[list[Candidate], _Timeline | None, list[Candidate]]:
    current = evaluate_order(order, day, **ctx)
    if current is None or not current.feasible:
        return order, current, []
    placed: list[Candidate] = []
    for candidate in fillers:
        best: tuple[float, float, int, int, list[Candidate], _Timeline] | None = None
        for position in range(len(order) + 1):
            trial = order[:position] + [candidate] + order[position:]
            result = evaluate_order(trial, day, **ctx)
            if result is None or not result.feasible:
                continue
            delta = max(result.walk_m - current.walk_m, MIN_INSERT_DELTA_M)
            score = candidate.weight / delta
            if best is None or (score, delta) > (best[0], best[1]) + (1e-12, 1e-12):
                best = (score, delta, position, candidate.exhibitor_id, trial, result)
        if best is None:
            continue
        order = best[4]
        current = best[5]
        placed.append(candidate)
    return order, current, placed


def _two_opt(order: list[Candidate], day: int, ctx: dict, rounds: int = 24) -> tuple[list[Candidate], _Timeline | None]:
    best = list(order)
    best_result = evaluate_order(best, day, **ctx)
    if best_result is None or not best_result.feasible:
        return best, best_result
    improved = True
    tick = 0
    while improved and tick < rounds:
        improved = False
        tick += 1
        length = len(best)
        for i in range(length - 1):
            if best[i].is_anchor:
                continue
            for j in range(i + 1, length):
                if best[j].is_anchor:
                    continue
                if any(item.is_anchor for item in best[i : j + 1]):
                    continue
                trial = best[:i] + list(reversed(best[i : j + 1])) + best[j + 1 :]
                result = evaluate_order(trial, day, **ctx)
                if result is not None and result.feasible and result.walk_m < best_result.walk_m - 1e-6:
                    best, best_result = trial, result
                    improved = True
    return best, best_result


def plan_routes(
    candidates: list[Candidate],
    *,
    day_count: int = 2,
    day_minutes: int = DEFAULT_DAY_MINUTES,
    day_start_minute: int = DEFAULT_DAY_START_MINUTE,
    speed: float = DEFAULT_SPEED_M_PER_MIN,
    priorities: tuple[str, ...] | list[str] = DEFAULT_PRIORITIES,
    max_visits: int | None = None,
) -> dict:
    """排两日路线，返回 visits / metrics / log / status（纯函数）。"""
    if day_count < 1:
        raise ValueError("day_count 至少为 1")
    allowed = {str(item).upper() for item in priorities}
    pool = [replace(item) for item in candidates if item.priority in allowed]
    pool.sort(key=lambda item: (-item.weight, item.priority, item.exhibitor_id))

    anchors_by_day, deferred = _resolve_anchor_conflicts(pool, day_count)
    anchor_ids = {item.exhibitor_id for day in anchors_by_day.values() for item in day}
    deferred_ids = {item.exhibitor_id for item in deferred}
    fillers = [item for item in pool if item.exhibitor_id not in anchor_ids and item.exhibitor_id not in deferred_ids]
    fillers.sort(key=lambda item: (-item.weight, item.exhibitor_id))

    ctx = {
        "day_start_minute": day_start_minute,
        "day_minutes": day_minutes,
        "speed": speed,
    }
    day_orders: dict[int, list[Candidate]] = {}
    day_results: dict[int, _Timeline | None] = {}
    dropped_ids: set[int] = set()
    placed_ids: set[int] = set()
    for day in range(1, day_count + 1):
        order = list(anchors_by_day[day])
        before = {item.exhibitor_id for item in order}
        order, result = _drop_infeasible_anchor(order, day, ctx, deferred)
        dropped_ids |= before - {item.exhibitor_id for item in order}
        available = [
            item
            for item in fillers
            if item.exhibitor_id not in placed_ids and item.exhibitor_id not in dropped_ids
        ]
        order, result, placed = _greedy_fill(order, day, ctx, available)
        placed_ids |= {item.exhibitor_id for item in placed}
        order, result = _two_opt(order, day, ctx)
        day_orders[day] = order
        day_results[day] = result

    visits: list[dict] = []
    total_walk = 0.0
    total_dwell = 0
    covered_weight = 0
    by_day: list[dict] = []
    for day in range(1, day_count + 1):
        order = day_orders[day]
        result = day_results[day]
        if result is None or not result.feasible:
            by_day.append({"day": day, "visits": 0, "walk_m": 0.0, "dwell_min": 0, "end_eta": None, "util_pct": 0.0})
            continue
        for index, item in enumerate(order, start=1):
            visits.append(
                {
                    "exhibitor_id": item.exhibitor_id,
                    "day": day,
                    "seq": index,
                    "eta_min": result.eta[index - 1],
                    "eta_label": format_minute(result.eta[index - 1]),
                    "dwell_min": item.dwell_min,
                    "walk_before_m": round(result.legs[index - 1], 1),
                    "conflict": item.conflict,
                    "conflict_kind": item.conflict_kind,
                    "conflict_note": item.conflict_note,
                    "status": item.status,
                    "priority": item.priority,
                    "weight": item.weight,
                }
            )
        total_walk += result.walk_m
        dwell = sum(item.dwell_min for item in order)
        total_dwell += dwell
        covered_weight += sum(item.weight for item in order)
        by_day.append(
            {
                "day": day,
                "visits": len(order),
                "walk_m": round(result.walk_m, 1),
                "dwell_min": dwell,
                "end_eta": format_minute(result.end_min),
                "util_pct": round(100 * ((result.end_min - day_start_minute) / day_minutes), 1),
            }
        )

    for index, item in enumerate(deferred, start=1):
        visits.append(
            {
                "exhibitor_id": item.exhibitor_id,
                "day": item.appt_day or 1,
                "seq": DEFERRED_SEQ_BASE + index,
                "eta_min": item.appt_start or day_start_minute,
                "eta_label": format_minute(item.appt_start or day_start_minute),
                "dwell_min": item.dwell_min,
                "walk_before_m": 0.0,
                "conflict": True,
                "conflict_kind": item.conflict_kind or "appointment",
                "conflict_note": item.conflict_note,
                "status": "deferred",
                "priority": item.priority,
                "weight": item.weight,
            }
        )

    total_weight = sum(item.weight for item in pool)
    conflict_visits = [item for item in visits if item["conflict"]]
    conflict_pairs = sum(1 for item in deferred if item.conflict_kind == "appointment")
    max_visits_hit = max_visits is not None and len(visits) >= max_visits
    metrics = {
        "day_count": day_count,
        "day_minutes": day_minutes,
        "day_start": format_minute(day_start_minute),
        "speed_m_per_min": speed,
        "priorities": sorted(allowed),
        "candidates": len(pool),
        "visit_count": sum(1 for item in visits if item["status"] != "deferred"),
        "row_count": len(visits),
        "total_walk_m": round(total_walk, 1),
        "total_dwell_min": total_dwell,
        "walk_minutes": round(total_walk / speed, 1) if speed else 0.0,
        "covered_weight": covered_weight,
        "total_weight": total_weight,
        "coverage_pct": round(100 * covered_weight / total_weight, 1) if total_weight else 0.0,
        "conflict_visits": len(conflict_visits),
        "conflict_pairs": conflict_pairs,
        "deferred": len(deferred),
        "deferred_appointment": sum(1 for item in deferred if item.conflict_kind == "appointment"),
        "deferred_overtime": sum(1 for item in deferred if item.conflict_kind == "overtime"),
        "max_visits_hit": max_visits_hit,
        "by_day": by_day,
    }
    log_lines = [
        f"候选 {len(pool)} 家（{'/'.join(sorted(allowed))}），{day_count} 天 × {day_minutes} 分钟，步行速度 {speed:g} m/min",
    ]
    for item in by_day:
        log_lines.append(
            f"Day {item['day']}：{item['visits']} 家，步行 {item['walk_m']:.0f}m，停留 {item['dwell_min']} 分钟，结束 {item['end_eta'] or '—'}，利用率 {item['util_pct']:.1f}%"
        )
    log_lines.append(
        f"合计步行 {metrics['total_walk_m']:.0f}m，覆盖权重 {covered_weight}/{total_weight}（{metrics['coverage_pct']:.1f}%），冲突 {conflict_pairs} 组 / 标记 {len(conflict_visits)} 条"
    )
    if deferred:
        for item in deferred:
            log_lines.append(
                f"冲突：{item.name}（{item.booth_no}，{item.priority}）{CONFLICT_KIND_LABELS.get(item.conflict_kind or '', '')} → {VISIT_STATUS_LABELS.get(item.status, item.status)}"
            )
    status = "success" if visits and any(item["status"] != "deferred" for item in visits) else "partial"
    return {"visits": visits, "metrics": metrics, "log": "\n".join(log_lines), "status": status}


# ---------------------------------------------------------------------------
# 算法 B+：变更影响核对 + 增量重排 + 新旧对照
# ---------------------------------------------------------------------------


def snapshot_of_row(row: dict) -> dict:
    """展商排程相关输入的规范快照（存进 run.params，供日后影响核对）。"""
    return {
        "booth_no": str(row.get("booth_no") or "").strip().upper(),
        "priority": str(row.get("priority") or "P2").upper(),
        "dwell_min": int(row.get("dwell_min") or 0),
        "appointment_day": _optional_int(row.get("appointment_day")),
        "appointment_start_min": _optional_int(row.get("appointment_start_min")),
        "appointment_end_min": _optional_int(row.get("appointment_end_min")),
    }


def _appt_label(snap: dict | None) -> str:
    if not snap or snap.get("appointment_day") is None or snap.get("appointment_start_min") is None:
        return "无预约"
    start = snap["appointment_start_min"]
    end = snap.get("appointment_end_min") if snap.get("appointment_end_min") is not None else start + 30
    return f"Day{snap['appointment_day']} {format_minute(start)}-{format_minute(end)}"


def diff_exhibitor(old: dict, new: dict) -> list[dict]:
    """两份快照的字段级差异（展位/优先级/停留/预约）。"""
    changes: list[dict] = []
    if old["booth_no"] != new["booth_no"]:
        changes.append({"field": "booth_no", "old": old["booth_no"], "new": new["booth_no"]})
    if old["priority"] != new["priority"]:
        changes.append({"field": "priority", "old": old["priority"], "new": new["priority"]})
    if old["dwell_min"] != new["dwell_min"]:
        changes.append({"field": "dwell_min", "old": old["dwell_min"], "new": new["dwell_min"]})
    if any(old[key] != new[key] for key in ("appointment_day", "appointment_start_min", "appointment_end_min")):
        changes.append({"field": "appointment", "old": _appt_label(old), "new": _appt_label(new)})
    return changes


def _appt_window(snap: dict) -> tuple[int, int, int] | None:
    if snap.get("appointment_day") is None or snap.get("appointment_start_min") is None:
        return None
    start = int(snap["appointment_start_min"])
    end = snap.get("appointment_end_min")
    return (int(snap["appointment_day"]), start, int(end) if end is not None else start + 30)


def _windows_overlap(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    return a[0] == b[0] and a[1] < b[2] and b[1] < a[2]


def _is_scheduled(row: dict) -> bool:
    return row["seq"] < DEFERRED_SEQ_BASE and row.get("status") != "deferred"


def compute_impact(
    base_snapshot: dict,
    current: dict,
    *,
    priorities,
    base_visits: list[dict],
) -> dict:
    """对比运行时的输入快照与当前名录，算出受影响展商 / 失效到访 / 新入候选 / 受影响天。

    传播规则：预约窗口发生变化的展商，会把「窗口与其新旧预约重叠」的未变更展商一起带入影响集
    （冲突取舍可能因此翻转）；名录里被删除的展商按失效处理。
    """
    allowed = {str(item).upper() for item in priorities}
    snap = {int(key): value for key, value in base_snapshot.items()}
    curr = {int(key): value for key, value in current.items()}
    visit_exhibitor_ids = {row["exhibitor_id"] for row in base_visits}

    changes: dict[int, list[dict]] = {}
    removed_ids: list[int] = []
    incoming_ids: list[int] = []
    for ex_id in sorted(curr):
        old = snap.get(ex_id)
        if old is None:
            if curr[ex_id]["priority"] in allowed:
                incoming_ids.append(ex_id)
            continue
        field_changes = diff_exhibitor(old, curr[ex_id])
        if field_changes:
            changes[ex_id] = field_changes
    for ex_id in sorted(snap):
        if ex_id not in curr:
            removed_ids.append(ex_id)

    affected: set[int] = set()
    for ex_id in changes:
        if ex_id in visit_exhibitor_ids or curr[ex_id]["priority"] in allowed:
            affected.add(ex_id)
    for ex_id in removed_ids:
        if ex_id in visit_exhibitor_ids:
            affected.add(ex_id)

    changed_windows: list[tuple[int, int, int]] = []
    for ex_id in changes:
        for snap_row in (snap.get(ex_id), curr.get(ex_id)):
            window = _appt_window(snap_row) if snap_row else None
            if window:
                changed_windows.append(window)
    for ex_id in incoming_ids:
        window = _appt_window(curr[ex_id])
        if window:
            changed_windows.append(window)
    for ex_id in removed_ids:
        window = _appt_window(snap[ex_id])
        if window:
            changed_windows.append(window)

    propagated: set[int] = set()
    for ex_id in sorted(curr):
        if ex_id in affected or ex_id in incoming_ids or ex_id not in visit_exhibitor_ids:
            continue
        window = _appt_window(curr[ex_id])
        if window and any(_windows_overlap(window, other) for other in changed_windows):
            affected.add(ex_id)
            propagated.add(ex_id)

    invalidated = [row for row in base_visits if row["exhibitor_id"] in affected]
    affected_days = sorted(
        {row["day"] for row in invalidated if _is_scheduled(row)}
        | {curr[ex]["appointment_day"] for ex in affected if ex in curr and curr[ex]["appointment_day"] is not None}
        | {curr[ex]["appointment_day"] for ex in incoming_ids if curr[ex]["appointment_day"] is not None}
    )
    # 重排池里的「新」成员：名录新增 + 变更后新进入候选（在基准版本没有到访行）
    new_candidate_ids = sorted(set(incoming_ids) | {ex for ex in affected if ex not in visit_exhibitor_ids})
    return {
        "changes": changes,
        "removed_ids": removed_ids,
        "incoming_ids": incoming_ids,
        "new_candidate_ids": new_candidate_ids,
        "propagated_ids": sorted(propagated),
        "affected_ids": sorted(affected),
        "invalidated_count": len(invalidated),
        "manual_at_risk": sorted({row["exhibitor_id"] for row in invalidated if row.get("reviewer")}),
        "affected_days": affected_days,
        "empty": not affected and not incoming_ids,
    }


def plan_incremental(
    candidates: list[Candidate],
    base_visits: list[dict],
    *,
    affected_ids,
    incoming_ids=None,
    day_count: int = 2,
    day_minutes: int = DEFAULT_DAY_MINUTES,
    day_start_minute: int = DEFAULT_DAY_START_MINUTE,
    speed: float = DEFAULT_SPEED_M_PER_MIN,
    priorities=DEFAULT_PRIORITIES,
    max_visits: int | None = None,
) -> dict:
    """增量重排：只重算受影响展商与受影响天，未受影响的人工确认逐字保留。

    - candidates：按当前名录构建的全部候选（函数内部再按 priorities 过滤）；
    - base_visits：基准版本的到访行（dict，含 status/reviewer 等人工状态）；
    - affected_ids：compute_impact 算出的受影响展商集合；
    - incoming_ids：新入候选（名录新增 / 变更后新进入候选）；缺省时按「基准无到访行」推导；
    - 未受影响的天整段逐字保留；受影响的天以人工确认站为固定锚点重排；
    - 每个重排池成员必有一行输出（排入或改期），保证来源/失效/重排/输出对得上。
    """
    if day_count < 1:
        raise ValueError("day_count 至少为 1")
    allowed = {str(item).upper() for item in priorities}
    affected = {int(item) for item in affected_ids}
    pool_all = {item.exhibitor_id: replace(item) for item in candidates if item.priority in allowed}

    base_rows = [dict(row) for row in base_visits]
    base_by_ex = {row["exhibitor_id"]: row for row in base_rows}
    invalidated = [row for row in base_rows if row["exhibitor_id"] in affected]
    carried = [row for row in base_rows if row["exhibitor_id"] not in affected]

    manual_kept: list[dict] = []
    kept_deferred: list[dict] = []
    carried_planned: list[dict] = []
    for row in carried:
        if not _is_scheduled(row):
            kept_deferred.append(row)
        elif row.get("reviewer"):
            manual_kept.append(row)
        else:
            carried_planned.append(row)

    origin: dict[int, str] = {}
    manual_overlay: dict[int, dict] = {}
    pool: list[Candidate] = []
    pinned: list[Candidate] = []
    dropped: list[dict] = []
    verbatim_scheduled: list[dict] = []  # 逐字保留的在路线行（未受影响天 / 超出排程天数的人工行）
    pool_built = 0  # 重排池成员数（失效仍候选 + 新增候选 + 连带释放），每个成员必有一行输出

    for row in invalidated:
        candidate = pool_all.get(row["exhibitor_id"])
        if candidate is None:
            dropped.append(row)  # 已不在候选范围（优先级移出 / 展位非法 / 名录删除）
        else:
            pool.append(candidate)
            origin[candidate.exhibitor_id] = "replanned"
            pool_built += 1
    incoming_set = (
        {int(item) for item in incoming_ids}
        if incoming_ids is not None
        else {ex_id for ex_id in pool_all if ex_id not in base_by_ex}
    )
    incoming_count = 0
    for ex_id in sorted(incoming_set):
        candidate = pool_all.get(ex_id)
        if candidate is not None and ex_id not in base_by_ex:
            pool.append(candidate)
            origin[ex_id] = "incoming"
            incoming_count += 1
            pool_built += 1
    for row in manual_kept:
        candidate = pool_all.get(row["exhibitor_id"])
        if candidate is None:
            dropped.append(row)
            continue
        if not 1 <= int(row["day"]) <= day_count:
            # 人工改期到本次排程天数之外：无法钉位，整行逐字保留
            verbatim_scheduled.append(row)
            origin[row["exhibitor_id"]] = "carried"
            continue
        pinned.append(
            replace(
                candidate,
                dwell_min=int(row["dwell_min"]),
                appt_day=int(row["day"]),
                appt_start=int(row["eta_min"]),
                appt_end=int(row["eta_min"]) + int(row["dwell_min"]) + PINNED_WINDOW_SLACK_MIN,
                pinned=True,
            )
        )
        origin[row["exhibitor_id"]] = "pinned"
        manual_overlay[row["exhibitor_id"]] = row

    ctx = {"day_start_minute": day_start_minute, "day_minutes": day_minutes, "speed": speed}

    # 预约锚点（重排池 + 人工确认钉点）先落位，钉点不可被动摇
    anchor_input = [item for item in pool if item.is_anchor] + pinned
    anchors_by_day, deferred_new = _resolve_anchor_conflicts(anchor_input, day_count)

    open_days = {row["day"] for row in invalidated if _is_scheduled(row)}
    # 仅有钉点（人工确认）的天不算受影响天；须有重排池锚点真正落位才开放
    open_days |= {day for day, anchors in anchors_by_day.items() if any(not item.pinned for item in anchors)}
    open_days = {day for day in open_days if 1 <= day <= day_count}

    # 钉点所在天若未开放，该人工确认行整行逐字保留（不参与重排）
    for row in manual_kept:
        if origin.get(row["exhibitor_id"]) == "pinned" and row["day"] not in open_days:
            verbatim_scheduled.append(row)
            origin[row["exhibitor_id"]] = "carried"

    # 开放天上的非人工保留站释放进重排池；其预约窗口与变更无重叠（影响核对已传播），直接落位
    released: list[dict] = []
    for row in carried_planned:
        if row["day"] not in open_days:
            verbatim_scheduled.append(row)
            continue
        candidate = pool_all.get(row["exhibitor_id"])
        if candidate is None:
            dropped.append(row)
            continue
        released.append(row)
        origin[candidate.exhibitor_id] = "released"
        pool_built += 1
        if candidate.is_anchor and candidate.appt_day in anchors_by_day:
            anchors_by_day[candidate.appt_day].append(candidate)
            anchors_by_day[candidate.appt_day].sort(key=lambda item: (item.appt_start or 0, item.exhibitor_id))
        else:
            pool.append(candidate)

    fillers = sorted(
        (item for item in pool if not item.is_anchor),
        key=lambda item: (-item.weight, item.exhibitor_id),
    )

    day_orders: dict[int, list[Candidate]] = {}
    day_results: dict[int, _Timeline | None] = {}
    placed_ids: set[int] = set()
    for day in range(1, day_count + 1):
        if day not in open_days:
            continue
        order = list(anchors_by_day[day])
        order, result = _drop_infeasible_anchor(order, day, ctx, deferred_new)
        available = [item for item in fillers if item.exhibitor_id not in placed_ids]
        order, result, placed = _greedy_fill(order, day, ctx, available)
        placed_ids |= {item.exhibitor_id for item in placed}
        order, result = _two_opt(order, day, ctx)
        if result is None or (not result.feasible and order):
            raise ReplanInfeasibleError(f"Day{day} 重排后不可行")
        day_orders[day] = order
        day_results[day] = result

    # 每个重排池成员必有一行输出：未排入也未改期的（含天数外锚点）统一转改期
    placed_all = set(placed_ids)
    for day in open_days:
        placed_all |= {item.exhibitor_id for item in day_orders.get(day, [])}
    deferred_new_ids = {item.exhibitor_id for item in deferred_new}
    unplaced = [
        item
        for item in pool
        if item.exhibitor_id not in placed_all and item.exhibitor_id not in deferred_new_ids
    ]
    for candidate in unplaced:
        candidate.conflict = True
        candidate.conflict_kind = "overtime"
        candidate.conflict_note = "受影响天当日容量不足，未排入"
        candidate.status = "deferred"
    deferred_new.extend(unplaced)

    visits: list[dict] = []

    def _emit(candidate: Candidate, day: int, seq: int, eta_min: int, walk_before_m: float) -> None:
        row_origin = origin.get(candidate.exhibitor_id, "replanned")
        overlay = manual_overlay.get(candidate.exhibitor_id, {})
        if row_origin == "pinned" and overlay:
            eta_min = int(overlay["eta_min"])  # 钉点站按原 ETA 落字（窗口含取整冗余，时间承诺不变）
        visits.append(
            {
                "exhibitor_id": candidate.exhibitor_id,
                "day": day,
                "seq": seq,
                "eta_min": eta_min,
                "eta_label": format_minute(eta_min),
                "dwell_min": candidate.dwell_min,
                "walk_before_m": round(walk_before_m, 1),
                "conflict": bool(overlay.get("conflict", candidate.conflict)),
                "conflict_kind": overlay.get("conflict_kind", candidate.conflict_kind),
                "conflict_note": overlay.get("conflict_note", candidate.conflict_note),
                "status": overlay.get("status", candidate.status),
                "reviewer": overlay.get("reviewer"),
                "review_note": overlay.get("review_note"),
                "priority": candidate.priority,
                "weight": candidate.weight,
                "origin": row_origin,
            }
        )

    def _emit_verbatim(row: dict, row_origin: str) -> None:
        visits.append(
            {
                "exhibitor_id": row["exhibitor_id"],
                "day": row["day"],
                "seq": row["seq"],
                "eta_min": row["eta_min"],
                "eta_label": format_minute(row["eta_min"]),
                "dwell_min": row["dwell_min"],
                "walk_before_m": round(float(row.get("walk_before_m") or 0), 1),
                "conflict": bool(row.get("conflict")),
                "conflict_kind": row.get("conflict_kind"),
                "conflict_note": row.get("conflict_note"),
                "status": row.get("status") or "planned",
                "reviewer": row.get("reviewer"),
                "review_note": row.get("review_note"),
                "priority": row.get("priority"),
                "weight": row.get("weight") or 0,
                "origin": row_origin,
            }
        )

    total_walk = 0.0
    total_dwell = 0
    covered_weight = 0
    by_day: list[dict] = []
    verbatim_days = {row["day"] for row in verbatim_scheduled}
    all_days = sorted(set(range(1, day_count + 1)) | verbatim_days)
    for day in all_days:
        if day not in open_days:
            day_rows = sorted((row for row in verbatim_scheduled if row["day"] == day), key=lambda item: (item["seq"], item["exhibitor_id"]))
            for row in day_rows:
                _emit_verbatim(row, "carried")
            walk = sum(float(row.get("walk_before_m") or 0) for row in day_rows)
            dwell = sum(int(row["dwell_min"]) for row in day_rows)
            end_min = max((int(row["eta_min"]) + int(row["dwell_min"]) for row in day_rows), default=day_start_minute)
            total_walk += walk
            total_dwell += dwell
            covered_weight += sum(int(row.get("weight") or 0) for row in day_rows)
            by_day.append(
                {
                    "day": day,
                    "visits": len(day_rows),
                    "walk_m": round(walk, 1),
                    "dwell_min": dwell,
                    "end_eta": format_minute(end_min) if day_rows else None,
                    "util_pct": round(100 * ((end_min - day_start_minute) / day_minutes), 1) if day_rows else 0.0,
                }
            )
            continue
        order = day_orders.get(day, [])
        result = day_results.get(day)
        if result is None or not result.feasible:
            by_day.append({"day": day, "visits": 0, "walk_m": 0.0, "dwell_min": 0, "end_eta": None, "util_pct": 0.0})
            continue
        for index, candidate in enumerate(order, start=1):
            _emit(candidate, day, index, result.eta[index - 1], result.legs[index - 1])
        total_walk += result.walk_m
        dwell = sum(item.dwell_min for item in order)
        total_dwell += dwell
        covered_weight += sum(item.weight for item in order)
        by_day.append(
            {
                "day": day,
                "visits": len(order),
                "walk_m": round(result.walk_m, 1),
                "dwell_min": dwell,
                "end_eta": format_minute(result.end_min),
                "util_pct": round(100 * ((result.end_min - day_start_minute) / day_minutes), 1),
            }
        )

    # 改期行：保留的逐字保留，新产生的接在其后续号
    for row in sorted(kept_deferred, key=lambda item: (item["seq"], item["exhibitor_id"])):
        _emit_verbatim(row, "kept_deferred")
    deferred_seq = max(
        [DEFERRED_SEQ_BASE - 1] + [int(row["seq"]) for row in kept_deferred if int(row["seq"]) >= DEFERRED_SEQ_BASE]
    ) + 1
    for candidate in sorted(deferred_new, key=lambda item: (-item.weight, item.exhibitor_id)):
        visits.append(
            {
                "exhibitor_id": candidate.exhibitor_id,
                "day": candidate.appt_day or 1,
                "seq": deferred_seq,
                "eta_min": candidate.appt_start or day_start_minute,
                "eta_label": format_minute(candidate.appt_start or day_start_minute),
                "dwell_min": candidate.dwell_min,
                "walk_before_m": 0.0,
                "conflict": True,
                "conflict_kind": candidate.conflict_kind or "appointment",
                "conflict_note": candidate.conflict_note,
                "status": "deferred",
                "reviewer": None,
                "review_note": None,
                "priority": candidate.priority,
                "weight": candidate.weight,
                "origin": origin.get(candidate.exhibitor_id, "replanned"),
            }
        )
        deferred_seq += 1

    scheduled = [row for row in visits if _is_scheduled(row)]
    deferred_rows = [row for row in visits if not _is_scheduled(row)]
    total_weight = sum(item.weight for item in pool_all.values())
    conflict_visits = [row for row in visits if row["conflict"]]
    conflict_pairs = sum(1 for row in deferred_rows if row["conflict_kind"] == "appointment" and row["origin"] != "kept_deferred")
    metrics = {
        "day_count": day_count,
        "day_minutes": day_minutes,
        "day_start": format_minute(day_start_minute),
        "speed_m_per_min": speed,
        "priorities": sorted(allowed),
        "candidates": len(pool_all),
        "visit_count": len(scheduled),
        "row_count": len(visits),
        "total_walk_m": round(total_walk, 1),
        "total_dwell_min": total_dwell,
        "walk_minutes": round(total_walk / speed, 1) if speed else 0.0,
        "covered_weight": covered_weight,
        "total_weight": total_weight,
        "coverage_pct": round(100 * covered_weight / total_weight, 1) if total_weight else 0.0,
        "conflict_visits": len(conflict_visits),
        "conflict_pairs": conflict_pairs,
        "deferred": len(deferred_rows),
        "deferred_appointment": sum(1 for row in deferred_rows if row["conflict_kind"] == "appointment"),
        "deferred_overtime": sum(1 for row in deferred_rows if row["conflict_kind"] == "overtime"),
        "max_visits_hit": max_visits is not None and len(visits) >= max_visits,
        "by_day": by_day,
        "replan": {
            "invalidated": len(invalidated),
            "dropped": len(dropped),
            "carried": len(carried),
            "carried_manual": len(manual_kept) + sum(1 for row in kept_deferred if row.get("reviewer")),
            "released": len(released),
            "incoming": incoming_count,
            "pool": pool_built,
            "pool_placed": sum(1 for row in scheduled if row["origin"] in ("released", "replanned", "incoming")),
            "pool_deferred": sum(1 for row in deferred_rows if row["origin"] in ("released", "replanned", "incoming")),
            "pinned": len(manual_kept),
            "touched_days": sorted(open_days),
        },
    }
    log_lines = [
        f"增量重排：失效 {len(invalidated)} 行（移出候选 {len(dropped)}），保留 {len(carried)} 行（人工 {metrics['replan']['carried_manual']}），"
        f"重排池 {metrics['replan']['pool']} 家（新增候选 {incoming_count} / 连带释放 {len(released)}），触及天 {sorted(open_days) or '无'}",
    ]
    for item in by_day:
        log_lines.append(
            f"Day {item['day']}：{item['visits']} 家，步行 {item['walk_m']:.0f}m，停留 {item['dwell_min']} 分钟，结束 {item['end_eta'] or '—'}，利用率 {item['util_pct']:.1f}%"
        )
    log_lines.append(
        f"合计步行 {metrics['total_walk_m']:.0f}m，覆盖权重 {covered_weight}/{total_weight}（{metrics['coverage_pct']:.1f}%），改期 {len(deferred_rows)} 条"
    )
    status = "success" if scheduled else "partial"
    return {"visits": visits, "metrics": metrics, "log": "\n".join(log_lines), "status": status}


def build_diff(base_visits: list[dict], new_visits: list[dict], *, reasons: dict[int, str] | None = None) -> list[dict]:
    """新旧版本逐站对照：改时/换天/调序/新增/移出/转改期/改期→排入/状态重置。"""
    reasons = reasons or {}
    base_by_ex = {row["exhibitor_id"]: row for row in base_visits}
    new_by_ex = {row["exhibitor_id"]: row for row in new_visits}
    entries: list[dict] = []

    def slot(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {
            "day": row["day"],
            "seq": row["seq"],
            "eta_min": row["eta_min"],
            "eta_label": format_minute(row["eta_min"]),
            "status": row.get("status") or "planned",
        }

    for ex_id in sorted(set(base_by_ex) | set(new_by_ex)):
        base = base_by_ex.get(ex_id)
        new = new_by_ex.get(ex_id)
        if base is None:
            change = "added" if _is_scheduled(new) else "deferred"
            entries.append(
                {
                    "exhibitor_id": ex_id,
                    "change": change,
                    "reason": reasons.get(ex_id, "新入候选展商"),
                    "manual_lost": False,
                    "old": None,
                    "new": slot(new),
                }
            )
            continue
        if new is None:
            entries.append(
                {
                    "exhibitor_id": ex_id,
                    "change": "removed",
                    "reason": reasons.get(ex_id, "已不在候选范围"),
                    "manual_lost": bool(base.get("reviewer")),
                    "old": slot(base),
                    "new": None,
                }
            )
            continue
        base_sched = _is_scheduled(base)
        new_sched = _is_scheduled(new)
        manual_lost = bool(base.get("reviewer")) and not new.get("reviewer")
        if base_sched and not new_sched:
            change = "deferred"
        elif not base_sched and new_sched:
            change = "restored"
        elif not base_sched and not new_sched:
            continue  # 改期→改期，位置无意义，视为保留
        elif base["day"] != new["day"]:
            change = "day_changed"
        elif base["eta_min"] != new["eta_min"]:
            change = "time_changed"
        elif manual_lost or base.get("status") != new.get("status"):
            change = "status_reset"
        elif base.get("reviewer"):
            continue  # 人工行：时间/天/状态一致即视为保留（序号重排不算变更）
        elif base["seq"] != new["seq"]:
            change = "seq_changed"
        else:
            continue
        entries.append(
            {
                "exhibitor_id": ex_id,
                "change": change,
                "reason": reasons.get(ex_id, "同日复排连带调整"),
                "manual_lost": manual_lost,
                "old": slot(base),
                "new": slot(new),
            }
        )

    def sort_key(entry: dict) -> tuple:
        anchor = entry["new"] or entry["old"] or {"day": 0, "eta_min": 0}
        return (anchor["day"], anchor["eta_min"], entry["exhibitor_id"])

    entries.sort(key=sort_key)
    return entries
