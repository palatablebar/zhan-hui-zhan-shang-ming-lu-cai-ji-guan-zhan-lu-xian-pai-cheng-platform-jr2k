import { api } from '../api.js';
import { ENUMS, can, diffTone, priorityTone, routeTone, visitTone } from '../config.js';
import { badge, drawer, emptyState, errorBox, errorMessage, esc, field, fmtTime, toast } from '../ui.js';

const state = { runId: '', day: '', pending: false };

function metricBlock(metrics) {
  return `<div class="metrics-grid">
    <div class="metric"><div class="k">总步行</div><div class="v">${esc(metrics.total_walk_m ?? '—')}<small>m</small></div></div>
    <div class="metric"><div class="k">覆盖权重</div><div class="v">${esc(metrics.coverage_pct ?? '—')}<small>/100</small></div></div>
    <div class="metric"><div class="k">排入展商</div><div class="v">${esc(metrics.visit_count ?? 0)}<small>家</small></div></div>
    <div class="metric"><div class="k">冲突 / 改期</div><div class="v">${esc(metrics.conflict_pairs ?? 0)}<small>组 / ${esc(metrics.deferred ?? 0)} 条</small></div></div>
  </div>`;
}

function reconcileBlock(reconcile) {
  const days = (reconcile.by_day || []).map((item) => `Day${item.day} ${item.before}→${item.after}`).join(' · ');
  return `<div data-testid="reconcile-strip">
    <div class="eyebrow" style="margin-top:14px">影响核对 · 对账（来源 = 失效 + 保留，输出 = 保留未动 + 重排池）</div>
    <div class="metrics-grid" style="margin-top:6px">
      <div class="metric"><div class="k">来源 ${esc(reconcile.source_run_no || '')}</div><div class="v">${esc(reconcile.source_rows)}<small>行 / ${esc(reconcile.source_scheduled)} 站</small></div></div>
      <div class="metric"><div class="k">失效项</div><div class="v">${esc(reconcile.invalidated)}<small>行</small></div></div>
      <div class="metric"><div class="k">保留</div><div class="v">${esc(reconcile.carried)}<small>行 · 人工 ${esc(reconcile.carried_manual)}</small></div></div>
      <div class="metric"><div class="k">重排池</div><div class="v">${esc(reconcile.pool)}<small>家 · 新增 ${esc(reconcile.incoming)}</small></div></div>
      <div class="metric"><div class="k">可见路线</div><div class="v">${esc(reconcile.output_scheduled)}<small>站 · ${esc(days || '—')}</small></div></div>
      <div class="metric"><div class="k">对账</div><div class="v">${reconcile.balanced ? '✓' : '✗'}<small>${reconcile.balanced ? '来源/失效/重排/可见对得上' : '数值不平，请核查'}</small></div></div>
    </div>
  </div>`;
}

function slotLabel(slot) {
  if (!slot) return '—';
  return `Day${slot.day} ${slot.eta_label} · ${ENUMS.visitStatus[slot.status] || slot.status}`;
}

function diffPanel(diff) {
  return `<section class="panel" data-testid="diff-panel">
    <div class="panel-head"><h2>新旧版本对照</h2><span class="meta">${diff.length} 处变更 · 标出改时的站与原因</span></div>
    <div class="table-wrap"><table class="data">
      <thead><tr><th>展商</th><th>展位</th><th>变更</th><th>旧版本</th><th>新版本</th><th>为什么改</th></tr></thead>
      <tbody>${diff
        .map(
          (entry) => `<tr>
            <td><b>${esc(entry.exhibitor_name)}</b>${entry.manual_lost ? '<div class="mono" style="color:var(--danger)">人工确认失效</div>' : ''}</td>
            <td class="mono">${esc(entry.booth_no || '—')}</td>
            <td>${badge(diffTone(entry.change), ENUMS.diffKind[entry.change] || entry.change)}</td>
            <td class="mono">${esc(slotLabel(entry.old))}</td>
            <td class="mono">${esc(slotLabel(entry.new))}</td>
            <td class="muted">${esc(entry.reason || '—')}</td>
          </tr>`,
        )
        .join('')}</tbody>
    </table></div>
  </section>`;
}

