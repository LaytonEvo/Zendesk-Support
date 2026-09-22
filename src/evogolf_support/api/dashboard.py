"""The admin dashboard: is Zendesk being used, and are the drafts trusted?

Server-rendered so it needs nothing but a browser and a token. Every number
comes from the corpus, and the adoption figures come from comparing each
draft against the reply the agent actually sent.

It is written to read correctly when there is no data yet, because for the
first week there will not be, and a dashboard full of zeroes dressed up as
results is worse than one that says plainly that it is still counting.
"""

from __future__ import annotations

from html import escape
from typing import Any

# Categorical slots 1 and 2 from the reference palette, validated for both
# surfaces: worst adjacent CVD dE 24.7 light / 26.8 dark, contrast >= 3:1.
CSS = """
:root{
  --ground:#f1f4f1;--surface:#fff;--ink:#16211c;--soft:#57685f;--faint:#8b9b93;
  --line:#dce2dd;--hair:#eceff0;
  --good:#1e7a4f;--warn:#b26a00;--bad:#b3332f;
  --s1:#2a78d6;--s2:#eb6834;
  --ramp-3:#16436f;--ramp-2:#2a78d6;--ramp-1:#a8cdf2;
  --display:"Bricolage Grotesque",ui-sans-serif,system-ui,sans-serif;
  --body:"Public Sans",ui-sans-serif,system-ui,-apple-system,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#111614;--surface:#191f1c;--ink:#e7ece9;--soft:#9aa9a1;--faint:#7c8b83;
  --line:#2a332e;--hair:#222a26;
  --good:#5bbd8b;--warn:#d79a3c;--bad:#e07a76;
  --s1:#3987e5;--s2:#d95926;
  --ramp-3:#cfe3f8;--ramp-2:#3987e5;--ramp-1:#1c4a78;
}}
:root[data-theme="dark"]{
  --ground:#111614;--surface:#191f1c;--ink:#e7ece9;--soft:#9aa9a1;--faint:#7c8b83;
  --line:#2a332e;--hair:#222a26;
  --good:#5bbd8b;--warn:#d79a3c;--bad:#e07a76;
  --s1:#3987e5;--s2:#d95926;
  --ramp-3:#cfe3f8;--ramp-2:#3987e5;--ramp-1:#1c4a78;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--body);
     font-size:16px;line-height:1.55}
.wrap{max-width:900px;margin:0 auto;padding-block:36px 64px;
      padding-left:20px;padding-right:20px;display:flex;flex-direction:column;gap:34px}
h1{font-family:var(--display);font-weight:800;font-size:clamp(26px,5vw,36px);
   margin:0 0 6px;letter-spacing:-.02em}
h2{font-family:var(--display);font-weight:600;font-size:19px;margin:0 0 3px}
p{margin:0 0 12px;max-width:62ch}p:last-child{margin-bottom:0}
.sub{color:var(--soft);font-size:15px;margin:0}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.14em;
         text-transform:uppercase;color:var(--soft);margin:0 0 10px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:22px}
.hero{display:flex;flex-wrap:wrap;gap:26px;align-items:baseline}
.hero .big{font-family:var(--display);font-weight:800;font-size:clamp(46px,11vw,76px);
           line-height:.95;letter-spacing:-.03em}
.hero .of{color:var(--soft);font-size:15px;max-width:34ch}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
       background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.tiles>div{background:var(--surface);padding:16px 18px}
.tiles .n{font-family:var(--display);font-weight:700;font-size:29px;line-height:1.1;
          font-variant-numeric:tabular-nums}
.tiles .k{font-size:13px;color:var(--soft);margin-top:3px}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:13.5px;color:var(--soft);
        margin:0 0 14px}
.legend span{display:inline-flex;align-items:center;gap:7px}
.sw{width:11px;height:11px;border-radius:3px;display:inline-block}
.split{display:flex;height:34px;border-radius:7px;overflow:hidden;gap:2px;background:var(--surface)}
.split div{display:flex;align-items:center;justify-content:center;
           font-size:13px;font-weight:600;font-variant-numeric:tabular-nums;color:#fff}
.chart{width:100%;height:auto;display:block}
.chart text{font-family:var(--body);fill:var(--soft)}
.bar{transition:opacity .12s}
.bar:hover{opacity:.75;cursor:default}
table{border-collapse:collapse;width:100%;font-size:15px}
th{text-align:left;font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;
   text-transform:uppercase;color:var(--soft);padding:0 10px 8px 0;border-bottom:1px solid var(--line)}
td{padding:10px 10px 10px 0;border-bottom:1px solid var(--hair);font-variant-numeric:tabular-nums}
.empty{padding:26px 20px;text-align:center;color:var(--soft);font-size:15px;
       border:1px dashed var(--line);border-radius:10px}
.note{font-size:13.5px;color:var(--faint);margin-top:14px}
.verdict{font-size:16px}
.verdict b{display:block;font-family:var(--display);font-size:19px;margin-bottom:4px}
.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.foot{font-family:var(--mono);font-size:11.5px;color:var(--faint);
      display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
@media (max-width:520px){.hero{gap:14px}}
"""


