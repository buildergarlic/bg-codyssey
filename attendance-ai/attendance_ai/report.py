"""HTML 보고서 (외부 파일·인터넷 없이 열리는 단일 파일)."""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from .engine import AnalysisResult, evidence_text
from .fmt import day_kor, dt_full, dt_short, hm
from .models import KOR


def _h(day0: datetime, t: datetime) -> float:
    return round((t - day0).total_seconds() / 3600, 3)


def _payload(r: AnalysisResult) -> dict:
    emps = []
    sess_by_emp = r.sessions
    days_by_emp: dict = {}
    for row in r.days:
        days_by_emp.setdefault(row.emp_id, []).append(row)
    for e in r.employees:
        day_map = {}
        for row in days_by_emp.get(e.emp_id, []):
            day_map[row.work_date] = {
                "date": day_kor(row.work_date), "status": row.status,
                "in": row.first_in.strftime("%H:%M") if row.first_in else "",
                "out": (row.last_out.strftime("%m/%d %H:%M") if row.last_out and row.first_in
                        and row.last_out.date() != row.first_in.date() else
                        row.last_out.strftime("%H:%M") if row.last_out else ""),
                "work": hm(row.work_minutes), "outing": hm(row.outing_minutes) if row.outing_minutes else "",
                "night": hm(row.night_minutes) if row.night_minutes else "",
                "base": hm(row.baseline_minutes) if row.baseline_minutes is not None else "계산불가",
                "baseProblem": row.baseline_problem, "baseWhy": row.baseline_why,
                "diff": hm(row.work_minutes - row.baseline_minutes) if row.baseline_minutes is not None else "",
                "notes": row.notes, "sessions": [], "corrected": row.corrected,
            }
        for x in sess_by_emp.get(e.emp_id, []):
            day = day_map.get(x.work_date)
            if day is None:
                continue
            day0 = datetime.combine(x.work_date, datetime.min.time())
            day["sessions"].append({
                "iv": [[_h(day0, a), _h(day0, b)] for a, b in x.intervals],
                "marks": [{
                    "h": _h(day0, d.ts), "s": d.state, "lab": d.event.label, "conf": round(d.confidence, 4),
                    "act": d.action, "t": dt_short(d.ts), "n": len(d.event.punches),
                    # 용량을 줄이기 위해 정상 판정은 근거를 생략 (CSV 에는 모두 있음)
                    "why": d.reasons if d.action != "정상" else [],
                } for d in x.decisions],
            })
        emps.append({
            "id": e.emp_id, "name": e.name, "punches": e.punches, "events": e.events, "days": e.days,
            "work": hm(e.work_minutes), "corrected": e.corrected, "review": e.review,
            "baseProblem": e.baseline_problem_days, "acc": round(e.menu_accuracy * 100),
            "typIn": e.typical_in, "typOut": e.typical_out,
            "rows": [day_map[k] for k in sorted(day_map)],
        })
    corrections = []
    for emp, ds in r.decisions.items():
        for d in ds:
            if d.changed or d.event.forced is not None:
                corrections.append({"id": emp, "name": d.event.name, "t": dt_full(d.ts), "lab": KOR[d.event.label],
                                    "s": KOR[d.state], "conf": round(d.confidence, 4), "act": d.action,
                                    "why": d.reasons, "ev": evidence_text(d.evidence)})
    review = [{"kind": it.kind, "id": it.emp_id, "name": it.name,
               "date": day_kor(it.work_date) if it.work_date else "", "t": dt_full(it.ts), "lab": it.selected,
               "s": it.suggestion, "conf": None if it.confidence is None else round(it.confidence, 4),
               "why": it.detail} for it in r.review]
    lr = r.load_report
    return {
        "meta": {
            "source": Path(lr.source).name if lr else "", "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "period": [dt_full(r.data_start), dt_full(r.data_end)],
            "skipped": [{"row": n, "why": w, "raw": raw[:120]} for n, w, raw in (lr.skipped if lr else [])],
            "unknownLabels": lr.unknown_labels if lr else {}, "columns": lr.columns if lr else {},
            "confirmLog": r.confirm_log,
            "settings": {"auto": r.settings.auto_correct_threshold, "review": r.settings.review_threshold,
                         "dup": r.settings.duplicate_window_min, "gap": r.settings.new_shift_gap_hours,
                         "maxIv": r.settings.max_interval_hours},
        },
        "stats": r.stats, "review": review, "corrections": corrections, "employees": emps,
    }


