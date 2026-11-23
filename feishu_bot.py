import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path("state.json")
GENESIS_DATE = datetime(2009, 1, 3, tzinfo=timezone.utc)
COOLDOWN_SECONDS = 3600
BOTTOM_THRESHOLD = 0.45
TOP_THRESHOLD = 1.20


def http_get_json(url: str, timeout: int = 15):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Ahr999-Feishu-Monitor/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_btc_data():
    binance_hosts = [
        "data-api." + "binance.vision",
        "api." + "binance.com",
    ]
    for host in binance_hosts:
        base = "https://" + host
        try:
            ticker = http_get_json(f"{base}/api/v3/ticker/price?symbol=BTCUSDT")
            klines = http_get_json(f"{base}/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=200")
            price = float(ticker["price"])
            closes = [float(k[4]) for k in klines]
            if len(closes) >= 200 and price > 0:
                return price, closes[-200:], f"Binance ({host})"
        except Exception as exc:
            print(f"[WARN] {host} failed: {exc}")

    try:
        okx_base = "https://" + "www." + "okx.com"
        ticker = http_get_json(f"{okx_base}/api/v5/market/ticker?instId=BTC-USDT")
        klines = http_get_json(f"{okx_base}/api/v5/market/candles?instId=BTC-USDT&bar=1Dutc&limit=200")
        price = float(ticker["data"][0]["last"])
        closes = [float(row[4]) for row in reversed(klines["data"])]
        if len(closes) >= 150 and price > 0:
            return price, closes[-200:], "OKX"
    except Exception as exc:
        print(f"[WARN] OKX failed: {exc}")

    raise RuntimeError("All market data sources (Binance, OKX) failed.")


def calculate_ahr999(price: float, closes: list[float], now_utc: datetime):
    geo_mean_200 = math.exp(sum(math.log(c) for c in closes) / len(closes))
    days_since_genesis = (now_utc - GENESIS_DATE).total_seconds() / 86400.0
    exp_valuation = 10 ** (5.84 * math.log10(days_since_genesis) - 17.01)
    ahr999 = (price / geo_mean_200) * (price / exp_valuation)
    return ahr999, geo_mean_200, exp_valuation, days_since_genesis


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_zone_info(ahr999: float):
    if ahr999 <= BOTTOM_THRESHOLD:
        return "抄底区间 (≤ 0.45)", "green", "极度低估，适合分批抄底"
    if ahr999 < TOP_THRESHOLD:
        return "定投区间 (0.45 ~ 1.20)", "blue", "处于定投区间，可按计划定投"
    return "观望/过热区间 (≥ 1.20)", "red", "估值偏高，建议谨慎追高或止盈"


def send_feishu_card(webhook_url: str, title: str, template: str, trigger_reason: str,
                     price: float, ahr999: float, geo_mean: float, exp_val: float,
                     zone_label: str, advice: str, source: str, bjt_str: str):
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Ahr999 指数**\n`{ahr999:.4f}`"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**当前区间**\n{zone_label}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**BTC 现价**\n`${price:,.2f}`"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**200日定投成本**\n`${geo_mean:,.2f}`"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**指数增长估值**\n`${exp_val:,.2f}`"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**触发原因**\n{trigger_reason}"}},
                    ],
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"建议: {advice} | 数据源: {source} | 北京时间: {bjt_str}"}
                    ],
                },
            ],
        },
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
        print(f"[INFO] Feishu response ({resp.status}): {body}")
        res_json = json.loads(body)
        if res_json.get("code", 0) != 0 and res_json.get("StatusCode", 0) != 0:
            raise RuntimeError(f"Feishu webhook returned error: {body}")


def main():
    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise RuntimeError("Missing FEISHU_WEBHOOK_URL environment variable.")

    force_push = os.environ.get("FORCE_PUSH", "false").lower() == "true"
    now_utc = datetime.now(timezone.utc)
    now_bjt = now_utc.astimezone(timezone(timedelta(hours=8)))
    bjt_str = now_bjt.strftime("%Y-%m-%d %H:%M:%S")
    now_ts = time.time()

    price, closes, source = fetch_btc_data()
    ahr999, geo_mean, exp_val, days = calculate_ahr999(price, closes, now_utc)
    zone_label, template, advice = get_zone_info(ahr999)

    print(
        f"[METRICS] BJT={bjt_str} | Source={source} | Price=${price:,.2f} | "
        f"200dCost=${geo_mean:,.2f} | ExpVal=${exp_val:,.2f} | Ahr999={ahr999:.4f} | Days={days:.2f}"
    )

    state = load_state()
    reasons = []
    title = "📊 BTC Ahr999 指数监控播报"

    if force_push:
        reasons.append("手动立即推送 (FORCE_PUSH)")

    if now_bjt.hour in (8, 20) and now_bjt.minute < 20:
        slot_key = now_bjt.strftime("%Y-%m-%d-%H")
        if state.get("last_scheduled_slot") != slot_key:
            reasons.append(f"每日定时播报 ({now_bjt.strftime('%H:00')} BJT)")
            state["last_scheduled_slot"] = slot_key

    alert_type = None
    if ahr999 <= BOTTOM_THRESHOLD:
        alert_type = "bottom"
        title = "🟢 BTC Ahr999 抄底阈值告警 (≤ 0.45)"
        template = "green"
    elif ahr999 >= TOP_THRESHOLD:
        alert_type = "top"
        title = "🔴 BTC Ahr999 过热阈值告警 (≥ 1.20)"
        template = "red"

    if alert_type:
        last_ts = float(state.get("last_alert_ts", 0))
        last_type = state.get("last_alert_type")
        if (now_ts - last_ts) >= COOLDOWN_SECONDS or last_type != alert_type:
            reasons.append("低于 0.45 抄底告警" if alert_type == "bottom" else "高于 1.20 过热告警")
            state["last_alert_ts"] = now_ts
            state["last_alert_type"] = alert_type
        else:
            print(f"[INFO] Alert '{alert_type}' suppressed due to 60-minute cooldown.")

    if not reasons:
        print("[INFO] No push condition met. Skipping Feishu notification.")
        save_state(state)
        return

    trigger_reason = " + ".join(reasons)
    print(f"[INFO] Sending Feishu card: {trigger_reason}")
    send_feishu_card(
        webhook_url=webhook_url,
        title=title,
        template=template,
        trigger_reason=trigger_reason,
        price=price,
        ahr999=ahr999,
        geo_mean=geo_mean,
        exp_val=exp_val,
        zone_label=zone_label,
        advice=advice,
        source=source,
        bjt_str=bjt_str,
    )
    save_state(state)


if __name__ == "__main__":
    main()
