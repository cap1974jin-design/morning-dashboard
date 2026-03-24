"""朝刊マーケットダッシュボード - データ取得モジュール"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import feedparser
import yfinance as yf

logger = logging.getLogger("morning_dashboard")

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# --- 銘柄定義 ---
MARKET_SYMBOLS = {
    "株価指数": {
        "日経225": "^N225",
        "日経225先物": "NKD=F",
        "TOPIX": "2001.T",
        "S&P500": "^GSPC",
        "NASDAQ": "^IXIC",
        "ダウ平均": "^DJI",
    },
    "為替": {
        "USD/JPY": "USDJPY=X",
        "EUR/USD": "EURUSD=X",
        "EUR/JPY": "EURJPY=X",
    },
    "暗号資産": {
        "BTC/USD": "BTC-USD",
        "ETH/USD": "ETH-USD",
    },
    "コモディティ": {
        "金 (Gold)": "GC=F",
        "原油 (WTI)": "CL=F",
    },
}

# --- RSSフィード定義 ---
RSS_FEEDS = {
    "政治経済": [
        {"name": "Yahoo経済", "url": "https://news.yahoo.co.jp/rss/topics/business.xml"},
        {"name": "Yahoo政治", "url": "https://news.yahoo.co.jp/rss/topics/domestic.xml"},
    ],
    "地政学・国際": [
        {"name": "Yahoo国際", "url": "https://news.yahoo.co.jp/rss/topics/world.xml"},
        {"name": "Reuters Japan", "url": "https://assets.wor.jp/rss/rdf/reuters/top.rdf"},
    ],
    "ビジネス": [
        {"name": "Google News JP", "url": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtcGhHZ0pLVUNnQVAB?hl=ja&gl=JP&ceid=JP:ja"},
        {"name": "Yahoo IT", "url": "https://news.yahoo.co.jp/rss/topics/it.xml"},
    ],
}

MAX_NEWS_PER_FEED = 10


def fetch_market_data() -> dict:
    """yfinanceで全銘柄の現在値・前日比・変動率を取得"""
    all_symbols = []
    symbol_to_name = {}
    for category, symbols in MARKET_SYMBOLS.items():
        for name, symbol in symbols.items():
            all_symbols.append(symbol)
            symbol_to_name[symbol] = (category, name)

    results = {}
    tickers = yf.Tickers(" ".join(all_symbols))

    for symbol in all_symbols:
        category, name = symbol_to_name[symbol]
        try:
            ticker = tickers.tickers[symbol]
            hist = ticker.history(period="5d")
            if len(hist) < 2:
                logger.warning(f"{name} ({symbol}): データ不足")
                continue

            current = float(hist["Close"].iloc[-1])
            previous = float(hist["Close"].iloc[-2])
            change = current - previous
            change_pct = (change / previous) * 100

            if category not in results:
                results[category] = {}
            results[category][name] = {
                "symbol": symbol,
                "price": round(current, 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
            }
        except Exception as e:
            logger.error(f"{name} ({symbol}) 取得失敗: {e}")

    return results


def fetch_news() -> dict:
    """RSSフィードからニュースを取得"""
    results = {}

    for category, feeds in RSS_FEEDS.items():
        articles = []
        for feed_info in feeds:
            try:
                feed = feedparser.parse(feed_info["url"])
                for entry in feed.entries[:MAX_NEWS_PER_FEED]:
                    published = ""
                    if hasattr(entry, "published"):
                        published = entry.published
                    elif hasattr(entry, "updated"):
                        published = entry.updated

                    articles.append({
                        "title": entry.get("title", ""),
                        "link": entry.get("link", ""),
                        "source": feed_info["name"],
                        "published": published,
                    })
            except Exception as e:
                logger.error(f"RSS取得失敗 ({feed_info['name']}): {e}")

        # 重複除去（タイトルベース）
        seen = set()
        unique = []
        for a in articles:
            if a["title"] not in seen:
                seen.add(a["title"])
                unique.append(a)
        results[category] = unique[:MAX_NEWS_PER_FEED]

    return results


def fetch_all() -> dict:
    """全データを取得してJSONに保存"""
    logger.info("データ取得開始...")

    market = fetch_market_data()
    news = fetch_news()

    now = datetime.now()
    data = {
        "fetched_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "market": market,
        "news": news,
    }

    filepath = DATA_DIR / f"morning_{now.strftime('%Y-%m-%d')}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info(f"データ保存完了: {filepath}")
    return data


def load_latest() -> Optional[dict]:
    """最新のキャッシュJSONを読み込み"""
    files = sorted(DATA_DIR.glob("morning_*.json"), reverse=True)
    if not files:
        return None

    with open(files[0], "r", encoding="utf-8") as f:
        return json.load(f)
