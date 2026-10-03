"""電脳NEWS（旧・朝刊マーケットダッシュボード） - 静的ページ生成

データ取得 → Claude で「今朝の要点」を作成 → site/index.html を書き出す。
GitHub Actions が毎朝 6:30 JST に実行し、GitHub Pages に公開する（.github/workflows/morning.yml）。

    python3 build.py              # 取得＋要約＋ページ生成
    python3 build.py --no-ai      # 要約なしで生成（API キー不要・見た目の確認用）
    python3 build.py --from-json  # 取得せず data/ の最新 JSON から生成
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from data_fetcher import DATA_DIR, JST, fetch_all, load_latest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("morning_build")

ROOT = Path(__file__).resolve().parent
SITE_DIR = ROOT / "site"
ASSETS_DIR = ROOT / "assets"
SITE_URL = "https://cap1974jin-design.github.io/morning-dashboard/"
# LINE・X などでリンクを貼った時のプレビューに出る説明文（無いと本文の先頭が切り取られる）
DESCRIPTION = "相場とニュースを、ひと目で。日経平均・為替・米国株・金利と、各社の主要ニュースを毎時更新でまとめています。"
MODEL = os.environ.get("MORNING_MODEL", "claude-opus-5-5")
PICKS_PER_CATEGORY = 3
WEEKDAYS = "月火水木金土日"

# 値の単位（%表示や小数桁）を銘柄ごとに決める
PERCENT_UNIT = {"米10年債利回り"}
DECIMALS = {"USD/JPY": 2, "EUR/JPY": 2, "GBP/JPY": 2, "米10年債利回り": 2, "VIX": 2}


# ---------------------------------------------------------------- 要約（Claude）

SYSTEM_PROMPT = """あなたは日本の経済紙の朝刊デスクです。読者は日本在住の経営者・個人投資家で、
朝の数分で「今日の世界」を把握したいと考えています。

与えられた相場の数字とニュース見出しだけを根拠に、朝刊の要点をまとめてください。
- 見出しに無い事実・数字を足さない。推測は推測と分かる書き方にする。
- 文体は冷静な常体（「〜した」「〜だ」）。煽り・誇張・感嘆符は使わない。
- 個別銘柄の売買推奨や投資助言はしない。
- points は「今日知っておくべきこと」5つ。1つ60字以内。相場・政治経済・国際・テックを偏りなく。
- picks は各分野から重要な順に3本。why はその記事がなぜ重要かを40字以内で。id は与えられたものだけを使う。
- exclude には、朝刊に載せる価値の無い記事（生活・グルメ・買い物レビュー・自己啓発・個別銘柄の株価ページ・PR など）の id を入れる。"""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "lead": {"type": "string", "description": "今朝を一文で表す見出し（30字以内）"},
        "points": {"type": "array", "items": {"type": "string"}},
        "market_comment": {"type": "string", "description": "相場の流れを2〜3文で"},
        "watch": {"type": "string", "description": "今日ここに注目、を1文で"},
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "why": {"type": "string"}},
                "required": ["id", "why"],
                "additionalProperties": False,
            },
        },
        "exclude": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["lead", "points", "market_comment", "watch", "picks", "exclude"],
    "additionalProperties": False,
}


def _article_ids(news: dict) -> dict:
    """記事に短い ID（例 c0-3）を振る。要約側はこの ID で記事を指す"""
    ids = {}
    for ci, (category, articles) in enumerate(news.items()):
        for ai, article in enumerate(articles):
            ids[f"c{ci}-{ai}"] = (category, article)
    return ids


def _summary_input(data: dict, ids: dict) -> str:
    lines = [f"日付: {data['date']}", "", "## 相場（前日比）"]
    for category, items in data.get("market", {}).items():
        for name, v in items.items():
            lines.append(f"- {category} / {name}: {v['price']:,} ({v['change_pct']:+.2f}%)")
    current = None
    for aid, (category, article) in ids.items():
        if category != current:
            current = category
            lines += ["", f"## ニュース：{category}"]
        lines.append(f"- [{aid}] {article['title']}（{article['source']}）")
    return "\n".join(lines)


def summarize(data: dict) -> Optional[dict]:
    """Claude に今朝の要点を作らせる。失敗しても None を返してページ生成は続ける"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY が無いため要約を省略")
        return None
    import anthropic

    ids = _article_ids(data.get("news", {}))
    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _summary_input(data, ids)}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
    except anthropic.APIStatusError as e:
        logger.error(f"要約 API エラー ({e.status_code}): {e.message}")
        return None
    except anthropic.APIConnectionError as e:
        logger.error(f"要約 API に接続できず: {e}")
        return None

    if response.stop_reason in ("refusal", "max_tokens"):
        logger.error(f"要約が完了せず: stop_reason={response.stop_reason}")
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    summary = json.loads(text)

    picks = {}
    for pick in summary.get("picks", []):
        if pick["id"] in ids:
            category, article = ids[pick["id"]]
            if len(picks.setdefault(category, [])) < PICKS_PER_CATEGORY:
                picks[category].append({**article, "why": pick["why"]})
    summary["picks"] = picks
    summary["exclude"] = [ids[i][1]["title"] for i in summary.get("exclude", []) if i in ids]
    summary["model"] = response.model
    summary["usage"] = {"input": response.usage.input_tokens, "output": response.usage.output_tokens}
    logger.info(f"要約完了: in={response.usage.input_tokens} out={response.usage.output_tokens}")
    return summary