def render_report(r: AnalysisResult) -> str:
    data = json.dumps(_payload(r), ensure_ascii=False).replace("</", "<\\/")
    s = r.stats
    ba = s["by_action"]
    kpis = [
        ("직원", f"{s['employees']:,}명", f"지문 기록 {s['punches']:,}건"),
        ("AI 자동 정정", f"{ba.get('자동정정', 0):,}건", "메뉴를 잘못 고른 기록을 바로잡음"),
        ("담당자 검토", f"{s['review_items']:,}건", "AI가 확신하지 못하거나 기록이 빠진 항목"),
        ("확인이 필요한 근무일", f"{s['baseline_problem_days']:,}일 → {s['ai_review_days']:,}일",
         "메뉴 기준 계산 → AI 판정 후"),
        ("총 실근무시간", hm(s["work_minutes"]), f"야간 {hm(s['night_minutes'])}"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-label">{html.escape(a)}</div><div class="kpi-value">{html.escape(b)}</div>'
        f'<div class="kpi-sub">{html.escape(c)}</div></div>' for a, b, c in kpis)
    period = f"{dt_full(r.data_start)[:16]} ~ {dt_full(r.data_end)[:16]}" if r.data_start else ""
    return (_TEMPLATE
            .replace("__KPIS__", kpi_html)
            .replace("__PERIOD__", html.escape(period))
            .replace("__SOURCE__", html.escape(Path(r.load_report.source).name if r.load_report else "(직접 입력)"))
            .replace("__AUTO__", f"{r.settings.auto_correct_threshold * 100:g}")
            .replace("__DATA__", data))


_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 근태 판정 보고서</title>
<style>
:root{
  --bg:#f6f6f3;--surface:#ffffff;--surface2:#f0f0ec;--text:#1c1c1e;--muted:#6a6a70;--border:#e2e2dc;
  --in:#2f6fdb;--out:#d9661f;--bar:#2f6fdb;--night:rgba(60,60,120,.07);
  --fix:#c62828;--review:#b7791f;--ok:#2e7d32;--chip:#ececE6;
  --shadow:0 1px 2px rgba(0,0,0,.05),0 1px 8px rgba(0,0,0,.04);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#111113;--surface:#1b1b1e;--surface2:#232327;--text:#ececef;--muted:#9c9ca6;--border:#2f2f35;
    --in:#6ea2ff;--out:#ff9a57;--bar:#6ea2ff;--night:rgba(160,160,255,.07);
    --fix:#ff6b6b;--review:#e3b341;--ok:#5cc46a;--chip:#2a2a30;--shadow:none;
  }
}
:root[data-theme="dark"]{
  --bg:#111113;--surface:#1b1b1e;--surface2:#232327;--text:#ececef;--muted:#9c9ca6;--border:#2f2f35;
  --in:#6ea2ff;--out:#ff9a57;--bar:#6ea2ff;--night:rgba(160,160,255,.07);
  --fix:#ff6b6b;--review:#e3b341;--ok:#5cc46a;--chip:#2a2a30;--shadow:none;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 16px 64px}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:16px;margin:36px 0 12px}
.sub{color:var(--muted);font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:20px 0 8px}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;box-shadow:var(--shadow)}
.kpi-label{font-size:12px;color:var(--muted)}
.kpi-value{font-size:22px;font-weight:650;margin:2px 0;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.kpi-sub{font-size:12px;color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;box-shadow:var(--shadow)}
.how{padding:14px 18px;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px 22px}
.how b{display:block;font-size:13px;margin-bottom:2px}
.how p{margin:0;color:var(--muted);font-size:13px}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
input[type=search],select{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:7px 10px;font:inherit;min-width:0}
input[type=search]{flex:1 1 220px}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12px;background:var(--surface2);position:sticky;top:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.nowrap{white-space:nowrap}
tr:last-child td{border-bottom:none}
.why{color:var(--muted);font-size:12px}
.why li{margin:1px 0}
.why ul{margin:0;padding-left:16px}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:var(--chip);white-space:nowrap}
.chip.fix{color:var(--fix);background:color-mix(in srgb,var(--fix) 12%,transparent)}
.chip.review{color:var(--review);background:color-mix(in srgb,var(--review) 14%,transparent)}
.chip.ok{color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent)}
.chip.in{color:var(--in);background:color-mix(in srgb,var(--in) 12%,transparent)}
.chip.out{color:var(--out);background:color-mix(in srgb,var(--out) 12%,transparent)}
details.emp{background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:8px;box-shadow:var(--shadow)}
details.emp>summary{cursor:pointer;list-style:none;padding:11px 16px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
details.emp>summary::-webkit-details-marker{display:none}
details.emp>summary::before{content:"▸";color:var(--muted);transition:transform .15s}
details.emp[open]>summary::before{transform:rotate(90deg)}
.emp-name{font-weight:600;min-width:120px}
.emp-meta{color:var(--muted);font-size:12px;display:flex;gap:12px;flex-wrap:wrap}
.emp-body{padding:4px 16px 16px;border-top:1px solid var(--border)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:10px 0 4px;align-items:center}
.legend svg{vertical-align:-3px;margin-right:4px}
svg.tl{width:100%;height:auto;display:block}
svg.tl text{fill:var(--muted);font-size:11px}
.empty{padding:18px;color:var(--muted);text-align:center}
.more{margin-top:8px}
button{background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font:inherit;cursor:pointer}
.notice{font-size:12px;color:var(--muted);margin-top:28px;line-height:1.7}
.warn{border-left:3px solid var(--review);padding:8px 12px;background:var(--surface);margin:12px 0;font-size:13px;border-radius:0 8px 8px 0}
@media print{details.emp{break-inside:avoid}.toolbar{display:none}}
</style>
</head>
<body>
<div class="wrap">
  <h1>AI 근태 판정 보고서</h1>
  <div class="sub">기간 __PERIOD__ · 원본 __SOURCE__ · <span id="gen"></span></div>
  <div class="kpis">__KPIS__</div>
  <div id="warnings"></div>

  <h2>AI는 이렇게 판단합니다</h2>
  <div class="card how">
    <div><b>① 메뉴가 아니라 지문 시각</b><p>출근·퇴근 메뉴는 '틀릴 수 있는 힌트'로만 쓰고, 실제로 지문을 찍은 시각을 기준으로 근로시간을 계산합니다.</p></div>
    <div><b>② 순서</b><p>출근 다음엔 퇴근, 퇴근 다음엔 출근. 순서가 어긋나면 메뉴 오선택·재입력·기록 누락 중 무엇인지 따집니다.</p></div>
    <div><b>③ 개인별 평소 패턴</b><p>직원마다 평소 출근·퇴근 시간대와 근무 길이를 기록에서 스스로 학습합니다. 야간·교대근무도 개인 패턴으로 이해합니다.</p></div>
    <div><b>④ 확신할 때만 자동 정정</b><p>확신도 __AUTO__% 이상이고 결과 근무시간이 상식적일 때만 자동 정정, 그 밖에는 담당자 검토로 넘깁니다. 원본 기록은 바꾸지 않습니다.</p></div>
  </div>

  <h2>담당자 검토 목록 <span class="sub" id="review-count"></span></h2>
  <div class="toolbar"><input type="search" id="q-review" placeholder="사번·이름·유형으로 찾기"></div>
  <div class="card tbl-wrap"><table id="t-review"><thead><tr>
    <th>유형</th><th>사번</th><th>이름</th><th>근무일</th><th>지문시각</th><th>선택메뉴</th><th>AI 제안</th><th class="num">확신도</th><th>설명</th>
  </tr></thead><tbody></tbody></table></div>
  <div class="more" id="more-review"></div>

  <h2>AI 정정 내역 <span class="sub" id="fix-count"></span></h2>
  <div class="toolbar"><input type="search" id="q-fix" placeholder="사번·이름으로 찾기">
    <select id="f-fix"><option value="">전체</option><option>자동정정</option><option>정정제안</option><option>검토필요</option><option>담당자확정</option></select></div>
  <div class="card tbl-wrap"><table id="t-fix"><thead><tr>
    <th>처리</th><th>사번</th><th>이름</th><th>지문시각</th><th>선택메뉴</th><th>AI 판정</th><th class="num">확신도</th><th>판정 근거</th>
  </tr></thead><tbody></tbody></table></div>
  <div class="more" id="more-fix"></div>

  <h2>직원별 상세</h2>
  <div class="toolbar">
    <input type="search" id="q-emp" placeholder="사번·이름으로 찾기">
    <select id="f-emp"><option value="">전체 직원</option><option value="review">검토 항목 있는 직원</option><option value="fix">정정 있는 직원</option></select>
  </div>
  <div id="emps"></div>

  <div class="notice" id="notice"></div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
const D = JSON.parse(document.getElementById('data').textContent);
const $ = s => document.querySelector(s);
const el = (tag, attrs, ...kids) => { const e = document.createElement(tag);
  if (attrs) for (const [k,v] of Object.entries(attrs)) { if (k==='class') e.className=v; else if (k==='text') e.textContent=v; else e.setAttribute(k,v); }
  for (const k of kids) if (k!=null) e.append(k.nodeType ? k : document.createTextNode(k)); return e; };
const pct = c => c==null ? '' : (c*100).toFixed(1)+'%';
const chipFor = a => { const cls = a==='자동정정' ? 'fix' : (a==='정상'||a==='AI판정') ? 'ok' : a==='담당자확정' ? 'in' : 'review'; return el('span',{class:'chip '+cls,text:a}); };
const stateChip = s => el('span',{class:'chip '+(s==='출근'?'in':s==='퇴근'?'out':''),text:s});
const whyList = arr => { const u = el('ul'); for (const w of arr) u.append(el('li',{text:w})); return el('div',{class:'why'},u); };
$('#gen').textContent = '생성 ' + D.meta.generated;

// 경고 (읽지 못한 행, 해석하지 못한 메뉴 값)
const W = $('#warnings');
if (D.meta.skipped.length) W.append(el('div',{class:'warn',text:`원본 파일에서 ${D.meta.skipped.length}개 행을 읽지 못했습니다 (예: ${D.meta.skipped.slice(0,3).map(s=>s.row+'행 '+s.why).join(', ')}). model_summary.json 에서 전체 목록을 확인하세요.`}));
const ul = Object.entries(D.meta.unknownLabels);
if (ul.length) W.append(el('div',{class:'warn',text:'해석하지 못한 메뉴 값: '+ul.map(([k,v])=>`'${k}' ${v}건`).join(', ')+' — 이 기록은 메뉴 없이 시각·순서로만 판정했습니다. --in-values / --out-values 옵션으로 지정할 수 있습니다.'}));
if (D.meta.confirmLog.length) W.append(el('div',{class:'warn',text:'담당자 확정 반영: '+D.meta.confirmLog.length+'건 — '+D.meta.confirmLog.slice(0,5).join(' / ')}));

// 페이지 단위로 표 그리기
function pagedTable(tbody, moreBox, rows, render, step){
  let shown = 0;
  tbody.textContent=''; moreBox.textContent='';
  const more = () => { const end = Math.min(rows.length, shown+step); for (; shown<end; shown++) tbody.append(render(rows[shown]));
    moreBox.textContent=''; if (shown<rows.length) moreBox.append(el('button',{text:`더 보기 (${rows.length-shown}건 남음)`,type:'button'})); };
  moreBox.onclick = e => { if (e.target.tagName==='BUTTON') more(); };
  if (!rows.length) tbody.append(el('tr',null,el('td',{colspan:'9',class:'empty',text:'해당 항목이 없습니다.'})));
  more();
}
const match = (q, ...xs) => !q || xs.some(x => String(x||'').toLowerCase().includes(q));

function drawReview(){
  const q = $('#q-review').value.trim().toLowerCase();
  const rows = D.review.filter(r => match(q, r.id, r.name, r.kind));
  $('#review-count').textContent = `${rows.length}건`;
  pagedTable($('#t-review tbody'), $('#more-review'), rows, r => el('tr',null,
    el('td',null,el('span',{class:'chip review',text:r.kind})), el('td',{class:'nowrap',text:r.id}), el('td',{class:'nowrap',text:r.name}),
    el('td',{class:'nowrap',text:r.date}), el('td',{class:'nowrap',text:r.t}), el('td',null,r.lab?stateChip(r.lab):''),
    el('td',{text:r.s}), el('td',{class:'num',text:pct(r.conf)}), el('td',null,whyList(r.why.split(' | ')))), 50);
}
function drawFix(){
  const q = $('#q-fix').value.trim().toLowerCase(), f = $('#f-fix').value;
  const rows = D.corrections.filter(r => match(q, r.id, r.name) && (!f || r.act===f));
  $('#fix-count').textContent = `${rows.length}건`;
  pagedTable($('#t-fix tbody'), $('#more-fix'), rows, r => el('tr',null,
    el('td',null,chipFor(r.act)), el('td',{class:'nowrap',text:r.id}), el('td',{class:'nowrap',text:r.name}),
    el('td',{class:'nowrap',text:r.t}), el('td',null,stateChip(r.lab)), el('td',null,stateChip(r.s)),
    el('td',{class:'num',text:pct(r.conf)}), el('td',null,whyList(r.why.concat(['증거 기여도: '+r.ev])))), 50);
}

// 타임라인 (근무일별 0시 ~ 다음날 12시)
const NS='http://www.w3.org/2000/svg';
const sv = (tag, attrs, ...kids) => { const e = document.createElementNS(NS, tag); for (const [k,v] of Object.entries(attrs||{})) e.setAttribute(k,v); for (const k of kids) e.append(k); return e; };
function timeline(emp){
  const H=36, L=96, R=10, W=900, rowH=20, top=22, rows=emp.rows;
  const x = h => L + (Math.max(0,Math.min(H,h))/H)*(W-L-R);
  const svg = sv('svg',{class:'tl',viewBox:`0 0 ${W} ${top+rows.length*rowH+6}`,role:'img','aria-label':emp.name+' 출퇴근 타임라인'});
  // 야간(22~06시) 음영
  for (const [a,b] of [[0,6],[22,30]]) svg.append(sv('rect',{x:x(a),y:top-4,width:x(b)-x(a),height:rows.length*rowH+4,fill:'var(--night)'}));
  for (let h=0; h<=H; h+=3){
    svg.append(sv('line',{x1:x(h),x2:x(h),y1:top-4,y2:top+rows.length*rowH,stroke:'var(--border)','stroke-width':h%24===0?1.2:.6}));
    const t = sv('text',{x:x(h),y:12,'text-anchor':'middle'}); t.textContent = h<24 ? h+'시' : (h===24 ? '다음날 0시' : (h-24)+'시'); svg.append(t);
  }
  rows.forEach((d,i) => {
    const y = top + i*rowH + rowH/2;
    const lab = sv('text',{x:4,y:y+4}); lab.textContent = d.date.slice(5); svg.append(lab);
    if (d.status.startsWith('검토')) svg.append(sv('circle',{cx:L-10,cy:y,r:3.5,fill:'var(--review)'}));
    else if (d.corrected) svg.append(sv('circle',{cx:L-10,cy:y,r:3.5,fill:'var(--fix)'}));
    for (const s of d.sessions){
      for (const [a,b] of s.iv) svg.append(sv('rect',{x:x(a),y:y-4,width:Math.max(1.5,x(b)-x(a)),height:8,rx:2,fill:'var(--bar)','fill-opacity':.28}));
      for (const m of s.marks){
        const changed = m.lab && m.lab!==m.s, rev = m.act==='검토필요'||m.act==='정정제안';
        const stroke = rev ? 'var(--review)' : changed ? 'var(--fix)' : 'var(--surface)';
        const sw = (rev||changed) ? 2.4 : 1.2, fill = m.s==='IN' ? 'var(--in)' : 'var(--out)';
        const shape = m.s==='IN' ? sv('circle',{cx:x(m.h),cy:y,r:4.6,fill,stroke,'stroke-width':sw})
                                 : sv('rect',{x:x(m.h)-4.4,y:y-4.4,width:8.8,height:8.8,rx:1.5,fill,stroke,'stroke-width':sw});
        const title = sv('title',{});
        const kor = v => v==='IN'?'출근':v==='OUT'?'퇴근':'(선택없음)';
        title.textContent = `${m.t} · 선택 ${kor(m.lab)} → AI ${kor(m.s)} · 확신도 ${pct(m.conf)} · ${m.act}${m.n>1?` · ${m.n}회 입력`:''}`+(m.why.length?'\n- '+m.why.join('\n- '):'');
        shape.append(title); svg.append(shape);
      }
    }
  });
  return svg;
}
function legend(){
  const item = (svgKids, text) => { const s = sv('svg',{width:14,height:14,viewBox:'0 0 14 14'}); for (const k of svgKids) s.append(k); return el('span',null,s,text); };
  return el('div',{class:'legend'},
    item([sv('circle',{cx:7,cy:7,r:4.6,fill:'var(--in)'})],'출근(복귀)'),
    item([sv('rect',{x:2.6,y:2.6,width:8.8,height:8.8,rx:1.5,fill:'var(--out)'})],'퇴근(외출)'),
    item([sv('circle',{cx:7,cy:7,r:4.6,fill:'var(--in)',stroke:'var(--fix)','stroke-width':2.4})],'AI가 정정'),
    item([sv('circle',{cx:7,cy:7,r:4.6,fill:'var(--in)',stroke:'var(--review)','stroke-width':2.4})],'검토 필요'),
    item([sv('rect',{x:1,y:4,width:12,height:6,rx:2,fill:'var(--bar)','fill-opacity':.28})],'사업장 내 근무'),
    item([sv('rect',{x:1,y:1,width:12,height:12,fill:'var(--night)',stroke:'var(--border)'})],'야간(22~06시)'),
    el('span',{text:'· 점에 마우스를 올리면 판정 근거가 보입니다'}));
}
function dayTable(emp){
  const t = el('table');
  t.append(el('thead',null,el('tr',null,...['근무일','출근','퇴근','실근무','외출','야간','상태','메뉴 기준','차이','비고'].map((h,i)=>el('th',{class:i>=3&&i<=5||i===8?'num':'',text:h})))));
  const tb = el('tbody');
  for (const d of emp.rows){
    const st = d.status.startsWith('검토') ? 'review' : d.status==='자동정정' ? 'fix' : 'ok';
    tb.append(el('tr',null, el('td',{class:'nowrap',text:d.date}), el('td',{class:'nowrap',text:d.in}), el('td',{class:'nowrap',text:d.out}),
      el('td',{class:'num',text:d.work}), el('td',{class:'num',text:d.outing}), el('td',{class:'num',text:d.night}),
      el('td',null,el('span',{class:'chip '+st,text:d.status})),
      el('td',{class:'nowrap',text:d.base + (d.baseProblem && d.baseWhy ? ` (${d.baseWhy})` : '')}),
      el('td',{class:'num',text:d.diff==='0:00'?'':d.diff}), el('td',{class:'why',text:d.notes})));
  }
  t.append(tb); return el('div',{class:'tbl-wrap'},t);
}
function drawEmps(){
  const q = $('#q-emp').value.trim().toLowerCase(), f = $('#f-emp').value, box = $('#emps');
  box.textContent='';
  const list = D.employees.filter(e => match(q, e.id, e.name) && (!f || (f==='review' ? e.review>0 : e.corrected>0)));
  if (!list.length) box.append(el('div',{class:'card empty',text:'해당 직원이 없습니다.'}));
  for (const e of list){
    const meta = el('span',{class:'emp-meta'},
      el('span',{text:`근무 ${e.days}일 · ${e.work}`}),
      el('span',{text:`평소 출근 ${e.typIn.join(' / ')||'-'} · 퇴근 ${e.typOut.join(' / ')||'-'}`}),
      el('span',{text:`메뉴 정확도 약 ${e.acc}%`}));
    const sum = el('summary',null, el('span',{class:'emp-name',text:`${e.name} (${e.id})`}), meta,
      e.corrected ? el('span',{class:'chip fix',text:`자동정정 ${e.corrected}`}) : null,
      e.review ? el('span',{class:'chip review',text:`검토 ${e.review}`}) : null);
    const det = el('details',{class:'emp'}, sum);
    det.addEventListener('toggle', () => { if (det.open && !det.dataset.ready){ det.dataset.ready='1';
      det.append(el('div',{class:'emp-body'}, legend(), timeline(e), dayTable(e))); } }, {once:false});
    box.append(det);
  }
}
$('#q-review').addEventListener('input', drawReview);
$('#q-fix').addEventListener('input', drawFix); $('#f-fix').addEventListener('change', drawFix);
$('#q-emp').addEventListener('input', drawEmps); $('#f-emp').addEventListener('change', drawEmps);
drawReview(); drawFix(); drawEmps();

const S = D.meta.settings;
$('#notice').textContent = '';
$('#notice').append(
  el('div',{text:`판정 기준 — 자동 정정: 확신도 ${Math.round(S.auto*100)}% 이상 / 검토: ${Math.round(S.review*100)}% 미만 / 중복 입력 묶음: ${S.dup}분 / 새 근무 판단: 퇴근 후 ${S.gap}시간 / 한 구간 최대: ${S.maxIv}시간`}),
  el('div',{text:'실근무 = 출근~퇴근 구간의 합(외출 제외, 휴게시간 미공제). 근무일 = 출근한 날짜(자정을 넘는 근무 포함). 출근은 처음 찍은 시각, 퇴근은 마지막 찍은 시각을 씁니다.'}),
  el('div',{text:'원본 기록은 수정하지 않으며, 모든 판정과 근거는 corrected_punches.csv 에 원본 행 번호와 함께 남습니다. 이 보고서에는 개인 근태 정보가 들어 있으므로 열람 권한이 있는 담당자만 보도록 관리하세요.'}));
})();
</script>
</body>
</html>
"""
