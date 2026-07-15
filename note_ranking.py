#!/usr/bin/env python3
"""
note.com で「フォロワー数は少ないが、記事だけが伸びている」投稿を毎朝抽出する。

考え方:
  並び順は単純に「いいね数が多い順」。観察したいのは「フォロワー数からは
  説明がつかないレベルで伸びた記事」（例: フォロワー1人が数百いいね）なので、
  いいね数がフォロワー数の500%（5倍）以上であることを必須条件にする
  （RATIO_MIN=5.0）。さらに「フォロワー1人が挨拶投稿で数件いいね」のような
  トリビアルなノイズを除くため、絶対いいね数の下限（MIN_LIKES）も設ける。
  この2条件は厳しいため、日によっては0件のこともある（それが正しい挙動）。

取得方法:
  note.com公式の「カテゴリ別 人気順（sort=hot）」API
  (/v1/categories/{category}?sort=hot) を使う。これはnote自身の
  アルゴリズムが「今伸びている」と判定した記事の一覧であり、ハッシュタグの
  新着一覧より狙いに合致する。またレスポンスに著者のfollower_countが
  最初から含まれるため、著者ごとの追加APIコールが不要（高速）。
  CATEGORIES は note.com が持つ公式ジャンル一覧（実在確認済み）。

手動実行:
  python3 ~/Ai-agent/automation/note_ranking.py

出力:
  - WordPress（good-daily-life.com/kouiunolab）に固定ページとして自動投稿・更新
  - iCloud Drive: ~/Library/Mobile Documents/com~apple~CloudDocs/note_ranking/index.html
    （スマホの「ファイル」アプリ→iCloud Drive→note_ranking から開ける。WordPress投稿の予備）
  - ローカル控え: ~/Ai-agent/automation/note_ranking/output/index.html

環境変数（automation/.env）:
  WP_XMLRPC_URL ... 例 https://www.good-daily-life.com/kouiunolab/xmlrpc.php
  WP_USERNAME   ... WordPressログインユーザー名
  WP_PASSWORD   ... WordPressログインパスワード
"""

import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xmlrpc.client
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- 観察対象カテゴリ（note.com公式ジャンル。実在確認済み。ジャンルを絞らない） ----
CATEGORIES = [
    "tech", "love", "beauty", "fashion", "art", "game", "music", "movie",
    "sports", "entertainment", "business", "pet", "health", "gourmet",
    "manga", "gadget", "photo", "education", "travel", "lifestyle",
    "career", "novel", "radio", "science",
]

LOOKBACK_HOURS = 24 * 7       # 直近7日間のローリング窓（毎日更新するが、対象は直近1週間分）。真の爆伸びは稀なため広く取る
MAX_PAGES_PER_CATEGORY = 20   # 1カテゴリあたりの最大ページ数（暴走防止。1ページ=10件。hotソートなので古い記事も混ざりうる）
FOLLOWER_MAX = 100            # これ以下のフォロワー数を「無名」とみなす
MIN_LIKES = 100                # 「めっちゃ伸びた」と呼べる絶対いいね数の下限（実データで新規アカウントの上限は~70だったため、それを超える値に設定）
RATIO_MIN = 5.0               # いいね数がフォロワー数の500%以上（下限のみ。上限は設けない）
SELF_INTRO_KEYWORDS = [       # 自己紹介・初投稿系のノイズを除外するためのタイトルキーワード
    "自己紹介", "はじめまして", "初めまして", "はじめてのnote", "初めてのnote", "初投稿",
]
TOP_N = 40                   # 出力する件数
REQUEST_INTERVAL_SEC = 0.4   # note.com への配慮（連打しない）

BASE = "https://note.com/api"
UA = {"User-Agent": "Mozilla/5.0 (compatible; note-ranking-personal-tool/1.0)"}

SCRIPT_DIR = Path(__file__).parent
LOCAL_OUT_DIR = SCRIPT_DIR / "note_ranking" / "output"
ICLOUD_OUT_DIR = (
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "note_ranking"
)
WP_PAGE_ID_FILE = SCRIPT_DIR / "note_ranking_wp_page_id.txt"
WP_PAGE_TITLE = "noteの隠れた伸び記事"
WP_PAGE_SLUG = "note-ranking"
LAST_RUN_FILE = SCRIPT_DIR / "note_ranking_last_run.txt"

JST = timezone(timedelta(hours=9))

_env_loaded = False