# ---------------------------------------------------------------- ページ生成

def esc(s) -> str:
    return html.escape(str(s), quote=True)


def safe_url(url: str) -> str:
    """配信元の URL は http(s) だけ通す（javascript: などをリンクにしない）"""
    url = str(url or "").strip()
    return esc(url) if url.lower().startswith(("https://", "http://")) else "#"


def fmt_value(name: str, value: float) -> str:
    digits = DECIMALS.get(name, 2 if value < 1000 else 0)
    s = f"{value:,.{digits}f}"
    return s + "%" if name in PERCENT_UNIT else s


def sparkline(history: list, up: bool) -> str:
    if len(history) < 2:
        return '<div class="spark spark-empty"></div>'
    w, h, pad = 120, 32, 2
    lo, hi = min(history), max(history)
    span = (hi - lo) or 1
    step = (w - pad * 2) / (len(history) - 1)
    pts = [(pad + i * step, pad + (h - pad * 2) * (1 - (v - lo) / span)) for i, v in enumerate(history)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pad},{h} " + line + f" {pts[-1][0]:.1f},{h}"
    cls = "up" if up else "down"
    return (
        f'<svg class="spark {cls}" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">'
        f'<polygon points="{area}" class="spark-area"/><polyline points="{line}" class="spark-line"/>'
        f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="2.2" class="spark-dot"/></svg>'
    )


def render_tile(name: str, v: dict) -> str:
    up = v["change"] >= 0
    big = abs(v["change_pct"]) >= 1.5
    sign = "+" if up else "−"
    note = f'<span class="note">{esc(v["note"])}</span>' if v.get("note") else ""
    return f"""
      <div class="tile {'up' if up else 'down'}{' big' if big else ''}">
        <div class="tile-head"><span class="tile-name">{esc(name)}</span>{note}</div>
        <div class="tile-value">{fmt_value(name, v['price'])}</div>
        <div class="tile-delta">{sign}{abs(v['change_pct']):.2f}%<span class="abs">{sign}{abs(v['change']):,.2f}</span></div>
        {sparkline(v.get('history', []), up)}
      </div>"""


def render_market(market: dict) -> str:
    """分類ごとの折りたたみ帯。閉じていても騰落率の一覧は見え、押すとカードが順に出てくる"""
    blocks = []
    for category, items in market.items():
        chips = "".join(
            f'<span class="chip {"up" if v["change"] >= 0 else "down"}">{esc(n)}'
            f'<b>{"+" if v["change"] >= 0 else "−"}{abs(v["change_pct"]):.2f}%</b></span>'
            for n, v in items.items()
        )
        tiles = "".join(
            render_tile(n, v).replace('<div class="tile ', f'<div style="--i:{i}" class="tile ', 1)
            for i, (n, v) in enumerate(items.items())
        )
        blocks.append(f"""
      <div class="mgroup">
        <button class="mbar" aria-expanded="false">
          <span class="mname">{esc(category)}</span>
          <span class="chips">{chips}</span>
          <span class="chev" aria-hidden="true"></span>
        </button>
        <div class="mbody"><div class="mbody-inner"><div class="tiles">{tiles}</div></div></div>
      </div>""")
    return "".join(blocks)


def render_summary(summary: Optional[dict]) -> str:
    if not summary:
        return ""
    points = "".join(f"<li>{esc(p)}</li>" for p in summary["points"])
    return f"""
    <section class="brief">
      <div class="eyebrow">今朝の要点</div>
      <h2 class="lead">{esc(summary['lead'])}</h2>
      <ol class="points">{points}</ol>
      <div class="watch"><span>今日の注目</span>{esc(summary['watch'])}</div>
    </section>"""


