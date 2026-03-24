"""朝刊マーケットダッシュボード - Streamlit アプリ"""

import streamlit as st
from data_fetcher import fetch_all, load_latest


def render_market_section(market: dict):
    """マーケット概況セクション"""
    for category, items in market.items():
        st.subheader(category)
        cols = st.columns(len(items))
        for col, (name, data) in zip(cols, items.items()):
            change = data["change"]
            change_pct = data["change_pct"]
            arrow = "△" if change >= 0 else "▽"
            delta_str = f"{arrow} {abs(change):,.2f} ({abs(change_pct):.2f}%)"
            col.metric(
                label=name,
                value=f"{data['price']:,.2f}",
                delta=delta_str,
                delta_color="normal" if change >= 0 else "inverse",
            )


def render_news_section(news: dict):
    """ニュースセクション"""
    tabs = st.tabs(list(news.keys()))
    for tab, (category, articles) in zip(tabs, news.items()):
        with tab:
            if not articles:
                st.info("ニュースが取得できませんでした")
                continue
            for article in articles:
                source_badge = f"**[{article['source']}]**"
                published = article.get("published", "")
                st.markdown(
                    f"{source_badge} [{article['title']}]({article['link']})  \n"
                    f"<small style='color:gray'>{published}</small>",
                    unsafe_allow_html=True,
                )
                st.divider()


def main():
    st.set_page_config(
        page_title="朝刊マーケットダッシュボード",
        page_icon="📊",
        layout="wide",
    )

    st.title("📊 朝刊マーケットダッシュボード")
    st.caption("毎朝8時更新 — 政治経済・地政学・マーケット情報")

    # リフレッシュボタン
    col_refresh, col_status = st.columns([1, 4])
    with col_refresh:
        if st.button("🔄 最新データ取得", type="primary"):
            with st.spinner("データ取得中..."):
                data = fetch_all()
                st.session_state["dashboard_data"] = data
                st.rerun()

    # データ読み込み
    if "dashboard_data" not in st.session_state:
        data = load_latest()
        if data:
            st.session_state["dashboard_data"] = data

    data = st.session_state.get("dashboard_data")

    if not data:
        st.warning("データがありません。「最新データ取得」ボタンを押してください。")
        return

    with col_status:
        st.caption(f"最終取得: {data['fetched_at']}")

    st.divider()

    # マーケット概況
    st.header("マーケット概況")
    if data.get("market"):
        render_market_section(data["market"])
    else:
        st.info("マーケットデータなし")

    st.divider()

    # ニュース
    st.header("ニュース")
    if data.get("news"):
        render_news_section(data["news"])
    else:
        st.info("ニュースデータなし")


if __name__ == "__main__":
    main()
