const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const number = value => Number(value || 0).toLocaleString('zh-CN');
const formatTokens = n => {
  if (!n) return '0';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e4) return (n / 1e3).toFixed(1) + 'k';
  return number(n);
};
const formatCompact = n => {
  if (!n) return '0';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
};
const percent = value => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
const time = value => new Date(value * 1000).toLocaleTimeString('zh-CN', {hour12:false});
const actionNames = {CHECK:'过牌',CALL:'跟注',BET:'下注',RAISE:'加注',FOLD:'弃牌',ALL_IN:'全下'};
const stages = {IDLE:'等待开局',PREFLOP:'翻牌前',FLOP:'翻牌圈',TURN:'转牌圈',RIVER:'河牌圈',RIT_DECISION:'RIT 选择',SHOWDOWN:'摊牌',HAND_END:'本手结束'};
let settings, styles, rooms = [], state = {}, toastTimer, busy = false;

async function api(path, method='GET', data) {
  const response = await fetch(path, {method, headers:{'Content-Type':'application/json'}, body:data == null ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail));
  return result;
}
function toast(message, error=false) {
  $('toast').textContent = message;
  $('toast').classList.toggle('error', error);
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').hidden = true, 6000);
}
async function perform(work) {
  if (busy) return;
  busy = true;
  updateButtons();
  try { await work(); } catch (error) { toast(error.message, true); }
  finally { busy = false; updateButtons(); }
}
function updateButtons() {
  const active = state.running || state.stopping;
  $('start').disabled = busy || active || !$('room-select').value;
  $('stop').disabled = busy || (!state.running && !state.stopping);
  $('create-test').disabled = busy || active;
  $('login').disabled = busy || active;
  $('settings-form').querySelectorAll('input,textarea,button').forEach(input => input.disabled = busy || active);
  $('room-select').disabled = !!active;
}
function drawStyles() {
  $('styles').innerHTML = Object.entries({...styles,mixed:{name:'混合风格',description:'按自定义比例，每手切换打法'}}).map(([key,style]) =>
    `<label class="style-option"><input type="radio" name="style" value="${escape(key)}" ${settings.style===key?'checked':''}><span><strong>${escape(style.name)}</strong><small>${escape(style.description)}</small></span></label>`).join('');
  $('mix-weights').innerHTML = Object.entries(styles).map(([key,style]) => `<label class="weight">${escape(style.name)}<input type="number" min="0" max="100" step="1" data-weight="${key}" value="${settings.mix_weights[key] || 0}" aria-label="${escape(style.name)}权重"></label>`).join('');
  $('mix-panel').hidden = settings.style !== 'mixed';
  $('styles').addEventListener('change', () => $('mix-panel').hidden = document.querySelector('input[name=style]:checked').value !== 'mixed');
}
function readSettings() {
  const weights = {};
  document.querySelectorAll('[data-weight]').forEach(input => weights[input.dataset.weight] = Number(input.value));
  return {server_url:$('server-url').value.trim(), model_url:$('model-url').value.trim(), username:$('username').value.trim(),
    proxy_url:$('proxy-url').value.trim(),
    password:$('password').value, api_key:$('api-key').value, model:$('model').value.trim(),
    style:document.querySelector('input[name=style]:checked').value, mix_weights:weights,
    custom_prompt:$('custom-prompt').value, social:$('social').checked};
}
async function saveSettings() {
  const result = await api('/api/settings','PUT',readSettings());
  settings = result.settings;
  $('hpoker-link').href = settings.server_url;
  $('password').value = $('api-key').value = '';
}
async function refreshRooms(preferred) {
  rooms = await api('/api/rooms');
  const selected = preferred || state.room_id || $('room-select').value;
  $('room-select').innerHTML = '<option value="">选择房间…</option>' + rooms.map(room =>
    `<option value="${escape(room.room_id)}">${escape(room.room_name)} · ${room.seated_count}/${room.max_seats} 人 · ${room.cash_value===0?'娱乐局':`${room.cash_value} H 币`}</option>`).join('');
  if (rooms.some(room => room.room_id === selected)) $('room-select').value = selected;
  roomInfo(); updateButtons();
}
function roomInfo() {
  const room = rooms.find(item => item.room_id === $('room-select').value);
  $('room-info').textContent = room ? `房间 ${room.room_id} · 盲注 ${room.small_blind}/${room.big_blind} · 买入 ${number(room.buyin_chips)} 筹码 · ${room.cash_value} H 币` : rooms.length ? '选择房间后开始。' : '暂无房间，可创建娱乐测试房。';
}
function cards(items, placeholders=0) {
  const symbols = {s:'♠',h:'♥',c:'♣',d:'♦'};
  return items.map(card => {
    const value = typeof card === 'string' ? card : card.notation;
    const suit = value.slice(-1);
    return `<div class="card ${escape(suit)}" aria-label="${escape(value)}">${escape(value.slice(0,-1))}<span>${symbols[suit]||''}</span></div>`;
  }).join('') + Array.from({length:Math.max(0,placeholders-items.length)}, () => '<div class="card placeholder" aria-label="尚未发出">·</div>').join('');
}
function seat(player, table) {
  if (!player) return '<div class="seat empty">空位</div>';
  const hero = player.player_id === state.user?.user_id;
  const position = player.seat_index === table.dealer_seat ? 'D' : player.seat_index === table.sb_seat ? 'SB' : player.seat_index === table.bb_seat ? 'BB' : '';
  return `<div class="seat ${hero?'hero':''} ${player.is_folded?'folded':''} ${player.seat_index===table.current_turn_seat?'active':''}"><div class="seat-name">${escape(player.avatar)} ${escape(player.name)} ${position?`· ${position}`:''}</div><div class="seat-stack">${number(player.chips)}</div><div class="seat-move">${escape(player.last_action || (hero?'你的 AI':'等待行动'))}</div></div>`;
}
function renderAnalysis(analysis) {
  if (!analysis) {
    $('analysis').innerHTML = '<p class="empty-note">手动计算仅供你查看，结果不会提供给 AI。</p>'; return;
  }
  $('analysis').innerHTML = `<div class="calculation-numbers"><div><strong>${percent(analysis.equity)}</strong><span>随机范围权益</span></div><div><strong>${analysis.outs?.count ?? '—'}</strong><span>候选 outs</span></div><div><strong>${percent(analysis.pot_odds)}</strong><span>跟注赔率门槛</span></div></div><div class="calculation-detail">${escape(analysis.current_hand)} · ${analysis.samples} 次${analysis.method==='exact'?'精确枚举':'模拟'} · ${analysis.opponents} 位对手${analysis.betting?`<br>跟注 ${number(analysis.betting.call_bb)} BB · 剩余筹码的 ${percent(analysis.betting.call_stack_fraction)} · 可争夺底池 ${number(analysis.betting.eligible_pot_after_call)}`:''}${analysis.outs?`<br>下一张提升牌型：${percent(analysis.outs.next_card_probability)}`:''}</div>`;
}
function logDescription(log) {
  const d = log.data;
  if (log.kind === 'action') return `${state.players?.find(p=>p.player_id===d.player_id)?.name || d.player_id} · ${actionNames[d.action]||d.action}${d.amount ? ' '+d.amount : ''} · ${stages[d.street]||d.street}`;
  if (log.kind === 'decision') return `${actionNames[d.action]||d.action}${d.amount?' '+d.amount:''} · ${d.reason}`;
  if (log.kind === 'model_call') return `${{reflection:'反思',social:'社交',decision:'对局'}[d.agent] || '对局'} · ${d.elapsed}s · 第 ${d.round} 次请求 · 输入 ${d.prompt_tokens ?? '—'} / 思考 ${d.reasoning_tokens ?? '—'} tokens · ${d.tools.join(', ') || d.finish}`;
  if (log.kind === 'social_decision') return `${d.trigger === 'chat' ? '聊天回应 · ' : d.commented_hand != null ? '点评第 '+d.commented_hand+' 手 · ' : ''}${d.message || d.emoji || ''}${d.show && d.show !== 'none' ? ' · '+({first:'亮第一张',second:'亮第二张',both:'亮两张',hide:'盖牌'}[d.show] || d.show) : ''} · ${d.reason}${Object.values(d.results).some(r=>r.error)?' · '+Object.values(d.results).filter(r=>r.error).map(r=>r.error).join('；'):''} · ${d.elapsed}s`;
  if (log.kind === 'reflection_start') return `第 ${d.hand} 手 · ${{loop:'循环复盘',fold:'弃牌后',hand_end:'结算后',chat:'桌上交流'}[d.trigger] || d.trigger}`;
  if (log.kind === 'reflection') return `第 ${d.hand} 手 · ${d.summary}${d.plan != null?' · '+(d.plan_applied?'共同计划：'+(d.plan||'已清除'):'局面已变，未采用旧计划'):''} · 更新 ${d.updates.length} / 清理 ${d.delete_subjects.length} 条记忆 · ${d.elapsed}s`;
  if (log.kind === 'chat') return `${d.name}：${d.message||d.emoji}`;
  if (log.kind === 'tool') return `${d.name} · ${d.result?.error || (d.name==='remember_note'?d.arguments.note: d.result?.equity != null ? '权益 '+percent(d.result.equity) : '已调用')}`;
  if (log.kind === 'hand_start') return `第 ${d.hand} 手 · ${styles[d.style]?.name || d.style}`;
  if (log.kind === 'hand_end') return `第 ${d.hand} 手结束 · 总底池 ${d.total_pot}`;
  if (log.kind === 'street') return `${stages[d.street]||d.street} · ${d.board?.map(c=>c.display||c.notation).join(' ') || '尚无公共牌'}`;
  if (log.kind === 'table_decision') return `${d.action} · ${d.reason}`;
  if (log.kind === 'fallback') return `${d.action} · ${d.reason}`;
  return d.message || `${d.room_id || ''} ${d.event || ''}`;
}
function render(next) {
  state = next;
  const table = state.room?.table || {}, me = (table.seats || []).find(p=>p?.player_id===state.user?.user_id);
  $('connection').textContent = state.running ? '● AGENT ONLINE' : state.stopping ? '● 正在离座' : '● 本地控制台';
  $('account-label').textContent = state.user ? `${state.user.nickname || state.user.username} · 已登录` : '账号未登录';
  $('metric-hands').textContent = state.metrics.hands;
  $('metric-profit').textContent = (state.metrics.net_chips > 0 ? '+' : '') + number(state.metrics.net_chips);
  $('metric-profit').className = state.metrics.net_chips < 0 ? 'negative' : 'positive';
  $('metric-actions').innerHTML = `${state.metrics.decisions} <small>/ ${state.metrics.fallbacks}</small>`;
  const sessionTok = state.tokens?.session || {calls:0, prompt_tokens:0, completion_tokens:0, reasoning_tokens:0, total_tokens:0};
  const totalTok = state.tokens?.total || {calls:0, prompt_tokens:0, completion_tokens:0, reasoning_tokens:0, total_tokens:0};
  const handTok = state.tokens?.hand || {calls:0, prompt_tokens:0, completion_tokens:0, reasoning_tokens:0, total_tokens:0};
  $('metric-session-tokens').textContent = formatTokens(sessionTok.total_tokens);
  $('metric-session-tokens').title = `本次牌局: 总计 ${number(sessionTok.total_tokens)} tokens (输入 ${number(sessionTok.prompt_tokens)} / 输出 ${number(sessionTok.completion_tokens)}${sessionTok.reasoning_tokens ? ` / 思考 ${number(sessionTok.reasoning_tokens)}` : ''}) · 调用 ${sessionTok.calls} 次${handTok.total_tokens ? `\n当前手牌: ${number(handTok.total_tokens)} tokens (输入 ${number(handTok.prompt_tokens)} / 输出 ${number(handTok.completion_tokens)})` : ''}`;
  $('metric-session-sub').textContent = `入 ${formatCompact(sessionTok.prompt_tokens)} · 出 ${formatCompact(sessionTok.completion_tokens)}${handTok.total_tokens ? ` (本手 ${formatCompact(handTok.total_tokens)})` : ''}`;
  $('metric-total-tokens').textContent = formatTokens(totalTok.total_tokens);
  $('metric-total-tokens').title = `历史累计: 总计 ${number(totalTok.total_tokens)} tokens (输入 ${number(totalTok.prompt_tokens)} / 输出 ${number(totalTok.completion_tokens)}${totalTok.reasoning_tokens ? ` / 思考 ${number(totalTok.reasoning_tokens)}` : ''}) · 调用 ${number(totalTok.calls)} 次`;
  $('metric-total-sub').textContent = `入 ${formatCompact(totalTok.prompt_tokens)} · 出 ${formatCompact(totalTok.completion_tokens)}`;
  $('metric-context').textContent = state.metrics.input_tokens ? `${number(state.metrics.input_tokens)} tokens` : state.metrics.input_chars ? `${number(state.metrics.input_chars)} 字符` : '—';
  $('table-title').textContent = state.room_id ? `${state.room.config.room_name} · 第 ${table.hand_number} 手` : '等待入座';
  $('table-stage').textContent = stages[table.street] || 'NO SESSION';
  $('pot').textContent = number(table.total_pot);
  const seats = table.seats || Array(6).fill(null);
  const split = Math.ceil(seats.length / 2);
  $('top-seats').innerHTML = seats.slice(0,split).map(p=>seat(p,table)).join('');
  $('bottom-seats').innerHTML = seats.slice(split).reverse().map(p=>seat(p,table)).join('');
  $('board').innerHTML = cards(table.board_cards || [],5);
  $('hero-cards').innerHTML = cards(me?.hole_cards || [],2);
  $('hero-label').textContent = me ? `${styles[state.style]?.name || 'AI'} · 你的手牌` : '你的 AI 手牌';
  $('table-message').textContent = state.error || (state.running ? state.status : state.stopping ? state.status : '选择房间，开始下一手。');
  $('agent-status').textContent = `${state.status} · 社交 ${state.social_status || '待命'} · 反思 ${state.reflection_status || '待命'}`;
  $('status-light').classList.toggle('active',state.running);
  const left = table.turn_started_at ? Math.max(0,Math.ceil(table.turn_started_at + table.current_turn_duration - table.server_time)) : null;
  $('turn-clock').textContent = left == null ? '—' : `当前行动剩余 ${left}s`;
  const decision = state.last_decision;
  $('decision-title').textContent = decision ? `${actionNames[decision.action]||decision.action}${decision.amount?' '+number(decision.amount):''} · 第 ${decision.hand} 手` : '等待模型行动';
  $('decision-reason').textContent = decision?.reason || '模型会在轮到自己时调用工具，并给出简短决策依据。';
  $('decision-time').textContent = decision ? `${decision.elapsed}s` : '—';
  renderAnalysis(state.analysis);
  $('calculate').disabled = !me || me.hole_cards.length !== 2;
  $('players').innerHTML = state.players.length ? state.players.map(p=> `<tr><td>${escape(p.name)}<small>${escape(p.position)}</small></td><td>${number(p.chips)}</td><td>${p.observed.observed_hands || 0}</td><td>${p.observed.vpip_pct==null?'—':p.observed.vpip_pct+'%'}</td><td>${p.observed.pfr_pct==null?'—':p.observed.pfr_pct+'%'}</td><td>${p.observed.aggression_factor ?? '—'}</td><td>${escape(p.observed.style || '样本不足')}</td></tr>`).join('') : '<tr><td colspan="7" class="empty-note">入座后显示当前玩家。</td></tr>';
  $('memory-count').textContent = state.memories.length;
  $('shared-plan').hidden = !state.shared_plan;
  $('shared-plan').textContent = state.shared_plan ? `第 ${state.shared_plan.hand} 手共同计划：${state.shared_plan.plan}` : '';
  $('memories').innerHTML = state.memories.length ? state.memories.map(item=> `<div class="memory-item"><strong>${item.subject==='self'?'自己的桌上形象':escape(state.players.find(p=>p.player_id===item.subject)?.name || item.subject)}</strong><p>${escape(item.note)}</p><small>更新于 ${time(item.updated)}</small></div>`).join('') : '<p class="empty-note">有依据的玩家观察和自身形象，会由 AI 保存下来。</p>';
  const logNames = {action:'玩家行动',decision:'模型决策',model_call:'模型耗时',social_decision:'社交选择',social_error:'社交错误',reflection_start:'开始反思',reflection:'反思总结',reflection_error:'反思错误',tool:'工具调用',chat:'桌上交流',hand_start:'新一手',hand_end:'结算',street:'牌面更新',table_decision:'桌面选择',fallback:'保底操作',error:'错误',model_error:'模型错误',connection:'连接',session_start:'开始',session_stop:'停止',test_room:'测试房',room_end:'房间结束'};
  $('logs').innerHTML = state.logs.slice().reverse().map(log=> `<div class="log-item ${['error','model_error','reflection_error','social_error'].includes(log.kind)?'error':''}"><time>${time(log.timestamp)}</time><strong>${logNames[log.kind]||escape(log.kind)}</strong><span>${escape(logDescription(log))}</span></div>`).join('');
  updateButtons();
}
async function showHistory() {
  const records = await api('/api/history');
  $('history').innerHTML = records.length ? records.map(hand=> `<details class="history-item"><summary>第 ${hand.number} 手 · 房间 ${escape(hand.room)} · ${new Date(hand.timestamp*1000).toLocaleString('zh-CN')} · ${hand.data.complete_observation?'完整观察':'部分观察'}</summary><pre>${escape(JSON.stringify(hand.data,null,2))}</pre></details>`).join('') : '<p class="empty-note">完成第一手后，牌面、玩家、完整行动与结算会保存在这里。</p>';
}
async function poll() {
  try { render(await api('/api/status')); }
  catch (error) { $('connection').textContent = '控制台连接中断'; }
  setTimeout(poll,1000);
}
$('settings-form').addEventListener('submit', event => {event.preventDefault(); perform(async()=>{await saveSettings(); toast('设置已保存');});});
$('login').addEventListener('click',()=>perform(async()=>{await saveSettings(); await api('/api/login','POST'); toast('登录成功'); await refreshRooms();}));
$('load-models').addEventListener('click',()=>perform(async()=>{await saveSettings(); const result=await api('/api/models'); $('model-list').innerHTML=result.models.map(m=>`<option value="${escape(m)}"></option>`).join(''); if(result.models.length===1)$('model').value=result.models[0]; toast(`发现 ${result.models.length} 个模型`);}));
$('refresh-rooms').addEventListener('click',()=>perform(()=>refreshRooms()));
$('room-select').addEventListener('change',()=>{roomInfo();updateButtons();});
$('create-test').addEventListener('click',()=>perform(async()=>{await saveSettings(); const room=await api('/api/test-room','POST',{bots:3}); await refreshRooms(room.room_id); toast('已创建 0 H 币娱乐房，加入 3 个机器人');}));
$('start').addEventListener('click',()=>perform(async()=>{await saveSettings(); render(await api('/api/start','POST',{room_id:$('room-select').value})); toast('AI 已入座并开始观察');}));
$('stop').addEventListener('click',()=>perform(async()=>{render(await api('/api/stop','POST')); toast(state.stopping?'已停止决策，等待当前全下牌局结束后退出':'已停止并退出房间');}));
$('calculate').addEventListener('click',()=>perform(async()=>{const table=state.room.table, me=table.seats.find(p=>p?.player_id===state.user.user_id); const result=await api('/api/calculate','POST',{hole:me.hole_cards.map(c=>c.notation),board:table.board_cards.map(c=>c.notation),opponents:Math.max(1,table.seats.filter(p=>p&&p.has_cards&&!p.is_folded&&p.player_id!==me.player_id).length)}); result.betting=state.betting; result.pot_odds=state.betting?.pot_odds; renderAnalysis(result);}));
function setTab(history) {
  $('live-view').hidden=history; $('history-view').hidden=!history;
  $('live-tab').classList.toggle('active',!history); $('history-tab').classList.toggle('active',history);
  $('live-tab').setAttribute('aria-selected',String(!history)); $('history-tab').setAttribute('aria-selected',String(history));
  if(history)perform(showHistory);
}
$('live-tab').addEventListener('click',()=>setTab(false));
$('history-tab').addEventListener('click',()=>setTab(true));
$('refresh-history').addEventListener('click',()=>perform(showHistory));
(async()=>{
  try {
    const result=await api('/api/settings'); settings=result.settings; styles=result.styles;
    $('hpoker-link').href=settings.server_url; $('hpoker-link').hidden=false;
    $('server-url').value=settings.server_url; $('model-url').value=settings.model_url;
    $('proxy-url').value=settings.proxy_url;
    $('username').value=settings.username; $('model').value=settings.model;
    $('custom-prompt').value=settings.custom_prompt; $('social').checked=settings.social;
    $('password').placeholder=settings.has_password?'已保存，留空保留':'输入密码';
    $('api-key').placeholder=settings.has_api_key?'已保存，留空保留':'输入 API Key';
    drawStyles(); await refreshRooms(); poll();
  } catch(error) {toast(error.message,true);}
})();
