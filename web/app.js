const formatNumber = new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 0 });
const formatDecimal = new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 1 });
const state = { transactions: [], projects: [], selected: new Map(), refreshing: false, editingProject: null,
  pendingProjectSale: null, mails: [], currentView: 'overview', craftingCatalog: null, currentPlan: null,
  plannerPrices: {}, plannerPriceSources: {}, plannerInventory: {}, stationFees: {}, focusCosts: {}, planTimer: null, planRequest: 0,
  targetPriceSource: '尚未取得市場資料', transmutationPlan: null, comparePrices: {}, comparePriceSources: {}, compareFees: {}, compareTimer: null, compareRequest: 0,
  autoProjectName: '',
  settings: { market_tax_rate: 4, setup_fee_rate: 2.5 }, settingsLoaded: false };

const byId = id => document.getElementById(id);
const text = (id, value) => { byId(id).textContent = value; };
const localTime = (value, options = {}) => new Date(value).toLocaleString('zh-TW', { hour12: false, ...options });
const formValue = id => byId(id).value;
const numericValue = id => Number(formValue(id)) || 0;
const viewLabels = {
  overview: ['交易總覽','掌握成交、實收與專案淨利'],
  transactions: ['成交帳本','檢查每筆買賣、費用與三分鐘批次'],
  projects: ['製造專案','自動展開多階配方，估算材料成本、收益與淨利'],
  sync: ['同步中心','確認捕捉器、郵件內容與訂單結果'],
};

function showView(name) {
  if (!viewLabels[name]) return;
  state.currentView=name;
  document.querySelectorAll('.view').forEach(view => view.classList.toggle('active',view.id===`view-${name}`));
  document.querySelectorAll('.nav-button').forEach(button => button.classList.toggle('active',button.dataset.view===name));
  text('view-title',viewLabels[name][0]); text('view-subtitle',viewLabels[name][1]);
  window.scrollTo({top:0,behavior:'smooth'});
}

function statusLabel(status) {
  if (!status || !status.online) return ['offline', '捕捉器離線', '統計服務仍可查閱舊資料；請啟動 albion-capture 接收新成交'];
  if (status.version && !status.version.startsWith('0.5.')) return ['warning', '捕捉器需要重新啟動', `目前為 v${status.version}；請重新啟動 v0.5.0 以辨識零成交與郵件解析狀態`];
  if (status.state === 'encrypted') return ['error', '市場資料已加密', status.message || '目前封包無法解析'];
  if (status.state === 'connected') {
    const who = [status.character_name, status.location_name].filter(Boolean).join('・');
    return ['online', '已連接 Albion', who || `已收到 ${formatNumber.format(status.packets_seen)} 個封包`];
  }
  return ['warning', '捕捉器在線，等待遊戲', status.message || '啟動遊戲後會自動更新'];
}

function renderStatus(captures) {
  const [kind, title, detail] = statusLabel(captures.find(item => item.online) || captures[0]);
  ['status-dot','sync-status-dot','sidebar-status-dot'].forEach(id => { byId(id).className=`status-dot ${kind}`; });
  text('status-title',title); text('status-detail',detail);
  text('sync-status-title',title); text('sync-status-detail',detail); text('sidebar-status-title',title);
}

function renderChart(summary) {
  const chart = byId('daily-chart');
  const labels = { day: 'LAST 24 HOURS', week: 'LAST 7 DAYS', month: 'LAST 30 DAYS', year: 'LAST YEAR', all: 'ALL TIME' };
  text('chart-period', labels[summary.period] || 'PERIOD');
  if (!summary.daily.length) { chart.innerHTML = '<p class="empty">這個區間還沒有成交資料</p>'; return; }
  const values = summary.daily.slice(-24);
  const max = Math.max(...values.flatMap(item => [item.spent || 0, item.revenue || 0]), 1);
  chart.replaceChildren(...values.map(item => {
    const wrapper = document.createElement('div'); wrapper.className = 'bar-wrap';
    wrapper.title = `${item.day}：花費 ${formatNumber.format(item.spent || 0)}／出售 ${formatNumber.format(item.revenue || 0)} 銀幣`;
    const pair = document.createElement('div'); pair.className = 'bar-pair';
    ['spent', 'revenue'].forEach(kind => {
      const bar = document.createElement('div'); bar.className = `bar ${kind}`;
      bar.style.height = `${Math.max(2, ((item[kind] || 0) / max) * 100)}%`; pair.append(bar);
    });
    const label = document.createElement('span'); label.className = 'bar-label';
    label.textContent = summary.bucket === 'hour' ? `${item.day.slice(11)}時` : item.day.slice(5);
    wrapper.append(pair, label); return wrapper;
  }));
}

function renderTopItems(items) {
  const list = byId('top-items');
  if (!items.length) { list.innerHTML = '<li class="empty">這個區間還沒有成交資料</li>'; return; }
  list.replaceChildren(...items.slice(0, 6).map(item => {
    const row = document.createElement('li'); const name = document.createElement('strong');
    name.textContent = item.item_name || item.item_id; name.title = item.item_id;
    const detail = document.createElement('small'); detail.textContent = `買 ${formatNumber.format(item.bought_quantity)}・賣 ${formatNumber.format(item.sold_quantity)}`; name.append(detail);
    const amounts = document.createElement('span'); amounts.textContent = `−${formatNumber.format(item.spent)}`;
    const revenue = document.createElement('small'); revenue.textContent = `＋${formatNumber.format(item.revenue)}`; amounts.append(revenue);
    row.append(name, amounts); return row;
  }));
}

function transactionLabel(item) {
  if (item.direction === 'buy') return item.transaction_kind === 'order' ? '購入訂單成交' : '購入';
  return item.transaction_kind === 'order' ? '出售訂單成交' : '出售';
}

function filteredTransactions() {
  const direction = formValue('direction-filter'), kind = formValue('kind-filter'), category = formValue('category-filter');
  return state.transactions.filter(item => (!direction || item.direction === direction) &&
    (!kind || item.transaction_kind === kind) && (!category || item.item_category === category));
}

function selectionCell(item) {
  const wrapper = document.createElement('div'); wrapper.className = 'selection-control';
  const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.className = 'project-check';
  checkbox.checked = state.selected.has(item.id); checkbox.setAttribute('aria-label', `選取 ${item.item_name}`);
  const quantity = document.createElement('input'); quantity.type = 'number'; quantity.className = 'project-quantity'; quantity.min = '1';
  const existing = state.editingProject ? (state.editingProject.items.find(x => x.transaction_id === item.id)?.quantity || 0) : 0;
  const capacity = item.available_quantity + existing;
  quantity.max = String(capacity); quantity.value = String(state.selected.get(item.id) || Math.max(1, capacity));
  quantity.disabled = !checkbox.checked || capacity <= 0;
  checkbox.disabled = capacity <= 0 && !checkbox.checked;
  checkbox.addEventListener('change', () => {
    quantity.disabled = !checkbox.checked;
    if (checkbox.checked) state.selected.set(item.id, Number(quantity.value)); else state.selected.delete(item.id);
    updateMaterialReadiness();
  });
  quantity.addEventListener('change', () => {
    const value = Math.min(capacity, Math.max(1, Number(quantity.value) || 1)); quantity.value = String(value);
    if (checkbox.checked) state.selected.set(item.id, value);
    updateMaterialReadiness();
  });
  wrapper.append(checkbox, quantity); return wrapper;
}

function actionButton(label, handler, className = '') {
  const button = document.createElement('button'); button.type = 'button'; button.className = `icon-button ${className}`;
  button.textContent = label; button.addEventListener('click', handler); return button;
}

