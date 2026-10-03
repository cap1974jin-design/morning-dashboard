# 電脳NEWS（旧・朝刊マーケットダッシュボード）

朝の数分で一日の情報をざっと摂取するための1枚のページ。**毎時50分に自動で更新される**（2026-10-03 本人指示で1日1回から変更）。

## ページの中身

| 欄 | 内容 | 取得元 |
|---|---|---|
| 今朝の要点 | **現在は使っていない**（下記「要約について」） | Claude API |
| 相場 | 分類ごとの折りたたみ帯（閉じていても騰落率が見える・押すとカードが順に出る・「すべて開く」あり）。カードは前日比と約1か月の推移グラフ。±1.5%以上動いた銘柄は色付き | Yahoo Finance（yfinance）。TOPIX のみ Yahoo!ファイナンス日本版 |
| ニュース | 4分野のタブ。見出し4本ずつのスライドを横にめくる（スマホは指で・PC は矢印） | 各社 RSS ＋ Google ニュース |

**相場の銘柄**（`data_fetcher.py` の `MARKET_SYMBOLS`）
- 株価指数：日経225／日経225先物（CME・円建て `NIY=F`）／TOPIX／S&P500／NASDAQ／ダウ
- 為替：USD/JPY／EUR/JPY／GBP/JPY
- 金利・リスク：米10年債利回り／VIX
- 暗号資産：BTC／ETH　コモディティ：金／WTI原油

**ニュースの配信元**（`RSS_FEEDS`）：朝日・時事・東洋経済・ダイヤモンド・BBC日本語・CNN・ITmedia・GIGAZINE・Impress Watch・Yahoo・Google ニュース（検索とトピック）。**並べ方**：同じ出来事を報じた記事は見出しの似具合（文字2つ組の重なり 0.40 以上）で1本にまとめ、報じた社数が多い順に上へ置く（「3社が報道」と表示）。社数が同じものは各配信元から交互に拾った順で、1社に偏らない。48時間より古い記事は外す。弱点：短い見出し同士は話題が近いだけでまとまることがある（2026-10-03 は「ガソリン高騰」と「トランプ氏 中間選挙」が同じ扱いになった）。生活記事・買い物レビュー・個別銘柄ページなどは見出しの言葉（`NOISE_PATTERNS`）で外す。

## 決めたこと（経緯）

- **TOPIX先物は載せない**：Yahoo Finance に銘柄が無い。nikkei225jp.com は robots.txt で `/_data/` への自動取得を断っているため使わない（2026-10-03 本人了承）。
- **旧版の「TOPIX」は誤りだった**：`2001.T` は TOPIX ではなくニップン（製粉）の株価。`998405.T`（Yahoo!ファイナンス日本版）に修正。
- **ロイターの RSS（wor.jp）は配信停止**していたので外した。NHK の RSS は更新が8月で止まっていたので使わない。
- **会員制の媒体は載せない**（本人指示）：日経・日経xTECH・日経ビジネス・ブルームバーグ・WSJ など（`EXCLUDED_SOURCES`）。Google ニュース経由で入ってくる分は媒体名で外す。日経xTECH の代わりに GIGAZINE と Impress Watch を足した。記事ごとに有料・無料が混ざる媒体（朝日・東洋経済・ダイヤモンド等）は残している。
- 為替は EUR/USD を外し GBP/JPY を追加（本人指示）。題名は「電脳NEWS」（本人指示）。

## 要約について

`build.py` には Claude API で「今朝の要点」と各分野の注目3本を作る機能がある（月400円前後の見込み）。
2026-10-03 に本人判断で**付けない**ことにした。ワークフローは `--no-ai` で動かしている。
使う時は `.github/workflows/morning.yml` の `--no-ai` を外し、Secrets に `ANTHROPIC_API_KEY` を入れる。

## ファビコン

`assets/` に置いた金色の「電」（ネオン風・2026-10-03 本人選定）。字形は Noto Sans JP（SIL OFL）の Black から取った。
`favicon.svg` が原本で、PNG はそれを Chrome で書き出したもの。スマホでホーム画面に追加した時のアイコンも同じ（本人指示）：iPhone は `apple-touch-icon.png`（角丸なし・OS が丸める）、Android は `manifest.webmanifest` の `icon-192/512` と、丸く切り抜かれても欠けないよう字を縮めた `icon-maskable-512.png`。`build.py` が毎回 `site/` にコピーする。

## リンクのプレビュー（LINE・X など）

`build.py` の `DESCRIPTION` が説明文、`assets/og.png`（1200×630）が画像。指定が無いと LINE はページ本文の先頭（「相場すべて開く 株価指数…」）を切り取って出していた（2026-10-03 本人指摘で追加）。
LINE はプレビューを数日キャッシュするので、変更直後は URL の末尾に `?v=2` などを付けて貼ると新しい表示になる。

## 仕組み

```
GitHub Actions（毎時50分・.github/workflows/morning.yml）
  └─ python build.py
       ├─ data_fetcher.fetch_all()  相場＋ニュース → data/morning_YYYY-MM-DD.json
       ├─ summarize()               Claude API（現在は --no-ai で止めている）
       └─ render_page()             site/index.html（外部 JS なし・ライト/ダーク自動）
  └─ GitHub Pages に公開
```

- 必要な設定：Settings → Pages → Source を「GitHub Actions」。
- **60日停止の対策**：公開リポジトリは60日間コミットが無いと定期実行が止まる。その日の最初の回だけ `data/morning_YYYY-MM-DD.json` をコミットして防いでいる（1日1コミット）。このため手元から push する前は `git pull --rebase` が要る。
- 費用：公開リポジトリの Actions と Pages は無料（毎時でも0円）。
- モデルは環境変数 `MORNING_MODEL` で変えられる（既定 `claude-opus-5-5`）。

## 手元で動かす

```bash
pip3 install -r requirements.txt
python3 build.py --no-ai      # API キー無しで見た目を確認
python3 build.py              # ANTHROPIC_API_KEY を環境変数に入れて実行
open site/index.html
```

`app.py`（Streamlit 版）は旧サイト（streamlit.app）用に残している。止めるかどうかは未定。