def _load_env():
    global _env_loaded
    if _env_loaded:
        return
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    _env_loaded = True


def api_get(path: str) -> dict | None:
    url = BASE + urllib.parse.quote(path, safe="/?&=%")
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [note_ranking] API失敗 {path}: {e}")
        return None


def fetch_category_hot_notes(category: str, cutoff: datetime) -> list[dict]:
    """指定カテゴリの人気順(hot)記事のうち、publish_atがcutoff以降のものを集める。
    hotソートは新着順ではないため厳密な早期打ち切りはできない。
    MAX_PAGES_PER_CATEGORYを上限に全ページ走査し、期間外は都度スキップする。"""
    collected = []
    page = 1
    while page <= MAX_PAGES_PER_CATEGORY:
        data = api_get(f"/v1/categories/{category}?sort=hot&page={page}")
        time.sleep(REQUEST_INTERVAL_SEC)
        if not data or "data" not in data:
            break
        notes = data["data"].get("notes", [])
        if not notes:
            break

        for n in notes:
            try:
                published = datetime.fromisoformat(n["publish_at"])
            except Exception:
                continue
            if published >= cutoff:
                collected.append(n)

        if data["data"].get("last_page"):
            break
        page += 1
    return collected


PAGE_STYLE = """
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; max-width: 640px; margin-inline: auto;
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", sans-serif;
    background: #fafafa; color: #1a1a1a;
  }
  @media (prefers-color-scheme: dark) {
    body { background: #121212; color: #eee; }
  }
  header { margin-bottom: 16px; }
  h1 { font-size: 1.3rem; margin: 0 0 4px; }
  .meta { font-size: 0.78rem; opacity: 0.6; line-height: 1.5; }
  .card {
    display: flex; gap: 12px; text-decoration: none; color: inherit;
    background: #fff; border-radius: 12px; padding: 10px; margin-bottom: 10px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }
  @media (prefers-color-scheme: dark) {
    .card { background: #1e1e1e; box-shadow: none; border: 1px solid #2a2a2a; }
  }
  .thumb {
    width: 80px; height: 80px; flex: none; border-radius: 8px;
    background-size: cover; background-position: center; background-color: #ddd;
  }
  .body { min-width: 0; }
  .title {
    font-weight: 600; font-size: 0.92rem; line-height: 1.35;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .author { font-size: 0.76rem; opacity: 0.7; margin-top: 4px; }
  .stats { font-size: 0.76rem; margin-top: 4px; display: flex; gap: 8px; flex-wrap: wrap; }
  .likes { font-weight: 700; color: #d9480f; }
  @media (prefers-color-scheme: dark) { .likes { color: #ff8a5c; } }
  .ratio { opacity: 0.6; }
  .tag { opacity: 0.55; }
  .time { font-size: 0.7rem; opacity: 0.5; margin-top: 2px; }
  .empty { text-align: center; opacity: 0.6; padding: 40px 0; }
"""


def push_to_wordpress(body_content: str) -> str | None:
    """WordPress固定ページとして投稿・更新する。成功時はページURLを返す"""
    _load_env()
    xmlrpc_url = os.environ.get("WP_XMLRPC_URL")
    username = os.environ.get("WP_USERNAME")
    password = os.environ.get("WP_PASSWORD")
    if not (xmlrpc_url and username and password):
        print("[note_ranking] WordPress未設定（.envにWP_XMLRPC_URL/WP_USERNAME/WP_PASSWORDが必要）。投稿をスキップします")
        return None

    wp_content = f"<style>{PAGE_STYLE}</style>\n{body_content}"

    server = xmlrpc.client.ServerProxy(xmlrpc_url)
    content_struct = {
        "post_type": "page",
        "post_status": "publish",
        "post_title": WP_PAGE_TITLE,
        "post_name": WP_PAGE_SLUG,
        "post_content": wp_content,
    }

    existing_id = WP_PAGE_ID_FILE.read_text().strip() if WP_PAGE_ID_FILE.exists() else None

    try:
        if existing_id:
            server.wp.editPost(0, username, password, existing_id, content_struct)
            page_id = existing_id
        else:
            page_id = server.wp.newPost(0, username, password, content_struct)
            WP_PAGE_ID_FILE.write_text(str(page_id), encoding="utf-8")

        post = server.wp.getPost(0, username, password, page_id, ["link"])
        return post.get("link")
    except Exception as e:
        print(f"[note_ranking] WordPress投稿失敗: {e}")
        return None


