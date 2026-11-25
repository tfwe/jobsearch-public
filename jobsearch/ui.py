"""Shared design tokens, layout CSS and small markup helpers for the two screens.

One stylesheet for both pages, inline in the document: no build step, no CDN, no
external font (the stacks fall back to the system faces when Inter or JetBrains Mono
are not installed). Light is the default; dark follows the system unless the sidebar
toggle has stored a choice, which a tiny head script applies before first paint.

Everything here is presentation only. It never reads the store and never decides a
status or a score: the callers pass finished values in.
"""
from __future__ import annotations

import html as _html
from datetime import datetime, timezone

__all__ = [
    "esc", "page", "kbd", "score_pill", "flag", "bar", "card", "btn", "rel_age",
    "rel_phrase", "theme_toggle", "THEME_BOOT", "CSS", "NOSCRIPT_CSS",
]

LIGHT = """--bg:#f6f5f1;--bg2:#efede6;--bg3:#fbfaf7;--card:#ffffff;--ink:#1b1b1a;
--ink2:#3d3c38;--muted:#5f5e59;--line:#dedcd3;--line2:#cfcdc4;--line3:#e6e4dc;
--acc:#0f5f52;--onacc:#ffffff;--acc2:#bfe0d8;--s2t:#0b3d35;--warnb:#fde9d2;
--warnt:#7a3b00;--warnl:#f0c79a;--warnbar:#b4540a"""

DARK = """--bg:#121413;--bg2:#0d0f0e;--bg3:#171a19;--card:#1d211f;--ink:#ecebe6;
--ink2:#c9c7bf;--muted:#a3a29b;--line:#2a2e2c;--line2:#3d4340;--line3:#2a2e2c;
--acc:#4fb8a4;--onacc:#06201b;--acc2:#14403a;--s2t:#bfe9df;--warnb:#3a2412;
--warnt:#f2c08f;--warnl:#6a4522;--warnbar:#e8924a"""

# With scripting off, the panes the script would slide in have to be in the flow:
# otherwise a click on a row reloads the page with its preview rendered off-canvas.
NOSCRIPT_CSS = """<noscript><style>
@media (max-width:1100px){
  .app{grid-template-columns:232px minmax(0,1fr)}
  .preview{position:static;transform:none;width:auto;box-shadow:none;grid-column:1/-1;
  border-left:0;border-top:1px solid var(--line)}
  .pv-close{display:none}
  .scrim{display:none!important}
}
@media (max-width:800px){
  .app{grid-template-columns:minmax(0,1fr)}
  .sidebar{position:static;transform:none;width:auto;border-right:0;
  border-bottom:1px solid var(--line)}
  .side-toggle{display:none}
}
</style></noscript>"""

# Applied in <head> so a stored choice never flashes the wrong theme first.
THEME_BOOT = (
    "<script>try{var t=localStorage.getItem('jb-theme');"
    "if(t==='dark'||t==='light'){document.documentElement.setAttribute('data-theme',t);}}"
    "catch(e){}</script>"
)

CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{color-scheme:light;%LIGHT%;
--r:8px;--rl:12px;
--font:Inter,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,"DejaVu Sans Mono",Menlo,monospace}
[data-theme="dark"]{color-scheme:dark;%DARK%}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){color-scheme:dark;%DARK%}
}
html,body{height:100%}
/* Scrollbars follow the theme instead of the platform default (light on dark). */
*{scrollbar-color:var(--line2) transparent;scrollbar-width:thin}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--line2);border-radius:5px;
border:2px solid transparent;background-clip:content-box}
::-webkit-scrollbar-thumb:hover{background:var(--muted);background-clip:content-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.4 var(--font);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--acc)}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,
[tabindex]:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
button,input,select,textarea{font:inherit;color:inherit}
img,svg{display:block}
h1,h2,h3,p,ul,ol{margin:0}
ul{list-style:none;padding:0}
.mono,.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}
.sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
clip:rect(0 0 0 0);white-space:nowrap;border:0}
.skip{position:absolute;left:-9999px;top:0;background:var(--card);padding:10px;z-index:99}
.skip:focus{left:8px;top:8px}

