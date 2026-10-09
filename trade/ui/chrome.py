"""
trade/ui/chrome.py — everything around the tabs: page-wide CSS, tab memory across reruns, the notification envelope,
the Cash / Portfolio / API-left readout, and the look-ahead box pinned into the top bar.

The CSS/JS are kept as unindented module constants on purpose: indenting multi-line markup inside a function would
change the strings (and st.markdown treats 4-space-indented lines as code blocks).
"""
import streamlit as st
import streamlit.components.v1 as components

import config
from trade.clv import start_background as _start_clv_capture
from trade.core.balance_feed import start as _start_balance_feed

# The live dashboard's countdown timer reruns every 1s, its positions table
# every 10s, and Review every 30s (see @st.fragment usage below) — Streamlit's
# built-in top-right "running" indicator would
# otherwise be flashing almost constantly during an active session. There's
# no config option for this (it's core UX plumbing), only its own testid.
# Buttons also get shorter padding here — Streamlit's default button height
# reads as noticeably tall/clunky once there are several in a row.
PAGE_CSS = """
<style>
[data-testid="stStatusWidget"] { display: none; }
button[data-testid^="stBaseButton-"] {
    padding-top: 0.28rem;
    padding-bottom: 0.28rem;
}
/* Button-only rows keyed "_btnrow_*" (st.container(key=...)) — a wide leading
   spacer column pushes the buttons to the right edge, and this fixes the gap
   between them at 15px regardless of Streamlit's column-count gap presets. */
div[class*="st-key-_btnrow_"] [data-testid="stHorizontalBlock"] {
    gap: 15px;
}
/* ✕ panel-close buttons — no button pill (transparent, borderless), a
   smaller glyph, pushed flush to the right edge of their column. */
/* Inner tabs (Trade | Review) sit on the same row, to the right of the outer tabs (Pinnacle | Market Making) */
[data-baseweb="tab-panel"] [data-baseweb="tab-list"] {
    margin-top: -58px; margin-left: 200px; width: calc(100% - 200px);
    position: relative; z-index: 2;
}
[data-baseweb="tab-panel"] [data-baseweb="tab-list"] [data-baseweb="tab-border"] { display: none; }
/* No dead space above the tabs: less top padding, and the zero-height tab-memory script frame takes no room */
[data-testid="stMainBlockContainer"] { padding-top: 3.5rem !important; }
/* Market Making ON / Cancel all buttons: same height as the input boxes beside them */
div[class*="st-key-_mm_onoff"] button, div[class*="st-key-_mm_cancel_all"] button { min-height: 38px; height: 38px; position: relative; top: -1px; }
[data-testid="stElementContainer"]:has(> iframe[data-testid="stIFrame"]) {
    position: absolute; width: 0; height: 0; margin: 0; overflow: hidden;
}
div[class*="st-key-_close_inspect"] button,
div[class*="st-key-_close_calib_detail"] button,
div[class*="st-key-_close_bar_"] button {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 0.2rem !important;
    font-size: 0.75rem;
    float: right;
}
/* Streamlit's own Deploy button and "..." menu — replaced by the notification envelope injected
   below, in the same corner. Targeted narrowly (not the whole stToolbar) so the "expand sidebar"
   control, which lives in that same header strip once the sidebar is collapsed, keeps working. */
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"] { display: none !important; }
</style>
"""

