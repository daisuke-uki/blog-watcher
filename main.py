import feedparser
import json
import yaml
import requests
import os
from datetime import datetime, timedelta, timezone
from time import mktime

SEEN_FILE = "seen.json"
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
LLM_API_KEY = os.environ["LLM_API_KEY"]

# 初回実行時にさかのぼる日数
FIRST_RUN_DAYS = 30


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


def summarize(title, content):
    """Claude APIで3行要約を生成する。"""
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": LLM_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 300,
            "messages": [
                {
                    "role": "user",
                    "content": f"以下の記事を3行で要約してください。\nタイトル: {title}\n本文: {content[:2000]}",
                }
            ],
        },
    )
    response.raise_for_status()
    return response.json()["content"][0]["text"]


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

    for site in sites:
        feed = feedparser.parse(site["rss"])
        for entry in feed.entries:
            if entry.link in seen:
                continue

            if is_first_run:
                published = get_published_datetime(entry)
                if published is not None and published < cutoff_date:
                    # 30日より古い記事は「既読扱い」にするだけで通知はしない
                    new_seen.add(entry.link)
                    continue

            summary_source = getattr(entry, "summary", "")
            summary = summarize(entry.title, summary_source)
            notify_slack(site["name"], entry.title, entry.link, summary)
            new_seen.add(entry.link)

    save_seen(new_seen)


if __name__ == "__main__":
    main()