/* ---------- controls ---------- */
.btn{min-height:44px;display:inline-flex;align-items:center;justify-content:center;gap:8px;
border-radius:var(--r);border:1px solid var(--line2);background:var(--card);color:var(--ink);
padding:0 14px;font-weight:500;cursor:pointer;text-decoration:none;white-space:nowrap}
.btn:hover{background:var(--bg3);border-color:var(--acc)}
.btn[disabled],.btn[aria-disabled="true"]{opacity:.5;cursor:not-allowed}
.btn-primary{background:var(--acc);border-color:var(--acc);color:var(--onacc);font-weight:600}
.btn-primary:hover{background:var(--s2t);border-color:var(--s2t);color:var(--bg3)}
[data-theme="dark"] .btn-primary:hover{background:var(--acc2);color:var(--s2t)}
.btn-sm{min-height:36px;font-size:12.5px;padding:0 10px}
.btn-ghost{background:transparent;border-color:transparent;color:var(--muted)}
.btn-ghost:hover{color:var(--ink);background:var(--bg3);border-color:var(--line2)}
.link-quiet{color:var(--muted);font-size:12.5px;text-decoration:underline;text-underline-offset:2px}
.link-quiet:hover{color:var(--acc)}
.kbd{font-family:var(--mono);font-size:11px;line-height:1;border:1px solid var(--line2);
border-bottom-width:2px;border-radius:4px;padding:2px 5px;background:var(--bg);color:var(--ink2)}
.chip{display:inline-flex;align-items:center;gap:6px;min-height:34px;padding:0 10px;
border-radius:999px;border:1px solid var(--line2);background:var(--card);color:var(--ink2);
font-size:12.5px;text-decoration:none;white-space:nowrap}
.chip:hover{border-color:var(--acc);color:var(--ink)}
.chip.on{border-color:var(--acc);background:var(--acc2);color:var(--s2t);font-weight:600}
.chip .x{font-family:var(--mono);font-size:11px;opacity:.75}
.dot{width:7px;height:7px;border-radius:50%;background:var(--acc);flex:none;display:inline-block}
.dot.warn{background:var(--warnbar)}
.flag{display:inline-block;font-size:11px;line-height:1.5;padding:1px 6px;border-radius:5px;
background:var(--warnb);color:var(--warnt);border:1px solid var(--warnl);font-weight:500;
white-space:nowrap;vertical-align:1px}

/* ---------- cards ---------- */
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--rl);
padding:14px;margin:0 0 12px}
.card-hd{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:8px}
.card-t{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
font-weight:600}
.meta{color:var(--muted);font-size:12px}
.h2{font-size:19px;line-height:1.25;font-weight:600;margin:6px 0 2px;overflow-wrap:anywhere}

/* ---------- triage shell ---------- */
.app{display:grid;grid-template-columns:232px minmax(0,1fr) 420px;height:100vh;height:100dvh;
overflow:hidden;background:var(--bg)}
.sidebar{background:var(--bg2);border-right:1px solid var(--line);display:flex;
flex-direction:column;gap:12px;padding:14px 12px;overflow-y:auto;min-height:0}
.brand{display:flex;align-items:center;gap:8px;font-weight:600;font-size:15px;padding:0 6px}
.brand svg{width:16px;height:16px}
.scan{background:var(--card);border:1px solid var(--line);border-radius:var(--rl);padding:10px}
.scan-line{display:flex;align-items:center;gap:7px;font-weight:500;font-size:13px}
.scan-sub{color:var(--muted);font-size:11.5px;margin-top:3px;line-height:1.35}
.scan .btn{margin-top:8px;width:100%}
.queue-panel{background:var(--card);border:1px solid var(--line);border-radius:var(--rl);padding:10px;margin:0 0 8px}
.queue-panel[hidden]{display:none}
.qp-t{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:0 0 8px}
.qp-item{display:flex;flex-wrap:wrap;align-items:center;gap:3px 8px;font-size:12.5px;color:var(--ink2)}
.qp-item[hidden]{display:none}
.qp-run{padding-bottom:8px;margin-bottom:8px;border-bottom:1px solid var(--line3)}
.qp-item a,.qp-q a{color:var(--ink);font-weight:500;text-decoration:none}
.qp-item a:hover,.qp-q a:hover{text-decoration:underline}
.qp-kind,.qp-stage,.qp-when,.qp-left{color:var(--muted);font-size:11.5px}
.qp-clock{font-family:var(--mono);font-size:12px;color:var(--ink)}
.qp-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.qp-q{display:flex;flex-wrap:wrap;align-items:center;gap:2px 8px;font-size:12.5px;color:var(--ink2)}
.qp-pos{font-family:var(--mono);font-size:11px;color:var(--muted)}
.qp-empty{font-size:12px;color:var(--muted)}
.enr-chip{display:inline-flex;align-items:center;margin-left:6px;padding:0 6px;border-radius:999px;border:1px solid var(--line2);background:var(--bg3);color:var(--ink2);font-family:var(--mono);font-size:10.5px;line-height:16px;vertical-align:middle;white-space:nowrap}
.enr-letter{color:var(--muted)}
.row-enr{position:relative}
.nav{display:flex;flex-direction:column;gap:2px;min-height:0}
.nav-group{margin-top:10px}
.nav-t{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);
padding:0 8px 4px;font-weight:600}
.nav-i{display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:32px;
padding:0 8px;border-radius:var(--r);text-decoration:none;color:var(--ink2);
border:1px solid transparent;font-size:13px}
.nav-i:hover{background:var(--bg3);color:var(--ink)}
.nav-i.on{background:var(--card);border-color:var(--line2);color:var(--ink);font-weight:600}
.nav-i .n{font-family:var(--mono);font-size:11.5px;color:var(--muted)}
.nav-i.on .n{color:var(--ink2)}
.side-foot{margin-top:auto;padding-top:10px;border-top:1px solid var(--line);
display:flex;flex-direction:column;gap:8px}
.side-row{display:flex;align-items:center;justify-content:space-between;gap:6px}
.hidden-note{font-size:11.5px;color:var(--muted);line-height:1.4;padding:0 2px}
.hidden-note a{font-size:11.5px;color:var(--acc)}

