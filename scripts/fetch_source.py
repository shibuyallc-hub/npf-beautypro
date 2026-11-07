#!/usr/bin/env python3
"""Meta Marketing API と GA4 Data API から source.json を作る（広告マネージャ画面の代わり）。

環境変数:
  META_ACCESS_TOKEN, META_AD_ACCOUNT_ID(数字のみ。act_ は付けない)
  SERVICE_ACCOUNT_JSON_PATH（GA4 の閲覧権限を持つサービスアカウント）, GA4_PROPERTY_ID
  END_DATE（任意 YYYY-MM-DD。既定は前日=JST）
出力: source.json（build_data.py が読む形）。広告・キャンペーンの設定は一切変更しない（GET のみ）。

数え方（手作業の手順と同じ）:
  購入 = omni_purchase（既定のアトリビューション＝クリック7日＋ビュー1日）
  クリックのみ = 7d_click の omni_purchase
  クリック = inline_link_clicks
"""
import json, os, sys, time
from datetime import date, datetime, timedelta, timezone
import requests

API = "https://graph.facebook.com/v21.0"
TOKEN = os.environ["META_ACCESS_TOKEN"]
ACCT = os.environ["META_AD_ACCOUNT_ID"].replace("act_", "")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "source.json")

JST = timezone(timedelta(hours=9))
END = date.fromisoformat(os.environ["END_DATE"]) if os.environ.get("END_DATE") else (datetime.now(JST).date() - timedelta(days=1))

CFG = {
    "dog": dict(campaign="D1_BP_DOG", start=date(2026, 10, 12), ga4_start=date(2026, 9, 25),
                ga4_utm="dayone_bp_dog_2609",
                ads={"dog_お腹_15動画": "お", "dog_子犬_15s動画": "子", "dog_歯_15s動画": "歯"},
                # (ラベル, 開始, 終了)  終了 None=日曜始まりの週(日〜土)。最初の週のみ個別指定
                first_weeks=[("第3週", date(2026, 10, 12), date(2026, 10, 17))], later_label_from=4),
    "cat": dict(campaign="D1_BP_CAT_v2", start=date(2026, 10, 22), ga4_start=date(2026, 10, 22),
                ga4_utm="dayone_bp_cat_2610",
                ads={"cat_腸内_23s動画": "腸", "cat_低脂肪_22s動画": "低", "cat_下部尿路_19s動画": "尿"},
                first_weeks=[("配信開始週", date(2026, 10, 22), date(2026, 10, 24)),
                             ("翌週", date(2026, 10, 25), date(2026, 10, 31))], later_label_from=3),
}


def md(d):
    return f"{d.month}/{d.day}"


def meta_insights(since, until, windows=None):
    """level=ad の日別 insights を全ページ取得。"""
    params = {
        "level": "ad", "time_increment": 1,
        "fields": "ad_name,campaign_name,spend,impressions,inline_link_clicks,actions",
        "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
        "filtering": json.dumps([{"field": "impressions", "operator": "GREATER_THAN", "value": 0}]),
        "limit": 500, "access_token": TOKEN,
    }
    if windows:
        params["action_attribution_windows"] = json.dumps(windows)
    url, rows = f"{API}/act_{ACCT}/insights", []
    while url:
        for attempt in range(4):
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                break
            time.sleep(5 * (attempt + 1))
        else:
            raise SystemExit(f"Meta API error {r.status_code}: {r.text[:300]}")
        j = r.json()
        rows += j.get("data", [])
        url, params = j.get("paging", {}).get("next"), None
    return rows


def purchases(row, window_key=None):
    """omni_purchase を優先（purchase と両方返るため足すと2倍になる）。"""
    acts = {a["action_type"]: a for a in row.get("actions", [])}
    a = acts.get("omni_purchase") or acts.get("purchase")
    if not a:
        return 0
    return int(float(a.get(window_key, a["value"]) if window_key else a["value"]))