function visitRow(visit, ctx, editable, diffByEx) {
  const entry = diffByEx.get(visit.exhibitor_id);
  const delta =
    entry && entry.old && entry.new && entry.old.eta_min !== entry.new.eta_min
      ? `<div class="mono" style="color:var(--warn)">${esc(entry.old.eta_label)}→${esc(entry.new.eta_label)}</div>`
      : '';
  const diffBadge = entry
    ? `<div title="${esc(entry.reason || '')}">${badge(diffTone(entry.change), ENUMS.diffKind[entry.change] || entry.change)}</div>`
    : '';
  const actions = editable
    ? `<div class="day-actions">
        <button class="btn sm ghost" data-visit="${esc(visit.id)}" data-action="confirm">确认</button>
        ${visit.day === 1 ? `<button class="btn sm ghost" data-visit="${esc(visit.id)}" data-action="day2" data-day="2">改 Day2</button>` : `<button class="btn sm ghost" data-visit="${esc(visit.id)}" data-action="day1" data-day="1">改 Day1</button>`}
        <button class="btn sm ghost" data-visit="${esc(visit.id)}" data-action="defer">顺延</button>
      </div>`
    : '<span class="muted mono">只读</span>';
  return `<tr class="${visit.conflict ? 'is-conflict' : ''}" data-row="${esc(visit.id)}">
    <td class="mono">${String(visit.seq).padStart(2, '0')}</td>
    <td><span class="eta">${esc(visit.eta_label)}</span>${delta}</td>
    <td class="mono">${esc(visit.booth_no)}</td>
    <td><b>${esc(visit.exhibitor_name)}</b><div class="muted">${esc(visit.hall)} · ${esc(ENUMS.category[visit.category] || visit.category || '')}</div></td>
    <td>${badge(priorityTone(visit.priority), `${visit.priority} ${ENUMS.priority[visit.priority] || ''}`)}</td>
    <td class="num">${esc(visit.dwell_min)}′</td>
    <td class="num">${esc(visit.walk_before_m)}</td>
    <td>${badge(visitTone(visit.status), ENUMS.visitStatus[visit.status] || visit.status)}${diffBadge}${visit.conflict ? `<div class="mono" style="color:var(--danger)">${esc(visit.conflict_label || '冲突')}</div>` : ''}</td>
    <td>${actions}</td>
  </tr>`;
}

function dayPanel(day, visits, ctx, editable, diffByEx) {
  const rows = visits.filter((visit) => visit.day === day && visit.seq < 9000);
  const deferred = visits.filter((visit) => visit.day === day && visit.seq >= 9000);
  const walk = rows.reduce((sum, visit) => sum + Number(visit.walk_before_m || 0), 0);
  return `<section class="panel">
    <div class="panel-head">
      <h2>Day ${day} · 11/${day === 1 ? '18' : '19'}</h2>
      <span class="meta">${rows.length} 站 · 步行约 ${Math.round(walk)}m</span>
    </div>
    ${
      rows.length
        ? `<div class="table-wrap"><table class="data route-table">
            <thead><tr><th>序</th><th>ETA</th><th>展位</th><th>展商</th><th>优先级</th><th>停留</th><th>步行m</th><th>状态</th><th>操作</th></tr></thead>
            <tbody>${rows.map((visit) => visitRow(visit, ctx, editable, diffByEx)).join('')}</tbody>
          </table></div>`
        : emptyState({ title: '这一天没有排入展商', hint: '调整优先级后重新运行，或把冲突项改期到这一天。' })
    }
    ${
      deferred.length
        ? `<div class="panel-body" style="border-top:1px solid var(--rule)">
            <div class="eyebrow">冲突 / 改期项</div>
            ${deferred
              .map(
                (visit) => `<div class="tl-item conflict" style="margin-top:6px">
                  <div class="tl-time">${esc(visit.eta_label)}</div>
                  <div class="tl-axis"><span class="tl-dot"></span></div>
                  <div class="tl-body"><b>${esc(visit.exhibitor_name)}</b> <span class="mono">${esc(visit.booth_no)}</span>
                    <div class="muted">${esc(visit.conflict_note || '预约冲突')} · ${esc(ENUMS.visitStatus[visit.status] || visit.status)}</div>
                    <div class="day-actions" style="margin-top:4px">
                      <button class="btn sm ghost" data-visit="${esc(visit.id)}" data-action="day1" data-day="1">排到 Day1</button>
                      <button class="btn sm ghost" data-visit="${esc(visit.id)}" data-action="day2" data-day="2">排到 Day2</button>
                    </div>
                  </div>
                </div>`,
              )
              .join('')}
          </div>`
        : ''
    }
  </section>`;
}