# Remember the selected tab in every tab bar (per browser tab) and put it back if a rerun resets it to the first one.
# Streamlit re-mounts the tab bars on some reruns (the Market Making ON/OFF click did), which snaps the page back to
# Pinnacle → Trade. Same-origin script; no effect on anything else.
TAB_MEMORY_JS = """<script>
(function(){
  const P=window.parent, D=P.document, KEY='tabmem_v1';
  if(P.__tabmem) return; P.__tabmem=true;
  const store=()=>{try{return JSON.parse(P.sessionStorage.getItem(KEY)||'{}')}catch(e){return {}}};
  const put=o=>{try{P.sessionStorage.setItem(KEY,JSON.stringify(o))}catch(e){}};
  const lists=()=>[...D.querySelectorAll('[data-baseweb="tab-list"]')];
  const sig=l=>lists().indexOf(l)+':'+[...l.querySelectorAll('[data-baseweb="tab"]')].map(t=>t.innerText.trim()).join('|');
  D.addEventListener('click',e=>{const t=e.target.closest('[data-baseweb="tab"]'); if(!t)return;
    const l=t.closest('[data-baseweb="tab-list"]'); if(!l)return; const s=store(); s[sig(l)]=t.innerText.trim(); put(s)},true);
  function restore(){
    const s=store();
    lists().forEach(l=>{const want=s[sig(l)]; if(!want)return;
      const cur=l.querySelector('[data-baseweb="tab"][aria-selected="true"]'); if(cur&&cur.innerText.trim()===want)return;
      const tgt=[...l.querySelectorAll('[data-baseweb="tab"]')].find(t=>t.innerText.trim()===want); if(tgt)tgt.click();});
  }
  new MutationObserver(()=>{clearTimeout(P.__tabmemT);P.__tabmemT=setTimeout(restore,150)})
    .observe(D.body,{subtree:true,childList:true,attributes:true,attributeFilter:['aria-selected']});
})();
</script>"""