function renderLedger() {
  const body=byId('ledger-rows'), items=filteredTransactions();
  if (!items.length) { body.innerHTML='<tr><td colspan="11" class="empty">沒有符合條件的成交資料</td></tr>'; return; }
  body.replaceChildren(...items.map(item => {
    const row=document.createElement('tr'); if (item.status==='sold') row.classList.add('archived-row');
    const fees=(item.sales_tax || 0)+(item.setup_fee || 0);
    const values=[localTime(item.traded_at),transactionLabel(item),item.item_name || item.item_id,
      item.location_name || item.location_id || '—',item.quantity,item.unit_price,item.total_price,fees,item.net_total];
    values.forEach((value,index) => {
      const cell=document.createElement('td');
      if (index===1) { const badge=document.createElement('span'); badge.className=`type-badge ${item.direction}`; badge.textContent=value; cell.append(badge); }
      else cell.textContent=index>=4 ? formatNumber.format(value || 0) : value;
      if (index===2) { cell.className='item-id'; cell.title=item.item_id; }
      if (index>=4) cell.classList.add('number');
      if (index===3) cell.title=item.location_id || '';
      if (index===7) cell.title=`交易稅 ${formatNumber.format(item.sales_tax || 0)}・設定費 ${formatNumber.format(item.setup_fee || 0)}`;
      row.append(cell);
    });
    const status=document.createElement('td'), badge=document.createElement('span'); badge.className=`status-badge ${item.status}`;
    badge.textContent=item.status==='sold' ? '已封存' : '進行中'; status.append(badge); row.append(status);
    const actions=document.createElement('td'); actions.className='row-actions'; actions.append(actionButton('修改',() => openTransactionDialog(item)),
      actionButton(item.status==='sold' ? '恢復' : '封存',() => setTransactionStatus(item)),actionButton('刪除',() => deleteTransaction(item),'danger')); row.append(actions);
    return row;
  }));
}

function renderTransactions() {
  const body = byId('transaction-rows'); const items = state.transactions;
  if (!items.length) { body.innerHTML = '<tr><td colspan="11" class="empty">沒有符合條件的成交資料</td></tr>'; return; }
  body.replaceChildren(...items.map(item => {
    const row = document.createElement('tr'); if (item.status === 'sold') row.classList.add('archived-row');
    const selection = document.createElement('td'); selection.append(selectionCell(item)); row.append(selection);
    const values = [localTime(item.traded_at), transactionLabel(item), item.item_name || item.item_id, item.item_category,
      item.location_name || item.location_id || '—', `${formatNumber.format(item.quantity)}／${formatNumber.format(item.available_quantity)}`,
      formatNumber.format(item.unit_price), formatNumber.format(item.total_price)];
    values.forEach((value, index) => {
      const cell = document.createElement('td');
      if (index === 1) { const badge = document.createElement('span'); badge.className = `type-badge ${item.direction}`; badge.textContent = value; cell.append(badge); }
      else cell.textContent = value;
      if (index === 7 && (item.sales_tax || item.setup_fee)) {
        const net = document.createElement('small'); net.className = 'muted'; net.textContent = `${item.direction==='sell' ? '實收' : '實付'} ${formatNumber.format(item.net_total)}（費用 ${formatNumber.format(item.sales_tax + item.setup_fee)}）`;
        cell.append(document.createElement('br'), net); cell.title = `交易稅 ${formatNumber.format(item.sales_tax)}・設定費 ${formatNumber.format(item.setup_fee)}`;
      }
      if (index === 2) { cell.className = 'item-id'; cell.title = item.item_id; }
      if (index >= 5) cell.classList.add('number'); if (index === 4) cell.title = item.location_id || ''; row.append(cell);
    });
    const status = document.createElement('td'); const badge = document.createElement('span');
    badge.className = `status-badge ${item.status}`; badge.textContent = item.status === 'sold' ? '已售出' : '進行中'; status.append(badge); row.append(status);
    const actions = document.createElement('td'); actions.className = 'row-actions';
    actions.append(actionButton('修改', () => openTransactionDialog(item)),
      actionButton(item.status === 'sold' ? '恢復' : '已售出', () => setTransactionStatus(item)),
      actionButton('刪除', () => deleteTransaction(item), 'danger')); row.append(actions);
    return row;
  }));
}

function renderCategoryOptions() {
  const select = byId('category-filter'), current = select.value;
  const categories = [...new Set(state.transactions.map(item => item.item_category))].sort();
  const all = document.createElement('option'); all.value = ''; all.textContent = '全部';
  select.replaceChildren(all, ...categories.map(value => { const option = document.createElement('option'); option.value = value; option.textContent = value; return option; }));
  select.value = categories.includes(current) ? current : '';
}