def _tile(value: Any, label: str) -> str:
    return f'<div><div class="n">{escape(str(value))}</div><div class="k">{escape(label)}</div></div>'


def _daily_chart(daily: list[dict[str, Any]]) -> str:
    """Grouped bars: tickets and drafts per day. Two series, so a legend is
    always shown and each bar carries its value on hover."""
    if not any(d["tickets"] or d["drafts"] for d in daily):
        return ('<div class="empty">No tickets yet in this window. '
                'The shape will appear here once mail starts arriving.</div>')

    w, h = 860, 200
    pad_l, pad_b, pad_t = 30, 26, 10
    top = max(1, max(max(d["tickets"], d["drafts"]) for d in daily))
    slot = (w - pad_l) / len(daily)
    bw = min(14, (slot - 6) / 2)
    plot = h - pad_b - pad_t

    bars, labels, grid = [], [], []
    for i in range(3):
        value = round(top * i / 2)
        y = pad_t + plot - (value / top) * plot
        grid.append(f'<line x1="{pad_l}" x2="{w}" y1="{y:.1f}" y2="{y:.1f}" '
                    f'stroke="currentColor" stroke-opacity=".14"/>')
        grid.append(f'<text x="0" y="{y + 4:.1f}" font-size="11">{value}</text>')

    for i, day in enumerate(daily):
        x = pad_l + i * slot + 3
        for offset, (key, colour) in enumerate(
                (("tickets", "var(--s1)"), ("drafts", "var(--s2)"))):
            value = day[key]
            if not value:
                continue
            bh = max(2.0, (value / top) * plot)
            bx = x + offset * (bw + 2)
            bars.append(
                f'<rect class="bar" x="{bx:.1f}" y="{pad_t + plot - bh:.1f}" '
                f'width="{bw:.1f}" height="{bh:.1f}" rx="4" fill="{colour}">'
                f'<title>{day["date"]} — {value} {key}</title></rect>'
            )
        if i % 2 == 0 or i == len(daily) - 1:
            labels.append(f'<text x="{x + bw:.1f}" y="{h - 8}" font-size="10.5" '
                          f'text-anchor="middle">{day["date"][5:]}</text>')

    return (
        '<div class="legend">'
        '<span><i class="sw" style="background:var(--s1)"></i>Tickets arrived</span>'
        '<span><i class="sw" style="background:var(--s2)"></i>Drafts written</span>'
        '</div>'
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Tickets and drafts per day for the last {len(daily)} days">'
        + "".join(grid) + "".join(bars) + "".join(labels) + "</svg>"
    )


def _adoption_split(a: dict[str, Any]) -> str:
    judged = a["judged"]
    if not judged:
        return ('<div class="empty">No drafts have been answered yet, so there is '
                'nothing to measure. This fills in as the team replies.</div>')
    parts = [("used_as_is", "Sent as written", "var(--ramp-3)"),
             ("edited", "Edited then sent", "var(--ramp-2)"),
             ("low_overlap", "Little or no overlap", "var(--ramp-1)")]
    bar = "".join(
        f'<div style="flex:{a[key]};background:{colour};'
        f'color:{"#fff" if key != "low_overlap" else "var(--ink)"}">'
        f'{a[key] if a[key] / judged > 0.08 else ""}</div>'
        for key, _, colour in parts if a[key]
    )
    legend = "".join(
        f'<span><i class="sw" style="background:{colour}"></i>{label} '
        f'&middot; <b>{a[key]}</b></span>' for key, label, colour in parts
    )
    return f'<div class="legend">{legend}</div><div class="split">{bar}</div>'