# Notification center: replaces Streamlit's Deploy button / "..." menu (hidden above) with an envelope in the same
# corner. Fill/close events from the Market Making and Pinnacle boards call window.parent.__notifCenter.push(html)
# to pop the usual 5s toast AND log it here — the envelope keeps a scrollable history of everything that already
# disappeared off-screen, with a red dot while there's anything you haven't opened the panel to see yet.
NOTIFICATIONS_JS = """<script>
(function(){
  const P=window.parent, D=P.document;
  const VER=2;   // bump when this changes, so an already-open tab rebuilds instead of keeping the old one
  if(P.__notifCenter && P.__notifCenter.ver===VER) return;   // idempotent across reruns — build the DOM once
  if(P.__notifCenter){ ['nc-bell','nc-panel'].forEach(id=>{const el=D.getElementById(id); if(el) el.remove()}); }
  const css=D.createElement('style');
  css.textContent=`
    #nc-bell{position:fixed;top:8px;right:14px;z-index:999999;cursor:pointer;width:34px;height:34px;
      display:flex;align-items:center;justify-content:center;border-radius:8px;font-size:18px;color:#c9cdd6;
      user-select:none;background:rgba(255,255,255,.04)}
    #nc-bell:hover{background:rgba(255,255,255,.1)}
    #nc-dot{position:absolute;top:4px;right:4px;width:9px;height:9px;border-radius:50%;background:#e74c3c;
      box-shadow:0 0 0 2px #0e1117;display:none}
    #nc-panel{position:fixed;top:46px;right:14px;z-index:999999;width:360px;max-height:70vh;overflow-y:auto;
      background:#161a23;border:1px solid #2a2f3a;border-radius:8px;box-shadow:0 12px 30px rgba(0,0,0,.55);
      display:none;padding:4px 0}
    #nc-panel .nc-item{padding:8px 12px;border-bottom:1px solid #2a2f3a;font:12px/1.4 system-ui,sans-serif;color:#e6e6e6}
    #nc-panel .nc-item:last-child{border-bottom:none}
    #nc-panel .nc-empty{padding:18px;text-align:center;color:#8b93a1;font:12px system-ui,sans-serif}
    #nc-panel .nc-time{color:#8b93a1;font-size:10px;margin-top:3px}
    #nc-panel .nc-item.go{cursor:pointer}
    #nc-panel .nc-item.go:hover{background:rgba(255,255,255,.05)}
  `;
  D.head.appendChild(css);

  const bell=D.createElement('div'); bell.id='nc-bell'; bell.title='Notifications';
  bell.innerHTML='\u2709<span id="nc-dot"></span>';
  const panel=D.createElement('div'); panel.id='nc-panel';
  D.body.appendChild(bell); D.body.appendChild(panel);

  const dot=bell.querySelector('#nc-dot');
  const state={items:[], unread:0};
  function renderPanel(){
    panel.innerHTML = state.items.length
      ? state.items.map((it,i)=>`<div class="nc-item${it.meta&&it.meta.ticker?' go':''}" data-i="${i}"${it.meta&&it.meta.ticker?' title="Go to this market"':''}>${it.html}<div class="nc-time">${new Date(it.ts).toLocaleTimeString()}</div></div>`).join('')
      : '<div class="nc-empty">No notifications yet</div>';
  }
  function setUnread(n){ state.unread=n; dot.style.display = n>0 ? 'block' : 'none'; }

  bell.addEventListener('click', e=>{
    e.stopPropagation();
    const opening = panel.style.display!=='block';
    panel.style.display = opening ? 'block' : 'none';
    if(opening) setUnread(0);   // seeing the list clears the marker, whether or not it's scrolled through
  });
  D.addEventListener('click', ()=>{panel.style.display='none'});
  panel.addEventListener('click', e=>{
    e.stopPropagation();
    const row=e.target.closest('.nc-item.go'); if(!row) return;
    const it=state.items[+row.dataset.i]; if(!it||!it.meta) return;
    panel.style.display='none';
    P.__jumpToCard(it.meta);
  });

  // ── jump to a market's card: open its board's Trade tab, then scroll the card to the middle of the screen ──
  const BOARD_TABS={pin:'Pinnacle', mm:'Market Making'};
  const visible=el=>!!(el && el.offsetParent!==null);
  function clickTab(label){
    for(const l of D.querySelectorAll('[data-baseweb="tab-list"]')){
      if(!visible(l)) continue;
      const t=[...l.querySelectorAll('[data-baseweb="tab"]')].find(x=>x.innerText.trim()===label);
      if(t){ if(t.getAttribute('aria-selected')!=='true') t.click(); return true; }
    }
    return false;
  }
  function scroller(el){       // nearest scrollable ancestor in the page (Streamlit scrolls an inner container)
    for(let n=el.parentElement; n; n=n.parentElement){
      const oy=P.getComputedStyle(n).overflowY;
      if((oy==='auto'||oy==='scroll') && n.scrollHeight>n.clientHeight) return n;
    }
    return D.scrollingElement || D.documentElement;
  }
  function findCard(meta){
    for(const f of D.querySelectorAll('iframe')){
      if(!visible(f)) continue;
      let w; try{ w=f.contentWindow; if(!w || !w.__showCard) continue; }catch(e){ continue; }   // only our boards
      const card=w.document.querySelector(`[data-t="${CSS.escape(meta.ticker)}"]`);
      if(card) return {f, w};
    }
    return null;
  }
  P.__jumpToCard=function(meta){
    if(!meta || !meta.ticker) return;
    clickTab(BOARD_TABS[meta.board]||'Pinnacle');
    let tries=0;
    (function step(){
      clickTab('Trade');                                    // the inner tab, once the board's panel is showing
      const hit=findCard(meta);
      if(!hit){ if(++tries<30) setTimeout(step,100); return; }   // tab panels can take a moment to render
      const card=hit.w.__showCard(meta.ticker);
      if(!card) return;
      setTimeout(()=>{                                      // after a collapse re-layout, measure and scroll
        const fr=hit.f.getBoundingClientRect(), cr=card.getBoundingClientRect();
        const delta=fr.top+cr.top+cr.height/2-P.innerHeight/2;
        const sc=scroller(hit.f);
        if(sc===D.scrollingElement || sc===D.documentElement) P.scrollBy({top:delta, behavior:'smooth'});
        else sc.scrollBy({top:delta, behavior:'smooth'});
      }, 60);
    })();
  };

  P.__notifCenter = {
    ver: VER,
    push(html, meta){
      state.items.unshift({html, meta: meta||null, ts: Date.now()});
      state.items.length = Math.min(state.items.length, 50);
      setUnread(state.unread+1);
      renderPanel();
    }
  };
  renderPanel();
})();
</script>"""