/* source filter: a details/summary menu, so it opens without JS */
.chip-menu{position:relative}
.chip-menu>summary{list-style:none;cursor:pointer}
.chip-menu>summary::-webkit-details-marker{display:none}
.chip-menu>summary::after{content:"\\25BE";font-size:9px;margin-left:2px;opacity:.7}
.chip-menu[open]>summary{border-color:var(--acc)}
/* An absolutely positioned menu can still lay out inside a closed <details>;
   hide it outright so it can never paint over the list. */
.chip-menu:not([open])>.menu{display:none}
.menu{position:absolute;top:calc(100% + 4px);left:0;z-index:40;min-width:180px;
background:var(--card);border:1px solid var(--line2);border-radius:var(--r);
padding:5px;box-shadow:0 8px 24px rgba(0,0,0,.16);display:flex;flex-direction:column;gap:1px}
.menu .nav-i{min-height:32px}

.main{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}
.list-head{padding:12px 16px;border-bottom:1px solid var(--line);background:var(--bg);flex:none}
.search{display:flex;align-items:center;gap:8px;background:var(--card);
border:1px solid var(--line2);border-radius:var(--r);padding:0 10px;min-height:44px}
.search svg{width:15px;height:15px;color:var(--muted);flex:none}
.search input{flex:1;min-width:0;border:0;background:transparent;outline:none;min-height:42px;
color:var(--ink);font-size:13.5px}
.search input::placeholder{color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px;align-items:center}
.list-meta{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
margin-top:9px;color:var(--muted);font-size:12px}
.rows{overflow-y:auto;flex:1;min-height:0}
.row{display:grid;grid-template-columns:48px minmax(0,1fr) minmax(0,220px) 56px;gap:12px;
align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);
border-left:3px solid transparent;text-decoration:none;color:inherit}
.row:hover,.row.hov{background:var(--bg3);border-left-color:var(--line2)}
.row.sel{background:var(--card);border-left-color:var(--acc)}
.score{width:40px;height:32px;border-radius:6px;display:flex;align-items:center;
justify-content:center;font-family:var(--mono);font-size:13px;font-weight:600}
.score.hi{background:var(--acc);color:var(--onacc)}
.score.mid{background:var(--acc2);color:var(--s2t)}
.score.low{background:var(--line3);color:var(--ink2)}
/* The row cells are spans in the markup; they need to be boxes to clip a long
   location instead of pushing the grid wider than the pane. */