def wrap_document(body_content: str) -> str:
    """iCloud/ローカル保存用に、スタンドアロンで開ける完全なHTML文書にする"""
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>noteの隠れた伸び記事</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
{body_content}
</body>
</html>
"""


def render_body(items: list[dict], generated_at: datetime) -> str:
    rows = []
    for it in items:
        title = html.escape(it["title"])
        author = html.escape(it["author"])
        tag = html.escape(it["tag"])
        eyecatch = html.escape(it["eyecatch"], quote=True)
        rows.append(f"""
        <a class="card" href="{it['url']}" target="_blank" rel="noopener">
          <div class="thumb" style="background-image:url('{eyecatch}')"></div>
          <div class="body">
            <div class="title">{title}</div>
            <div class="author">{author}（フォロワー {it['follower_count']:,}人）</div>
            <div class="stats">
              <span class="likes">♥ {it['like_count']:,}</span>
              <span class="ratio">フォロワーの{it['ratio']:.1f}倍いいね</span>
              <span class="tag">{tag}</span>
            </div>
            <div class="time">{it['published_jst']}</div>
          </div>
        </a>""")

    return f"""<header>
  <h1>noteの隠れた伸び記事</h1>
  <div class="meta">
    更新: {generated_at.strftime('%Y-%m-%d %H:%M')} JST ／ 対象: note公式{len(CATEGORIES)}カテゴリの人気順（直近{LOOKBACK_HOURS // 24}日間）<br>
    条件: いいね{MIN_LIKES}以上 かつ いいねがフォロワー数の{RATIO_MIN:.0f}倍以上 ／ いいね数が多い順
  </div>
</header>
<main>
  {''.join(rows) if rows else '<div class="empty">直近' + str(LOOKBACK_HOURS // 24) + '日間で条件に合う記事はありませんでした</div>'}
</main>
"""


def main():
    now = datetime.now(JST)
    today_str = now.strftime("%Y-%m-%d")

    if LAST_RUN_FILE.exists() and LAST_RUN_FILE.read_text().strip() == today_str:
        print(f"[note_ranking] 本日({today_str})は実行済みのためスキップします")
        return

    cutoff = now - timedelta(hours=LOOKBACK_HOURS)

    seen_note_ids = {}

    for category in CATEGORIES:
        print(f"[note_ranking] カテゴリ:{category} を取得中...")
        notes = fetch_category_hot_notes(category, cutoff)
        for n in notes:
            key = n["key"]
            if key not in seen_note_ids:
                seen_note_ids[key] = {"note": n, "category": category}

    print(f"[note_ranking] 対象記事 {len(seen_note_ids)} 件。フィルタ中...")

    results = []
    for key, info in seen_note_ids.items():
        n = info["note"]
        title = n.get("name") or ""
        if any(kw in title for kw in SELF_INTRO_KEYWORDS):
            continue
        like_count = n.get("like_count", 0)
        if like_count < MIN_LIKES:
            continue
        user = n["user"]
        urlname = user.get("urlname")
        follower_count = user.get("follower_count")
        if not urlname or follower_count is None or follower_count == 0 or follower_count > FOLLOWER_MAX:
            continue
        ratio = like_count / follower_count
        if ratio < RATIO_MIN:
            continue

        published = datetime.fromisoformat(n["publish_at"]).astimezone(JST)
        results.append({
            "url": f"https://note.com/{urlname}/n/{key}",
            "title": n.get("name") or "(無題)",
            "author": user.get("nickname") or user.get("name") or urlname,
            "follower_count": follower_count,
            "like_count": like_count,
            "ratio": ratio,
            "tag": info["category"],
            "eyecatch": n.get("eyecatch") or "",
            "published_jst": published.strftime("%m/%d %H:%M"),
        })

    results.sort(key=lambda r: r["like_count"], reverse=True)
    results = results[:TOP_N]

    body_content = render_body(results, now)
    document = wrap_document(body_content)

    for out_dir in (LOCAL_OUT_DIR, ICLOUD_OUT_DIR):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(document, encoding="utf-8")
        print(f"[note_ranking] 出力: {out_dir / 'index.html'}")

    wp_url = push_to_wordpress(body_content)
    if wp_url:
        print(f"[note_ranking] WordPress更新: {wp_url}")

    LAST_RUN_FILE.write_text(today_str, encoding="utf-8")

    print(f"[note_ranking] 完了。該当 {len(results)} 件。")


if __name__ == "__main__":
    main()