# Live Cash / Portfolio readout, just left of the envelope — polled from trade/core/balance_feed.py's background
# thread over its own loopback channel (the browser has no Kalshi credentials, so it can't ask Kalshi directly).
# Uses the same Worker-based poller as the Market Making board's snapshot poll so it keeps refreshing in a background tab.
# Portfolio = cash + every open position marked to market (settled-but-unpaid positions at their 1/0 payout).
# Clicking it opens the per-position breakdown (Kalshi's cost basis vs. the current mark).
# The DOM + Worker are built once per browser page, but the feed's port/token are re-sent on EVERY rerun: restarting
# `streamlit run` (or a runOnSave reload that restarts the feed) gives the feed a new port/token while the browser tab
# reconnects without a page reload — without the re-send the old Worker kept polling a dead port and the numbers
# silently froze. Values dim and the tooltip says so if the feed hasn't produced a fresh read in STALE_SEC.
# (Port/token are spliced in with plain .replace() — not an f-string — since the JS below is full of the
# same { } characters an f-string would need doubled everywhere.)
BALANCE_WIDGET_JS = """<script>
(function(){
  const P=window.parent, D=P.document;
  const cfg={port:__PORT__, token:'__TOKEN__', host:'127.0.0.1'};   // components.html renders via a srcdoc iframe, whose location.hostname is empty
  const VER=3;   // bump when this widget's DOM changes, so an already-open tab rebuilds instead of keeping the old one
  if(P.__balFeed && P.__balFeed.ver===VER){ P.__balFeed.setCfg(cfg); return; }
  if(P.__balFeed){   // an older build of this widget, still alive in an already-open tab — tear it down and rebuild
    try{ P.__balFeed.terminate && P.__balFeed.terminate() }catch(e){}
    try{ P.__balFeed.stop && P.__balFeed.stop() }catch(e){}
    ['bal-wrap','pos-panel'].forEach(id=>{ const el=D.getElementById(id); if(el) el.remove() });
  }
  const STALE_SEC=45;
  const css=D.createElement('style');
  css.textContent=`
    #bal-wrap{position:fixed;top:8px;right:56px;z-index:999999;display:flex;gap:8px;height:34px}
    .bal-box{background:rgba(255,255,255,.04);border-radius:8px;padding:0 14px;text-align:center;min-width:74px;
      height:34px;box-sizing:border-box;display:flex;flex-direction:column;align-items:center;justify-content:center}
    .bal-box .v{font:700 15px/1.3 system-ui,sans-serif;color:#e6e6e6}
    .bal-box .l{font:500 10px/1.2 system-ui,sans-serif;color:#8b93a1;text-transform:uppercase;letter-spacing:.03em}
    .bal-box.port{cursor:pointer}
    .bal-box.use{position:relative;overflow:hidden}
    .bal-box.use .bar{position:absolute;left:0;right:0;bottom:0;height:3px;background:rgba(255,255,255,.08)}
    .bal-box.use .bar i{display:block;height:100%;width:0;background:#e74c3c}
    .bal-box.port .l{color:#2ecc71}
    #bal-wrap.stale .v{opacity:.45}
    #bal-wrap.stale .l{color:#e67e22 !important}
    #pos-panel{position:fixed;top:46px;right:56px;z-index:999999;width:300px;max-height:60vh;overflow-y:auto;
      background:#161a23;border:1px solid #2a2f3a;border-radius:8px;box-shadow:0 12px 30px rgba(0,0,0,.55);
      display:none;padding:4px 0}
    #pos-panel .pos-sum{padding:8px 12px;border-bottom:1px solid #2a2f3a;font:11px/1.5 system-ui,sans-serif;color:#8b93a1}
    #pos-panel .pos-sum b{color:#e6e6e6;font-weight:600}
    #pos-panel .pos-item{padding:7px 12px;border-bottom:1px solid #2a2f3a;font:12px/1.4 system-ui,sans-serif;
      color:#e6e6e6;display:flex;justify-content:space-between;gap:10px}
    #pos-panel .pos-item:last-child{border-bottom:none}
    #pos-panel .pos-tkr{color:#8b93a1;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:170px}
    #pos-panel .pos-empty{padding:16px;text-align:center;color:#8b93a1;font:12px system-ui,sans-serif}
    #pos-panel .pos-tag{font-size:9px;padding:1px 5px;border-radius:4px;margin-left:5px;background:#2a2f3a;color:#c9cdd6}
    .pos-pos{color:#2ecc71}.pos-neg{color:#e74c3c}
  `;
  D.head.appendChild(css);
  const wrap=D.createElement('div'); wrap.id='bal-wrap';
  wrap.innerHTML='<div class="bal-box use" title="Odds API requests left this month"><div class="v">__DASH__</div><div class="l">API left</div><div class="bar"><i></i></div></div>'+
                 '<div class="bal-box cash"><div class="v">__DASH__</div><div class="l">Cash</div></div>'+
                 '<div class="bal-box port"><div class="v">__DASH__</div><div class="l">Portfolio</div></div>';
  const panel=D.createElement('div'); panel.id='pos-panel';
  D.body.appendChild(wrap); D.body.appendChild(panel);
  // Let the look-ahead box (a Streamlit widget pinned into this bar by web_app.py's CSS) sit just left of these boxes.
  const placeCfg=()=>D.documentElement.style.setProperty('--topcfg-right', (56+wrap.offsetWidth+8)+'px');
  try{ new P.ResizeObserver(placeCfg).observe(wrap) }catch(e){}
  placeCfg();
  const useV=wrap.querySelector('.use .v'), useBar=wrap.querySelector('.use .bar i'), useBox=wrap.querySelector('.use');
  const cashV=wrap.querySelector('.cash .v'), portV=wrap.querySelector('.port .v'), portBox=wrap.querySelector('.port');
  const fmt=v=>v==null?'__DASH__':'$'+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  let last=null, lastOk=0;   // last snapshot received, and when (ms) the browser last reached the feed at all
  function renderPositions(d){
    const list=d.positions||[];
    const sum='<div class="pos-sum">Cash <b>'+fmt(d.cash)+'</b> + Positions <b>'+fmt(d.positions_value)+'</b> = <b>'+fmt(d.portfolio)+'</b></div>';
    panel.innerHTML = sum + (list.length ? list.map(p=>{
      const u=p.unrealized_dollars, cls=u>0?'pos-pos':(u<0?'pos-neg':'');
      const tag=p.result ? '<span class="pos-tag">settled '+String(p.result).toUpperCase()+'</span>'
                         : (p.status&&p.status!=='active' ? '<span class="pos-tag">'+p.status+'</span>' : '');
      return '<div class="pos-item"><div><div>'+p.side+' ×'+p.qty+tag+'</div><div class="pos-tkr">'+p.ticker+'</div></div>'+
        '<div style="text-align:right"><div>'+fmt(p.mark_dollars)+'</div><div class="'+cls+'">'+(u>=0?'+':'')+u.toFixed(2)+'</div></div></div>';
    }).join('') : '<div class="pos-empty">No open positions</div>');
  }
  function refreshStale(){
    const now=Date.now();
    const fresh = last && last.updated_at && (now/1000 - last.updated_at) < STALE_SEC && (now - lastOk) < STALE_SEC*1000;
    wrap.classList.toggle('stale', !fresh);
    let tip;
    if(!last) tip='Waiting for the first balance read…';
    else if((now-lastOk) >= STALE_SEC*1000) tip='Can\\'t reach the balance feed — numbers may be out of date';
    else if(!fresh) tip='Kalshi read failing'+(last.error?' ('+last.error+')':'')+' — numbers may be out of date';
    else tip='Updated '+new Date(last.updated_at*1000).toLocaleTimeString();
    wrap.title = tip + ' · click Portfolio for the position breakdown';
  }
  function apply(d){
    if(!d)return;
    last=d; lastOk=Date.now();
    cashV.textContent=fmt(d.cash); portV.textContent=fmt(d.portfolio); renderPositions(d); refreshStale();
    if(d.api_remaining!=null){
      const used=d.api_used||0, lim=used+d.api_remaining, pct=lim?Math.min(100,100*used/lim):0;
      useV.textContent=Number(d.api_remaining).toLocaleString();
      useBar.style.width=pct+'%'; useBar.style.background=pct>=90?'#e74c3c':pct>=70?'#e67e22':'#2ecc71';
      useBox.title='Odds API: '+Number(d.api_remaining).toLocaleString()+' requests left ('+used.toLocaleString()+' of '+lim.toLocaleString()+' used)';
    }
  }
  setInterval(refreshStale, 5000);
  portBox.addEventListener('click', e=>{
    e.stopPropagation();
    panel.style.display = panel.style.display!=='block' ? 'block' : 'none';
  });
  D.addEventListener('click', ()=>{panel.style.display='none'});
  panel.addEventListener('click', e=>e.stopPropagation());

  const src = `
    let cfg=null;
    onmessage=(e)=>{ if(e.data.type==='cfg'){cfg=e.data.cfg; tick(); if(!self._t)self._t=setInterval(tick,5000)} };
    async function tick(){
      if(!cfg)return;
      try{
        const r=await fetch('http://'+cfg.host+':'+cfg.port+'/snap?token='+cfg.token, {cache:'no-store'});
        if(!r.ok)throw 0;
        postMessage(await r.json());
      }catch(e){}
    }`;
  let mainCfg=cfg;
  async function mainTick(){
    try{const r=await fetch('http://'+mainCfg.host+':'+mainCfg.port+'/snap?token='+mainCfg.token, {cache:'no-store'}); if(r.ok)apply(await r.json())}catch(err){}
  }
  try{
    const w=new Worker(URL.createObjectURL(new Blob([src],{type:'application/javascript'})));
    w.onmessage=(e)=>apply(e.data);
    w.postMessage({type:'cfg',cfg});
    P.__balFeed={ver:VER, setCfg:c=>w.postMessage({type:'cfg',cfg:c}), terminate:()=>w.terminate()};
  }catch(e){
    mainTick(); const iv=setInterval(mainTick,5000);
    P.__balFeed={ver:VER, setCfg:c=>{mainCfg=c; mainTick()}, stop:()=>clearInterval(iv)};
  }
  refreshStale();
})();
</script>"""