def _verdict(usage: dict[str, Any], a: dict[str, Any]) -> str:
    lines = []
    if usage["tickets"] == 0:
        lines.append('<b class="warn">Nothing has come through yet</b>'
                     'No tickets in this window. Either it is very quiet, or mail '
                     'has stopped reaching Zendesk — worth checking if it lasts a day.')
    elif usage["unanswered"] > usage["tickets_answered"]:
        lines.append(f'<b class="warn">Zendesk is receiving, but not being worked</b>'
                     f'{usage["tickets"]} tickets arrived and {usage["tickets_answered"]} '
                     f'were answered in Zendesk. The rest were either answered elsewhere '
                     f'or not at all.')
    else:
        lines.append(f'<b class="good">Zendesk is being used</b>'
                     f'{usage["tickets_answered"]} of {usage["tickets"]} tickets were '
                     f'answered from Zendesk.')

    pct = a["adoption_percent"]
    if pct is None:
        lines.append('<b>Too early to judge the drafts</b>'
                     'No drafted ticket has been replied to yet.')
    elif pct >= 60:
        lines.append(f'<b class="good">The drafts are being relied on</b>'
                     f'{pct}% of replies started from the draft.')
    elif pct >= 30:
        lines.append(f'<b class="warn">The drafts are used about half the time</b>'
                     f'{pct}% of replies clearly started from the draft. Worth asking '
                     f'the team which ones they threw away, and why.')
    else:
        lines.append(f'<b class="bad">Little sign the drafts are being used</b>'
                     f'Only {pct}% of replies clearly started from the draft. That could '
                     f'mean the drafts are being ignored, or rewritten so heavily that '
                     f'nothing of them survives — either way it is worth asking the team '
                     f'before changing anything.')
    return "".join(f'<p class="verdict">{line}</p>' for line in lines)


def render(report: dict[str, Any], days: int) -> str:
    usage, a = report["usage"], report["adoption"]
    pct = a["adoption_percent"]
    channels = "".join(
        f"<tr><td>{escape(str(k))}</td><td>{v}</td></tr>"
        for k, v in report["channels"].items()
    ) or '<tr><td colspan="2" style="color:var(--soft)">Nothing yet</td></tr>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Support dashboard</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&family=Public+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400&display=swap">
<style>{CSS}</style></head><body><div class="wrap">

<header>
  <p class="eyebrow">Evolution Golf &middot; last {days} days</p>
  <h1>Support dashboard</h1>
  <p class="sub">Is Zendesk being used, and are the drafted replies being relied on?</p>
</header>

<div class="card">
  <div class="hero">
    <div class="big">{pct if pct is not None else "—"}{"%" if pct is not None else ""}</div>
    <div class="of">of replies started from the suggested draft.<br>
      Measured by comparing each draft with the reply the agent actually sent —
      not by asking. It counts only replies that clearly began as the draft, so
      it understates rather than flatters.</div>
  </div>
</div>

<section>
  <h2>What that means</h2>
  <div class="card">{_verdict(usage, a)}</div>
</section>

<section>
  <h2>Zendesk activity</h2>
  <p class="sub" style="margin-bottom:14px">Tickets arriving, and replies going out from Zendesk.</p>
  <div class="tiles">
    {_tile(usage["tickets"], "tickets arrived")}
    {_tile(usage["tickets_per_working_day"], "per working day")}
    {_tile(usage["tickets_answered"], "answered in Zendesk")}
    {_tile(usage["agent_replies"], "replies sent")}
    {_tile(usage["unanswered"], "no reply in Zendesk")}
  </div>
</section>

<section>
  <h2>Day by day</h2>
  <div class="card">{_daily_chart(report["daily"])}
  <p class="note">A draft can land on a ticket raised earlier, when a customer
  writes again — so the two bars are not always a like-for-like pair.</p></div>
</section>

<section>
  <h2>How the drafts were used</h2>
  <div class="card">{_adoption_split(a)}
  <p class="note">A reply that keeps the facts but rewrites every sentence
  scores about the same as one written independently, so "little or no overlap"
  means exactly that and nothing more. Read the top number as a floor.<br>
  {a["drafts"]} drafts written &middot; {a["judged"]} replied to and
  measurable &middot; {a["no_reply_yet"]} awaiting a reply &middot;
  {a["handover"]} handed straight to an agent by policy, so no draft was written.
  {"Average closeness to the draft: " + str(a["average_similarity"]) + "." if a["average_similarity"] is not None else ""}</p></div>
</section>

<section>
  <h2>Where conversations came from</h2>
  <div class="card"><table>
    <tr><th>Channel</th><th>Tickets</th></tr>{channels}
  </table>
  <p class="note">Anything tagged <code>gmail_online</code> is imported history
  from the online@ mailbox, not new Zendesk activity.</p></div>
</section>

<p class="foot"><span>Generated {escape(report["generated_at"])}</span>
<span>Counts only &middot; no customer details</span></p>

</div></body></html>"""