SLIDE_SIZE = 4


def _time_label(article: dict, now: datetime) -> str:
    if not article.get("time_jst"):
        return ""
    t = datetime.fromisoformat(article["time_jst"])
    return f"{t:%H:%M}" if t.date() == now.date() else f"{t.month}/{t.day} {t:%H:%M}"


def _coverage_label(article: dict) -> str:
    n = article.get("coverage", 1)
    return f'<span class="cov">{n}社が報道</span>' if n >= 2 else ""


def _also_label(article: dict) -> str:
    others = article.get("sources", [])[1:]
    return f'<div class="also">ほかに {esc("・".join(others))}</div>' if others else ""


def render_news(news: dict, summary: Optional[dict], now: datetime) -> str:
    """分野ごとのタブ。中身は見出し4本ずつのスライドを横にめくる"""
    picks = (summary or {}).get("picks", {})
    excluded = set((summary or {}).get("exclude", []))
    tabs, panels = [], []
    for i, (category, articles) in enumerate(news.items()):
        tid = f"t{i}"
        tabs.append(
            f'<input type="radio" name="news" id="{tid}" {"checked" if i == 0 else ""}>'
            f'<label for="{tid}">{esc(category)}</label>'
        )
        chosen = picks.get(category, [])
        chosen_titles = {a["title"] for a in chosen}
        rest = [a for a in articles if a["title"] not in chosen_titles and a["title"] not in excluded]
        ordered = chosen + rest
        slides = []
        for start in range(0, len(ordered), SLIDE_SIZE):
            items = "".join(
                f"""<a class="item{' picked' if a.get('why') or a.get('coverage', 1) >= 2 else ''}" href="{safe_url(a['link'])}" target="_blank" rel="noopener">
                  <div class="item-meta"><span class="badge">{esc(a['source'])}</span>{_coverage_label(a)}<time>{_time_label(a, now)}</time></div>
                  <div class="item-title">{esc(a['title'])}</div>
                  {f'<div class="item-why">{esc(a["why"])}</div>' if a.get('why') else ''}{_also_label(a)}</a>"""
                for a in ordered[start:start + SLIDE_SIZE]
            )
            slides.append(f'<div class="slide">{items}</div>')
        total = len(slides)
        panels.append(f"""
        <div class="panel" data-for="{tid}">
          <div class="slider">{''.join(slides)}</div>
          <div class="slider-nav">
            <button class="prev" aria-label="前へ">‹</button>
            <span class="count"><b>1</b> / {total}</span>
            <button class="next" aria-label="次へ">›</button>
          </div>
        </div>""")
    return f'<div class="newsbox">{"".join(tabs)}<div class="panels">{"".join(panels)}</div></div>'


SLIDER_JS = """
document.querySelectorAll('.mgroup').forEach(function (g) {
  g.querySelector('.mbar').onclick = function () {
    var open = g.classList.toggle('open');
    this.setAttribute('aria-expanded', open);
  };
});
var toggleAll = document.querySelector('.toggle-all');
toggleAll.onclick = function () {
  var groups = document.querySelectorAll('.mgroup');
  var open = !Array.prototype.every.call(groups, function (g) { return g.classList.contains('open'); });
  groups.forEach(function (g) { g.classList.toggle('open', open); g.querySelector('.mbar').setAttribute('aria-expanded', open); });
  toggleAll.textContent = open ? 'すべて閉じる' : 'すべて開く';
};
document.querySelectorAll('.panel').forEach(function (panel) {
  var slider = panel.querySelector('.slider');
  var count = panel.querySelector('.count b');
  function step() { return slider.querySelector('.slide').offsetWidth + 12; }
  var total = slider.querySelectorAll('.slide').length;
  function update() {
    var atEnd = slider.scrollLeft + slider.clientWidth >= slider.scrollWidth - 4;
    count.textContent = atEnd ? total : Math.min(total, Math.round(slider.scrollLeft / step()) + 1);
  }
  panel.querySelector('.prev').onclick = function () { slider.scrollBy({left: -step(), behavior: 'smooth'}); };
  panel.querySelector('.next').onclick = function () { slider.scrollBy({left: step(), behavior: 'smooth'}); };
  slider.addEventListener('scroll', function () { window.requestAnimationFrame(update); });
});
"""