TOP_CONFIG_CSS = """<style>
/* pinned into the top bar; --topcfg-right is set by the Cash/Portfolio script to sit just left of its boxes */
.st-key-topcfg{position:fixed !important;top:8px;right:var(--topcfg-right,330px);z-index:999999;width:auto !important;gap:0}
.st-key-_hrs{width:84px !important;height:34px;background:rgba(255,255,255,.04);border-radius:8px;position:relative;
  padding-top:2px;box-sizing:border-box}
.st-key-_hrs [data-testid="stNumberInputContainer"], .st-key-_hrs [data-baseweb="input"],
.st-key-_hrs [data-baseweb="input"] > div{background:transparent !important;border:none !important;height:19px;min-height:0 !important}
.st-key-_hrs input{font:700 15px/1.3 system-ui,sans-serif !important;color:#e6e6e6 !important;text-align:center;
  padding:0 !important;height:19px;min-height:0 !important}
.st-key-_hrs [data-testid="stNumberInputStepUp"], .st-key-_hrs [data-testid="stNumberInputStepDown"],
.st-key-_hrs [data-testid="InputInstructions"]{display:none !important}
.st-key-_hrs::after{content:'Look-ahead';position:absolute;bottom:3px;left:0;right:0;text-align:center;pointer-events:none;
  font:500 10px/1.2 system-ui,sans-serif;color:#8b93a1;text-transform:uppercase;letter-spacing:.03em}
</style>"""


