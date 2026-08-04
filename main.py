import feedparser
import json
import yaml
import requests
import os
import time
from datetime import datetime, timedelta, timezone
from time import mktime

SEEN_FILE = "seen.json"
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
LLM_API_KEY = os.environ["LLM_API_KEY"]

# 使用するGeminiモデル(必要に応じて変更可能)
GEMINI_MODEL = "gemini-3.5-flash"

# 初回実行時にさかのぼる日数
FIRST_RUN_DAYS = 7

# 無料枠のRPD(1日の上限)対策: 1回の実行で処理する記事数の上限
MAX_REQUESTS_PER_RUN = 15

# 無料枠のRPM(1分あたりの上限=5)対策: リクエスト間の待機秒数
REQUEST_INTERVAL_SECONDS = 13


def load_seen():
    """seen.jsonを読み込む。ファイルがなければ初回実行と判定する。"""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f)), False  # 2回目以降
    return set(), True  # 初回実行


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def get_published_datetime(entry):
    """RSSエントリの公開日時を取得する(取得できない場合はNone)。"""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime.fromtimestamp(mktime(entry.published_parsed), tz=timezone.utc)
    return None


def summarize(title, content, max_retries=5):
    """Gemini APIで3行要約を生成する。429(レート制限)の場合は待って再試行する。"""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={LLM_API_KEY}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": f"以下の記事を3行で要約してください。\nタイトル: {title}\n本文: {content[:2000]}"
                    }
                ]
            }
        ]
    }

    for attempt in range(max_retries):
        response = requests.post(url, headers={"content-type": "application/json"}, json=payload)

        if response.status_code == 429:
            # レート制限にかかった場合、待って再試行する(徐々に待ち時間を延ばす)
            wait_seconds = 15 * (attempt + 1)
            print(f"Rate limited. Waiting {wait_seconds}s before retry ({attempt + 1}/{max_retries})...")
            print(f"Response body: {response.text}")
            time.sleep(wait_seconds)
            continue

        if response.status_code >= 400:
            # 429以外のエラーも中身を確認できるようにログ出力してから例外を投げる
            print(f"Gemini API error {response.status_code}: {response.text}")

        response.raise_for_status()
        return response.json()["candidates"][0]["content"]["parts"][0]["text"]

    raise RuntimeError("Gemini API rate limit: max retries exceeded")


def notify_slack(site_name, title, link, summary):
    text = f"*[{site_name}]* <{link}|{title}>\n{summary}"
    requests.post(SLACK_WEBHOOK_URL, json={"text": text})


def main():
    with open("sites.yaml") as f:
        sites = yaml.safe_load(f)["sites"]

    seen, is_first_run = load_seen()
    new_seen = set(seen)

    # 初回実行時の基準日(この日より古い記事は通知しない)
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS)

    request_count = 0
    stopped_early = False

    try:
        for site in sites:
            if request_count >= MAX_REQUESTS_PER_RUN or stopped_early:
                break

            feed = feedparser.parse(site["rss"])
            for entry in feed.entries:
                if request_count >= MAX_REQUESTS_PER_RUN:
                    print(f"Reached MAX_REQUESTS_PER_RUN={MAX_REQUESTS_PER_RUN}. Remaining articles will be processed next run.")
                    stopped_early = True
                    break

                if entry.link in seen:
                    continue

                if is_first_run:
                    published = get_published_datetime(entry)
                    if published is not None and published < cutoff_date:
                        # 指定日数より古い記事は「既読扱い」にするだけで通知はしない
                        new_seen.add(entry.link)
                        continue

                summary_source = getattr(entry, "summary", "")

                try:
                    summary = summarize(entry.title, summary_source)
                except RuntimeError as e:
                    # レート制限などで要約が作れない場合、異常終了はさせず、
                    # この記事は未処理のまま次回実行に持ち越して今回はここで打ち切る
                    print(f"Stopping this run early due to: {e}")
                    stopped_early = True
                    break

                notify_slack(site["name"], entry.title, entry.link, summary)
                new_seen.add(entry.link)
                request_count += 1

                # 無料枠のRPM対策として、記事ごとに間隔を空ける
                time.sleep(REQUEST_INTERVAL_SECONDS)
    finally:
        # 途中で打ち切られた場合でも、ここまで通知済みの記事は必ず保存する
        save_seen(new_seen)


if __name__ == "__main__":
    main()