function openRunForm(view, ctx) {
  const { root, close } = drawer({
    title: '运行两日路线排程',
    subtitle: '算法 B：预约硬窗 + 权重/距离贪心插入 + 2-opt 改进',
    body: `<form id="run-form" style="display:grid;gap:12px">
      <div class="check-row">
        <span class="muted">候选优先级</span>
        <label><input type="checkbox" name="priority" value="P0" checked /> P0 必看</label>
        <label><input type="checkbox" name="priority" value="P1" checked /> P1 重点</label>
        <label><input type="checkbox" name="priority" value="P2" /> P2 顺访</label>
      </div>
      ${field({ label: '每天可用分钟', name: 'day_minutes', value: 480, type: 'number', attrs: 'min="240" max="720"' })}
      ${field({ label: '步行速度 (m/min)', name: 'walk_speed_m_per_min', value: 55, type: 'number', attrs: 'min="20" max="120"' })}
      ${field({ label: '最多排入（留空不限）', name: 'max_visits', value: '', type: 'number', attrs: 'min="1" max="400"' })}
      <div class="form-error" id="run-error"></div>
      <button class="btn accent" type="submit">开始排程</button>
    </form>`,
  });
  root.querySelector('#run-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const priorities = [...form.querySelectorAll('input[name=priority]:checked')].map((node) => node.value);
    if (!priorities.length) {
      root.querySelector('#run-error').textContent = '至少选一个优先级';
      return;
    }
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    button.textContent = '排程中…';
    try {
      const run = await api.post('/api/route-runs', {
        day_count: 2,
        day_minutes: Number(form.day_minutes.value),
        day_start_minute: 540,
        walk_speed_m_per_min: Number(form.walk_speed_m_per_min.value),
        priorities,
        max_visits: form.max_visits.value ? Number(form.max_visits.value) : null,
      });
      toast(`排程完成：${run.metrics.visit_count} 家 / 步行 ${run.metrics.total_walk_m}m / 覆盖 ${run.metrics.coverage_pct}`);
      close();
      state.runId = run.id;
      await routingPage.render(view, ctx);
    } catch (err) {
      root.querySelector('#run-error').textContent = errorMessage(err);
      button.disabled = false;
      button.textContent = '开始排程';
    }
  });
}

function impactSummary(impact) {
  const days = (impact.affected_days || []).map((day) => `Day${day}`).join('/') || '—';
  return `<div class="stat-strip">
    <div class="stat"><span class="k">变更展商</span><span class="v">${impact.changes.length}</span><span class="hint">含连带 ${impact.changes.filter((item) => item.propagated).length} 家</span></div>
    <div class="stat"><span class="k">失效到访</span><span class="v">${impact.invalidated_count}</span><span class="hint">受影响天 ${esc(days)}</span></div>
    <div class="stat"><span class="k">新入候选</span><span class="v">${impact.incoming.length}</span><span class="hint">进入重排池</span></div>
    <div class="stat"><span class="k">人工确认受影响</span><span class="v">${impact.manual_at_risk.length}</span><span class="hint">${impact.manual_at_risk.length ? '需重新复核' : '都会保留'}</span></div>
  </div>`;
}