function renderSnapshots(items) {
  const body = byId('snapshot-rows');
  if (!items.length) { body.innerHTML = '<tr><td colspan="5" class="empty">還沒有統計區間</td></tr>'; return; }
  body.replaceChildren(...items.map(item => {
    const row = document.createElement('tr');
    const details = item.items.map(x => `${x.item_name} ${x.direction === 'buy' ? '買' : '賣'} ${formatNumber.format(x.quantity)}／${formatNumber.format(x.total_price)}`).join('、');
    [localTime(item.period_start, { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' }), details,
      item.transaction_count, formatNumber.format(item.spent), formatNumber.format(item.revenue)].forEach((value, index) => {
      const cell = document.createElement('td'); cell.textContent = value; if (index > 1) cell.className = 'number'; row.append(cell);
    }); return row;
  }));
}

function renderProjects(items) {
  state.projects = items; const container = byId('projects');
  if (!items.length) { container.innerHTML = '<p class="empty">尚未建立專案</p>'; return; }
  container.replaceChildren(...items.map(project => {
    const card = document.createElement('article'); card.className = 'project-card';
    const header = document.createElement('header'); const title = document.createElement('h3'); title.textContent = project.name;
    const actions = document.createElement('div'); actions.append(actionButton('新增出售', () => openTransactionDialog(null, project)),
      actionButton('修改', () => editProject(project)), actionButton('刪除', () => deleteProject(project.id), 'danger')); header.append(title, actions);
    const detail = document.createElement('p'); detail.textContent = `${formatNumber.format(project.selection_count)} 筆分配・${project.project_type === 'manufacturing' ? '製造專案' : '一般交易'}`;
    const totals = document.createElement('p'); totals.className = 'project-totals';
    const hasPlan = Boolean(project.production_plan);
    totals.textContent = hasPlan ?
      `預估成本 ${formatNumber.format(project.planned_total_cost)}・預估實收 ${formatNumber.format(project.projected_revenue)}・預估淨利 ${formatNumber.format(project.projected_profit)}` :
      `成本 ${formatNumber.format(project.spent + project.extra_cost)}・出售 ${formatNumber.format(project.revenue)}・費用 ${formatNumber.format(project.fees)}・淨利 ${formatNumber.format(project.net_profit)}`;
    card.append(header, detail, totals);
    if (project.project_type === 'manufacturing') {
      const production = document.createElement('div'); production.className = 'production-grid';
      const outputName = project.output_item_name || project.output_item_id || '成品';
      production.innerHTML = hasPlan ?
        `<span>材料成本 <strong>${formatNumber.format(project.planned_material_cost)}</strong></span><span>製作台費 <strong>${formatNumber.format(project.planned_station_cost)}</strong></span><span>目標${outputName} <strong>${formatNumber.format(project.target_output)}</strong></span><span>實際已售 <strong>${formatNumber.format(project.sold_output_quantity)}</strong></span><span>損益兩平單價 <strong>${formatNumber.format(project.break_even_unit_price)}</strong></span><span>實際淨利 <strong>${formatNumber.format(project.net_profit)}</strong></span>` :
        `<span>材料 <strong>${formatNumber.format(project.input_quantity)}</strong></span><span>回報率 <strong>${formatDecimal.format(project.active_return_rate)}%</strong></span><span>預估${outputName} <strong>${formatNumber.format(project.expected_output)}</strong></span><span>尚未售出 <strong>${formatNumber.format(project.unsold_output_quantity)}</strong></span><span>損益兩平單價 <strong>${formatNumber.format(project.break_even_unit_price)}</strong></span><span>ROI <strong>${formatDecimal.format(project.roi)}%</strong></span>`;
      card.append(production);
      if (project.target_output) {
        const calculator = document.createElement('details');
        const summary = document.createElement('summary');
        const focusText = project.use_focus ? `專注製作 ${project.focus_crafts} 次／一般 ${project.normal_crafts} 次` : `一般製作 ${project.crafts_required} 次`;
        const modeLabels={expected:'淨耗用',p95:'95% 安全量',p99:'99% 安全量',guaranteed:'最壞情況保證'};
        summary.textContent = `目標 ${formatNumber.format(project.target_output)}・${hasPlan ? '多階淨耗用' : (modeLabels[project.planning_mode] || '安全量')}・${focusText}`; calculator.append(summary);
        const requirement = document.createElement('p');
        requirement.textContent = project.required_materials.map(material => {
          const ready=material.ready_required_quantity ?? material.required_quantity;
          const inventory=material.inventory_quantity || 0;
          return `${material.name || material.item_name || material.item_id}：帳本 ${formatNumber.format(material.allocated_quantity)}＋額外庫存 ${formatNumber.format(inventory)}／開工 ${formatNumber.format(ready)}，尚缺 ${formatNumber.format(material.shortage)}`;
        }).join('・') || '尚未設定原材料';
        calculator.append(requirement);
        if (project.focus_shortage) {
          const shortage = document.createElement('p'); shortage.className = 'focus-shortage';
          shortage.textContent = `專注不足 ${formatNumber.format(project.focus_shortage)} 點，已自動將超出部分改用一般回報率計算。`; calculator.append(shortage);
        }
        card.append(calculator);
      }
    }
    const names = document.createElement('p'); names.textContent = project.items.map(x => `${x.item_name} ${x.direction === 'buy' ? '買' : '賣'} × ${x.quantity}`).join('、'); card.append(names); return card;
  }));
}

function renderMails(items) {
  state.mails=items;
  const normalized=items.map(mail => ({...mail,state:mail.state==='verified' ? 'completed' : mail.state}));
  const counts={pending:0,completed:0,partial:0,no_trade:0,parse_error:0};
  normalized.forEach(mail => { if (counts[mail.state] !== undefined) counts[mail.state] += 1; });
  text('mail-total',formatNumber.format(items.length)); text('mail-pending',formatNumber.format(counts.pending));
  text('mail-completed',formatNumber.format(counts.completed+counts.partial)); text('mail-no-trade',formatNumber.format(counts.no_trade));
  text('mail-errors',formatNumber.format(counts.parse_error));
  const filter=formValue('mail-filter'); const visible=normalized.filter(mail => !filter || mail.state===filter);
  const body = byId('mail-rows');
  if (!visible.length) { body.innerHTML = `<tr><td colspan="5" class="empty">${items.length ? '沒有符合此狀態的市場信件' : '尚未收到市場信件；登入後載入信箱即可建立待驗證清單'}</td></tr>`; return; }
  const labels = { MARKETPLACE_BUYORDER_FINISHED_SUMMARY:'購入訂單完成', MARKETPLACE_BUYORDER_EXPIRED_SUMMARY:'購入訂單到期',
    MARKETPLACE_SELLORDER_FINISHED_SUMMARY:'出售訂單完成', MARKETPLACE_SELLORDER_EXPIRED_SUMMARY:'出售訂單到期',
    BLACKMARKET_SELLORDER_EXPIRED_SUMMARY:'黑市出售訂單到期' };
  const stateLabels={pending:'待開啟內容',completed:'成交',partial:'部分成交',no_trade:'零成交',parse_error:'解析失敗',ignored:'已忽略'};
  body.replaceChildren(...visible.map(mail => {
    const row = document.createElement('tr');
    const pendingFinished=mail.state==='pending' && mail.mail_type.includes('_FINISHED_');
    const trade = mail.transaction_id ? `${mail.item_name || mail.item_id} × ${formatNumber.format(mail.quantity)}／${formatNumber.format(mail.total_price)}` :
      (mail.state==='no_trade' ? '成交數量 0・未寫入帳本' : mail.state==='parse_error' ? '內容格式無法辨識' :
        pendingFinished ? '已知成交・等待品項與價格' : '訂單到期・等待確認成交數量');
    [mail.received_at ? localTime(mail.received_at) : localTime(mail.captured_at), labels[mail.mail_type] || mail.mail_type,
      mail.location_name || mail.location_id || '—', trade].forEach(value => { const cell=document.createElement('td'); cell.textContent=value; row.append(cell); });
    const stateCell=document.createElement('td'); const badge=document.createElement('span'); badge.className=`mail-state ${mail.state}`;
    badge.textContent=mail.state==='pending' ? (pendingFinished ? '成交待明細' : '到期待確認') : (stateLabels[mail.state] || mail.state);
    stateCell.append(badge); row.append(stateCell); return row;
  }));
}

function renderWarnings(warnings) {
  const panel = byId('warning-panel'); if (!warnings.length) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden'); byId('warnings').replaceChildren(...warnings.map(warning => {
    const item = document.createElement('li'); item.textContent = `${warning.message}（${localTime(warning.captured_at)}）`; return item;
  }));
}

async function resolveNames(items) {
  const ids = [...new Set(items.map(item => item.item_id))]; if (!ids.length) return;
  try {
    const response = await fetch(`/api/catalog?ids=${encodeURIComponent(ids.join(','))}`); if (!response.ok) return;
    const { names } = await response.json(); items.forEach(item => { if (names[item.item_id]) item.item_name = names[item.item_id]; });
  } catch (_) { /* local fallback is already present */ }
}

function plannerConfig() {
  return { family:formValue('recipe-family'), target_tier:numericValue('target-tier'), start_tier:numericValue('start-tier'),
    enchantment:numericValue('enchantment'), quantity:numericValue('target-output'), return_rate:numericValue('return-rate'),
    focus_return_rate:numericValue('focus-return-rate'), use_focus:byId('use-focus').checked,
    available_focus:numericValue('available-focus'), station_fees:{...state.stationFees}, focus_costs:{...state.focusCosts} };
}

function catalogItem(family, kind, tier, enchantment) {
  return state.craftingCatalog?.items.find(item => item.family===family && item.kind===kind && item.tier===Number(tier) && item.enchantment===Number(enchantment));
}

function syncStartTierOptions() {
  const select=byId('start-tier'), target=numericValue('target-tier'), enchantment=numericValue('enchantment');
  const previous=Number(select.value), minimum=enchantment ? 3 : 2;
  select.replaceChildren(...Array.from({length:target-minimum+1},(_,index) => {
    const tier=minimum+index, option=document.createElement('option'); option.value=String(tier); option.textContent=`${tier}.${tier >= 4 ? enchantment : 0}`; return option;
  }));
  select.value=String(Math.min(target,Math.max(minimum,previous || target-1)));
}

function schedulePlannerUpdate() {
  clearTimeout(state.planTimer); state.planTimer=setTimeout(updatePlanner,120);
}