.cell-t,.cell-l{display:flex;flex-direction:column;min-width:0;overflow:hidden}
.cell-l{align-items:flex-end}
.r-title{font-weight:600;display:flex;align-items:center;gap:7px;flex-wrap:wrap;
overflow-wrap:anywhere}
.r-co,.r-loc,.r-src{display:block;max-width:100%;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.r-co{color:var(--muted);font-size:12.5px;margin-top:2px}
.r-loc{color:var(--muted);font-size:12px}
.r-src{color:var(--muted);font-size:11.5px;margin-top:2px}
.r-age{font-family:var(--mono);font-size:12px;color:var(--ink2);text-align:right}
.list-foot{flex:none;background:var(--bg2);border-top:1px solid var(--line);padding:8px 16px;
display:flex;align-items:center;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:12px}
.list-foot .hint{display:inline-flex;align-items:center;gap:5px}
.empty{padding:40px 20px;text-align:center;color:var(--muted)}
.empty b{display:block;color:var(--ink);font-size:15px;margin-bottom:4px}

/* ---------- preview pane ---------- */
.preview{background:var(--bg3);border-left:1px solid var(--line);display:flex;
flex-direction:column;min-height:0}
.pv-scroll{overflow-y:auto;flex:1;min-height:0}
.pv-top{display:flex;justify-content:space-between;align-items:center;gap:8px;
padding:10px 14px;border-bottom:1px solid var(--line)}
.pv-close{display:none}
.pv-body{padding:14px}
.pv-meta{color:var(--muted);font-size:12px}
.pv-sub{color:var(--ink2);font-size:12.5px;margin-top:2px}
.big{font-family:var(--mono);font-size:34px;line-height:1;font-weight:600}
.big small{font-size:14px;color:var(--muted);font-weight:400}
.bars{margin-top:10px}
.bar{margin-top:10px}
.bar-top{display:flex;justify-content:space-between;gap:8px;font-size:12px}
.bar-l{color:var(--ink2)}
.bar-v{font-family:var(--mono);color:var(--ink2)}
.bar-track{height:6px;background:var(--line3);border-radius:3px;overflow:hidden;margin-top:4px}
.bar-fill{height:100%;background:var(--acc);border-radius:3px}
.bar-fill.neg{background:var(--warnbar)}
.caption{color:var(--muted);font-size:11.5px;line-height:1.4;margin-top:10px;
border-top:1px solid var(--line);padding-top:8px}
.checks{margin-top:4px}
.check{display:flex;gap:8px;align-items:flex-start;padding:5px 0;font-size:12.5px;
line-height:1.4;color:var(--ink2)}
.check .mk{font-family:var(--mono);font-size:12px;flex:none;width:14px;text-align:center;
color:var(--acc)}
.check.warn .mk{color:var(--warnbar)}
.check.warn{color:var(--warnt)}
.actions{flex:none;background:var(--bg3);border-top:1px solid var(--line);padding:12px 14px;
display:flex;flex-direction:column;gap:8px}
.btn-row{display:flex;gap:8px}
.btn-row .btn{flex:1}
.pv-empty{padding:28px 16px;color:var(--muted);font-size:13px;text-align:center}

/* ---------- workspace ---------- */
.ws{display:flex;flex-direction:column;height:100vh;height:100dvh;overflow:hidden}
.topbar{display:flex;align-items:center;gap:12px;padding:10px 16px;background:var(--bg2);
border-bottom:1px solid var(--line);flex:none;flex-wrap:wrap}
.tb-back{display:inline-flex;align-items:center;gap:5px;color:var(--muted);font-size:12.5px;
text-decoration:none;min-height:36px}
.tb-back:hover{color:var(--acc)}
.tb-id{min-width:0}
.tb-title{font-weight:600;font-size:15px;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;max-width:46ch}
.tb-co{color:var(--muted);font-size:12px}
.tb-meta{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;max-width:78ch}
.tb-actions{margin-left:auto;display:flex;align-items:center;gap:12px}
.orig{font-size:12.5px;color:var(--muted);text-decoration:underline;
text-underline-offset:2px;white-space:nowrap}
.orig:hover{color:var(--acc)}
.status-menu{position:relative}
.status-menu>summary{list-style:none}
.status-menu>summary::-webkit-details-marker{display:none}
.status-pill{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;cursor:pointer;
border-radius:999px;border:1px solid var(--line2);background:var(--card);
font-size:12px;color:var(--ink2);white-space:nowrap}
.status-pill:hover{border-color:var(--acc)}
.status-pill .sp-k{color:var(--muted)}
.status-menu[open]>.status-pill{border-color:var(--acc);color:var(--ink)}
.status-pill .caret{font-size:9px;color:var(--muted)}
.status-form{position:absolute;right:0;top:calc(100% + 6px);z-index:20;width:250px;
display:flex;flex-direction:column;gap:6px;padding:10px;background:var(--card);
border:1px solid var(--line2);border-radius:var(--r);box-shadow:0 8px 24px rgba(0,0,0,.18)}
.status-form select,.status-form input{width:100%;font:13px var(--font);color:var(--ink);
background:var(--bg3);border:1px solid var(--line2);border-radius:6px;padding:6px 7px}
.status-form .btn{justify-content:center}
.tabs{display:flex;align-items:stretch;gap:2px;padding:0 16px;background:var(--bg2);
border-bottom:1px solid var(--line);flex:none;overflow-x:auto}
.tab{display:inline-flex;align-items:center;gap:7px;padding:9px 14px 10px;font-size:13px;
color:var(--muted);text-decoration:none;border-bottom:2px solid transparent;
white-space:nowrap}
.tab:hover{color:var(--ink)}
.tab.on{color:var(--ink);font-weight:600;border-bottom-color:var(--acc)}
.tab.done{color:var(--ink2)}
.tab .tk{font-size:11px;color:var(--acc);line-height:1}
.cols{display:grid;grid-template-columns:380px minmax(0,1fr) 340px;flex:1;min-height:0}
.col{overflow-y:auto;min-height:0;padding:14px}
.col-l{border-right:1px solid var(--line);background:var(--bg)}
.col-c{background:var(--bg);display:flex;flex-direction:column;min-width:0}
.col-r{background:var(--bg3);border-left:1px solid var(--line)}
.post-text{white-space:pre-wrap;font-size:12.5px;line-height:1.5;color:var(--ink2);
overflow-wrap:anywhere}
.post-nav{display:flex;gap:6px;flex-wrap:wrap;position:sticky;top:0;z-index:5;
padding:0 0 10px;background:var(--bg)}
.post-nav .chip{text-decoration:none}
.post-sec{padding-bottom:14px}
.post-h{margin:0 0 6px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
color:var(--muted);scroll-margin-top:52px}
.post-sec{scroll-margin-top:52px}
mark{background:var(--acc2);color:var(--s2t);border-radius:3px;padding:0 2px}
.banner{display:flex;gap:8px;align-items:flex-start;border-radius:var(--r);padding:9px 11px;
font-size:12.5px;line-height:1.4;margin-bottom:12px;border:1px solid var(--line2);
background:var(--card);color:var(--ink2)}
.banner.warn{background:var(--warnb);border-color:var(--warnl);color:var(--warnt)}
.banner.ok{border-color:var(--line2);background:var(--card)}
.banner .mx{font-family:var(--mono);font-weight:600;flex:none}
/* ---------- step panels ---------- */
.panel{display:flex;flex-direction:column;gap:12px;min-width:0}
.panel-hd{display:flex;align-items:flex-start;gap:10px;justify-content:space-between;
flex-wrap:wrap}
.panel-hd .ph-l{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.panel-hd .ph-r{display:flex;align-items:center;gap:8px}
.panel-t{margin:0;font-size:15px;font-weight:600;letter-spacing:-.01em}
.panel-foot{display:flex;gap:8px;justify-content:flex-end;align-items:center;
padding-top:4px;border-top:1px solid var(--line3);margin-top:2px}
.panel-foot .btn{min-width:180px;justify-content:center}
.empty-state{display:flex;flex-direction:column;gap:10px;align-items:flex-start;
padding:22px;border:1px dashed var(--line2);border-radius:var(--rl);background:var(--card)}
.es-t{margin:0;font-size:14px;font-weight:600}
.empty-state .btn{min-width:200px;justify-content:center}
.findings{display:flex;flex-direction:column;gap:10px}
.finding{background:var(--card);border:1px solid var(--line);border-radius:var(--rl);
padding:12px 14px}
.f-hd{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.f-title{margin:0;font-size:13.5px;font-weight:600;line-height:1.3}
.tag{font-family:var(--mono);font-size:10.5px;font-weight:600;letter-spacing:.04em;
text-transform:uppercase;padding:2px 7px;border-radius:999px;border:1px solid var(--line2);
background:var(--bg3);color:var(--muted);flex:none}
.tag-gap{color:var(--warnt);background:var(--warnb);border-color:var(--warnl)}
.tag-str{color:var(--s2t);background:var(--acc2);border-color:var(--acc2)}
.tag-dec{color:var(--ink);background:var(--bg3);border-color:var(--line2)}
.kw-row{display:flex;flex-wrap:wrap;gap:6px}
.f-rows{display:flex;flex-direction:column;gap:6px}
.f-row{display:grid;grid-template-columns:96px minmax(0,1fr);gap:8px;align-items:baseline}
.fk{color:var(--muted);font-size:11.5px;letter-spacing:.02em}
.fv{font-size:12.5px;line-height:1.5;color:var(--ink2)}
.f-body{margin:0;font-size:12.5px;line-height:1.55;color:var(--ink2)}
.perfect{background:var(--card)}
.perfect>summary{cursor:pointer;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.perfect>summary .card-t{margin:0}
.perfect-body{padding-top:8px;font-size:12.5px;line-height:1.55;color:var(--ink2)}
.perfect-body p{margin:0 0 8px}
.raw-analysis{border:1px solid var(--line);border-radius:var(--rl);background:var(--card);
padding:10px 12px;font-size:12.5px}
.raw-analysis>summary{cursor:pointer;color:var(--muted)}
.raw-analysis pre{margin:8px 0 0}
.blockers{padding:12px 14px}
.blockers .card-t{margin:0 0 8px}
.blockers.calm{background:var(--card)}
.bl-calm{margin:0;font-size:12.5px;line-height:1.5;color:var(--muted)}
.bl{display:flex;flex-direction:column;gap:6px;padding:10px 0;border-top:1px solid var(--line3)}
.bl:first-of-type{padding-top:2px;border-top:0}
.bl-hd{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.bl-t{font-size:12.5px;font-weight:600;color:var(--ink)}
.variant-row{display:flex;align-items:center;gap:9px;flex-wrap:wrap;font-size:12.5px;
padding:9px 12px;border:1px solid var(--line);border-radius:var(--rl);background:var(--card)}
.vr-k{color:var(--muted)}
.variant-row .mono{font-size:13px;color:var(--ink)}
.variant-form{position:absolute;top:calc(100% + 6px);left:0;z-index:30;width:230px;
display:flex;flex-direction:column;gap:6px;padding:10px;background:var(--card);
border:1px solid var(--line2);border-radius:var(--r);box-shadow:0 8px 24px rgba(0,0,0,.18)}
.variant-form select{width:100%;font:13px var(--font);color:var(--ink);background:var(--bg3);
border:1px solid var(--line2);border-radius:6px;padding:6px 7px}
.chip-menu:not([open])>.variant-form{display:none}
.pkt-files{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:var(--rl);
background:var(--card);overflow:hidden}
.pkt-files .li{padding:8px 12px;border-top:1px solid var(--line3);font-size:12.5px;
display:flex;align-items:center;justify-content:space-between;gap:10px}
.pkt-files .li:first-child{border-top:0}
.pkt-files a{color:var(--ink);text-decoration:none;overflow-wrap:anywhere}
.pkt-files a:hover{color:var(--acc);text-decoration:underline}
.pkt-files .mono{color:var(--muted);font-size:11.5px;flex:none}
.pf-name{display:flex;flex-direction:column;gap:1px;min-width:0}
.pf-name .meta{font-size:11.5px}
.pf-acts{display:flex;gap:12px;flex:none}
.body-form{display:flex;flex-direction:column;gap:8px;min-width:0}
.ta.grow{flex:none;min-height:132px;overflow:hidden}
.ta-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding-top:2px}
.ta-bar .spacer{flex:1}
.ta-bar .btn{min-width:0}
.copy-note{color:var(--acc)}
.es-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.empty-state .own-letter,.empty-state .body-form{width:100%}
.own-letter>summary{list-style:none;cursor:pointer}
.own-letter>summary::-webkit-details-marker{display:none}
.own-letter>summary::after{content:"\25BE";font-size:9px;margin-left:6px;opacity:.7}
.own-letter[open]>summary{margin-bottom:10px}
.letter-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.submit-block{display:flex;flex-direction:column;gap:8px;padding:12px 14px;
border:1px solid var(--line);border-radius:var(--rl);background:var(--card)}
.submit-block .card-t{margin:0}
.sub-warn{margin:0;font-size:12.5px;line-height:1.5;color:var(--warnt)}
.questions-form{display:flex;flex-direction:column;gap:8px;align-items:flex-start}
.q-text{width:100%;box-sizing:border-box;min-height:8em;font-size:13px;line-height:1.5}
.answers summary{cursor:pointer;font-size:12.5px}
.q-answers{white-space:pre-wrap;font-size:12.5px;line-height:1.55;margin:8px 0}
.gate-banner{margin:0;padding:8px 12px;border-radius:8px;font-size:12.5px;line-height:1.5;
background:var(--warnb);color:var(--warnt);border:1px solid var(--warnl)}
.gate-blocked{border-width:2px;border-color:var(--warnbar);font-weight:500}
.gate-caution{border-style:dashed}
.editor-wrap{display:flex;flex-direction:column;gap:8px;flex:1;min-height:0}
.editor-hd{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.ta{width:100%;min-height:340px;flex:1;resize:vertical;background:var(--card);
border:1px solid var(--line2);border-radius:var(--rl);padding:12px;
font:13px/1.55 var(--font);color:var(--ink)}
.ta.mono{font-family:var(--mono);font-size:12.5px;line-height:1.6}
.count-ok{color:var(--acc)}
.count-warn{color:var(--warnbar)}
.ai-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
.sform,.ai-row form{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sform{margin-top:8px}
#ai-actions form{display:inline-flex}
.prep-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding-bottom:10px;
margin-bottom:10px;border-bottom:1px solid var(--line)}
.ai-note{color:var(--muted);font-size:11.5px}
.banner-job{margin-top:10px}
.li{display:flex;gap:8px;align-items:baseline;padding:6px 0;border-bottom:1px solid var(--line);
font-size:12px}
.li:last-child{border-bottom:0}
.li time{font-family:var(--mono);font-size:11px;color:var(--muted);flex:none}
.li .k{font-weight:600}
.pre{white-space:pre-wrap;font-size:12px;line-height:1.45;color:var(--ink2);
background:var(--bg2);border:1px solid var(--line);border-radius:var(--r);padding:9px;
overflow-wrap:anywhere;max-height:260px;overflow:auto}
.bottombar{display:flex;align-items:center;gap:10px;padding:9px 16px;background:var(--bg2);
border-top:1px solid var(--line);flex:none;flex-wrap:wrap}
.bottombar .say{color:var(--muted);font-size:12px;margin-right:auto}

/* ---------- toast + overlays ---------- */
.toast{position:fixed;left:50%;bottom:64px;transform:translateX(-50%) translateY(8px);
background:var(--ink);color:var(--bg);padding:10px 14px;border-radius:var(--r);
display:flex;gap:12px;align-items:center;font-size:13px;z-index:80;
box-shadow:0 6px 24px rgba(0,0,0,.28);opacity:0;pointer-events:none;
transition:opacity .15s ease,transform .15s ease}
.toast.on{opacity:1;transform:translateX(-50%);pointer-events:auto}
.toast .undo{background:transparent;border:0;color:inherit;text-decoration:underline;
cursor:pointer;font-weight:600;padding:6px}
.scrim{display:none;position:fixed;inset:0;background:rgba(0,0,0,.32);z-index:45}
/* Only dimmed when the preview is a slide-over, i.e. below 1100px. On a wide
   window the pane is a column and the scrim would sit over the page it dims. */

/* ---------- narrow ---------- */
@media (max-width:1100px){
  .app{grid-template-columns:232px minmax(0,1fr)}
  .scrim.on{display:block}
  .preview{position:fixed;top:0;right:0;bottom:0;width:min(420px,94vw);
  transform:translateX(103%);transition:transform .18s ease;z-index:50;
  box-shadow:-8px 0 28px rgba(0,0,0,.18)}
  .preview.open{transform:none}
  .pv-close{display:inline-flex}
  .cols{grid-template-columns:320px minmax(0,1fr)}
  .col-r{grid-column:1/-1;border-left:0;border-top:1px solid var(--line)}
  .ws{height:auto;min-height:100vh;overflow:auto}
  .col{overflow:visible}
}
@media (max-width:800px){
  .app{grid-template-columns:minmax(0,1fr)}
  .sidebar{position:fixed;top:0;bottom:0;left:0;width:264px;z-index:55;
  transform:translateX(-102%);transition:transform .18s ease}
  .sidebar.open{transform:none}
  .side-toggle{display:inline-flex}
  .row{grid-template-columns:44px minmax(0,1fr) 48px;gap:10px;padding:12px}
  .cell-l{display:none}
  .chip{min-height:44px}
  .cols{display:block}
  .col{border:0;border-top:1px solid var(--line)}
  .col-l{border-top:0}
  .tabs{padding:0 10px;margin-left:0}
}
@media (min-width:801px){.side-toggle{display:none}}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important;animation:none!important}
}
"""

CSS = CSS.replace("%LIGHT%", LIGHT).replace("%DARK%", DARK)


def esc(value: object) -> str:
    """Escape for text and quoted attributes alike."""
    return _html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str, *, body_class: str = "", head: str = "",
         theme_boot: bool = True, body_attrs: str = "") -> str:
    """A complete document: tokens, layout CSS, optional head script, then the body."""
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)}</title>"
        + f"<style>{CSS}</style>"
        + (THEME_BOOT if theme_boot else "")
        + NOSCRIPT_CSS
        + head
        + "</head>"
        + f"<body class=\"{esc(body_class)}\"{(' ' + body_attrs) if body_attrs else ''}>"
        + body
        + "</body></html>\n"
    )


def kbd(key: str, label: str | None = None) -> str:
    """A keyboard hint: the key chip, then the verb."""
    out = f'<span class="kbd">{esc(key)}</span>'
    if label:
        out += f" {esc(label)}"
    return f'<span class="hint">{out}</span>'


def score_pill(score: float | None) -> str:
    """Score pill: high 30+, mid 20-29, low below 20. The band is the CSS class only."""
    value = 0.0 if score is None else float(score)
    band = "hi" if value >= 30 else "mid" if value >= 20 else "low"
    text = f"{value:g}"
    return f'<span class="score {band}" title="reading-order score {esc(text)}">{esc(text)}</span>'


def flag(text: str) -> str:
    return f'<span class="flag">{esc(text)}</span>'


def bar(label: str, value: float, *, maximum: float, negative: bool = False) -> str:
    """One labelled contribution bar. Width is capped at 100% of the given scale."""
    pct = 0.0 if maximum <= 0 else max(0.0, min(100.0, abs(value) / maximum * 100.0))
    sign = f"{value:+g}" if value else "0"
    return (
        '<div class="bar"><div class="bar-top">'
        f'<span class="bar-l">{esc(label)}</span>'
        f'<span class="bar-v">{esc(sign)}</span></div>'
        f'<div class="bar-track"><div class="bar-fill{" neg" if negative else ""}" '
        f'style="width:{pct:.1f}%"></div></div></div>'
    )


def card(title: str, body: str, *, aside: str = "", cls: str = "", card_id: str = "") -> str:
    """A titled card. title goes through esc; body and aside are caller-built markup."""
    cid = f' id="{esc(card_id)}"' if card_id else ""
    head = (f'<div class="card-hd"><span class="card-t">{esc(title)}</span>'
            f'{aside}</div>') if title else ""
    return f'<div class="card {esc(cls)}"{cid}>{head}{body}</div>'


def btn(label: str, *, href: str | None = None, primary: bool = False, small: bool = False,
        attrs: str = "", icon: str = "") -> str:
    cls = "btn" + (" btn-primary" if primary else "") + (" btn-sm" if small else "")
    inner = (icon + esc(label)) if icon else esc(label)
    if href:
        return f'<a class="{cls}" href="{esc(href)}" {attrs}>{inner}</a>'
    return f'<button class="{cls}" type="submit" {attrs}>{inner}</button>'


def rel_age(iso: str | None, now: datetime | None = None) -> str:
    """Compact age for a timestamp: 'now', '14m', '3h', '2d', '3w', else the date."""
    dt = _parse(iso)
    if dt is None:
        return "—"
    now = now or datetime.now(timezone.utc)
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    if secs < 7 * 86400:
        return f"{secs // 86400}d"
    if secs < 365 * 86400:
        return f"{secs // (7 * 86400)}w"
    return dt.date().isoformat()


def rel_phrase(iso: str | None, now: datetime | None = None) -> str:
    """A sentence-ready age: '14 min ago'. Empty when the timestamp is unusable."""
    dt = _parse(iso)
    if dt is None:
        return ""
    now = now or datetime.now(timezone.utc)
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 60:
        return "just now"
    for size, word in ((3600, "min"), (86400, "hour"), (7 * 86400, "day"),
                       (30 * 86400, "week"), (365 * 86400, "month")):
        if secs < size:
            unit = {"min": 60, "hour": 3600, "day": 86400, "week": 7 * 86400,
                    "month": 30 * 86400}[word]
            n = secs // unit
            return f"{n} {word}{'' if n == 1 else 's'} ago"
    years = secs // (365 * 86400)
    return f"{years} year{'' if years == 1 else 's'} ago"


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def theme_toggle() -> str:
    """The sidebar footer control. Works only with JS; without it the system theme holds."""
    return (
        '<button class="btn btn-sm btn-ghost" type="button" data-theme-toggle '
        'aria-label="Switch between light and dark theme" '
        'title="Light or dark, remembered on this device">'
        '<span data-theme-label>Theme</span></button>'
    )