async function openImpact(view, ctx, run) {
  const { root, close } = drawer({
    title: `影响核对 · 基于 ${run.run_no}`,
    subtitle: '只重算受影响的展商与当天路线，未受影响的人工确认逐字保留',
    body: '<div class="empty"><div class="mark">正在比对名录快照…</div></div>',
    footer: `<button class="btn ghost" data-close>取消</button>
      <button class="btn accent" id="do-replan" data-testid="replan-submit" disabled>执行增量重排</button>`,
  });
  let impact;
  try {
    impact = await api.get(`/api/route-runs/${run.id}/impact`);
  } catch (err) {
    root.querySelector('.drawer-body').innerHTML = `<div class="error-box"><span>${esc(errorMessage(err))}</span></div>`;
    return;
  }
  const body = root.querySelector('.drawer-body');
  if (impact.empty) {
    body.innerHTML = `${emptyState({ title: '名录相对该版本没有变化', hint: '预约、优先级、展位都没有实质变更，无需重排。' })}`;
    return;
  }
  const changeRows = impact.changes
    .map((item) => {
      const fields = item.fields.length
        ? item.fields.map((fieldItem) => `${esc(fieldItem.label)} ${esc(fieldItem.old)}→${esc(fieldItem.new)}`).join('；')
        : '—';
      const tags = [item.propagated ? badge('info', '连带') : '', impact.manual_at_risk.some((risk) => risk.exhibitor_id === item.exhibitor_id) ? badge('warn', '人工确认受影响') : '']
        .filter(Boolean)
        .join(' ');
      return `<tr><td><b>${esc(item.exhibitor_name)}</b></td><td>${fields}</td><td>${tags}</td></tr>`;
    })
    .join('');
  const incoming = impact.incoming.map((item) => esc(item.exhibitor_name)).join('、');
  body.innerHTML = `
    ${impactSummary(impact)}
    <div class="eyebrow" style="margin:14px 0 6px">字段级变更</div>
    <div class="table-wrap"><table class="data">
      <thead><tr><th>展商</th><th>变更内容</th><th>标记</th></tr></thead>
      <tbody>${changeRows}</tbody>
    </table></div>
    ${incoming ? `<p class="note-line" style="margin-top:10px">新入候选：${incoming}</p>` : ''}
    <p class="note-line" style="margin-top:10px">重排只动受影响的展商与受影响天；未受影响的确认与改期逐字保留。若生成失败，可见路线保持当前版本；重试不会产生重复站。</p>
    <div class="form-error" id="impact-error"></div>`;
  const submit = root.querySelector('#do-replan');
  submit.disabled = false;
  submit.addEventListener('click', async () => {
    submit.disabled = true;
    submit.textContent = '重排中…';
    try {
      const next = await api.post(`/api/route-runs/${run.id}/replan`, {});
      if (next.status === 'failed') {
        toast('增量重排失败，已恢复为旧版本路线', 'err');
        close();
        await routingPage.render(view, ctx);
        return;
      }
      const rec = next.metrics.reconcile || {};
      const days = (rec.by_day || []).map((item) => `Day${item.day} ${item.before}→${item.after}`).join(' · ');
      toast(`重排完成：失效 ${rec.invalidated} · 保留 ${rec.carried} · 重排池 ${rec.pool} · 可见 ${days} 站`);
      close();
      state.runId = next.id;
      await routingPage.render(view, ctx);
    } catch (err) {
      const box = root.querySelector('#impact-error');
      if (box) box.textContent = errorMessage(err);
      submit.disabled = false;
      submit.textContent = '执行增量重排';
    }
  });
}

async function review(view, ctx, visitId, payload) {
  if (state.pending) return;
  state.pending = true;
  try {
    await api.patch(`/api/visits/${visitId}/review`, payload);
    toast('路线已更新');
    await routingPage.render(view, ctx);
  } catch (err) {
    toast(errorMessage(err), 'err');
  } finally {
    state.pending = false;
  }
}