async function updatePlanner() {
  const requestId=++state.planRequest; syncStartTierOptions();
  try {
    const response=await fetch('/api/crafting/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(plannerConfig())});
    const plan=await response.json(); if (!response.ok) throw new Error(plan.error || '配方計算失敗');
    if (requestId!==state.planRequest) return; state.currentPlan=plan; renderPlanner(plan);
  } catch (error) {
    if (requestId!==state.planRequest) return; state.currentPlan=null;
    text('plan-decision','無法計算'); text('planner-note',error.message);
  }
}

function allocatedQuantityForItem(itemId) {
  return state.transactions.reduce((total,item) => total + (item.direction==='buy' && item.item_id===itemId ? (state.selected.get(item.id) || 0) : 0),0);
}

function selectedWeightedPrice(itemId) {
  const rows=state.transactions.filter(item => item.direction==='buy' && item.item_id===itemId && (state.selected.get(item.id) || 0)>0);
  const quantity=rows.reduce((sum,item) => sum+(state.selected.get(item.id)||0),0);
  if (!quantity) return null;
  const cost=rows.reduce((sum,item) => sum+(item.net_total/item.quantity)*(state.selected.get(item.id)||0),0);
  return {price:Math.round(cost/quantity),quantity,orders:rows.length};
}

function updateMaterialReadiness() {
  if (!state.currentPlan) return;
  state.currentPlan.materials.forEach(material => {
    const allocated=allocatedQuantityForItem(material.item_id), inventory=Math.max(0,Number(state.plannerInventory[material.item_id])||0);
    const available=allocated+inventory, needed=material.gross_quantity, shortage=Math.max(needed-available,0);
    const coverage=document.querySelector(`.material-coverage[data-item-id="${CSS.escape(material.item_id)}"]`);
    const shortageCell=document.querySelector(`.material-shortage[data-item-id="${CSS.escape(material.item_id)}"]`);
    if (coverage) coverage.textContent=`帳本 ${formatNumber.format(allocated)}＋額外 ${formatNumber.format(inventory)}／${formatNumber.format(needed)}`;
    if (shortageCell) { shortageCell.textContent=shortage ? formatNumber.format(shortage) : '已備齊'; shortageCell.classList.toggle('ready',!shortage); }
    const weighted=selectedWeightedPrice(material.item_id), currentSource=state.plannerPriceSources[material.item_id] || '';
    if (weighted && currentSource!=='手動輸入') {
      state.plannerPrices[material.item_id]=weighted.price;
      state.plannerPriceSources[material.item_id]=`帳本分配加權平均・${formatNumber.format(weighted.quantity)} 件／${weighted.orders} 筆`;
      const priceInput=document.querySelector(`.planner-price-input[data-item-id="${CSS.escape(material.item_id)}"]`);
      if (priceInput) priceInput.value=String(weighted.price);
    }
  });
  updatePlanTotals();
}

function inferTargetFromInventory(material) {
  const available=allocatedQuantityForItem(material.item_id)+(Number(state.plannerInventory[material.item_id])||0);
  if (!available) { text('project-message',`請先輸入或分配 ${material.name} 的持有數量。`); byId('project-message').className='form-message error'; return; }
  const inferred=Math.max(1,Math.floor(state.currentPlan.quantity*available/Math.max(material.gross_quantity,1)));
  byId('target-output').value=String(inferred); byId('project-message').className='form-message success';
  text('project-message',`已依 ${material.name} 可用 ${formatNumber.format(available)} 個，反推目標為 ${formatNumber.format(inferred)} 個；其他材料會同步重算。`);
  schedulePlannerUpdate();
}

function renderPlanner(plan) {
  byId('planner-output-icon').src=plan.output_item.icon_url; text('planner-output-name',plan.output_item.name); text('planner-output-id',plan.output_item.item_id);
  const suggestedName=`${plan.output_item.name} × ${formatNumber.format(plan.quantity)}`, projectName=byId('project-name');
  if (!projectName.value.trim() || projectName.value===state.autoProjectName) projectName.value=suggestedName;
  state.autoProjectName=suggestedName;
  const steps=byId('refining-steps');
  if (!plan.steps.length) steps.innerHTML='<p class="empty">起始品項就是目標成品，不需要再精煉。</p>';
  else steps.replaceChildren(...plan.steps.map(step => {
    const card=document.createElement('article'); card.className='refining-step';
    const header=document.createElement('header'); const image=document.createElement('img'); image.src=step.output_icon_url; image.alt='';
    const heading=document.createElement('div'); const title=document.createElement('strong'); title.textContent=`製作 ${step.output_name} × ${formatNumber.format(step.crafts)}`;
    const focus=document.createElement('small'); focus.textContent=step.focus_crafts ? `專注 ${step.focus_crafts} 次・一般 ${step.normal_crafts} 次・消耗 ${formatNumber.format(step.focus_used)} 專注` : `一般製作 ${step.normal_crafts} 次`;
    heading.append(title,focus); header.append(image,heading);
    const flow=document.createElement('p'); flow.className='step-flow';
    flow.innerHTML=step.materials.map(material => `<b>${material.name}</b> 淨 ${formatNumber.format(material.required_quantity)}（毛 ${formatNumber.format(material.gross_quantity)}／回報 ${formatNumber.format(material.returned_quantity)}）`).join('<br>');
    const controls=document.createElement('div'); controls.className='step-controls';
    const fee=document.createElement('label'); fee.className='step-fee'; fee.append(document.createTextNode('每件製作台費'));
    const input=document.createElement('input'); input.type='number'; input.min='0'; input.value=String(state.stationFees[step.output_item_id] ?? step.fee_per_craft ?? 0); input.setAttribute('aria-label',`${step.output_name} 每件製作台費`);
    input.addEventListener('input',() => {
      const value=Math.max(0,Number(input.value)||0); state.stationFees[step.output_item_id]=value; step.fee_per_craft=value; step.station_cost=value*step.crafts;
      plan.total_station_cost=plan.steps.reduce((sum,item) => sum+item.station_cost,0); updatePlanTotals();
    });
    input.addEventListener('change',schedulePlannerUpdate); fee.append(input);
    const focusCost=document.createElement('label'); focusCost.className='step-fee'; focusCost.append(document.createTextNode('每件專注'));
    const focusInput=document.createElement('input'); focusInput.type='number'; focusInput.min='1'; focusInput.value=String(state.focusCosts[step.output_item_id] ?? step.focus_cost_per_craft); focusInput.setAttribute('aria-label',`${step.output_name} 每件專注消耗`);
    focusInput.addEventListener('input',() => { state.focusCosts[step.output_item_id]=Math.max(1,Number(focusInput.value)||step.base_focus_cost_per_craft); });
    focusInput.addEventListener('change',schedulePlannerUpdate); focusCost.append(focusInput); controls.append(fee,focusCost);
    card.append(header,flow,controls); return card;
  }));
  const body=byId('planner-material-rows');
  body.replaceChildren(...plan.materials.map(material => {
    const row=document.createElement('tr'); const itemCell=document.createElement('td'); itemCell.className='material-item-cell';
    const label=document.createElement('div'); label.className='material-item-label'; const image=document.createElement('img'); image.src=material.icon_url; image.alt='';
    const copy=document.createElement('div'); const name=document.createElement('strong'); name.textContent=material.name; const id=document.createElement('small'); id.textContent=material.item_id; copy.append(name,id); label.append(image,copy); itemCell.append(label); row.append(itemCell);
    [material.gross_quantity,material.required_quantity].forEach(value => { const cell=document.createElement('td'); cell.className='number'; cell.textContent=formatNumber.format(value); row.append(cell); });
    const inventoryCell=document.createElement('td'); inventoryCell.className='material-inventory';
    const coverage=document.createElement('span'); coverage.className='material-coverage'; coverage.dataset.itemId=material.item_id;
    const inventoryControls=document.createElement('div'); inventoryControls.className='inventory-controls';
    const inventoryInput=document.createElement('input'); inventoryInput.type='number'; inventoryInput.min='0'; inventoryInput.value=String(state.plannerInventory[material.item_id] || 0); inventoryInput.setAttribute('aria-label',`${material.name} 額外庫存`); inventoryInput.placeholder='額外庫存';
    inventoryInput.addEventListener('input',() => { state.plannerInventory[material.item_id]=Math.max(0,Number(inventoryInput.value)||0); updateMaterialReadiness(); });
    const infer=document.createElement('button'); infer.type='button'; infer.className='icon-button'; infer.textContent='依此反推'; infer.addEventListener('click',() => inferTargetFromInventory(material));
    inventoryControls.append(inventoryInput,infer); inventoryCell.append(coverage,inventoryControls); row.append(inventoryCell);
    const shortage=document.createElement('td'); shortage.className='number material-shortage'; shortage.dataset.itemId=material.item_id; row.append(shortage);
    const priceCell=document.createElement('td'); priceCell.className='number'; const input=document.createElement('input'); input.className='planner-price-input'; input.dataset.itemId=material.item_id; input.type='number'; input.min='0'; input.value=String(state.plannerPrices[material.item_id] || 0); input.setAttribute('aria-label',`${material.name} 單價`);
    input.addEventListener('input',() => { state.plannerPrices[material.item_id]=Math.max(0,Number(input.value)||0); state.plannerPriceSources[material.item_id]='手動輸入'; updatePlanTotals(); }); priceCell.append(input); row.append(priceCell);
    const subtotal=document.createElement('td'); subtotal.className='number material-subtotal'; subtotal.dataset.itemId=material.item_id; row.append(subtotal);
    const source=document.createElement('td'); source.className='price-source'; source.dataset.sourceId=material.item_id; source.textContent=state.plannerPriceSources[material.item_id] || '尚未輸入'; row.append(source); return row;
  }));
  text('plan-focus-summary',plan.use_focus ? `已用 ${formatNumber.format(plan.focus_used)} 專注・剩餘 ${formatNumber.format(plan.focus_remaining)}` : '未使用專注點；全部按一般回報率計算');
  text('planner-note','官方 Wiki 說明實際返還會按批次取整並貼近回報率；開工需備採配方毛投入，預估淨耗用採向上保守整數。專注成本請依角色專精後的遊戲顯示值覆寫。'); updatePlanTotals(); updateMaterialReadiness();
}

function updatePlanTotals() {
  const plan=state.currentPlan; if (!plan) return;
  let materialCost=0;
  plan.materials.forEach(material => {
    const subtotal=material.required_quantity*(state.plannerPrices[material.item_id] || 0); materialCost+=subtotal;
    const cell=document.querySelector(`.material-subtotal[data-item-id="${CSS.escape(material.item_id)}"]`); if (cell) cell.textContent=formatNumber.format(subtotal);
    const source=document.querySelector(`.price-source[data-source-id="${CSS.escape(material.item_id)}"]`); if (source) source.textContent=state.plannerPriceSources[material.item_id] || '尚未輸入';
  });
  const stationCost=plan.total_station_cost, extra=numericValue('extra-cost'), total=materialCost+stationCost+extra;
  const gross=plan.quantity*numericValue('target-sale-price'), revenue=Math.round(gross*(1-numericValue('sale-fee-rate')/100)), profit=revenue-total;
  text('plan-material-cost',formatNumber.format(materialCost)); text('plan-station-cost',formatNumber.format(stationCost)); text('plan-total-cost',formatNumber.format(total));
  text('plan-revenue',formatNumber.format(revenue)); text('plan-profit',formatNumber.format(profit));
  text('target-price-source',state.targetPriceSource);
  const line=document.querySelector('.plan-totals .profit-line'); line.classList.toggle('loss',profit<0);
  text('plan-decision',!numericValue('target-sale-price') ? '等待成品價格' : profit>=0 ? `預估可獲利 ${formatNumber.format(profit)}` : `預估虧損 ${formatNumber.format(Math.abs(profit))}`);
}

function marketLabel(server,location) {
  const servers={east:'亞洲服',west:'美洲服',europe:'歐洲服'};
  const locations={Martlock:'馬特洛克',Lymhurst:'林姆赫斯特',Bridgewatch:'橋望城','Fort Sterling':'斯特靈堡',Thetford:'塞特福德',Caerleon:'卡利昂',Brecilien:'布雷西利恩','Black Market':'黑市'};
  return `${servers[server] || server || '未知伺服器'}・${locations[location] || location || '未知市場'} (${location || 'unknown'})`;
}

function marketSource(item, priceKind='sell', server=item?.market_server, location=item?.city || item?.market_location) {
  const value=priceKind==='sell' ? item.sell_price_min : item.buy_price_max;
  const stamp=priceKind==='sell' ? item.sell_price_min_date : item.buy_price_max_date;
  const market=marketLabel(server,location);
  if (value) return `${market} 市場 ${formatNumber.format(value)}・${stamp && !stamp.startsWith('0001') ? stamp.replace('T',' ').slice(0,16) : '時間未知'}`;
  if (item.history_avg_price) return `${market} 近 ${item.history_sample_days || 14} 個成交日均價 ${formatNumber.format(item.history_avg_price)}・${item.history_avg_date?.slice(0,10) || '日期未知'}`;
  if (item.local_estimate?.buy_unit_price) return `本機成交推估・${item.local_estimate.updated_at?.slice(0,16).replace('T',' ') || '時間未知'}`;
  return '市場沒有可用價格';
}

function marketPrice(item) {
  return item?.sell_price_min || item?.history_avg_price || item?.local_estimate?.buy_unit_price || 0;
}

async function fetchPlannerMarketPrices() {
  if (!state.currentPlan) return; const button=byId('fetch-market-prices'); button.disabled=true; text('market-price-status','正在取得市場資料…');
  const ids=[...state.currentPlan.materials.map(item => item.item_id),state.currentPlan.output_item.item_id];
  try {
    const query=new URLSearchParams({ids:ids.join(','),server:formValue('market-server'),location:formValue('market-location')});
    const response=await fetch(`/api/market/prices?${query}`); const result=await response.json(); if (!response.ok) throw new Error(result.error || '市場價格取得失敗');
    result.items.forEach(item => {
      item.market_server=result.server; item.market_location=result.location;
      const price=marketPrice(item);
      if (item.item_id===state.currentPlan.output_item.item_id) {
        if (price) { byId('target-sale-price').value=String(price); state.targetPriceSource=marketSource(item); }
      } else if (price) { state.plannerPrices[item.item_id]=price; state.plannerPriceSources[item.item_id]=marketSource(item); }
    });
    renderPlanner(state.currentPlan); text('market-price-status',result.warning ? `${result.warning}；保留已輸入價格並使用可用的本機推估` : `${result.location} 已更新；即時價格缺少時自動使用近期成交均價`);
  } catch (error) { text('market-price-status',error.message); }
  finally { button.disabled=false; }
}

function transmutationConfig() {
  return {family:formValue('compare-family'),target_tier:numericValue('compare-tier'),enchantment:numericValue('compare-enchantment')};
}

function scheduleComparatorUpdate() {
  clearTimeout(state.compareTimer); state.compareTimer=setTimeout(updateTransmutationPlan,120);
}

async function updateTransmutationPlan() {
  const requestId=++state.compareRequest;
  try {
    const response=await fetch('/api/crafting/transmutation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(transmutationConfig())});
    const plan=await response.json(); if (!response.ok) throw new Error(plan.error || '轉換路徑計算失敗');
    if (requestId!==state.compareRequest) return; state.transmutationPlan=plan; await loadComparatorLedgerPrices(plan);
    if (requestId!==state.compareRequest) return; renderComparator(plan);
  } catch (error) { if (requestId===state.compareRequest) byId('compare-routes').innerHTML=`<p class="empty">${error.message}</p>`; }
}

async function loadComparatorLedgerPrices(plan) {
  const ids=[...new Set([...plan.routes.map(route => route.input_item.item_id),plan.target_item.item_id])];
  try {
    const response=await fetch(`/api/ledger/prices?ids=${encodeURIComponent(ids.join(','))}`), result=await response.json();
    if (!response.ok) return;
    result.items.forEach(item => {
      if (!item.buy_unit_price || state.comparePriceSources[item.item_id]==='手動輸入') return;
      state.comparePrices[item.item_id]=item.buy_unit_price;
      state.comparePriceSources[item.item_id]=`帳本購入加權平均・${formatNumber.format(item.buy_quantity || 0)} 件（只供比較，不扣數量）`;
    });
  } catch (_) { /* comparison still supports manual and market prices */ }
}

function compareFeeKey(route) { return `${state.transmutationPlan.target_item.item_id}:${route.id}`; }

function renderComparator(plan) {
  byId('compare-output-icon').src=plan.target_item.icon_url; text('compare-output-name',plan.target_item.name); text('compare-output-id',plan.target_item.item_id);
  const container=byId('compare-routes'), routes=[...plan.routes,{id:'direct',label:'直接購買',description:`市場購買 ${plan.target_item.name}`,input_item:plan.target_item,input_per_output:1,base_silver_fee:0}];
  container.replaceChildren(...routes.map(route => {
    const card=document.createElement('article'); card.className='compare-route'; card.dataset.routeId=route.id;
    const header=document.createElement('header'), image=document.createElement('img'); image.src=route.input_item.icon_url; image.alt='';
    const title=document.createElement('div'); title.innerHTML=`<strong>${route.label}</strong><small>${route.description}</small>`; header.append(image,title); card.append(header);
    const fields=document.createElement('div'); fields.className='route-fields';
    const priceLabel=document.createElement('label'); priceLabel.append(document.createTextNode(`${route.input_item.name} 單價`));
    const price=document.createElement('input'); price.type='number'; price.min='0'; price.value=String(state.comparePrices[route.input_item.item_id] || 0); price.setAttribute('aria-label',`${route.input_item.name} 單價`);
    price.addEventListener('input',() => { state.comparePrices[route.input_item.item_id]=Math.max(0,Number(price.value)||0); state.comparePriceSources[route.input_item.item_id]='手動輸入'; updateComparatorTotals(); }); priceLabel.append(price); fields.append(priceLabel);
    if (route.id!=='direct') {
      const feeLabel=document.createElement('label'); feeLabel.append(document.createTextNode('每件銀幣費'));
      const fee=document.createElement('input'), key=compareFeeKey(route); fee.type='number'; fee.min='0'; fee.value=String(state.compareFees[key] ?? route.base_silver_fee); fee.setAttribute('aria-label',`${route.description} 每件銀幣費`);
      if (state.compareFees[key]===undefined) state.compareFees[key]=route.base_silver_fee;
      fee.addEventListener('input',() => { state.compareFees[key]=Math.max(0,Number(fee.value)||0); updateComparatorTotals(); }); feeLabel.append(fee); fields.append(feeLabel);
    }
    card.append(fields);
    const source=document.createElement('p'); source.className='route-source'; source.dataset.itemId=route.input_item.item_id; source.textContent=state.comparePriceSources[route.input_item.item_id] || '尚未取得價格';
    const total=document.createElement('div'); total.className='route-total'; total.innerHTML='<span>數量總成本</span><strong>0</strong><small></small>'; card.append(source,total); return card;
  }));
  container.querySelectorAll('.compare-route:not([data-route-id="direct"])').forEach(card => {
    const route=plan.routes.find(item => item.id===card.dataset.routeId), button=document.createElement('button'); button.type='button'; button.className='button route-project-button';
    button.textContent='建立專案並分配帳本數量'; button.addEventListener('click',() => createTransmutationProject(route)); card.append(button);
  });
  updateComparatorTotals();
}

async function createTransmutationProject(route) {
  const plan=state.transmutationPlan, quantity=Math.max(1,numericValue('compare-quantity'));
  let remaining=quantity*route.input_per_output; const selections=[];
  state.transactions.filter(item => item.direction==='buy' && item.item_id===route.input_item.item_id && item.available_quantity>0).forEach(item => {
    if (!remaining) return; const selected=Math.min(remaining,item.available_quantity); selections.push({transaction_id:item.id,quantity:selected}); remaining-=selected;
  });
  const fee=state.compareFees[compareFeeKey(route)] ?? route.base_silver_fee, targetPrice=state.comparePrices[plan.target_item.item_id] || 0;
  const payload={name:`${route.description} ${plan.target_item.name} × ${quantity}`,selections,project_type:'manufacturing',
    input_item_id:route.input_item.item_id,output_item_id:plan.target_item.item_id,output_item_name:plan.target_item.name,
    return_rate:0,focus_return_rate:0,use_focus:false,material_per_unit:route.input_per_output,output_per_craft:1,
    extra_cost:fee*quantity,sale_fee_rate:0,target_sale_price:targetPrice,target_output:quantity,available_focus:0,
    focus_cost_per_craft:0,planning_mode:'expected',materials:[{item_id:route.input_item.item_id,item_name:route.input_item.name,quantity_per_craft:route.input_per_output}],
    notes:`原料轉換 ${route.description}；每件銀幣費 ${fee}`};
  try {
    const response=await fetch('/api/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}), result=await response.json();
    if (!response.ok) throw new Error(result.error || '建立專案失敗');
    text('compare-note',`已建立「${payload.name}」，帳本自動分配 ${formatNumber.format(quantity*route.input_per_output-remaining)}／${formatNumber.format(quantity*route.input_per_output)}；尚缺會顯示在專案中。`); await refresh();
  } catch (error) { text('compare-note',error.message); }
}

function updateComparatorTotals() {
  const plan=state.transmutationPlan; if (!plan) return; const quantity=Math.max(1,numericValue('compare-quantity'));
  const cards=[...document.querySelectorAll('.compare-route')], candidates=[];
  cards.forEach(card => {
    const id=card.dataset.routeId, route=id==='direct' ? {id,input_item:plan.target_item,input_per_output:1} : plan.routes.find(item => item.id===id);
    const unitPrice=state.comparePrices[route.input_item.item_id] || 0, fee=id==='direct' ? 0 : (state.compareFees[compareFeeKey(route)] ?? route.base_silver_fee);
    const cost=quantity*(unitPrice*route.input_per_output+fee); card.querySelector('.route-total strong').textContent=formatNumber.format(cost);
    card.classList.remove('best'); card.querySelector('.route-total small').textContent=''; if (unitPrice>0) candidates.push({id,cost,card});
    const source=card.querySelector('.route-source'); if (source) source.textContent=state.comparePriceSources[route.input_item.item_id] || '尚未取得價格';
  });
  if (!candidates.length) return; const cheapest=Math.min(...candidates.map(item => item.cost));
  candidates.forEach(item => { const difference=item.cost-cheapest; if (!difference) { item.card.classList.add('best'); item.card.querySelector('.route-total small').textContent='目前最便宜'; } else item.card.querySelector('.route-total small').textContent=`比最低多 ${formatNumber.format(difference)}`; });
}

async function fetchComparatorPrices() {
  const plan=state.transmutationPlan; if (!plan) return; const button=byId('fetch-compare-prices'); button.disabled=true;
  const ids=[...new Set([...plan.routes.map(route => route.input_item.item_id),plan.target_item.item_id])];
  try {
    const query=new URLSearchParams({ids:ids.join(','),server:formValue('compare-market-server'),location:formValue('compare-market-location')});
    const response=await fetch(`/api/market/prices?${query}`), result=await response.json(); if (!response.ok) throw new Error(result.error || '市場價格取得失敗');
    result.items.forEach(item => { item.market_server=result.server; item.market_location=result.location; const price=marketPrice(item); if (price) { state.comparePrices[item.item_id]=price; state.comparePriceSources[item.item_id]=marketSource(item); } });
    renderComparator(plan); const unavailable=ids.filter(id => !state.comparePrices[id]).length;
    text('compare-note',`${result.location} 已更新三條路徑；${unavailable ? `${unavailable} 個品項仍無資料，可手動輸入。` : '即時缺價時已使用近期成交均價。'} 銀幣費仍請以遊戲畫面為準。`);
  } catch (error) { text('compare-note',`${error.message}；已保留手動輸入與先前成功取得的價格。`); }
  finally { button.disabled=false; }
}

async function loadCraftingCatalog() {
  try {
    const response=await fetch('/api/crafting/catalog'), result=await response.json(); if (!response.ok) throw new Error('物品目錄載入失敗');
    state.craftingCatalog=result; schedulePlannerUpdate(); scheduleComparatorUpdate();
  } catch (error) { text('planner-note',error.message); }
}

function projectPayload() {
  const plan=state.currentPlan, config=plannerConfig();
  const materials=(plan?.materials || []).map(material => ({item_id:material.item_id,item_name:material.name,
    quantity_per_craft:material.required_quantity/Math.max(plan.quantity,1)}));
  const plannerState={...config,prices:{...state.plannerPrices},inventory:{...state.plannerInventory},auto_allocate:byId('auto-allocate-materials').checked,
    market_server:formValue('market-server'),market_location:formValue('market-location')};
  return { project_type:'manufacturing', input_item_id:materials[0]?.item_id || '', output_item_id:plan?.output_item.item_id || '',
    output_item_name:plan?.output_item.name || '', return_rate:config.return_rate, focus_return_rate:config.focus_return_rate,
    use_focus:config.use_focus, material_per_unit:materials[0]?.quantity_per_craft || 1, output_per_craft:1,
    extra_cost:numericValue('extra-cost'), sale_fee_rate:numericValue('sale-fee-rate'), target_sale_price:numericValue('target-sale-price'),
    target_output:config.quantity, available_focus:config.available_focus, focus_cost_per_craft:0,
    planning_mode:'expected', planner_state:plannerState, materials };
}

function clearProjectEditor() {
  state.editingProject=null; state.selected.clear(); state.plannerPrices={}; state.plannerPriceSources={}; state.plannerInventory={}; state.stationFees={}; state.focusCosts={}; state.targetPriceSource='尚未取得市場資料'; state.autoProjectName='';
  byId('project-name').value=''; byId('create-project').textContent='儲存為專案';
  const defaults={'recipe-family':'leather','target-tier':'7','enchantment':'1','start-tier':'5','target-output':'100',
    'return-rate':'36.7','focus-return-rate':'53.9','available-focus':'0','extra-cost':'0','sale-fee-rate':'4','target-sale-price':'0'};
  Object.entries(defaults).forEach(([id,value]) => { byId(id).value=value; }); byId('use-focus').checked=false; byId('auto-allocate-materials').checked=true; schedulePlannerUpdate(); renderTransactions();
}

function autoAllocateProjectMaterials() {
  const plan=state.currentPlan; if (!plan) return;
  plan.materials.forEach(material => {
    const manualInventory=Math.max(0,Number(state.plannerInventory[material.item_id])||0);
    let needed=Math.max(0,material.gross_quantity-manualInventory-allocatedQuantityForItem(material.item_id));
    if (!needed) return;
    state.transactions.filter(item => item.direction==='buy' && item.item_id===material.item_id).forEach(item => {
      if (!needed) return;
      const existing=state.editingProject?.items.find(value => value.transaction_id===item.id)?.quantity || 0;
      const already=state.selected.get(item.id) || 0, capacity=Math.max(0,item.available_quantity+existing-already);
      const added=Math.min(needed,capacity); if (added) { state.selected.set(item.id,already+added); needed-=added; }
    });
  });
  renderTransactions(); updateMaterialReadiness();
}

async function saveProject() {
  const message=byId('project-message');
  if (!formValue('project-name').trim()) { message.className='form-message error'; message.textContent='請先輸入專案名稱。'; return; }
  if (!state.currentPlan) { message.className='form-message error'; message.textContent='配方尚未計算完成。'; return; }
  if (byId('auto-allocate-materials').checked) autoAllocateProjectMaterials();
  const selections=[...state.selected].map(([transaction_id,quantity]) => ({transaction_id,quantity}));
  const editing=state.editingProject, url=editing ? `/api/projects/${editing.id}` : '/api/projects';
  try {
    const response=await fetch(url,{method:editing ? 'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:formValue('project-name').trim(),selections,...projectPayload()})});
    const result=await response.json(); if (!response.ok) throw new Error(result.error || '專案儲存失敗');
    clearProjectEditor(); message.className='form-message success'; message.textContent=editing ? '專案已更新。':'規劃專案已建立；之後可繼續加入實際成交。'; await refresh();
  } catch (error) { message.className='form-message error'; message.textContent=error.message; }
}

function editProject(project) {
  state.editingProject=project; state.selected=new Map(project.items.map(item => [item.transaction_id,item.quantity]));
  const config=project.planner_state || {}; state.plannerPrices={...(config.prices || {})}; state.plannerPriceSources={}; state.plannerInventory={...(config.inventory || {})}; state.stationFees={...(config.station_fees || {})}; state.focusCosts={...(config.focus_costs || {})};
  const fields={'project-name':project.name,'recipe-family':config.family || 'leather','target-tier':config.target_tier || 7,
    'enchantment':config.enchantment ?? 0,'start-tier':config.start_tier || 5,'target-output':config.quantity || project.target_output || 100,
    'return-rate':config.return_rate ?? project.return_rate,'focus-return-rate':config.focus_return_rate ?? project.focus_return_rate,
    'available-focus':config.available_focus ?? project.available_focus,'extra-cost':project.extra_cost,'sale-fee-rate':project.sale_fee_rate,
    'target-sale-price':project.target_sale_price,'market-server':config.market_server || 'east','market-location':config.market_location || 'Martlock'};
  Object.entries(fields).forEach(([id,value]) => { byId(id).value=String(value ?? ''); }); byId('use-focus').checked=Boolean(config.use_focus ?? project.use_focus); byId('auto-allocate-materials').checked=config.auto_allocate!==false;
  byId('create-project').textContent='更新專案'; byId('project-message').className='form-message muted'; byId('project-message').textContent='正在修改專案；原有成交分配會保留。';
  schedulePlannerUpdate(); renderTransactions(); byId('project-name').scrollIntoView({behavior:'smooth',block:'center'});
}

async function deleteProject(id) {
  if (!window.confirm('確定刪除此專案？成交紀錄本身會保留。')) return;
  const response = await fetch(`/api/projects/${id}`, { method:'DELETE' }); if (response.ok) { clearProjectEditor(); await refresh(); }
}

function itemIconUrl(itemId) {
  return `https://render.albiononline.com/v1/item/${encodeURIComponent(itemId)}.png?size=96&quality=1`;
}

function selectManualItem(item) {
  if (!item?.item_id) return;
  byId('manual-item-id').value=item.item_id; byId('manual-item-name').value=item.name || item.item_name || item.item_id;
  text('manual-selected-item',item.name || item.item_name || item.item_id); byId('manual-selected-icon').src=item.icon_url || itemIconUrl(item.item_id);
  document.querySelectorAll('.manual-item-option').forEach(button => button.classList.toggle('selected',button.dataset.itemId===item.item_id));
}

function manualResourceChoices() {
  const family=formValue('manual-picker-family'), kind=formValue('manual-picker-kind'), tier=numericValue('manual-picker-tier');
  return (state.craftingCatalog?.items || []).filter(item => item.family===family && item.kind===kind && item.tier===tier);
}

function itemChoiceButton(item,compact=false) {
  const button=document.createElement('button'); button.type='button'; button.className=`manual-item-option${compact ? ' compact' : ''}`; button.dataset.itemId=item.item_id;
  const image=document.createElement('img'); image.src=item.icon_url || itemIconUrl(item.item_id); image.alt=''; const label=document.createElement('span'); label.textContent=item.name || item.item_name || item.item_id;
  button.append(image,label); button.addEventListener('click',() => selectManualItem(item)); return button;
}

function renderManualItemPicker() {
  const current=formValue('manual-item-id'), choices=manualResourceChoices(), grid=byId('manual-item-grid');
  grid.replaceChildren(...choices.map(item => itemChoiceButton(item)));
  const unique=new Map(); state.transactions.forEach(item => { if (!unique.has(item.item_id)) unique.set(item.item_id,item); });
  const recent=byId('manual-recent-items'), recentItems=[...unique.values()].slice(0,8);
  recent.replaceChildren(...(recentItems.length ? recentItems.map(item => itemChoiceButton(item,true)) : [Object.assign(document.createElement('span'),{className:'muted',textContent:'帳本還沒有最近品項；可直接點下方圖片。'})]));
  if (current) {
    const found=choices.find(item => item.item_id===current) || unique.get(current);
    if (found) selectManualItem(found);
  }
}

function openTransactionDialog(item = null, project = null) {
  state.pendingProjectSale = project;
  text('transaction-dialog-title', item ? '修改成交紀錄' : (project ? `新增出售到「${project.name}」` : '手動補記成交')); byId('edit-transaction-id').value = item?.id || '';
  byId('manual-direction').value = item?.direction || (project ? 'sell' : 'buy'); byId('manual-kind').value = item?.transaction_kind || (project ? 'order' : 'instant');
  byId('manual-source').value = ['private_trade','storage_inventory'].includes(item?.source) ? item.source : 'manual_entry';
  byId('manual-item-id').value = item?.item_id || project?.output_item_id || ''; byId('manual-item-name').value = item?.item_name || project?.output_item_name || '';
  byId('manual-quantity').value = item?.quantity || 1; byId('manual-unit-price').value = item?.unit_price || 0;
  byId('manual-total-price').value = item?.total_price ?? ((item?.quantity || 1)*(item?.unit_price || 0));
  byId('manual-location').value = item?.location_id || ''; byId('manual-notes').value = item?.notes || '';
  byId('manual-tax-rate').value = item?.sales_tax_rate ?? state.settings.market_tax_rate;
  byId('manual-setup-rate').value = item?.setup_fee_rate ?? state.settings.setup_fee_rate;
  byId('manual-traded-at').value = item ? new Date(item.traded_at).toISOString().slice(0,16) : new Date().toISOString().slice(0,16);
  renderManualItemPicker();
  if (item?.item_id || project?.output_item_id) selectManualItem({item_id:item?.item_id || project.output_item_id,name:item?.item_name || project.output_item_name});
  else if (!formValue('manual-item-id')) selectManualItem(manualResourceChoices().find(value => value.enchantment===2) || manualResourceChoices()[0]);
  byId('manual-custom-item-id').value=''; byId('manual-custom-item-name').value='';
  text('transaction-message', item && item.allocated_quantity ? `已有 ${item.allocated_quantity} 件分配到專案，數量不可低於此值。` : ''); byId('transaction-dialog').showModal();
}

async function saveTransaction(event) {
  event.preventDefault(); const id = formValue('edit-transaction-id');
  if (!formValue('manual-item-id')) { byId('transaction-message').className='form-message error'; text('transaction-message','請先點選一個品項。'); return; }
  const payload = { direction:formValue('manual-direction'), transaction_kind:formValue('manual-kind'), item_id:formValue('manual-item-id'),
    item_name:formValue('manual-item-name'), quantity:numericValue('manual-quantity'), unit_price:numericValue('manual-unit-price'),
    total_price:numericValue('manual-total-price'), source:formValue('manual-source'), location_id:formValue('manual-location'), notes:formValue('manual-notes'), traded_at:new Date(formValue('manual-traded-at')).toISOString(),
    sales_tax_rate:numericValue('manual-tax-rate'), setup_fee_rate:numericValue('manual-setup-rate') };
  try {
    const response = await fetch(id ? `/api/transactions/${id}` : '/api/transactions', { method:id ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
    const result = await response.json(); if (!response.ok) throw new Error(result.error || '儲存失敗');
    if (!id && state.pendingProjectSale) {
      const project=state.pendingProjectSale;
      const selections=project.items.map(item => ({transaction_id:item.transaction_id,quantity:item.quantity}));
      selections.push({transaction_id:result.id,quantity:payload.quantity});
      const attach=await fetch(`/api/projects/${project.id}`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({selections})});
      const attachResult=await attach.json(); if (!attach.ok) throw new Error(`成交已建立，但加入專案失敗：${attachResult.error || '未知錯誤'}`);
    }
    state.pendingProjectSale=null; byId('transaction-dialog').close(); await refresh();
  } catch (error) { byId('transaction-message').className = 'form-message error'; text('transaction-message', error.message); }
}

async function saveFees() {
  try {
    const response=await fetch('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      market_tax_rate:numericValue('market-tax-rate'),setup_fee_rate:numericValue('setup-fee-rate')})});
    const result=await response.json(); if (!response.ok) throw new Error(result.error || '費率儲存失敗');
    state.settings=result.settings; window.alert('掛單交易稅與設定費已儲存，新成交會自動套用。');
  } catch (error) { window.alert(error.message); }
}

async function setTransactionStatus(item) {
  const status = item.status === 'sold' ? 'active' : 'sold';
  const response = await fetch(`/api/transactions/${item.id}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({status}) });
  if (response.ok) await refresh();
}

async function deleteTransaction(item) {
  if (!window.confirm(`確定刪除「${item.item_name}」這筆成交？此操作會從統計排除，但 CSV 仍保留軟刪除軌跡。`)) return;
  const response = await fetch(`/api/transactions/${item.id}`, {method:'DELETE'}); const result = await response.json();
  if (!response.ok) window.alert(result.error || '刪除失敗'); else await refresh();
}

function updateManualUnitPrice() {
  const quantity=Math.max(1,numericValue('manual-quantity')), total=numericValue('manual-total-price');
  byId('manual-unit-price').value=String(Math.round(total/quantity));
}

function updateManualTotalPrice() {
  byId('manual-total-price').value=String(Math.max(1,numericValue('manual-quantity'))*numericValue('manual-unit-price'));
}

async function refresh() {
  if (state.refreshing) return; state.refreshing = true;
  try {
    const period = formValue('period-filter'), sold = byId('show-sold').checked ? '&include_sold=1' : '';
    const responses = await Promise.all([fetch(`/api/summary?period=${period}`), fetch(`/api/transactions?limit=300${sold}`),
      fetch('/api/snapshots?limit=40'), fetch('/api/projects'), fetch('/api/mails?limit=100'), fetch('/api/settings')]);
    if (!responses.every(x => x.ok)) throw new Error('API error');
    const [summary, transactions, snapshots, projects, mails, settings] = await Promise.all(responses.map(x => x.json()));
    state.transactions = transactions.items; await resolveNames(state.transactions);
    state.settings=settings;
    if (!state.settingsLoaded) { byId('market-tax-rate').value=settings.market_tax_rate; byId('setup-fee-rate').value=settings.setup_fee_rate; state.settingsLoaded=true; }
    text('transactions', formatNumber.format(summary.totals.transactions)); text('bought-quantity', formatNumber.format(summary.totals.bought_quantity));
    text('sold-quantity', formatNumber.format(summary.totals.sold_quantity)); text('spent', formatNumber.format(summary.totals.spent));
    text('revenue', formatNumber.format(summary.totals.revenue)); text('net-revenue', formatNumber.format(summary.totals.net_revenue)); text('balance', formatNumber.format(summary.totals.balance));
    text('last-refresh', `更新 ${new Date().toLocaleTimeString('zh-TW', {hour12:false})}`);
    renderStatus(summary.capture); renderChart(summary); renderTopItems(summary.top_items); renderCategoryOptions(); renderLedger(); renderTransactions(); updateMaterialReadiness();
    renderSnapshots(snapshots.items); renderProjects(projects.items); renderMails(mails.items); renderWarnings(summary.warnings);
  } catch (_) {
    ['status-title','sync-status-title','sidebar-status-title'].forEach(id => text(id,'統計服務連線中斷'));
    text('status-detail','重新啟動服務後頁面會自動恢復'); text('sync-status-detail','重新啟動服務後頁面會自動恢復');
    ['status-dot','sync-status-dot','sidebar-status-dot'].forEach(id => { byId(id).className='status-dot error'; });
  } finally { state.refreshing = false; }
}

['direction-filter','kind-filter','category-filter'].forEach(id => byId(id).addEventListener('change',renderLedger));
byId('period-filter').addEventListener('change',refresh); byId('show-sold').addEventListener('change',refresh);
byId('mail-filter').addEventListener('change',() => renderMails(state.mails));
document.querySelectorAll('.nav-button,.jump-button').forEach(button => button.addEventListener('click',() => showView(button.dataset.view)));
byId('create-project').addEventListener('click',saveProject); byId('add-transaction').addEventListener('click',() => openTransactionDialog());
byId('save-fees').addEventListener('click',saveFees);
['recipe-family','target-tier','enchantment','start-tier','target-output','return-rate','focus-return-rate','available-focus','use-focus']
  .forEach(id => { byId(id).addEventListener('input',schedulePlannerUpdate); byId(id).addEventListener('change',schedulePlannerUpdate); });
['extra-cost','sale-fee-rate'].forEach(id => byId(id).addEventListener('input',updatePlanTotals));
byId('target-sale-price').addEventListener('input',() => { state.targetPriceSource='手動輸入（不是遊戲內預估市值）'; updatePlanTotals(); });
byId('fetch-market-prices').addEventListener('click',fetchPlannerMarketPrices);
['compare-family','compare-tier','compare-enchantment'].forEach(id => byId(id).addEventListener('change',scheduleComparatorUpdate));
byId('compare-quantity').addEventListener('input',updateComparatorTotals);
byId('fetch-compare-prices').addEventListener('click',fetchComparatorPrices);
byId('manual-total-price').addEventListener('input',updateManualUnitPrice);
byId('manual-unit-price').addEventListener('input',updateManualTotalPrice);
byId('manual-quantity').addEventListener('input',updateManualUnitPrice);
byId('manual-source').addEventListener('change',() => { if (formValue('manual-source')==='storage_inventory' && !numericValue('manual-total-price')) { byId('manual-unit-price').value='0'; } });
['manual-picker-family','manual-picker-kind','manual-picker-tier'].forEach(id => byId(id).addEventListener('change',renderManualItemPicker));
byId('apply-custom-item').addEventListener('click',() => {
  const itemId=formValue('manual-custom-item-id').trim().toUpperCase(); if (!itemId) return;
  selectManualItem({item_id:itemId,name:formValue('manual-custom-item-name').trim() || itemId});
});
byId('transaction-form').addEventListener('submit',saveTransaction);
document.querySelectorAll('.dialog-close').forEach(button => button.addEventListener('click', () => { state.pendingProjectSale=null; byId('transaction-dialog').close(); }));
loadCraftingCatalog(); refresh(); setInterval(refresh,3000);