def render_page(data: dict, summary: Optional[dict]) -> str:
    fetched = datetime.fromisoformat(data["fetched_at"]).astimezone(JST)
    date_label = f"{fetched.year}年{fetched.month}月{fetched.day}日（{WEEKDAYS[fetched.weekday()]}）"
    market_comment = (
        f'<p class="mcomment">{esc(summary["market_comment"])}</p>' if summary else ""
    )
    tab_css = "".join(
        f'#t{i}:checked ~ .panels [data-for="t{i}"]{{display:block}}'
        f'#t{i}:checked + label{{color:var(--ink);border-color:var(--accent)}}'
        for i in range(len(data.get("news", {})))
    )
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>電脳NEWS</title>
<meta name="robots" content="noindex">
<meta name="description" content="{DESCRIPTION}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="電脳NEWS">
<meta property="og:title" content="電脳NEWS">
<meta property="og:description" content="{DESCRIPTION}">
<meta property="og:url" content="{SITE_URL}">
<meta property="og:image" content="{SITE_URL}og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="ja_JP">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="icon" href="favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<meta name="theme-color" content="#0e1013">
<link rel="manifest" href="manifest.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="電脳NEWS">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Noto+Sans+JP:wght@400;500;700&family=Noto+Serif+JP:wght@600;700&display=swap" rel="stylesheet">
<style>
:root{{
  --bg:#f6f5f1; --surface:#ffffff; --ink:#15171a; --sub:#5d636b; --faint:#9aa0a6;
  --line:#e6e3dc; --accent:#b8873b; --up:#0f8a5f; --down:#c4373a;
  --up-bg:rgba(15,138,95,.08); --down-bg:rgba(196,55,58,.08);
}}
@media (prefers-color-scheme: dark){{
  :root{{
    --bg:#0e1013; --surface:#171a1f; --ink:#eceae4; --sub:#a3a8b0; --faint:#6b7179;
    --line:#262a31; --accent:#d4a55a; --up:#34c38f; --down:#f0646a;
    --up-bg:rgba(52,195,143,.10); --down-bg:rgba(240,100,106,.10);
  }}
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--ink);font-family:"Inter","Noto Sans JP",system-ui,sans-serif;
  line-height:1.7;-webkit-font-smoothing:antialiased;font-feature-settings:"palt"}}
a{{color:inherit;text-decoration:none}}
.wrap{{max-width:1080px;margin:0 auto;padding:28px 16px 64px}}
header{{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:12px;
  border-bottom:2px solid var(--ink);padding-bottom:14px;margin-bottom:28px}}
.mast{{font-family:"Noto Serif JP",serif;font-size:clamp(26px,5vw,38px);font-weight:700;letter-spacing:.04em;line-height:1.1}}
.mast small{{display:block;font-family:"Inter",sans-serif;font-size:11px;font-weight:600;letter-spacing:.28em;color:var(--accent);margin-bottom:6px}}
.date{{text-align:right;font-size:13px;color:var(--sub);white-space:nowrap}}
.date b{{display:block;font-size:15px;color:var(--ink);font-weight:600}}
.eyebrow{{font-size:11px;font-weight:700;letter-spacing:.24em;color:var(--accent);margin-bottom:10px}}
section{{margin-bottom:40px}}
.brief{{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:28px clamp(18px,4vw,36px)}}
.lead{{font-family:"Noto Serif JP",serif;font-size:clamp(21px,3.6vw,28px);line-height:1.45;margin-bottom:18px}}
.points{{list-style:none;counter-reset:p}}
.points li{{counter-increment:p;position:relative;padding:12px 0 12px 40px;border-top:1px solid var(--line);font-size:15.5px}}
.points li::before{{content:counter(p,decimal-leading-zero);position:absolute;left:0;top:12px;
  font-family:"Inter";font-weight:700;font-size:13px;color:var(--accent)}}
.watch{{margin-top:18px;padding:14px 16px;border-radius:10px;background:var(--bg);font-size:15px}}
.watch span{{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.14em;color:var(--bg);
  background:var(--ink);border-radius:4px;padding:2px 8px;margin-right:10px;vertical-align:2px}}
.brief-empty p{{color:var(--sub)}}
.sec-head{{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:14px}}
.sec-head h2{{font-family:"Noto Serif JP",serif;font-size:22px}}
.mcomment{{color:var(--sub);font-size:14.5px;margin:-4px 0 18px}}
.toggle-all{{font:600 12px "Inter","Noto Sans JP",sans-serif;color:var(--sub);background:none;border:1px solid var(--line);
  border-radius:999px;padding:5px 14px;cursor:pointer}}