def render_head():
    """Page CSS, background threads (closing-line capture, balance feed) and the injected top-bar widgets."""
    st.markdown(PAGE_CSS, unsafe_allow_html=True)
    _start_clv_capture()                     # closing-line capture thread (idempotent across reruns)
    feed = _start_balance_feed()             # Cash/Portfolio poller (idempotent across reruns)
    components.html(TAB_MEMORY_JS, height=0)
    components.html(NOTIFICATIONS_JS, height=0)
    components.html(BALANCE_WIDGET_JS.replace('__PORT__', str(feed['port'])).replace('__TOKEN__', feed['token'])
                    .replace('__DASH__', '—'), height=0)


# ── Top-bar run config ───────────────────────────────────────────────────────
# (Replaces the old sidebar.) Look-ahead is the only setting left on screen: a compact number box pinned into the top
# bar just left of the API-usage / Cash / Portfolio readout, styled like those boxes (CSS below). Match threshold is
# fixed at 0.85 and live games at config.LIVE — readers fall back to those via session_state.get(..., default).
# A fragment, so editing the box reruns only itself; the trade tab reads `_hrs` fresh on each of its own runs.
@st.fragment
def _top_config():
    with st.container(key='topcfg'):
        st.number_input('Look-ahead', min_value=1, max_value=168, step=1, key='_hrs',
                        label_visibility='collapsed', help='Look-ahead window in hours')


def render_top_config():
    if '_hrs' not in st.session_state:
        st.session_state['_hrs'] = config.LOOKAHEAD_HRS
    _top_config()
    st.markdown(TOP_CONFIG_CSS, unsafe_allow_html=True)