def ga4(cfg):
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (DateRange, Dimension, Metric, RunReportRequest,
                                                    FilterExpression, Filter)
    from google.oauth2 import service_account
    cred = service_account.Credentials.from_service_account_file(
        os.environ["SERVICE_ACCOUNT_JSON_PATH"], scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    cl = BetaAnalyticsDataClient(credentials=cred)
    prop = f"properties/{os.environ['GA4_PROPERTY_ID']}"
    dr = [DateRange(start_date=cfg["ga4_start"].isoformat(), end_date=END.isoformat())]
    sess = cl.run_report(RunReportRequest(property=prop, date_ranges=dr,
                                          dimensions=[Dimension(name="sessionCampaignName")],
                                          metrics=[Metric(name="sessions")]))
    buy = cl.run_report(RunReportRequest(
        property=prop, date_ranges=dr,
        dimensions=[Dimension(name="sessionCampaignName"), Dimension(name="eventName")],
        metrics=[Metric(name="eventCount")],
        dimension_filter=FilterExpression(filter=Filter(
            field_name="eventName", string_filter=Filter.StringFilter(value="purchase")))))
    s = sum(int(r.metric_values[0].value) for r in sess.rows if r.dimension_values[0].value == cfg["ga4_utm"])
    p = sum(int(r.metric_values[0].value) for r in buy.rows if r.dimension_values[0].value == cfg["ga4_utm"])
    return {"sessions": s, "purchases": p, "period": f"{md(cfg['ga4_start'])}〜{md(END)}"}


def safe_ga4(cfg):
    """DRY_RUN のときだけ GA4 失敗を許容（権限待ちの確認用）。本番は失敗したら止める。"""
    try:
        return ga4(cfg)
    except Exception as e:
        if os.environ.get("DRY_RUN") == "true":
            print("WARN GA4 取得失敗（dry_runのため続行）:", type(e).__name__, str(e)[:200])
            return {"sessions": 0, "purchases": 0, "period": "GA4未取得"}
        raise


def weeks(cfg):
    out, n = [], cfg["later_label_from"]
    for label, s, e in cfg["first_weeks"]:
        if s <= END:
            out.append([label, s, min(e, END)])
    cur = cfg["first_weeks"][-1][2] + timedelta(days=1)
    while cur <= END:
        e = min(cur + timedelta(days=6), END)   # 日曜始まり→土曜終わり
        out.append([f"第{n}週", cur, e]); n += 1; cur += timedelta(days=7)
    return out


def main():
    src = {"updated_at": f"{datetime.now(JST):%Y-%m-%d} 朝（{md(END)}まで確定）",
           "latest_date": END.isoformat(), "partial_date": None}
    since = min(c["start"] for c in CFG.values())
    default_rows = meta_insights(since, END)
    click_rows = meta_insights(since, END, windows=["7d_click"])
    for key, cfg in CFG.items():
        mine = [r for r in default_rows if r["campaign_name"] == cfg["campaign"]]
        unknown = {r["ad_name"] for r in mine} - set(cfg["ads"])
        if unknown:
            raise SystemExit(f"想定外の広告名: {unknown}")
        daily = {}
        for r in mine:
            d = date.fromisoformat(r["date_start"])
            if d < cfg["start"]:
                continue
            daily[(d, cfg["ads"][r["ad_name"]])] = (int(float(r["spend"])), int(r["impressions"]),
                                                  int(r.get("inline_link_clicks", 0)), purchases(r))
        click_only = {}
        for r in click_rows:
            if r["campaign_name"] == cfg["campaign"] and r["ad_name"] in cfg["ads"]:
                click_only[date.fromisoformat(r["date_start"])] = click_only.get(date.fromisoformat(r["date_start"]), 0) + purchases(r, "7d_click")
        src[key] = {
            "daily": [[md(d), a, v[0], v[1], v[3]] for (d, a), v in sorted(daily.items(), key=lambda x: (x[0][0], list(cfg["ads"].values()).index(x[0][1])))],
            "weekly": [], "ga4": safe_ga4(cfg),
        }
        for label, s, e in weeks(cfg):
            ws = [(d, a, v) for (d, a), v in daily.items() if s <= d <= e]
            partial = e == END and e.weekday() != 5 and (label.startswith("第") or label == "翌週") and \
                      not (label in [w[0] for w in cfg["first_weeks"]] and e == [w[2] for w in cfg["first_weeks"] if w[0] == label][0])
            w = {"label": label, "period": f"{md(s)}〜" if partial else f"{md(s)}〜{md(e)}",
                 "start": s.isoformat(), "end": e.isoformat(),
                 "spend": sum(v[0] for _, _, v in ws), "imp": sum(v[1] for _, _, v in ws),
                 "clicks": sum(v[2] for _, _, v in ws), "purchases": sum(v[3] for _, _, v in ws),
                 "click_only": sum(c for d, c in click_only.items() if s <= d <= e), "est": False}
            if partial:
                w["partial"] = True
            src[key]["weekly"].append(w)
        # check は API 値そのものから作らない（build_data.py 側で固定分と合算して整合を見る）
    json.dump(src, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("OK", OUT, "dog rows", len(src["dog"]["daily"]), "cat rows", len(src["cat"]["daily"]),
          "latest", END, "GA4", src["dog"]["ga4"], src["cat"]["ga4"])


if __name__ == "__main__":
    main()