.toggle-all:hover{{color:var(--ink);border-color:var(--accent)}}
.mgroup{{border-bottom:1px solid var(--line)}}
.mgroup:first-of-type{{border-top:1px solid var(--line)}}
.mbar{{all:unset;box-sizing:border-box;width:100%;display:flex;align-items:center;gap:14px;padding:14px 4px;cursor:pointer}}
.mbar:focus-visible{{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}}
.mname{{flex:0 0 auto;font-size:14px;font-weight:700;letter-spacing:.06em;min-width:92px}}
.chips{{flex:1;display:flex;flex-wrap:wrap;gap:6px 14px;min-width:0}}
.chip{{font-size:12px;color:var(--sub);white-space:nowrap}}
.chip b{{font-family:"Inter";font-weight:600;margin-left:5px;font-variant-numeric:tabular-nums}}
.chip.up b{{color:var(--up)}} .chip.down b{{color:var(--down)}}
.chev{{flex:0 0 auto;width:9px;height:9px;border-right:2px solid var(--faint);border-bottom:2px solid var(--faint);
  transform:rotate(45deg);margin:-4px 6px 0 0;transition:transform .3s}}
.mgroup.open .chev{{transform:rotate(225deg);margin-top:4px}}
.mbody{{display:grid;grid-template-rows:0fr;transition:grid-template-rows .45s cubic-bezier(.2,.8,.2,1)}}
.mgroup.open .mbody{{grid-template-rows:1fr}}
.mbody-inner{{overflow:hidden}}
.mbody .tiles{{padding:2px 0 16px}}
.mbody .tile{{opacity:0;transform:translateY(-14px)}}
.mgroup.open .mbody .tile{{animation:drop .42s cubic-bezier(.2,.8,.2,1) forwards;animation-delay:calc(var(--i) * 55ms)}}
@keyframes drop{{to{{opacity:1;transform:none}}}}
@media (prefers-reduced-motion:reduce){{
  .mbody{{transition:none}} .mgroup.open .mbody .tile{{animation:none;opacity:1;transform:none}}
}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
.tile{{min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 14px 10px;position:relative;overflow:hidden}}
.tile.big.up{{background:var(--up-bg);border-color:transparent}}
.tile.big.down{{background:var(--down-bg);border-color:transparent}}
.tile-head{{display:flex;justify-content:space-between;align-items:center;gap:6px}}
.tile-name{{font-size:12.5px;font-weight:600;color:var(--sub)}}
.note{{font-size:10px;color:var(--faint);white-space:nowrap}}
.tile-value{{font-family:"Inter";font-size:21px;font-weight:700;letter-spacing:-.01em;margin-top:2px;font-variant-numeric:tabular-nums}}
.tile-delta{{font-family:"Inter";font-size:13px;font-weight:600;font-variant-numeric:tabular-nums}}
.tile.up .tile-delta{{color:var(--up)}} .tile.down .tile-delta{{color:var(--down)}}
.abs{{font-weight:500;opacity:.7;margin-left:6px;font-size:11.5px}}
.spark{{display:block;width:100%;height:30px;margin-top:6px}}
.spark-empty{{height:30px;margin-top:6px}}
.spark-line{{fill:none;stroke-width:1.6;vector-effect:non-scaling-stroke}}
.spark.up .spark-line{{stroke:var(--up)}} .spark.down .spark-line{{stroke:var(--down)}}
.spark.up .spark-area{{fill:var(--up-bg)}} .spark.down .spark-area{{fill:var(--down-bg)}}
.spark.up .spark-dot{{fill:var(--up)}} .spark.down .spark-dot{{fill:var(--down)}}
.newsbox{{display:flex;flex-wrap:wrap;gap:0 4px}}
.newsbox input{{position:absolute;opacity:0;pointer-events:none}}
.newsbox label{{cursor:pointer;font-size:14px;font-weight:600;color:var(--faint);padding:8px 12px;border-bottom:2px solid transparent}}
.panels{{flex:0 0 100%;min-width:0;width:100%;border-top:1px solid var(--line);padding-top:16px}}
.panel{{display:none;min-width:0}}
{tab_css}
.slider{{display:flex;gap:12px;overflow-x:auto;scroll-snap-type:x mandatory;scrollbar-width:none;
  -webkit-overflow-scrolling:touch;padding-bottom:2px}}
.slider::-webkit-scrollbar{{display:none}}
.slide{{flex:0 0 calc(50% - 6px);scroll-snap-align:start;display:grid;grid-template-rows:repeat(4,1fr);gap:8px;min-width:0}}
.item{{display:flex;flex-direction:column;gap:6px;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;padding:14px 16px;min-height:104px;transition:border-color .15s,transform .15s}}
.item:hover{{border-color:var(--accent);transform:translateY(-1px)}}
.item.picked{{border-left:3px solid var(--accent)}}
.item-meta{{display:flex;align-items:center;gap:6px}}
.item-meta .badge + time{{margin-left:auto}}
.badge{{font-size:10.5px;font-weight:700;letter-spacing:.04em;color:var(--accent);
  border:1px solid var(--line);border-radius:999px;padding:1px 9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:55%}}
.cov{{font-size:10.5px;font-weight:700;color:var(--bg);background:var(--accent);border-radius:999px;padding:1px 8px;white-space:nowrap;margin-right:auto}}
.also{{font-size:11.5px;color:var(--faint);margin-top:auto}}
.item-meta time{{font-family:"Inter";font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums}}
.item-title{{font-weight:600;font-size:15px;line-height:1.55;overflow-wrap:anywhere;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
.item-why{{color:var(--sub);font-size:13px}}
.slider-nav{{display:flex;align-items:center;justify-content:center;gap:18px;margin-top:14px}}
.slider-nav button{{width:38px;height:38px;border-radius:50%;border:1px solid var(--line);background:var(--surface);
  color:var(--ink);font-size:22px;line-height:1;cursor:pointer;transition:border-color .15s}}
.slider-nav button:hover{{border-color:var(--accent)}}
.count{{font-family:"Inter";font-size:13px;color:var(--faint);font-variant-numeric:tabular-nums;min-width:54px;text-align:center}}
.count b{{color:var(--ink)}}
footer{{border-top:1px solid var(--line);padding-top:16px;font-size:11.5px;color:var(--faint);line-height:1.8}}
@media (max-width:520px){{
  .tiles{{grid-template-columns:repeat(2,minmax(0,1fr))}}
  .date{{text-align:left}}
  .newsbox label{{font-size:13px;padding:8px 8px}}
  .slide{{flex-basis:100%}}
  .mbar{{flex-wrap:wrap;gap:6px 10px}}
  .mname{{min-width:0}}
  .chips{{order:3;flex-basis:100%}}
  .chev{{margin-left:auto}}
  .tile-value{{font-size:18px}}
  .points li{{font-size:15px}}
}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="mast"><small>DENNOU NEWS — MORNING BRIEF</small>電脳NEWS</div>
    <div class="date"><b>{date_label}</b>{fetched:%H:%M} 更新</div>
  </header>
  {render_summary(summary)}
  <section>
    <div class="sec-head"><h2>相場</h2><button class="toggle-all">すべて開く</button></div>
    {market_comment}
    {render_market(data.get('market', {}))}
  </section>
  <section>
    <div class="sec-head"><h2>ニュース</h2></div>
    {render_news(data.get('news', {}), summary, fetched)}
  </section>
  <footer>
    相場は前営業日終値との比較（Yahoo Finance／Yahoo!ファイナンス）。推移は直近約1か月。日経225先物は CME 円建て。<br>
    ニュースは各社 RSS と Google ニュースの見出し。同じ出来事は1本にまとめ、報じた社数の多い順に並べています。{'要点は見出しをもとに自動で要約したもので、投資助言ではありません。' if summary else ''}
  </footer>
</div>
<script>{SLIDER_JS}</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-ai", action="store_true", help="要約を作らない")
    parser.add_argument("--from-json", action="store_true", help="取得せず最新の JSON を使う")
    args = parser.parse_args()

    data = load_latest() if args.from_json else fetch_all()
    if not data:
        logger.error("データがありません")
        return 1

    summary = None if args.no_ai else summarize(data)
    if summary:
        data["summary"] = summary
        path = DATA_DIR / f"morning_{data['date']}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    elif args.from_json:
        summary = data.get("summary")

    SITE_DIR.mkdir(exist_ok=True)
    for asset in ASSETS_DIR.iterdir():
        shutil.copy2(asset, SITE_DIR / asset.name)
    (SITE_DIR / "index.html").write_text(render_page(data, summary), encoding="utf-8")
    logger.info(f"ページ生成: {SITE_DIR / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