function runSwitcher(runs, currentId) {
  const options = runs
    .map(
      (item) =>
        `<option value="${esc(item.id)}" ${String(item.id) === String(currentId) ? 'selected' : ''}>${esc(item.run_no)} · ${esc(ENUMS.routeStatus[item.status] || item.status)} · ${esc(ENUMS.trigger[item.trigger] || item.trigger)}</option>`,
    )
    .join('');
  return `<select id="run-switch" data-testid="run-switch" style="max-width:320px">${options}</select>`;
}

export const routingPage = {
  async render(view, ctx) {
    const editable = can(ctx.user.role, 'visit:review');
    let runs;
    let current = null;
    try {
      runs = await api.get('/api/route-runs', { limit: 10 });
      current = await api.get('/api/route-runs/current').catch(() => null);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => routingPage.render(view, ctx));
      return;
    }
    if (!runs.items.length) {
      view.innerHTML = `<section class="panel">
        <div class="panel-head"><h2>路线规划</h2><span class="meta">algorithm B</span></div>
        <div class="panel-body">
          ${emptyState({ title: '还没有排程', hint: '选好优先级和时间预算，跑一次两日路线。' })}
          ${editable ? '<div style="text-align:center"><button class="btn accent" id="new-run">发起排程</button></div>' : '<p class="muted" style="text-align:center">只读角色不能发起排程</p>'}
        </div>
      </section>`;
      view.querySelector('#new-run')?.addEventListener('click', () => openRunForm(view, ctx));
      return;
    }
    const targetId = state.runId || ctx.query.run_id || (current && current.id) || runs.items[0].id;
    let run;
    try {
      run = await api.get(`/api/route-runs/${targetId}`);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => routingPage.render(view, ctx));
      return;
    }
    if (!runs.items.some((item) => String(item.id) === String(run.id))) {
      runs.items.unshift(run);
    }
    const visits = run.visits || [];
    const scheduled = visits.filter((visit) => visit.seq < 9000);
    const deferred = visits.filter((visit) => visit.seq >= 9000);
    const reconcile = run.metrics.reconcile;
    const diff = run.metrics.diff || [];
    const diffByEx = new Map(diff.map((entry) => [entry.exhibitor_id, entry]));
    const isVisible = current && String(current.id) === String(run.id);
    const canReplan = editable && (run.status === 'success' || run.status === 'partial');

    if (run.status === 'failed') {
      view.innerHTML = `<section class="panel">
        <div class="panel-head"><h2>路线规划</h2>${runSwitcher(runs.items, run.id)}</div>
        <div class="panel-body">
          <div class="error-box"><span>本次${esc(ENUMS.trigger[run.trigger] || run.trigger)}失败（${esc(run.run_no)}），可见路线已保持为旧版本${current ? ` ${esc(current.run_no)}` : ''}。</span></div>
          <pre class="code" style="margin-top:10px">${esc(run.log_text || '（无日志）')}</pre>
          <div style="margin-top:10px;display:flex;gap:8px">
            ${current ? '<button class="btn accent" id="back-current">查看当前可见版本</button>' : ''}
            ${editable ? '<button class="btn ghost" id="new-run">重新全量排程</button>' : ''}
          </div>
        </div>
      </section>`;
      view.querySelector('#back-current')?.addEventListener('click', async () => {
        state.runId = '';
        await routingPage.render(view, ctx);
      });
      view.querySelector('#new-run')?.addEventListener('click', () => openRunForm(view, ctx));
      view.querySelector('#run-switch')?.addEventListener('change', async (event) => {
        state.runId = event.target.value;
        await routingPage.render(view, ctx);
      });
      return;
    }

    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>路线规划</h2>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            ${runSwitcher(runs.items, run.id)}
            ${canReplan ? '<button class="btn sm accent" id="impact-check" data-testid="impact-btn">影响核对 / 增量重排</button>' : ''}
            ${editable ? '<button class="btn sm ghost" id="new-run">重新全量排程</button>' : ''}
            ${can(ctx.user.role, 'export:read') ? '<button class="btn sm ghost" id="export-csv">导出观展清单</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          <div class="kv" style="margin-bottom:10px">
            <dt>版本</dt><dd class="mono">${esc(run.run_no)} · ${esc(ENUMS.trigger[run.trigger] || run.trigger)} · ${esc(ENUMS.routeStatus[run.status] || run.status)}${isVisible ? ' · 当前可见' : ''}${run.base_run_id ? ` · 基于 #${esc(run.base_run_id)}` : ''}</dd>
            <dt>操作</dt><dd class="mono">${esc(run.operator_email)} · 完成于 ${fmtTime(run.finished_at)}</dd>
          </div>
          <div class="grid-side">
            <div>
              ${metricBlock(run.metrics || {})}
              <div class="kv" style="margin-top:10px">
                <dt>参数</dt><dd class="mono">${esc((run.params.priorities || []).join('/'))} · ${esc(run.params.day_minutes)} 分钟/天 · ${esc(run.params.walk_speed_m_per_min)} m/min</dd>
                <dt>覆盖</dt><dd class="mono">${esc(run.metrics.covered_weight)} / ${esc(run.metrics.total_weight)} 权重 · ${esc(run.metrics.visit_count)} 家</dd>
              </div>
              ${reconcile ? reconcileBlock(reconcile) : ''}
            </div>
            <div>
              <div class="eyebrow">排程日志</div>
              <pre class="code" style="margin-top:6px">${esc(run.log_text || '（无日志）')}</pre>
            </div>
          </div>
          ${
            (run.metrics.conflict_pairs || 0) > 0
              ? `<p class="note-line" style="margin-top:10px">预约冲突 ${run.metrics.conflict_pairs} 组、标记 ${run.metrics.conflict_visits} 条；双方都打标记，只保留权重高者，另一条转改期。</p>`
              : '<p class="note-line" style="margin-top:10px">本次排程没有预约冲突。</p>'
          }
        </div>
      </section>
      ${diff.length ? diffPanel(diff) : ''}
      ${dayPanel(1, visits, ctx, editable, diffByEx)}
      ${dayPanel(2, visits, ctx, editable, diffByEx)}
      ${
        deferred.length
          ? `<section class="panel"><div class="panel-head"><h2>改期与超时项</h2><span class="meta">${deferred.length} 条</span></div>
              <div class="panel-body"><div class="walk-order">${deferred
                .map((visit) => `<div class="p0"><span>${esc(visit.exhibitor_name)} · ${esc(visit.booth_no)} · ${esc(visit.appointment_label || '')}</span><span>${esc(visit.conflict_note || '')}</span></div>`)
                .join('')}</div></div></section>`
          : ''
      }`;

    view.querySelector('#run-switch')?.addEventListener('change', async (event) => {
      state.runId = event.target.value;
      await routingPage.render(view, ctx);
    });
    view.querySelector('#impact-check')?.addEventListener('click', () => openImpact(view, ctx, run));
    view.querySelector('#new-run')?.addEventListener('click', () => openRunForm(view, ctx));
    view.querySelector('#export-csv')?.addEventListener('click', async () => {
      try {
        await api.download(`/api/exports/visits.csv?run_id=${run.id}`, 'boothmap-visits.csv');
        toast('已开始下载观展清单');
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    view.querySelectorAll('[data-action]').forEach((button) => {
      button.addEventListener('click', () => {
        const visitId = button.dataset.visit;
        const action = button.dataset.action;
        if (action === 'confirm') review(view, ctx, visitId, { status: 'confirmed', note: '专员现场确认' });
        if (action === 'defer') review(view, ctx, visitId, { status: 'deferred', note: '预约冲突，顺延到会后跟进' });
        if (action === 'day1' || action === 'day2') {
          review(view, ctx, visitId, { status: 'adjusted', day: Number(button.dataset.day), note: '人工改期' });
        }
      });
    });
  },
};

export default routingPage;
