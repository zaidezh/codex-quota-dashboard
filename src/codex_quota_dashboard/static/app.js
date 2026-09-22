(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const charts = new Map();
  const colors = ["#8ab4ef", "#76c6b8", "#e4b979", "#b4a4da", "#d29ba5", "#9db98e"];
  const names = {overview: "Codex 额度走势", usage: "Codex 详情"};
  const state = {data: null, telemetry: {}, pointer: null, busy: false, view: "overview", range: "window", offline: false, scope: "current_mix", scenario: "current", burstHours: "2", burstAfter: "low"};
  state.budgetModel = ""; state.budgetBusy = false; state.budgetRevision = 0; state.budgetResultId = null;
  Object.assign(state,{quotaMode:'live',quotaDays:7,quotaDate:'',quotaData:null,quotaDataKey:null,quotaZoom:null,quotaRequest:0,runtimeRequest:0});
  const valid = v => v != null && Number.isFinite(Number(v));
  const numberFormats=new Map(),dateFormats=new Map(),timeIndexes=new WeakMap();
  const n = (v, digits = 0) => {if(!valid(v))return '—';if(!numberFormats.has(digits))numberFormats.set(digits,new Intl.NumberFormat('zh-CN',{maximumFractionDigits:digits}));return numberFormats.get(digits).format(Number(v));};
  const compact = v => valid(v) ? new Intl.NumberFormat("zh-CN", {notation: "compact", maximumFractionDigits: 2}).format(v) : "—";
  const pct = v => valid(v) ? `${n(v * 100, 1)}%` : "—";
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const model = value => String(!value || value==="unknown" ? "未知模型" : value).replace(/^gpt-/, "GPT-");
  const tier = value => ({standard: "标准", default: "标准", fast: "快速", priority: "快速", unknown: "档位未记录"}[value] || value || "档位未记录");
  const text = (id, value) => { $(id).textContent = value; };
  const date = (v, short = false, seconds = false) => {if(!v)return '—';const zone=state.data?.timezone || 'Asia/Shanghai',key=`${zone}:${short}:${seconds}`;if(!dateFormats.has(key))dateFormats.set(key,new Intl.DateTimeFormat("zh-CN", {
    timeZone: zone, month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false, ...(short ? {} : {year: "numeric"}),...(seconds?{second:'2-digit'}:{}),
  }));return dateFormats.get(key).format(new Date(v));};
  function bytes(v) {
    if (!valid(v)) return "—";
    let amount = Number(v), i = 0; const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    while (Math.abs(amount) >= 1024 && i < units.length - 1) { amount /= 1024; i++; }
    return `${n(amount, amount >= 100 ? 0 : 1)} ${units[i]}`;
  }
  const rate = v => valid(v) ? `${bytes(v)}/s` : "—";
  const valueOf = p => Array.isArray(p.value) ? p.value[p.value.length - 1] : p.value;
  function tooltip(params) {
    const values = Array.isArray(params) ? params : [params];
    if (!values.length) return "";
    const title = values[0].axisType === "xAxis.time" ? date(values[0].axisValue) : values[0].axisValueLabel || "";
    return `<strong>${esc(title)}</strong>` + values.filter(p => valid(valueOf(p))).map(p => `<div>${esc(p.seriesName)}：${n(valueOf(p), 2)}</div>`).join("");
  }
  function options() {
    return {animation: false, color: colors, backgroundColor: "transparent",
      textStyle: {fontFamily: 'Segoe UI, Microsoft YaHei UI, sans-serif', color: "#98a6b6"},
      tooltip: {trigger: "axis", confine: true, backgroundColor: "#121b25", borderColor: "#3a495c", textStyle: {color: "#e8edf3", fontSize: 12}, formatter: tooltip},
      legend: {icon: "rect", itemWidth: 18, itemHeight: 3, top: 0, textStyle: {color: "#98a6b6", fontSize: 11}},
      grid: {left: 12, right: 16, top: 42, bottom: 25, containLabel: true},
      xAxis: {type: "time", axisLine: {lineStyle: {color: "#354252"}}, axisTick: {show: false}, axisLabel: {color: "#98a6b6", fontSize: 11, hideOverlap: true}},
      yAxis: {type: "value", axisLine: {show: false}, axisLabel: {color: "#98a6b6", fontSize: 11}, splitLine: {lineStyle: {color: "#293441"}}}};
  }
  const line = (id, name, data, extra = {}) => ({id, name, type: "line", showSymbol: false, symbol: "none", connectNulls: false, data, ...extra});
  function chart(id, option) {
    let instance = charts.get(id);
    if (!instance) { instance = echarts.init($(id), null, {renderer: "canvas",useDirtyRect:id==='quotaChart'}); charts.set(id, instance); }
    if(id==='quotaChart'){instance.__boundaryLabelSignature=null;instance.__quotaOption={xAxis:[option.xAxis],dataZoom:option.dataZoom};}
    instance.setOption(option, {replaceMerge: ["series", "dataZoom"], lazyUpdate: false});
    if(id==='quotaChart')layoutBoundaryLabels(instance);
    if (id === "quotaChart" && !instance.__monitorZoomBound) {
      instance.on("datazoom", () => {
        const current=instance.getOption(),z=current.dataZoom?.[0],axis=current.xAxis?.[0];if(!z || !axis)return;
        const begin=Number(axis.min),span=Number(axis.max)-begin;
        state.quotaZoom=[valid(z.startValue)?Number(z.startValue):begin+span*z.start/100,
          valid(z.endValue)?Number(z.endValue):begin+span*z.end/100];
        instance.__quotaOption={xAxis:current.xAxis,dataZoom:current.dataZoom};state.quotaPlotRange=[...state.quotaZoom];
        if(state.timeRange){
          state.pendingPreset=null;state.rangeRevision=(state.rangeRevision||0)+1;state.rangeSwitching=false;
          state.quotaSelectionBase??={...state.timeRange};state.rangeEditing=false;
          state.timeRange={start:state.quotaZoom[0],end:state.quotaZoom[1],anchor:'fixed',span:null,preset:'custom'};syncRangeControls();
          clearTimeout(state.zoomTimer);state.zoomTimer=setTimeout(async()=>{state.quotaZoom=null;await loadQuotaRange();if(state.data)renderQuota(state.data.adaptive);},250);
        }
        requestAnimationFrame(()=>{layoutBoundaryLabels(instance);updateNowMarker();restorePlotPointer();});
      });
      instance.__monitorZoomBound = true;
    }
  }
  function layoutBoundaryLabels(instance) {
    if(!instance || instance.getWidth()<80)return;
    const boundaries=state.quotaBoundaries || [],option=instance.__quotaOption||instance.getOption(),axis=option.xAxis?.[0],zoom=option.dataZoom?.[0];
    if(!axis || !boundaries.length)return;
    const begin=valid(zoom?.startValue)?Number(zoom.startValue):Number(axis.min),end=valid(zoom?.endValue)?Number(zoom.endValue):Number(axis.max);
    const pixel=time=>instance.convertToPixel({xAxisIndex:0},time);
    const left=pixel(begin)+4,right=pixel(end)-4;
    if(!Number.isFinite(left+right) || right-left<60)return;
    const font='12px Segoe UI, Microsoft YaHei UI, sans-serif',groups=[];
    const nodes=boundaries.map((boundary,index)=>({boundary,index,time:Date.parse(boundary.time)}))
      .filter(node=>node.time>=begin && node.time<=end).map(node=>({...node,x:pixel(node.time)})).sort((a,b)=>a.x-b.x);
    const box=members=>{
      const representative=members[Math.floor((members.length-1)/2)];
      const reset=members.every(node=>node.boundary.kind==='quota_reset');
      const name=members.length>1?`${members.length}次${reset?'重置':'变化'}`:(eventNames[representative.boundary.kind]||'变化');
      const measured=echarts.format?.getTextRect(name,font).width ?? name.length*12;
      const width=Math.min(measured+4,right-left),x=Math.max(left,Math.min(right-width,representative.x-width/2));
      return {members,representative,name,width,left:x,right:x+width};
    };
    for(const node of nodes){
      let group=box([node]);
      while(groups.length && group.left<groups.at(-1).right+10)group=box([...groups.pop().members,...group.members]);
      groups.push(group);
    }
    const marks=boundaries.map(boundary=>({xAxis:boundary.time,name:eventNames[boundary.kind]||boundary.kind,lineStyle:{color:eventColor(boundary.kind),type:'dashed'},label:{show:false,color:eventColor(boundary.kind)}}));
    for(const group of groups)marks[group.representative.index].label={show:true,color:eventColor(group.representative.boundary.kind),formatter:group.name,position:'end',align:'center',
      fontSize:12,fontFamily:'Segoe UI, Microsoft YaHei UI, sans-serif',width:group.width,overflow:'truncate',
      offset:[group.left+group.width/2-group.representative.x,0]};
    const signature=JSON.stringify(marks);
    if(instance.__boundaryLabelSignature!==signature){
      instance.__boundaryLabelSignature=signature;
      // Updating markers can otherwise restore dataZoom's old percentage range.
      const dataZoom=(option.dataZoom || []).map(z=>({id:z.id,start:null,end:null,startValue:begin,endValue:end,rangeMode:['value','value']}));
      instance.setOption({series:[{id:'actual',markLine:{data:marks}}],dataZoom},{silent:true,lazyUpdate:false});
    }
  }
  function resizeCharts(){charts.forEach(c=>c.resize());if(state.data&&state.view==='overview')renderQuota(state.data.adaptive);else layoutBoundaryLabels(charts.get('quotaChart'));}
  const rangeZone = () => state.data?.timezone || 'Asia/Shanghai';
  function localParts(value) {
    const p=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:rangeZone(),year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value)).map(x=>[x.type,x.value]));
    return {day:`${p.year}-${p.month}-${p.day}`,time:`${p.hour}:${p.minute}`,year:Number(p.year)};
  }
  function fromLocal(day,clock) {
    if(!/^\d{4}-\d{2}-\d{2}$/.test(day)||!/^\d{2}:\d{2}$/.test(clock))throw Error('日期和时间请使用 YYYY-MM-DD 与 HH:mm');
    let [h,m]=clock.split(':').map(Number);if(h>24||m>59||(h===24&&m))throw Error('时间须在00:00至24:00之间');
    const midnight=Date.parse(day+'T00:00:00Z');if(!Number.isFinite(midnight)||new Date(midnight).toISOString().slice(0,10)!==day)throw Error('日期无效');
    const wall=midnight+(h*60+m)*60000;let guess=wall;
    for(let i=0;i<3;i++){const p=localParts(guess),rendered=Date.parse(p.day+'T'+p.time+':00Z');guess+=wall-rendered;}
    const expected=new Date(wall).toISOString().slice(0,16),actual=localParts(guess);
    if(actual.day+'T'+actual.time!==expected)throw Error('此本地时刻不存在，请调整时间');
    return guess;
  }
  const TRUSTED_RESET_STORAGE_KEY='codex-quota-monitor:last-trusted-reset:v1';
  function sameResetScope(cached,latest){
    return ['limit_id','plan_type','window_minutes'].every(key=>cached[key]==null||latest[key]==null||String(cached[key])===String(latest[key]));
  }
  function rememberTrustedReset(reset,latest){
    if(!Number.isFinite(reset)||reset<=Date.now()||typeof localStorage==='undefined')return;
    try{localStorage.setItem(TRUSTED_RESET_STORAGE_KEY,JSON.stringify({resets_at:new Date(reset).toISOString(),limit_id:latest.limit_id??null,plan_type:latest.plan_type??null,window_minutes:latest.window_minutes??null}));}catch{}
  }
  function cachedTrustedReset(latest,now){
    if(typeof localStorage==='undefined')return null;
    try{
      const cached=JSON.parse(localStorage.getItem(TRUSTED_RESET_STORAGE_KEY)||'null'),reset=Date.parse(cached?.resets_at);
      return Number.isFinite(reset)&&reset>now&&sameResetScope(cached,latest)?{time:reset,source:'cached'}:null;
    }catch{return null;}
  }
  function trustedResetAnchor(){
    const latest=state.data?.adaptive?.latest||{},now=Date.now(),observed=Date.parse(latest.observed_at),reset=Date.parse(latest.resets_at);
    const fresh=!state.offline&&Number.isFinite(observed)&&observed<=now&&now-observed<=180000&&Number.isFinite(reset)&&reset>now;
    if(fresh){rememberTrustedReset(reset,latest);state.resetAnchorSource='server';return {time:reset,source:'server'};}
    const cached=cachedTrustedReset(latest,now);
    state.resetAnchorSource=cached?'cached':'unavailable';
    if(cached)state.anchorMessage='服务器重置时间暂未刷新；沿用最近一次可信时间。';
    return cached;
  }
  function resolveQuotaRange() {
    const now=Date.now(),resetAnchor=trustedResetAnchor();
    if(!state.timeRange){state.timeRange={start:now-86400000,end:now,anchor:'fixed',span:null,preset:'custom',anchorValid:false};state.pendingPreset='7d';}
    if(state.pendingPreset==='reset'||state.pendingPreset==='7d'){
      const pending=state.pendingPreset;
      if(resetAnchor){state.timeRange={start:resetAnchor.time-7*86400000,end:resetAnchor.time,anchor:'reset',span:7*86400000,preset:pending,anchorValid:true};state.pendingPreset=null;}
      else{state.anchorMessage='无法确认下次重置时间；保留当前范围，数据恢复后再切换重置前7天。';return state.timeRange;}
    }
    if(state.rangeSwitching)return state.timeRange;
    const r=state.timeRange;let target=r.end;state.anchorMessage=resetAnchor?.source==='cached'?'服务器重置时间暂未刷新；沿用最近一次可信时间。':'';
    if(r.anchor==='now'){target=now;state.anchorMessage='终点使用当前时钟，刷新时更新范围。';}
    if(r.anchor==='reset'){
      if(resetAnchor){target=resetAnchor.time;if(resetAnchor.source==='cached')state.anchorMessage='服务器重置时间暂未刷新；沿用最近一次可信时间。';else if(r.anchorValid&&r.end!==resetAnchor.time)state.anchorMessage=`服务器重置时间变更：${date(r.end)} → ${date(resetAnchor.time)}`;r.anchorValid=true;}
      else{state.anchorMessage='服务器重置时间缺失、过期或采集陈旧；保留最后可确认范围。';r.anchorValid=false;}
    }
    if(target<=r.start&&!r.span){state.anchorMessage='锚点尚未形成正向区间，保留最后有效范围。';return r;}
    if(target!==r.end || (r.anchor==='reset'&&r.anchorValid&&r.preset==='reset')){r.end=target;if(r.span)r.start=target-r.span;}
    return r;
  }
  const minuteText=m=>`${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`;
  const draftAt=t=>{const p=localParts(t);return {day:p.day,minute:Number(p.time.slice(0,2))*60+Number(p.time.slice(3))};};
  const draftEpoch=d=>fromLocal(d.day,minuteText(d.minute));
  function pushDraft(draft,side,kind,value){
    const d={Start:{...draft.Start},End:{...draft.End}},other=side==='Start'?'End':'Start';
    if(kind==='date'){d[side].day=value;if((side==='Start'&&d.Start.day>d.End.day)||(side==='End'&&d.End.day<d.Start.day))d[other].day=value;}
    else{d[side].minute=value;if(draftEpoch(d.Start)>draftEpoch(d.End))d[other]={...d[side]};}
    return d;
  }
  function draftValid(d){return draftEpoch(d.Start)<draftEpoch(d.End);}
  function positionReadout(id,fraction){const e=$(id),parent=e.parentElement,w=parent?.clientWidth||300,ew=e.offsetWidth||100,x=1+fraction*(w-2),left=Math.max(0,Math.min(w-ew,x-ew/2));e.style.left=`${left}px`;e.style.setProperty('--cursor-offset',`${x-left}px`);}
  function writeEndpoint(side,value){
    const d=state.rulerDraft?.[side]||draftAt(value),year=Number(d.day.slice(0,4)),jan=Date.UTC(year,0,1),days=(Date.UTC(year+1,0,1)-jan)/86400000;
    const ordinal=(Date.parse(d.day+'T00:00Z')-jan)/86400000,dp=ordinal/(days-1),tp=d.minute/1440;
    $(`range${side}Year`).value=String(year);$(`range${side}Date`).value=d.day;$(`range${side}Time`).value=minuteText(d.minute);
    $(`ruler${side}DateButton`).textContent=`${Number(d.day.slice(5,7))}月${Number(d.day.slice(8))}日`;
    $(`range${side}Day`).max=days-1;$(`range${side}Day`).value=ordinal;$(`range${side}Minute`).value=d.minute;
    $(`range${side}Day`).setAttribute('aria-valuetext',d.day);$(`range${side}Minute`).setAttribute('aria-valuetext',minuteText(d.minute));
    positionReadout(`ruler${side}DateReadout`,dp);positionReadout(`ruler${side}TimeReadout`,tp);
    const other=state.rulerDraft?.[side==='Start'?'End':'Start'];
    for(const [part,mapped,position,ghost] of [['Date',other?.day.slice(0,4)===d.day.slice(0,4),dp,other?(Date.parse(other.day+'T00:00Z')-jan)/86400000/(days-1):0],['Time',other?.day===d.day,tp,(other?.minute||0)/1440]]){
      $(`ruler${side}${part}Ghost`).style.display=mapped?'block':'none';$(`ruler${side}${part}Selection`).style.display=mapped?'block':'none';
      $(`ruler${side}${part}Ghost`).style.left=`${ghost*100}%`;$(`ruler${side}${part}Selection`).style.left=`${Math.min(position,ghost)*100}%`;$(`ruler${side}${part}Selection`).style.width=`${Math.abs(position-ghost)*100}%`;
    }
  }
  function syncRangeControls(){
    const r=state.timeRange;if(!r)return;
    if(!state.rulerDragging&&!state.rulerInvalid)state.rulerDraft={Start:draftAt(r.start),End:draftAt(r.end)};
    writeEndpoint('Start',r.start);writeEndpoint('End',r.end);$('rangeEndAnchor').value=r.anchor;
    $('rangePreset').value=r.preset;$('quotaResetZoom').hidden=!state.quotaSelectionBase;
    document.querySelectorAll('[data-quota-preset]').forEach(b=>b.classList.toggle('selected',b.dataset.quotaPreset===r.preset));
    text('rangeAnchorStatus',state.anchorMessage||(r.anchor==='fixed'?'固定范围':'跟随有效时间锚点'));
  }
  function cacheForRange(){
    const r=state.timeRange,list=state.rangeCaches||[];if(state.rulerDragging){const broad=[...list].sort((a,b)=>(Date.parse(b.end)-Date.parse(b.start))-(Date.parse(a.end)-Date.parse(a.start)));return broad.find(c=>Date.parse(c.start)<=r.start&&Date.parse(c.end)>=r.end)||broad.find(c=>Date.parse(c.end)>r.start&&Date.parse(c.start)<r.end)||null;}return [...list].reverse().find(c=>Date.parse(c.start)<=r.start&&Date.parse(c.end)>=Math.min(r.end,Date.parse(state.data.generated_at)))||
      [...list].reverse().find(c=>Date.parse(c.end)>r.start&&Date.parse(c.start)<r.end)||null;
  }
  function previewRange(){
    state.rulerFrame=null;if(!state.data||state.rulerInvalid)return;const c=charts.get('quotaChart'),r=state.timeRange,cached=cacheForRange();if(cached!==state.quotaRenderedSource){renderQuota(state.data.adaptive);}
    if(c){c.__quotaOption={xAxis:[{min:r.start,max:r.end}],dataZoom:[{id:'quota-zoom',startValue:r.start,endValue:r.end}]};c.setOption({xAxis:[{min:r.start,max:r.end}],dataZoom:[{id:'quota-zoom',start:null,end:null,startValue:r.start,endValue:r.end,rangeMode:['value','value']}]},{silent:true,lazyUpdate:true});state.quotaPlotRange=[r.start,r.end];requestAnimationFrame(()=>{updateNowMarker();restorePlotPointer();});}
    text('rulerStatus',cached?'正在使用缓存预览；松手后补充细节':'所选范围尚无缓存，正在读取');
    text('quotaRangeSummary',`${date(r.start)} — ${date(r.end)} · ${rangeZone()}`);
  }
  function scheduleRangeQuery(final=false){
    const cached=cacheForRange(),r=state.timeRange;if(!final&&state.rulerDragging&&cached&&Date.parse(cached.start)<=r.start&&Date.parse(cached.end)>=r.end){clearTimeout(state.rulerQueryTimer);return;}
    clearTimeout(state.rulerQueryTimer);const delay=final?0:Math.max(0,450-(performance.now()-(state.lastRangeQuery||0)));
    state.rulerQueryTimer=setTimeout(async()=>{if(state.rulerInvalid)return;state.lastRangeQuery=performance.now();await loadQuotaRange();if(state.data&&!state.rulerInvalid){if(!state.rulerDragging||cacheForRange()!==state.quotaRenderedSource)renderQuota(state.data.adaptive);if(!state.rulerDragging)text('rulerStatus',state.quotaError||'');}},delay);
  }
  function changeDraft(side,kind,value){
    state.pendingPreset=null;state.rangeRevision=(state.rangeRevision||0)+1;state.rangeSwitching=false;
    state.rulerDraft??={Start:draftAt(state.timeRange.start),End:draftAt(state.timeRange.end)};
    state.timeRange={...state.timeRange,anchor:'fixed',span:null,preset:'custom'};state.rulerDraft=pushDraft(state.rulerDraft,side,kind,value);state.rulerInvalid=!draftValid(state.rulerDraft);state.rangeEditing=true;
    writeEndpoint('Start',state.timeRange.start);writeEndpoint('End',state.timeRange.end);$('rangeEndAnchor').value='fixed';$('rangePreset').value='custom';
    if(state.rulerInvalid){clearTimeout(state.rulerQueryTimer);state.quotaAbort?.abort();state.quotaRequest++;text('rulerStatus','当前日期/时刻尚未形成正向区间；图表保留最后有效范围');return;}
    state.timeRange={start:draftEpoch(state.rulerDraft.Start),end:draftEpoch(state.rulerDraft.End),anchor:'fixed',span:null,preset:'custom'};state.quotaZoom=null;
    if(!state.rulerFrame)state.rulerFrame=requestAnimationFrame(previewRange);scheduleRangeQuery(!state.rulerDragging);
  }
  function editEndpoint(side,part){
    const d=state.rulerDraft?.[side]||draftAt(state.timeRange[side==='Start'?'start':'end']);
    try{
      if(part==='Minute'||part==='Time'){
        let m=Number($(`range${side}Minute`).value);
        if(part==='Time'){const v=$(`range${side}Time`).value;if(!/^\d{2}:\d{2}$/.test(v))throw Error('请输入HH:mm');const [h,mm]=v.split(':').map(Number);m=h*60+mm;if(mm>59||h>24||(h===24&&mm))throw Error('时间须为00:00至24:00');}
        changeDraft(side,'time',m);
      }else{
        let day=$(`range${side}Date`).value,year=Number($(`range${side}Year`).value);
        if(part==='Year'){const month=Number(d.day.slice(5,7)),last=new Date(Date.UTC(year,month,0)).getUTCDate();day=`${year}-${String(month).padStart(2,'0')}-${String(Math.min(last,Number(d.day.slice(8)))).padStart(2,'0')}`;}
        if(part==='Day')day=new Date(Date.UTC(year,0,1)+Number($(`range${side}Day`).value)*86400000).toISOString().slice(0,10);
        fromLocal(day,'00:00');changeDraft(side,'date',day);
      }
    }catch(e){text('rulerStatus',e.message);}
  }
  function endRulerDrag(){
    state.rulerDragging=false;state.rangeEditing=state.rulerInvalid;
    if(state.deferredSnapshot){Object.assign(state,state.deferredSnapshot);state.deferredSnapshot=null;}
    if(!state.rulerInvalid)scheduleRangeQuery(true);
    if(state.refreshQueued){state.refreshQueued=false;refresh();}
  }
  function bindRulers(){
    for(const side of ['Start','End']){
      $(`range${side}Year`).innerHTML=Array.from({length:131},(_,i)=>`<option value="${1970+i}">${1970+i}</option>`).join('');
      for(const part of ['Year','Date','Time'])$(`range${side}${part}`).addEventListener('change',()=>{editEndpoint(side,part);$(`ruler${side}DateEditor`).classList.add('hidden');});
      $(`ruler${side}DateButton`).addEventListener('click',()=>{$(`ruler${side}DateEditor`).classList.toggle('hidden');});
      for(const part of ['Day','Minute']){
        const rail=$(`range${side}${part}`);
        rail.addEventListener('pointerdown',e=>{state.rulerDragging=true;state.rangeEditing=true;state.plotCursor=null;hidePlotPointer();renderQuota(state.data.adaptive);rail.setPointerCapture?.(e.pointerId);if(part==='Minute')editEndpoint(side,part);});
        rail.addEventListener('input',()=>editEndpoint(side,part));
        rail.addEventListener('pointerup',endRulerDrag);rail.addEventListener('pointercancel',endRulerDrag);
        rail.addEventListener('change',()=>{if(!state.rulerDragging){editEndpoint(side,part);scheduleRangeQuery(true);}});
      }
    }
    $('rangeEndAnchor').addEventListener('change',()=>{if(!state.timeRange)return;const anchor=$('rangeEndAnchor').value;if(anchor==='reset'&&!confirmedReset()){text('rulerStatus','无法确认下次重置时间；保留当前范围');syncRangeControls();return;}const end=anchor==='reset'?confirmedReset():anchor==='now'?Date.now():state.timeRange.end;if(end<=state.timeRange.start){text('rulerStatus','终点尚未晚于起点；保留当前范围');syncRangeControls();return;}selectRange({...state.timeRange,end,anchor,span:null,preset:'custom'});});
  }
  function hidePlotPointer(reason='control-interaction'){state.pointerHiddenReason=reason;state.pointerRequested=false;const c=charts.get('quotaChart');if(c){if(c.__mouseMarker)c.__mouseMarker.hidden=true;c.dispatchAction({type:'updateAxisPointer',currTrigger:'leave'});c.dispatchAction({type:'hideTip'});}}
  function restorePlotPointer(){
    state.plotPointerFrame=null;const c=charts.get('quotaChart'),cursor=state.plotCursor;if(!c||!cursor||state.rulerDragging)return;
    const b=$('quotaChart').getBoundingClientRect(),x=cursor.x-b.left,y=cursor.y-b.top;
    if(!c.containPixel({gridIndex:0},[x,y])){hidePlotPointer('outside-plot');return;}
    const t=c.convertFromPixel({xAxisIndex:0},x);state.quotaPointer=t;
    let marker=c.__mouseMarker;if(!marker){marker=document.createElement('div');marker.id='quota-mouse-pointer';marker.className='quota-mouse-pointer';$('quotaChart').appendChild(marker);c.__mouseMarker=marker;}
    const top=c.convertToPixel({yAxisIndex:0},100),bottom=c.convertToPixel({yAxisIndex:0},0);marker.hidden=false;marker.style.left=x+'px';marker.style.top=top+'px';marker.style.height=(bottom-top)+'px';state.pointerRequested=true;state.pointerHiddenReason=null;state.pointerCompleted={at:Date.now(),x,y,time:t};
    c.dispatchAction({type:'updateAxisPointer',currTrigger:'mousemove',x,y,axesInfo:[{axisDim:'x',axisIndex:0,value:t}]});
  }
  function bindPlotPointer(c){
    if(c.__pointerBound)return;c.__pointerBound=true;const el=$('quotaChart');
    el.addEventListener('pointermove',e=>{state.pointerEvent={type:e.type,at:Date.now(),clientX:e.clientX,clientY:e.clientY};state.pointerEventCount=(state.pointerEventCount||0)+1;state.plotCursor={x:e.clientX,y:e.clientY};if(!state.plotPointerFrame)state.plotPointerFrame=requestAnimationFrame(restorePlotPointer);});
    el.addEventListener('pointerleave',e=>{state.pointerEvent={type:e.type,at:Date.now(),clientX:e.clientX,clientY:e.clientY};state.plotCursor=null;hidePlotPointer('pointerleave');});
    el.addEventListener('click',e=>{state.lastPointerClick=Date.now();state.plotCursor={x:e.clientX,y:e.clientY};restorePlotPointer();const b=el.getBoundingClientRect();if(c.containPixel({gridIndex:0},[e.clientX-b.left,e.clientY-b.top]))showRuntimeDetails(state.quotaPointer);});
  }
  function getDiagnostics(){
    const c=charts.get('quotaChart'),el=$('quotaChart'),b=el.getBoundingClientRect(),r=state.quotaPlotRange,cursor=state.plotCursor,x=cursor?cursor.x-b.left:null,y=cursor?cursor.y-b.top:null;
    const layer=node=>{if(!node)return {connected:false};const box=node.getBoundingClientRect(),s=getComputedStyle(node);return {connected:node.isConnected,hidden:node.hidden,rect:{x:box.x,y:box.y,width:box.width,height:box.height},display:s.display,visibility:s.visibility,opacity:s.opacity,zIndex:s.zIndex,border:s.borderLeft,pixelVerified:false};};
    return JSON.parse(JSON.stringify({version:'quota-rulers-v17',sampledAt:Date.now(),pointer:{lastEvent:state.pointerEvent||null,eventCount:state.pointerEventCount||0,client:cursor||null,local:{x,y},inside:!!(cursor&&c?.containPixel({gridIndex:0},[x,y])),time:state.quotaPointer||null,requestedVisible:!!state.pointerRequested,hiddenReason:state.pointerHiddenReason||null,completed:state.pointerCompleted||null,layer:layer(c?.__mouseMarker)},now:{time:c?.__nowAt,layer:layer(c?.__nowMarker)},plot:{rect:{x:b.x,y:b.y,width:b.width,height:b.height},bounds:r&&c?{left:c.convertToPixel({xAxisIndex:0},r[0]),right:c.convertToPixel({xAxisIndex:0},r[1]),top:c.convertToPixel({yAxisIndex:0},100),bottom:c.convertToPixel({yAxisIndex:0},0)}:null,range:r},selection:{range:state.timeRange,revision:state.rangeRevision||0,switching:!!state.rangeSwitching,dragging:!!state.rulerDragging,invalid:!!state.rulerInvalid,pendingPreset:state.pendingPreset||null,lastClickAt:state.lastPointerClick||null},request:{revision:state.quotaRequest,loading:state.quotaLoading,error:state.quotaError,key:state.quotaDataKey},evidence:'DOM geometry and computed styles only; pixelVerified is never inferred from geometry.'}));
  }
  Object.defineProperty(window,'codexQuotaUI',{value:Object.freeze({getDiagnostics}),writable:false,configurable:false});
  function updateNowMarker(){
    const c=charts.get('quotaChart'),r=state.quotaPlotRange;if(!c||!r)return;
    let marker=c.__nowMarker;
    if(!marker){marker=document.createElement('div');marker.id='quota-wallclock-now';marker.className='quota-wallclock-now';const label=document.createElement('span');label.textContent='现在';marker.appendChild(label);$('quotaChart').appendChild(marker);c.__nowMarker=marker;}
    const now=Date.now();c.__nowAt=now;marker.hidden=state.view!=='overview'||now<r[0]||now>r[1];
    let guide=c.__currentQuotaGuide;
    if(!guide){guide=document.createElement('div');guide.id='quota-current-guide';guide.className='quota-current-guide';guide.setAttribute('aria-hidden','true');$('quotaChart').appendChild(guide);c.__currentQuotaGuide=guide;}
    const current=latestBefore(state.quotaRenderedSource?.points||[],now),used=Number(current?.used_percent??state.data?.adaptive?.latest?.used_percent),guideVisible=!marker.hidden&&Number.isFinite(used);
    guide.hidden=!guideVisible;
    if(guideVisible){
      const x=c.convertToPixel({xAxisIndex:0},now),y=c.convertToPixel({yAxisIndex:0},used),right=c.convertToPixel({xAxisIndex:0},r[1]),top=c.convertToPixel({yAxisIndex:0},100),dx=right-x,dy=top-y,length=Math.hypot(dx,dy);
      guide.hidden=!(length>4);
      if(!guide.hidden){guide.style.left=x+'px';guide.style.top=y+'px';guide.style.width=length+'px';guide.style.transform=`rotate(${Math.atan2(dy,dx)}rad)`;}
    }
    if(!marker.hidden){const x=c.convertToPixel({xAxisIndex:0},now),top=c.convertToPixel({yAxisIndex:0},100),bottom=c.convertToPixel({yAxisIndex:0},0),left=c.convertToPixel({xAxisIndex:0},r[0]),right=c.convertToPixel({xAxisIndex:0},r[1]);marker.style.left=x+'px';marker.style.top=top+'px';marker.style.height=(bottom-top)+'px';marker.firstChild.style.left=(Math.max(left,Math.min(right-34,x+3))-x)+'px';c.__nowX=x;}
  }

  const quotaKey=()=>state.timeRange?`${state.timeRange.start}:${state.timeRange.end}`:'';
  async function loadQuotaRange() {
    if(!state.data)return;const r=resolveQuotaRange();syncRangeControls();
    const request=++state.quotaRequest;state.quotaAbort?.abort();const controller=new AbortController();state.quotaAbort=controller;
    const timer=setTimeout(()=>controller.abort(),15000),key=quotaKey();if(state.quotaDataKey!==key)state.quotaData=null;
    state.quotaLoading=true;state.quotaError='';
    try{
      const query=new URLSearchParams({start:new Date(r.start).toISOString(),end:new Date(r.end).toISOString(),as_of:state.data.generated_at,max_points:'1800'});
      const response=await fetch(`./api/quota/history?${query}`,{signal:controller.signal,cache:'no-store'});
      if(!response.ok)throw Error(`范围接口暂不可用（HTTP ${response.status}），下一分钟重试。`);
      if(!(response.headers.get('Content-Type')||'').includes('json'))throw Error('范围接口未返回JSON，可能正在部署；下一分钟重试。');
      const result=await response.json();if(request!==state.quotaRequest)return;
      if(!Array.isArray(result.points))throw Error('范围数据格式不正确');state.rangeCaches=[...(state.rangeCaches||[]).filter(c=>c.start!==result.start||c.end!==result.end),result].slice(-8);if(key!==quotaKey())return;state.quotaData=result;state.quotaDataKey=key;state.quotaAvailable=result;
    }catch(error){if(request===state.quotaRequest)state.quotaError=error.name==='AbortError'?'范围读取超时，下一分钟重试。':error.message;}
    finally{clearTimeout(timer);if(request===state.quotaRequest)state.quotaLoading=false;}
  }
  function confirmedReset(){return trustedResetAnchor()?.time||null;}
  async function selectRange(range){
    const revision=state.rangeRevision=(state.rangeRevision||0)+1;state.quotaAbort?.abort();state.quotaRequest++;clearTimeout(state.zoomTimer);clearTimeout(state.rulerQueryTimer);
    state.pendingPreset=null;state.rangeSwitching=true;state.rulerInvalid=false;state.rulerDraft=null;state.rangeEditing=false;state.quotaZoom=null;state.timeRange=range;
    syncRangeControls();renderQuota(state.data.adaptive);text('rulerStatus',cacheForRange()?'已显示缓存，正在补充范围数据':'此范围暂无缓存，正在读取');
    await loadQuotaRange();if(revision!==state.rangeRevision)return;
    if(state.deferredSnapshot){Object.assign(state,state.deferredSnapshot);state.deferredSnapshot=null;}
    renderQuota(state.data.adaptive);state.rangeSwitching=false;state.refreshQueued=false;syncRangeControls();text('rulerStatus',state.quotaError||'');
  }
  async function quotaPreset(preset) {
    const now=Date.now(),r=state.timeRange||resolveQuotaRange(),reset=preset==='reset'||preset==='7d'?confirmedReset():null;
    if((preset==='reset'||preset==='7d')&&!reset){state.rangeRevision=(state.rangeRevision||0)+1;state.rangeSwitching=false;state.quotaAbort?.abort();state.quotaRequest++;state.pendingPreset=preset;resolveQuotaRange();syncRangeControls();text('rulerStatus',state.anchorMessage);return;}
    state.quotaSelectionBase=null;const span=preset==='24h'?86400000:7*86400000,end=reset||now;
    return selectRange(preset==='custom'?{...r,anchor:'fixed',span:null,preset}:{start:end-span,end,span,anchor:reset?'reset':'now',preset,anchorValid:!!reset});
  }
  const eventNames={quota_reset:'重置',quota_window_change:'窗口变更',quota_recovery_unclassified:'额度回升',scope_change:'范围变化',reset_credit_inventory:'券库存变化',reset_credit_used:'券使用',credit_expiry:'券到期',reset_announcement:'重置预告'};
  const eventColor=kind=>({quota_reset:'#e4b979',quota_window_change:'#c4a0e9',quota_recovery_unclassified:'#e494ac',scope_change:'#9ab2cf',reset_credit_inventory:'#e49b7c',reset_credit_used:'#e49b7c',credit_expiry:'#cbb86e',reset_announcement:'#79a8dd'}[kind]||'#c4a0e9');
  const effortNames={low:'轻度',medium:'中等',high:'高度',xhigh:'特高',max:'最大',ultra:'超强',unknown:'深度未知'};
  const groupName=key=>{const at=key.lastIndexOf('|');return `${model(key.slice(0,at))} · ${effortNames[key.slice(at+1)]||key.slice(at+1)}`;};
  function groupOrder(key){const rank=['luna','terra','sol','astra'].findIndex(m=>key.includes(m)),e=['unknown','low','medium','high','xhigh','max','ultra'].indexOf(key.split('|').at(-1));return (rank<0?-1:rank)*10+Math.max(0,e);}
  function groupColor(key){
    const ramps={astra:['#a99ab9','#eadfff','#cbb5f4','#a985e5','#9067d4','#7043bd','#552c95'],
      luna:['#92aba3','#c4f7e5','#83d5b9','#4ab698','#259982','#127766','#085848'],
      sol:['#98a9ba','#d1e9ff','#9bc8ef','#6da9dd','#438bc4','#246ca7','#154d7d'],
      terra:['#abaf92','#f0e6b6','#d8ce86','#bbb55e','#9b973e','#77782a','#585b1d']};
    const family=Object.keys(ramps).find(name=>key.includes(name)),i=Math.max(0,['unknown','low','medium','high','xhigh','max','ultra'].indexOf(key.split('|').at(-1)));
    return family?ramps[family][i]:'#a2abb6';
  }
  function latestBefore(points,time){let stamps=timeIndexes.get(points);if(!stamps){stamps=points.map(p=>typeof p.time==='number'?p.time:Date.parse(p.time));timeIndexes.set(points,stamps);}let lo=0,hi=stamps.length;while(lo<hi){const mid=(lo+hi)>>1;if(stamps[mid]<=time)lo=mid+1;else hi=mid;}return lo?points[lo-1]:null;}
  function compositionAt(time){const c=(state.quotaRenderedSource||state.quotaData)?.composition,p=latestBefore(c?.points||[],time);return p&&time<Date.parse(p.end)?p:null;}
  function displayComposition(points,start,end,pixels,predicted=false){
    if(!points.length||end<=start)return [];
    if(predicted){const span=end-start;start=Math.max(start,Date.parse(points[0].time));end=Math.min(end,Date.parse(points.at(-1).time));if(end<=start)return [];pixels*=Math.min(1,(end-start)/span);}
    // Display-only interval integration. Original minute values stay available
    // to the tooltip and exports; no forecast/ledger value is overwritten.
    const count=Math.max(1,Math.min(points.length,Math.floor(pixels/3))),width=(end-start)/count;
    const bins=Array.from({length:count},(_,i)=>({time:start+i*width,end:Math.min(end,start+(i+1)*width),groups:{},mean:0,peak:0,seconds:0}));
    for(let i=0;i<points.length;i++){
      const p=points[i],q=points[i+1]||p,a=Date.parse(p.time),b=predicted?(points[i+1]?Date.parse(q.time):a):Math.min(Date.parse(p.end),a+(p.observed_seconds??p.seconds)*1000);
      const lo=Math.max(a,start),hi=Math.min(b,end);if(!(hi>lo))continue;
      const keys=new Set([...Object.keys(p.groups||{}),...(predicted?Object.keys(q.groups||{}):[])]);
      for(let j=Math.max(0,Math.floor((lo-start)/width));j<Math.min(count,Math.ceil((hi-start)/width));j++){
        const bin=bins[j],left=Math.max(lo,bin.time),right=Math.min(hi,bin.end),dt=(right-left)/1000;if(dt<=0)continue;
        const value=(x,y,t)=>x+(y-x)*(t-a)/(b-a);
        bin.seconds+=dt;
        if(predicted){const totalA=value(p.expected_total||0,q.expected_total||0,left),totalB=value(p.expected_total||0,q.expected_total||0,right);bin.peak=Math.max(bin.peak,totalA,totalB);}
        else bin.peak=Math.max(bin.peak,p.peak_concurrency||0);
        for(const key of keys){
          const leftValue=predicted?p.groups[key]?.expected||0:p.groups[key]?.mean_concurrency||0,rightValue=predicted?q.groups[key]?.expected||0:leftValue;
          const amount=predicted?(value(leftValue,rightValue,left)+value(leftValue,rightValue,right))*.5*dt:leftValue*dt;
          bin.groups[key]=(bin.groups[key]||0)+amount;
        }
      }
    }
    for(const bin of bins){const sum=Object.values(bin.groups).reduce((a,b)=>a+b,0);bin.mean=bin.seconds?sum/bin.seconds:null;bin.shares=Object.fromEntries(Object.entries(bin.groups).map(([key,v])=>[key,sum?v/sum:0]));}
    return bins;
  }
  function concurrencySegments(points,bins,step){
    const result=[];let cursor=0;
    for(let i=1;i<points.length;i++){
      const a=points[i-1],b=points[i],start=Date.parse(a[0]),end=Date.parse(b[0]);if(a[1]==null||b[1]==null||end<start)continue;
      while(cursor<bins.length&&bins[cursor].end<=start)cursor++;
      let x=start,j=cursor;
      do{const bin=bins[j];if(bin&&bin.end<=x){j++;continue;}const covered=bin&&bin.time<=x&&x<bin.end,to=Math.min(end,covered?bin.end:bin?Math.max(x,bin.time):end),value=t=>step?a[1]:start===end?b[1]:a[1]+(b[1]-a[1])*(t-start)/(end-start);
        result.push([x,value(x),to,step&&to===end?b[1]:value(to),covered&&bin.seconds?bin.mean:-1]);if(to===end)break;if(to===x){j++;continue;}x=to;
      }while(x<=end);
    }return result;
  }
  async function showRuntimeDetails(time) {
    state.quotaSelectedTime=time;text('runtimeDetailTime',date(time,false,true));
    if(time>Date.parse(state.data.generated_at)){$('runtimeDetailRows').innerHTML='<tr><td colspan="3">未来只有条件预测，不存在实际执行线程名单。</td></tr>';return;}
    const seq=++state.runtimeRequest;
    try{const response=await fetch(`./api/quota/runtime?at=${encodeURIComponent(new Date(time).toISOString())}&as_of=${encodeURIComponent(state.data.generated_at)}`,{cache:'no-store'});if(!response.ok)throw Error(`HTTP ${response.status}`);const r=await response.json();if(seq!==state.runtimeRequest)return;
      text('runtimeDetailTime',`${date(time,false,true)} · 已证实 ${r.count} 个线程；${r.status==='partial'?'覆盖不完整，可能还有未确认的运行。':'仅表示可恢复的本机记录。'}`);
      $('runtimeDetailRows').innerHTML=r.threads.map(t=>{const label=esc(t.title||t.thread),identity=String(t.thread||'');const task=identity.startsWith('local-task-')?label:`<a href="codex://threads/${encodeURIComponent(identity)}">${label}</a>`;return `<tr><td>${task}<br><small>${esc(t.host)}</small></td><td>${esc(groupName(t.model+'|'+t.effort))}<br><small>${esc(tier(t.tier))}</small></td><td>${esc(({observed:'开始结束已记录',reconstructed_start:'起点由执行证据恢复',reconstructed_end:'以最终输出恢复结束',open_observed_prefix:'结束未确认，仅已观测部分'})[t.quality]||t.quality)}<br><small>${date(t.start)} — ${date(t.end)}</small></td></tr>`;}).join('')||'<tr><td colspan="3">此刻没有可确认的运行记录；缺记录不等于全机空闲。</td></tr>';
    }catch(error){text('runtimeDetailTime',`${date(time,false,true)} · 明细暂不可用：${error.message}`);}
  }
  function m3Data() { return state.data?.forecast_v2?.m3 || {}; }
  function m3Points() {
    return (m3Data().points || []).map(point => {
      const band=point.compatibility_used_pp || {},center=Number(point.expected_used_pp);
      const lower=valid(band.lower_used_pp)?Number(band.lower_used_pp):center;
      const upper=valid(band.upper_used_pp)?Number(band.upper_used_pp):center;
      return {time:point.time,median:Math.min(100,center),lower:Math.min(100,lower),upper:Math.min(100,upper)};
    });
  }
  function renderQuota(a) {
    const r=resolveQuotaRange(),loaded=state.rulerDragging?cacheForRange():(state.quotaDataKey===quotaKey()?state.quotaData:cacheForRange()),begin=r.start,end=r.end,now=Date.parse(state.data.generated_at),latest=a.latest||{};
    const allForecast=m3Points(),firstForecast=Date.parse(allForecast[0]?.time),lastForecast=Date.parse(allForecast.at(-1)?.time);
    const geometryStart=state.rulerDragging?Math.min(Date.parse(loaded?.start)||begin,Number.isFinite(firstForecast)?firstForecast:begin):begin,geometryEnd=state.rulerDragging?Math.max(Date.parse(loaded?.end)||end,Number.isFinite(lastForecast)?lastForecast:end):end;
    const actual=(loaded?.points||[]).filter(p=>Date.parse(p.time)>=geometryStart-300000&&Date.parse(p.time)<=geometryEnd);
    const forecast=allForecast.filter(p=>Date.parse(p.time)>=geometryStart&&Date.parse(p.time)<=geometryEnd);
    const boundaries=(loaded?.boundaries||[]).filter(b=>Date.parse(b.time)>=geometryStart&&Date.parse(b.time)<=geometryEnd);state.quotaBoundaries=boundaries;
    if(!loaded)text('rulerStatus','所选范围尚未加载，正在读取');
    state.quotaRenderedSource=loaded;const composition=loaded?.composition||{},distribution={status:'unavailable',coverage:'unavailable',points:[]},hasFuture=end>now&&forecast.length>0;
    syncRangeControls();text('quotaRangeSummary',`${date(begin,false,true)} — ${date(end,false,true)} · ${rangeZone()} · 历史与未来使用同一范围。有采样日期在年份轴上浅色标示，缺测仍保留。${loaded?.status==='empty'?' 本段没有服务器采样。':''}`);
    $('quotaHistoryError').classList.toggle('hidden',!state.quotaError);text('quotaHistoryError',state.quotaError||'');
    for(const id of ['quotaForecastLegend','quotaBandLegend','quotaFutureResults'])$(id).classList.toggle('hidden',!hasFuture);
    $('quotaBoundaryNotice').classList.toggle('hidden',!boundaries.length);text('quotaBoundaryNotice',boundaries.length?`本段有 ${boundaries.length} 个服务器变化边界。保留原时刻虚线；相近文字合并，详情逐条列出。`:'');
    $('quotaBoundaryDetails').classList.toggle('hidden',!boundaries.length);text('quotaBoundarySummary',`查看本段 ${boundaries.length} 次变化明细`);
    $('quotaBoundaryRows').innerHTML=boundaries.map(b=>`<tr><td>${date(b.time,false,true)}</td><td>${esc(eventNames[b.kind]||b.kind)}</td><td>${valid(b.before_used_percent)?n(100-b.before_used_percent)+'% → '+n(100-b.after_used_percent)+'%':valid(b.before_count)?n(b.before_count)+'张 → '+n(b.after_count)+'张':esc(b.title||'—')}</td><td>${b.details?esc(b.details):date(b.before_resets_at)+' → '+date(b.after_resets_at)}</td></tr>`).join('');
    const buckets=composition.points||[],future=distribution.points||[],keys=[...new Set([...buckets.flatMap(p=>Object.keys(p.groups||{})),...future.flatMap(p=>Object.keys(p.groups||{}))])].sort((a,b)=>groupOrder(a)-groupOrder(b)||a.localeCompare(b));
    const pixels=Math.max(120,($('quotaChart').clientWidth||charts.get('quotaChart')?.getWidth()||1000)-110),span=end-begin;
    const historical=displayComposition(buckets,geometryStart,Math.min(geometryEnd,now),pixels*Math.max(0,Math.min(geometryEnd,now)-geometryStart)/(geometryEnd-geometryStart));
    const anticipated=displayComposition(future,Math.max(geometryStart,now),geometryEnd,pixels*Math.max(0,geometryEnd-Math.max(geometryStart,now))/(geometryEnd-geometryStart),true);
    state.quotaDisplay={historical,anticipated};
    $('compositionLegend').innerHTML=keys.map(k=>`<span><i data-color="${groupColor(k)}"></i>${esc(groupName(k))}</span>`).join('');
    document.querySelectorAll('#compositionLegend [data-color]').forEach(e=>{e.style.backgroundColor=e.dataset.color;});
    text('compositionCoverage',`背景：已观测运行时长构成；走势线颜色：显示桶平均并发，灰色为未知。${composition.status==='partial'||composition.status==='unavailable'||!composition.status?'历史覆盖不完整，空白不表示空闲。':''}${hasFuture?` 未来为已覆盖任务的预计构成${distribution.coverage==='partial'?'，持续目标续跑等仍有未知部分，不能理解为全部任务将停止':''}。`:''}`);
    const compact=($('quotaChart').clientWidth||1000)<600;
    const o=options();o.legend.show=false;o.grid={...o.grid,top:28,bottom:compact?80:20,right:compact?16:110};o.xAxis={...o.xAxis,min:begin,max:end,axisPointer:{show:true,snap:false,animation:false,triggerEmphasis:false,zlevel:10,lineStyle:{type:'dashed',color:'#c7d2e1',opacity:0}}};
    o.yAxis=[{...o.yAxis,min:0,max:100,axisLabel:{...o.yAxis.axisLabel,formatter:'{value}%'}},{type:'value',show:false,min:0},{type:'value',show:false,min:0,max:1}];
    const selection=state.quotaZoom?.map(v=>Math.max(begin,Math.min(end,v))),zoom=selection&&selection[1]>selection[0]?{start:null,end:null,startValue:selection[0],endValue:selection[1],rangeMode:['value','value']}:{start:0,end:100,startValue:null,endValue:null,rangeMode:['percent','percent']};
    o.dataZoom=[{id:'quota-zoom',type:'inside',zoomOnMouseWheel:'ctrl',moveOnMouseMove:false,preventDefaultMouseMove:false,moveOnMouseWheel:false,filterMode:'none',...zoom}];
    o.tooltip={...o.tooltip,triggerOn:'none',transitionDuration:0,extraCssText:'max-width:min(380px,calc(100vw - 76px));white-space:normal;line-height:1.45;',alwaysShowContent:false,axisPointer:{type:'line',snap:false,animation:false,lineStyle:{type:'dashed'}},formatter:params=>{
      const t=state.quotaPointer??Number(params[0]?.axisValue),out=[`<strong>${esc(date(t,false,true))}</strong>`],measured=latestBefore(actual,t),b=compositionAt(t),fp=t<=Date.parse(future.at(-1)?.time)?latestBefore(future,t):null,q=t<=Date.parse(forecast.at(-1)?.time)?latestBefore(forecast,t):null;
      if(measured&&measured.used_percent!=null&&t<=now)out.push(`<div>服务器实际：${n(measured.used_percent,1)}%<br><small>采样 ${esc(date(measured.time,false,true))} · 距指针 ${n((t-Date.parse(measured.time))/1000)} 秒</small></div>`);else if(t<=now)out.push('<div>此刻缺少服务器采样</div>');
      if(t>now&&q)out.push(`<div>M3 条件预计已用：${n(q.median,1)}%（参数敏感范围 ${n(q.lower,1)}–${n(q.upper,1)}%）<br><small>签发 ${esc(date(m3Data().issued_at||m3Data().input_cutoff))}</small></div>`);
      else if(t>now)out.push('<div>该时刻暂无已签发预测</div>');
      if(t<=now&&b){out.push(`<div>桶平均已观测并发：${n(b.mean_concurrency,2)}<br><small>${esc(date(b.time,false,true))} — ${esc(date(b.end,false,true))}</small></div>`);for(const [k,g] of Object.entries(b.groups))out.push(`<div>${esc(groupName(k))}：平均 ${n(g.mean_concurrency,2)} · ${pct(g.share)}</div>`);if(b.uncertain_seconds||composition.status==='partial')out.push('<div>部分执行边界未知；这是已观测下限</div>');}
      if(t>now&&fp){out.push(`<div>预计已覆盖并发：${n(fp.expected_total,2)}</div>`);for(const [k,g] of Object.entries(fp.groups))out.push(`<div>${esc(groupName(k))}：预计 ${n(g.expected,2)}</div>`);if(distribution.coverage==='partial')out.push('<div>续跑等未覆盖，不能视为全机总数</div>');}
      const displayed=latestBefore(t>now?anticipated:historical,t);if(displayed&&t<displayed.end&&displayed.seconds)out.push(`<div class="tooltip-resolution">显示桶：平均 ${n(displayed.mean,2)}<br><small>${esc(date(displayed.time,false,true))} — ${esc(date(displayed.end,false,true))}；上方保留原采样值</small></div>`);
      for(const ev of boundaries.filter(e=>Math.abs(Date.parse(e.time)-t)<Math.max(60000,(end-begin)/300)))out.push(`<div>${esc(eventNames[ev.kind]||ev.kind)} · ${esc(date(ev.time,false,true))}</div>`);
      out.push('<small>背景占比是运行线程时长，不是额度份额。点击可查看线程。</small>');return out.join('');
    }};
    const passive={silent:true,tooltip:{show:false},axisPointer:{show:false},emphasis:{disabled:true}};
    const background=(prefix,label,rows,opacity)=>keys.map(k=>line(prefix+k,label+groupName(k),rows.flatMap(p=>[[p.time,p.seconds?p.shares[k]||0:null],[p.end,p.seconds?p.shares[k]||0:null]]),{...passive,yAxisIndex:2,stack:prefix,step:'end',lineStyle:{width:0},areaStyle:{color:groupColor(k),opacity},z:0}));
    const observedSeries=background('composition-','',historical,.50),predictedSeries=hasFuture?background('composition-future-','预计 ',anticipated,.44):[];
    const colored=(id,points,bins,step)=>({id,type:'custom',coordinateSystem:'cartesian2d',clip:true,silent:true,tooltip:{show:false},animation:false,encode:{x:[0,2],y:[1,3]},data:concurrencySegments(points,bins,step),z:step?8:9,renderItem:(params,api)=>{const a=api.coord([api.value(0),api.value(1)]),b=api.coord([api.value(2),api.value(3)]);return {type:'polyline',shape:{points:step?[a,[b[0],a[1]],b]:[a,b]},style:{stroke:api.visual('color'),lineWidth:2.6,fill:null,lineDash:step?null:[6,4],lineDashOffset:step?0:-a[0]}};}});
    o.series=[colored('actual-concurrency-color',actual.map(p=>[p.time,p.used_percent]),historical,true),colored('forecast-concurrency-color',curveData(forecast),anticipated,false),...observedSeries,...predictedSeries,
      line('pointer-domain','时间指针',[[begin,0],[end,0]],{silent:true,lineStyle:{opacity:0},emphasis:{disabled:true},tooltip:{show:true}}),
      line('band-base','范围下沿',forecast.map(p=>[p.time,p.lower]),{...passive,stack:'scenario',lineStyle:{opacity:0},areaStyle:{opacity:0},z:2}),
      line('band-width','参数敏感范围',forecast.map(p=>[p.time,Math.max(0,p.upper-p.lower)]),{...passive,stack:'scenario',lineStyle:{opacity:0},areaStyle:{color:'#8ab4ef',opacity:.12},z:2}),
      line('actual','服务器实际',actual.map(p=>[p.time,p.used_percent]),{emphasis:{disabled:true},step:'end',lineStyle:{opacity:0},z:8,markLine:{silent:true,symbol:'none',lineStyle:{type:'dashed'},label:{show:false},data:boundaries.map(b=>({xAxis:b.time,lineStyle:{color:eventColor(b.kind)},label:{show:false,color:eventColor(b.kind)}}))}}),
      line('median','M3 条件走势（近期速率延续）',curveData(forecast),{emphasis:{disabled:true},lineStyle:{opacity:0},z:9})];
    const maximum=Math.max(1,Math.ceil(Math.max(0,...buckets.map(p=>p.mean_concurrency||0),...future.map(p=>p.expected_total||0))));
    o.visualMap={type:'continuous',min:0,max:maximum,dimension:4,seriesIndex:[0,1],calculable:false,hoverLink:false,orient:compact?'horizontal':'vertical',...(compact?{left:'center',bottom:0}:{right:0,top:'center'}),itemWidth:12,itemHeight:compact?135:150,text:[`${maximum} 并发`,'0 并发'],textStyle:{color:'#d5e4f2',fontSize:10},inRange:{color:['#e8f5ff','#ffe08a','#ff914d','#f04463']},outOfRange:{color:'#a4acb5'}};
    chart('quotaChart',o);const c=charts.get('quotaChart');
    state.quotaPlotRange=[begin,end];bindPlotPointer(c);updateNowMarker();restorePlotPointer();
    text('quotaObservation',`实测 ${loaded?.raw_samples??actual.length} 点 · ${loaded?.reduction?'按显示尺度保留首末与极值':''} · 纵轴为已用额度`);
    text('quotaScopeNote',`M1 实测与 M3 条件走势同图；M3 签发 ${date(m3Data().issued_at||m3Data().input_cutoff)}`);
  }

  function curveData(points, hit) {
    const stop = hit ? Date.parse(hit) : Infinity;
    const data = points.filter(p => Date.parse(p.time) < stop).map(p => [p.time, p.median]);
    if (hit) data.push([hit, 100]);
    return data;
  }
  function m2Data() { return state.data?.forecast_v2?.m2 || {}; }
  function m2StatusName(status) { return ({feasible:'精确相容',approximate:'按假设解释',infeasible:'无法形成解释',timeout:'求解超时',numeric_failure:'数值失败',insufficient_evidence:'证据不足',unavailable:'暂不可用',disabled:'未启用'})[status] || status || '未知'; }
  function referenceSourceName(f=m3Data()) {
    if(f.reference_source==='local_m2_explanation'||f.reference_source==='local_m2_fit')return '本地 M2 解释 · 自动同构';
    if(f.reference_source==='bootstrap_reference') {
      const multiplier=f.reference_metadata?.target_capacity_multiplier??state.data?.runtime?.bootstrap_capacity_multiplier;
      return valid(multiplier)?`启动参考 · 目标容量 ${n(multiplier,2)}×`:'启动参考';
    }
    return '尚无可用参数';
  }
  function renderM3Forecast(a, details = false) {
    const f=m3Data(),points=m3Points(),latest=a.latest||{},work=f.workload_window||{},models=work.models||[],final=points.at(-1);
    const statusName={conditional:"条件走势已生成",idle:"近期负载为空",insufficient_evidence:"证据不足",unavailable:"暂不可用",disabled:"未启用"};
    const source=referenceSourceName(f);
    text("forecastState",statusName[f.status]||f.status||"未知");
    text("forecastSample",`${n(work.request_count)} 次原生请求 · ${n(work.lookback_minutes)} 分钟窗口`);
    const hit=points.find(p=>p.median>=100)?.time;
    text("forecastSummary",final?`${source}驱动的 M3 条件走势：${hit?`约在 ${date(hit)} 达到显示上限`:`本次重置前参考剩余 ${n(Math.max(0,100-final.median),1)}%`}。该结果假设近期 wall-clock token 速率延续。`:`M3 暂不绘制未来走势：${f.reason||"等待可用 M2 解释参数或显式启动参考"}。服务器实测仍继续显示。`);
    text("scenarioAssumption",`M3 假设最近 ${n(work.lookback_minutes)} 分钟的每模型 wall-clock token 速率延续至本次重置；这是条件投影，不是概率预测。`);
    text("scenarioRemaining",final?`${n(Math.max(0,100-final.median),1)}%（参数敏感范围 ${n(Math.max(0,100-final.upper),1)}–${n(Math.max(0,100-final.lower),1)}%）`:"—");
    text("scenarioEndLabel","本次重置前 M3 条件剩余");
    text("scenarioHitting",hit?date(hit):"M3 参考线未达到上限");
    text("scenarioEvidence",`${source} · 本地 M2 ${m2StatusName(m2Data().status)}`);
    if(!details)return;
    text("activeTasks",n(models.length));text("openTurns",n(work.request_count));
    const remainMinutes=(Date.parse(latest.resets_at)-Date.parse(state.data.generated_at))/60000;
    text("budgetRate",remainMinutes>0?`${n((100-latest.used_percent)/remainMinutes,4)} 百分点 / 分钟`:"—");
    const first=points[0],elapsed=first&&final?(Date.parse(final.time)-Date.parse(first.time))/60000:0;
    text("stateRate",elapsed>0?`${n((final.median-first.median)/elapsed,4)} 百分点 / 分钟`:"—");
    text("activityNote",`M3 负载来自 ${n(work.lookback_minutes)} 分钟 wall-clock 聚合；空闲时间包含在分母内。参数来源：${source}；M2 残差不代表概率。`);
    const o=options();o.yAxis={...o.yAxis,name:"已用额度（百分点）",min:0,max:100};
    o.series=[line("m3-center","M3 按解释走势",points.map(p=>[p.time,p.median])),line("m3-low","参数敏感下界",points.map(p=>[p.time,p.lower]),{lineStyle:{type:"dashed"}}),line("m3-high","参数敏感上界",points.map(p=>[p.time,p.upper]),{lineStyle:{type:"dashed"}})];chart("loadChart",o);
    $("m3WorkloadRows").innerHTML=models.length?models.map(row=>`<tr><td>${esc(model(row.model))}</td><td>${n(work.lookback_minutes)} 分钟原生请求聚合</td><td>${compact(row.tokens_per_wall_minute?.uncached_input)}</td><td>${compact(row.tokens_per_wall_minute?.cached_input)}</td><td>${compact(row.tokens_per_wall_minute?.output)}</td></tr>`).join(""):'<tr><td colspan="5">近期窗口没有可投影的原生请求</td></tr>';
    text("observerState",`M3 参考 ${f.reference_id||"未就绪"} · 输入截止 ${date(f.input_cutoff)}`);
    text("referenceWeight",source);text("nearWeight",`${n(work.lookback_minutes)} 分钟 wall-clock`);text("matchedHours",`${n(m2Data().evidence?.period_count)} 个 M2 周期`);text("historyHours",`${n(m2Data().evidence?.raw_observation_count)} 个权威快照`);text("backtestError","等待事前签发成熟样本");
    text("forecastExplanation",`M3 优先读取本地 M2 的解释参数，并使用原生请求构造条件负载。解释可带公开残差；只有尚未形成可用本地解释时，才读取显式启用的启动参考。`);
  }
  function countdown() {
    const reset = state.data?.adaptive?.latest?.resets_at;
    if (!reset) { text("resetCountdown", "—"); return; }
    let minutes = Math.max(0, Math.floor((Date.parse(reset) - Date.now()) / 60000));
    const days = Math.floor(minutes / 1440); minutes %= 1440;
    text("resetCountdown", days ? `${days}天 ${Math.floor(minutes / 60)}小时` : `${Math.floor(minutes / 60)}小时 ${minutes % 60}分`);
  }
  function renderOverview() {
    const a = state.data.adaptive, latest = a.latest || {};
    text("remainingQuota", valid(latest.remaining_percent) ? `${n(latest.remaining_percent)}%` : "—");
    text("quotaUsed", valid(latest.used_percent) ? `本窗口已用 ${n(latest.used_percent)}% · 服务器观测` : "等待服务器观测");
    const m2Current=state.data.forecast_v2?.m2?.current_cycle?.current||{},explainedUsed=Number(m2Current.explained_cycle_used_pp??m2Current.explained_used_pp);
    if(valid(explainedUsed)) {
      const explainedRemaining=Math.max(0,Math.min(100,100-explainedUsed));
      const usedLow=Number(m2Current.explained_cycle_lower_pp??m2Current.explained_lower_pp??m2Current.compatible_lower_pp),usedHigh=Number(m2Current.explained_cycle_upper_pp??m2Current.explained_upper_pp??m2Current.compatible_upper_pp);
      text("explainedRemainingQuota",`${n(explainedRemaining,2)}%`);
      text("explainedRemainingNote",valid(usedLow)&&valid(usedHigh)?`按解释当前扣量 ${n(explainedUsed,2)}%；对齐剩余 ${n(Math.max(0,100-usedHigh),2)}–${n(Math.min(100,100-usedLow),2)}%`:`按解释当前扣量 ${n(explainedUsed,2)}%`);
    } else {
      text("explainedRemainingQuota","—");
      text("explainedRemainingNote","等待 M2 当前解释");
    }
    countdown(); text("resetAt", date(latest.resets_at));
    renderM3Forecast(a);
    renderQuota(a);
  }
  function budgetModel() { return m2Data().model_parameters?.find(item => item.model === state.budgetModel); }
  function renderUsage() {
    const data=state.data,a=data.adaptive||{},day=a.day_summary||data.summary?.last_24_hours||{};
    renderConsumption();renderM3Forecast(a,true);renderCalibration();
    text("dayRequests",n(day.native_requests));text("dayLegacy","全部来自去重后的原生请求");text("dayInput",compact(day.input_tokens));text("dayOutput",compact(day.output_tokens));text("dayCache",pct(day.cache_rate));
    $("modelRows").innerHTML=a.model_rows?.length?a.model_rows.map(row=>`<tr><td class="model-id">${esc(model(row.model))}</td><td>${n(row.native_requests)}</td><td>${compact(row.input_tokens)}</td><td>${compact(row.cached_input_tokens)}</td><td>${compact(row.output_tokens)}</td><td>${pct(row.cache_ratio)}</td><td>${esc((row.tiers||[]).map(tier).join(" / "))}</td></tr>`).join(""):'<tr><td colspan="7">尚无用量记录</td></tr>';
    const mix=Object.entries(a.target?.mix||{}).sort((x,y)=>y[1]-x[1]);
    $("mixRows").innerHTML=mix.length?mix.map(([key,share])=>{const at=key.lastIndexOf("|");return `<div class="mix-item"><span class="mix-name">${esc(model(key.slice(0,at)))} <span class="quiet">· ${esc(tier(key.slice(at+1)))}</span></span><span class="mix-value">${pct(share)}</span><div class="mix-track"><span class="mix-fill" data-share="${Math.max(0,Math.min(1,share))}"></span></div></div>`;}).join(""):'<p class="empty">近期没有足够请求识别当前模型构成。</p>';
    document.querySelectorAll(".mix-fill").forEach(element=>{element.style.width=`${Number(element.dataset.share)*100}%`;});
    const unknownShare=mix.filter(([key])=>key.endsWith("|unknown")).reduce((sum,[,value])=>sum+value,0);
    text("mixDetail",`近期输入缓存命中约 ${pct(a.target?.cache_ratio)}。${unknownShare?`${pct(unknownShare)} 的记录档位未记录。`:""}占比只描述原生请求构成，不代表额度份额。`);
    const days=(a.daily_usage||data.series?.daily||[]).slice(-30),daily=options();daily.xAxis={...daily.xAxis,type:"category",data:days.map(row=>row.day.slice(5))};daily.yAxis.axisLabel.formatter=compact;
    daily.series=[{id:"uncached",name:"未缓存输入",type:"bar",stack:"input",data:days.map(row=>Math.max(0,row.input_tokens-row.cached_input_tokens))},{id:"cached",name:"缓存输入",type:"bar",stack:"input",data:days.map(row=>row.cached_input_tokens)},{id:"output",name:"输出",type:"bar",data:days.map(row=>row.output_tokens)}];chart("dailyChart",daily);
    const hourly=(a.hourly_usage||[]).filter(row=>Date.parse(row.hour)>=Date.parse(data.generated_at)-48*3600000),totals=new Map(),grouped=new Map();
    hourly.forEach(row=>{totals.set(row.model,(totals.get(row.model)||0)+row.total_tokens);grouped.set(`${row.hour}\0${row.model}`,(grouped.get(`${row.hour}\0${row.model}`)||0)+row.total_tokens);});
    const models=[...totals].sort((x,y)=>y[1]-x[1]).slice(0,6).map(row=>row[0]),hours=[...new Set(hourly.map(row=>row.hour))].sort(),byModel=options();byModel.yAxis.axisLabel.formatter=compact;
    byModel.series=models.map(name=>line(name,model(name),hours.map(hour=>[hour,grouped.get(`${hour}\0${name}`)||0]),{lineStyle:{width:1.7}}));chart("modelChart",byModel);
  }
  function renderConsumption() {
    const artifact=m2Data(),evidence=artifact.evidence||{},candidates=artifact.candidates||[],cycle=artifact.current_cycle||{},current=cycle.current||{},points=cycle.points||[];
    const status=m2StatusName(artifact.status),explainedCandidates=candidates.filter(item=>item.status==='feasible'||item.status==='approximate').length,fit=artifact.fit_error||{};
    const residual=valid(fit.constraint_slack_sum_pp)?`；严格约束总残差 ${n(fit.constraint_slack_sum_pp,3)} 个百分点`:'';
    text('consumptionSummary',`M2 · ${status}。${n(evidence.request_count)} 次原生请求与 ${n(evidence.compressed_observation_count)} 个权威观测共同形成各模型的大致消耗参数${residual}。`);
    text('consumptionRawError',status);text('consumptionMeterError',`${explainedCandidates} / ${candidates.length||3}`);text('consumptionHit',n(evidence.request_count));text('consumptionTarget',n(evidence.period_count));
    text('consumptionDrift',`输入截止 ${date(artifact.input_cutoff)}；${n(evidence.raw_observation_count)} 个权威快照压缩为 ${n(evidence.compressed_observation_count)} 个约束点。按“目标机器记录为主要可观测解释”计算，未观测活动与时序、取整差异计入残差。`);
    text('resetAccountingSummary',cycle.period_start?`${date(cycle.period_start)} 开始的当前周期 · ${m2StatusName(cycle.status)}；曲线由同一套 M2 解释参数计算。`:`M2 当前周期：${m2StatusName(cycle.status)}`);
    const explained=current.explained_cycle_used_pp??current.explained_used_pp,observed=current.observed_cycle_used_pp??current.actual_used_pp,bandLow=current.explained_cycle_lower_pp??current.explained_lower_pp??current.compatible_lower_pp,bandHigh=current.explained_cycle_upper_pp??current.explained_upper_pp??current.compatible_upper_pp;
    text('resetExpected',valid(explained)?`${n(explained,2)} 百分点${valid(bandLow)&&valid(bandHigh)?`（对齐 ${n(bandLow,2)}–${n(bandHigh,2)}）`:''}`:'—');text('resetObserved',valid(observed)?n(observed,1)+' 百分点':'—');text('resetResidual',valid(current.fit_residual_pp)?`${Number(current.fit_residual_pp)>=0?'+':''}${n(current.fit_residual_pp,2)} 百分点`:'—');text('resetRatio',cycle.through?date(cycle.through):'—');
    const value=(row,key,fallback)=>row[key]??row[fallback],chartOption=options();chartOption.yAxis={...chartOption.yAxis,name:'累计扣量（百分点）',nameTextStyle:{align:'left'},min:0};
    chartOption.series=[line('reset-observed','服务器实测',points.map(row=>[row.time,value(row,'observed_cycle_used_pp','actual_used_pp')]),{step:'end',itemStyle:{color:'#76c6b8'},lineStyle:{color:'#76c6b8',width:2}}),line('m2-explained','按解释得到的曲线',points.filter(row=>valid(value(row,'explained_cycle_used_pp','explained_used_pp'))).map(row=>[row.time,value(row,'explained_cycle_used_pp','explained_used_pp')]),{itemStyle:{color:'#8ab4ef'},lineStyle:{color:'#8ab4ef',width:2.4}}),line('m2-lower','对齐敏感下界',points.filter(row=>valid(row.explained_cycle_lower_pp??row.explained_lower_pp??row.compatible_lower_pp)).map(row=>[row.time,row.explained_cycle_lower_pp??row.explained_lower_pp??row.compatible_lower_pp]),{itemStyle:{color:'#e4b979'},lineStyle:{type:'dashed',color:'#e4b979',width:1.7}}),line('m2-upper','对齐敏感上界',points.filter(row=>valid(row.explained_cycle_upper_pp??row.explained_upper_pp??row.compatible_upper_pp)).map(row=>[row.time,row.explained_cycle_upper_pp??row.explained_upper_pp??row.compatible_upper_pp]),{itemStyle:{color:'#b4a4da'},lineStyle:{type:'dashed',color:'#b4a4da',width:1.7}})];chart('resetAccountingChart',chartOption);
    $('consumptionRows').innerHTML=points.slice().reverse().map(row=>`<tr><td>${date(row.time)}</td><td>${valid(value(row,'explained_cycle_used_pp','explained_used_pp'))?n(value(row,'explained_cycle_used_pp','explained_used_pp'),2):'—'}</td><td>${valid(row.explained_cycle_lower_pp??row.explained_lower_pp??row.compatible_lower_pp)?n(row.explained_cycle_lower_pp??row.explained_lower_pp??row.compatible_lower_pp,2):'—'}</td><td>${valid(row.explained_cycle_upper_pp??row.explained_upper_pp??row.compatible_upper_pp)?n(row.explained_cycle_upper_pp??row.explained_upper_pp??row.compatible_upper_pp,2):'未限定'}</td><td>${n(value(row,'observed_cycle_used_pp','actual_used_pp'),1)}</td><td>${valid(row.fit_residual_pp)?`${Number(row.fit_residual_pp)>=0?'+':''}${n(row.fit_residual_pp,2)}`:'—'}</td></tr>`).join('')||'<tr><td colspan="6">等待 M2 当前周期证据</td></tr>';
  }
  function renderBudgetProfiles() { $("budgetProfile").value="compatibility";text("budgetProfileNote","估算仅使用你填写的 token 与当前 M2 解释参数；上下界表示时间对齐敏感性。");$("calculateBudget").disabled=!budgetModel()||!m2Data().quote_ready||state.budgetBusy; }
  function renderCalibration() {
    const artifact=m2Data(),models=artifact.model_parameters||[],evidence=artifact.evidence||{},candidates=artifact.candidates||[];
    const cell=item=>!item?"—":`<span class="calib-point">${valid(item.reference)?`约 ${n(item.reference,5)}`:'尚无解释点'}</span><small>${item.range_kind==='alignment_sensitivity_box'?'对齐敏感范围':'严格相容范围'} ${valid(item.lower)?n(item.lower,5):'—'}–${valid(item.upper)?n(item.upper,5):'未限定'}</small>`;
    $("calibrationRows").innerHTML=models.length?models.map(item=>`<tr><td class="model-id">${esc(model(item.model))}</td>${["uncached_input","cached_input","output"].map(key=>`<td class="calib-cell">${cell(item.channels?.[key])}</td>`).join("")}<td>${n(evidence.compressed_observation_count)} 个约束</td><td>M2 ${m2StatusName(artifact.status)}</td></tr>`).join(""):'<tr><td colspan="6">M2 尚未形成可展示的参数集合</td></tr>';
    text("calibrationSummary",`M2 当前状态：${m2StatusName(artifact.status)}。参考值取最佳解释，对齐敏感范围来自固定 −120、0、120 秒候选；严格检验状态为 ${m2StatusName(artifact.strict_status)}。`);text("calibrationScope","appserver_account_rate_limits · codex:primary");text("calibrationPeriod",date(artifact.input_cutoff));
    text("calibrationRank",candidates.map(item=>`${item.alignment_offset_seconds} 秒：${m2StatusName(item.status)}${item.strict_status&&item.strict_status!==item.status?`（严格检验 ${m2StatusName(item.strict_status)}）`:''}`).join("；")||"—");text("calibrationValidation",`${n(evidence.request_count)} 次请求`);text("calibrationBaselines",`${n(evidence.raw_observation_count)} → ${n(evidence.compressed_observation_count)} 个观测`);text("calibrationTolerance",artifact.evidence_mode==='reconstructed'?'重建证据；到达时钟仍需升级':'严格事前证据');
    if(!state.budgetModel)state.budgetModel=models.find(item=>item.model==="gpt-5.6-luna")?.model||models[0]?.model||"";
    $("budgetModel").innerHTML=models.map(item=>`<option value="${esc(item.model)}">${esc(model(item.model))}</option>`).join("")||'<option value="">暂无可用模型</option>';$("budgetModel").value=state.budgetModel;renderBudgetProfiles();
    if(state.budgetResultId&&artifact.m2_id!==state.budgetResultId)text("budgetRange","M2 证据已更新；保留了你的输入，选择计算可更新预算。");
  }
  async function calculateBudget() {
    if(state.budgetBusy||!budgetModel())return;$("budgetError").classList.add("hidden");
    const revision=state.budgetRevision,values=["budgetCalls","budgetUncached","budgetCached","budgetOutput"].map(id=>$(id).value);
    if(values.some(value=>String(value??"").trim()===""||!Number.isFinite(Number(value))||Number(value)<0||Number(value)>1e9)){text("budgetError","请填写0至10亿之间的有限非负数。");$("budgetError").classList.remove("hidden");return;}
    const [calls,uncached_input,cached_input,output]=values.map(Number);state.budgetBusy=true;$("calculateBudget").disabled=true;const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
    try{const response=await fetch("./api/forecast-v2/m2/quote",{method:"POST",headers:{"Content-Type":"application/json"},signal:controller.signal,body:JSON.stringify({items:[{model:state.budgetModel,calls,uncached_input,cached_input,output}]})});const result=await response.json();if(!response.ok)throw Error(result.error||result.message||`预算读取失败 HTTP ${response.status}`);if(revision!==state.budgetRevision)return;state.budgetResultId=result.m2_id;text("budgetEstimate",result.estimate_pp<=1e-10?"M2 解释点触及零边界，不能理解为免费":`按解释估算约 ${n(result.estimate_pp,5)} 个百分点`);text("budgetRange",`时间对齐敏感范围：${n(result.lower_pp,5)}–${valid(result.upper_pp)?n(result.upper_pp,5):"尚无有限上界"} 个百分点。只计填写的调用 token；有误差，且范围不是概率区间。`);}catch(error){if(revision===state.budgetRevision){text("budgetError",error.name==="AbortError"?"预算计算超时，请稍后重试。":error.message);$("budgetError").classList.remove("hidden");}}finally{clearTimeout(timeout);state.budgetBusy=false;$("calculateBudget").disabled=!budgetModel();}
  }
  function downloadJson(filename,value){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:"application/json"})),anchor=document.createElement("a");anchor.href=url;anchor.download=filename;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function render() {
    if (!state.data) return;
    renderM3Forecast(state.data.adaptive||{});
    ({overview: renderOverview, usage: renderUsage}[state.view])();
    text("generated", `快照 ${date(state.data.generated_at)} · 北京时间`);
  }
  function selectView(view) {
    if (!names[view]) return; state.view = view;
    document.querySelectorAll(".view").forEach(e => e.classList.toggle("hidden", e.id !== `view-${view}`));
    document.querySelectorAll("[data-view]").forEach(e => { e.classList.toggle("active", e.dataset.view === view); if (e.dataset.view === view) e.setAttribute("aria-current", "page"); else e.removeAttribute("aria-current"); });
    text("viewTitle", names[view]); history.replaceState(null, "", `#${view}`); render();
    requestAnimationFrame(resizeCharts);
  }
  async function digest(bytes){return [...new Uint8Array(await crypto.subtle.digest("SHA-256",bytes))].map(value=>value.toString(16).padStart(2,"0")).join("");}
  async function decode(bytes){return JSON.parse(await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).text());}
  async function load() {
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),30000);
    const get=async path=>{const response=await fetch(path,{cache:"no-store",signal:controller.signal});if(!response.ok)throw new Error(`读取失败 HTTP ${response.status}`);return response;};
    try {
      let incoming;
      const api=await fetch(`./api/dashboard?t=${Date.now()}`,{cache:"no-store",signal:controller.signal});
      if(api.ok){const result=await api.json();if(!result.snapshot?.adaptive)throw new Error("额度快照尚未就绪，请稍后刷新");incoming={data:result.snapshot,sourceMode:result.mode,privacy:result.privacy,offline:false};}
      else if(api.status===404){const pointer=await (await get(`./control/latest.json?t=${Date.now()}`)).json(),bytes=await (await get(`./${pointer.path}?h=${pointer.sha256}`)).arrayBuffer();if(await digest(bytes)!==pointer.sha256)throw new Error("数据校验未通过，请稍后刷新");const data=await decode(bytes);if(!data.adaptive)throw new Error("额度快照尚未就绪，请稍后刷新");incoming={data,pointer,sourceMode:"host_adapter",privacy:"local_state_not_exposed",offline:false};}
      else throw new Error(`读取失败 HTTP ${api.status}`);
      try{if("caches" in window){const cache=await caches.open("codex-quota-system-v31");await cache.put("./last-view",new Response(JSON.stringify(incoming),{headers:{"content-type":"application/json"}}));}}catch{/* 缓存失败不能隐藏新快照。 */}
      return incoming;
    } catch(error) {
      try{if("caches" in window){const cached=await (await caches.open("codex-quota-system-v31")).match("./last-view");if(cached)return {...await cached.json(),offline:true};}}catch{/* 保留原始读取错误。 */}
      throw error;
    } finally {clearTimeout(timeout);}
  }
  async function refresh() {
    if(state.rulerDragging||state.rangeSwitching){state.refreshQueued=true;return;}
    if (state.busy) return; state.busy = true; $("refresh").disabled = true; $("error").classList.add("hidden");
    const revision=state.rangeRevision||0;
    try {
      const incoming=await load();if(state.rulerDragging||state.rangeSwitching||revision!==(state.rangeRevision||0)){state.deferredSnapshot=incoming;return;}Object.assign(state,incoming); await loadQuotaRange();if(state.rangeSwitching||revision!==(state.rangeRevision||0))return; $("offline").classList.toggle("hidden", !state.offline);
      const age = (Date.now() - Date.parse(state.data.generated_at)) / 1000, stale = age > 180;
      $("freshness").className = `status-label ${state.offline || stale ? "warn" : "good"}`;
      text("freshness", state.offline ? "离线数据" : stale ? "数据已过期" : "数据已更新");
      text('modeBadge','M1–M3 本地系统');$('modeBadge').className='mode-badge live';text('sourceMode','页面只读冻结快照');render();
    } catch (error) { $("freshness").className = "status-label bad"; text("freshness", "读取失败"); text("error", `${error.name === "AbortError" ? "读取超时，将在下一分钟重试。" : error.message}`); $("error").classList.remove("hidden"); }
    finally { state.busy = false; $("refresh").disabled = false; }
  }
  document.querySelectorAll("[data-view]").forEach(e => e.addEventListener("click", () => selectView(e.dataset.view)));
  $('rangePreset').addEventListener('change',()=>quotaPreset($('rangePreset').value));
  bindRulers();
  $('quotaResetZoom').addEventListener('click',()=>{if(!state.quotaSelectionBase)return;const range={...state.quotaSelectionBase,anchor:'fixed',span:null};state.quotaSelectionBase=null;selectRange(range);});
  $('quotaChart').addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Enter','Escape'].includes(event.key))return;event.preventDefault();if(event.key==='Escape'){state.quotaSelectedTime=null;charts.get('quotaChart')?.dispatchAction({type:'hideTip'});return;}const r=state.timeRange,step=event.shiftKey?3600000:60000;const t=Math.max(r.start,Math.min(r.end,(state.quotaSelectedTime??r.start)+(event.key==='ArrowRight'?step:event.key==='ArrowLeft'?-step:0)));showRuntimeDetails(t);const c=charts.get('quotaChart');if(c){const b=$('quotaChart').getBoundingClientRect();state.plotCursor={x:b.left+c.convertToPixel({xAxisIndex:0},t),y:b.top+b.height*.45};restorePlotPointer();}});
  $("refresh").addEventListener("click", refresh);
  $("budgetModel").addEventListener("change",event=>{state.budgetModel=event.target.value;state.budgetRevision++;state.budgetResultId=null;renderBudgetProfiles();text("budgetEstimate","模型已更改，请重新计算");});
  $("budgetProfile").addEventListener("change",renderBudgetProfiles);
  for(const id of ["budgetCalls","budgetUncached","budgetCached","budgetOutput"])$(id).addEventListener("input",()=>{state.budgetRevision++;state.budgetResultId=null;text("budgetEstimate","输入已更改，请重新计算");});
  $("calculateBudget").addEventListener("click",calculateBudget);
  $("exportM2").addEventListener("click",()=>downloadJson("codex-quota-m2.json",m2Data()));
  $("exportM3").addEventListener("click",()=>downloadJson("codex-quota-m3.json",m3Data()));
  $("exportData").addEventListener("click",()=>state.data&&downloadJson(`codex-quota-${new Date().toISOString().slice(0,10)}.json`,state.data));
  window.addEventListener("resize", resizeCharts);
  window.addEventListener("hashchange", () => selectView(location.hash.slice(1)));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  setInterval(refresh, 60000); setInterval(countdown, 30000);setInterval(updateNowMarker,1000);
  selectView(location.hash.slice(1) in names ? location.hash.slice(1) : "overview"); refresh();
})();
