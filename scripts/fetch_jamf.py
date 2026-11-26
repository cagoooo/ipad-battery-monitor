"""從 Jamf Pro 抓全校 iPad 電量，算出需要追蹤的平板，輸出成監控後台用的 data.json。

只用 Python 標準函式庫（GitHub Actions 不必安裝套件）。
帳密只從環境變數讀取：
  JAMF_URL               例：https://xxxx.jamfcloud.com
  JAMF_CLIENT_ID / JAMF_CLIENT_SECRET   （建議：Jamf「API 角色與用戶端」建立的唯讀用戶端）
  或 JAMF_USER / JAMF_PASSWORD           （沒有用戶端時退而使用帳號密碼）
  GCHAT_WEBHOOK          （選填）Google Chat 聊天室的 Webhook 網址，有新問題時推播
  JAMF_SEND_UPDATE_INVENTORY=1           （選填）順便請所有平板立刻回報最新電量

輸出的 data.json 只放「裝置名稱、電量、回報時間、機型」，不放序號、IP、UDID 等資訊。
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TPE = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc

LOW = 50          # 低於這個百分比 → 需要追蹤
CRITICAL = 20     # 低於這個百分比 → 危險
NIGHT_FULL = 80   # 半夜還在充電車裡，電量應該要超過這個值
STALE_HOURS = 72  # 超過這麼久沒回報 → 失聯
HISTORY_DAYS = 14
HISTORY_MAX = 80

# 依 Jamf 使用者名稱前綴分車（與 Jamf 智慧群組命名一致）
CARTS = [
    ("STU1", "1、2 年級車", "student"),
    ("STU3", "3 年級車", "student"),
    ("STU4", "4 年級車", "student"),
    ("STU5", "5 年級車", "student"),
    ("STU6", "6 年級車", "student"),
    ("STU7", "第七車（2024 新增）", "student"),
    ("ENG1", "英文 1 車", "student"),
    ("ENG2", "英文 2 車", "student"),
    ("ENG3", "英文 3 車", "student"),
    ("TEA", "老師機", "teacher"),
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Jamf:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.token = None

    def _req(self, method, path, headers=None, data=None, timeout=60):
        r = urllib.request.Request(self.base + path, method=method, headers=headers or {}, data=data)
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body) if body else None

    def login(self):
        cid, secret = os.environ.get("JAMF_CLIENT_ID"), os.environ.get("JAMF_CLIENT_SECRET")
        if cid and secret:
            data = urllib.parse.urlencode({"client_id": cid, "client_secret": secret,
                                           "grant_type": "client_credentials"}).encode()
            res = self._req("POST", "/api/oauth/token",
                            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, data)
            self.token, self.kind = res["access_token"], "oauth"
        else:
            user, pw = os.environ["JAMF_USER"], os.environ["JAMF_PASSWORD"]
            basic = base64.b64encode(f"{user}:{pw}".encode()).decode()
            res = self._req("POST", "/api/v1/auth/token", {"Authorization": "Basic " + basic, "Accept": "application/json"})
            self.token, self.kind = res["token"], "basic"

    def get(self, path):
        return self._req("GET", path, {"Authorization": "Bearer " + self.token, "Accept": "application/json"})

    def post(self, path):
        return self._req("POST", path, {"Authorization": "Bearer " + self.token, "Accept": "application/json"}, b"")

    def logout(self):
        try:
            if self.token and self.kind == "basic":  # OAuth 權杖會自己過期，不必登出
                self.post("/api/v1/auth/invalidate-token")
        except Exception:
            pass

    def mobile_devices(self):
        out, page = [], 0
        while True:
            d = self.get(f"/api/v2/mobile-devices/detail?section=GENERAL&section=HARDWARE"
                         f"&section=USER_AND_LOCATION&page={page}&page-size=200&sort=displayName%3Aasc")
            out += d["results"]
            if len(out) >= d["totalCount"] or not d["results"]:
                return out
            page += 1


def parse_time(s):
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", s)
    return dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC) if m else None


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def classify(username: str, name: str):
    key = (username or name or "").upper()
    for prefix, label, kind in CARTS:
        if re.search(rf"(^|[-_]){prefix}[-_]", key) or key.startswith(prefix + "-"):
            m = re.search(r"-(\d+)\s*$", key)
            return prefix, label, kind, (int(m.group(1)) if m else None)
    return "OTHER", "其他", "other", None


def is_night(t: dt.datetime):
    local = t.astimezone(TPE)
    return local.hour < 7 or (local.hour == 7 and local.minute < 30)


def build(devs, prev, now):
    prev_by_id = {str(d["id"]): d for d in prev.get("devices", [])}
    cutoff = now - dt.timedelta(days=HISTORY_DAYS)
    out = []
    for x in devs:
        g, h, ul = x.get("general") or {}, x.get("hardware") or {}, x.get("userAndLocation") or {}
        did = str(x.get("mobileDeviceId"))
        name = g.get("displayName") or ""
        user = ul.get("username") or ""
        cart, cart_label, kind, slot = classify(user, name)
        bat = h.get("batteryLevel")
        rep = parse_time(g.get("lastInventoryUpdateDate"))
        contact = parse_time(g.get("lastContactDate"))

        hist = [list(p) for p in prev_by_id.get(did, {}).get("history", [])]
        if rep and bat is not None and (not hist or hist[-1][0] != iso(rep)):
            hist.append([iso(rep), bat])
        hist = [p for p in hist if parse_time(p[0]) and parse_time(p[0]) >= cutoff][-HISTORY_MAX:]

        flags = []
        age_h = (now - rep).total_seconds() / 3600 if rep else None
        if bat is not None:
            if bat < CRITICAL:
                flags.append("critical")
            elif bat < LOW:
                flags.append("low")
        if age_h is None or age_h > STALE_HOURS:
            flags.append("stale")
        if kind == "student":
            # 半夜（00:00–07:30）回報時電量還不到 80% → 很可能整晚沒充到電
            recent_night = [p for p in hist if is_night(parse_time(p[0]))
                            and (now - parse_time(p[0])).total_seconds() < 20 * 3600]
            # 但之後若已回升到 80% 以上，就代表其實有在充，不必再追
            if recent_night and recent_night[-1][1] < NIGHT_FULL and hist[-1][1] < NIGHT_FULL:
                flags.append("night")
            # 連續兩次回報（相隔 2 小時以上）都低於 50%，而且沒有回升
            if len(hist) >= 2:
                (t1, b1), (t2, b2) = hist[-2], hist[-1]
                gap = (parse_time(t2) - parse_time(t1)).total_seconds() / 3600
                if b1 < LOW and b2 < LOW and b2 <= b1 + 2 and gap >= 2:
                    flags.append("persist")
        trend = None
        if len(hist) >= 2:
            trend = hist[-1][1] - hist[-2][1]

        out.append({
            "id": did, "name": name, "user": user, "cart": cart, "cartLabel": cart_label,
            "kind": kind, "slot": slot, "model": h.get("model") or "", "battery": bat,
            "reportedAt": iso(rep), "contactAt": iso(contact), "trend": trend,
            "flags": flags, "history": hist,
        })
    return out


def summarize(devices):
    s = {"total": len(devices)}
    for f in ("critical", "low", "night", "persist", "stale"):
        s[f] = sum(1 for d in devices if f in d["flags"])
    s["needCheck"] = sum(1 for d in devices if d["kind"] == "student" and "stale" not in d["flags"] and
                         any(f in d["flags"] for f in ("critical", "low", "night", "persist")))
    return s


def notify(devices, prev, now):
    """有新出現的問題才推播；同一台同一次回報只通知一次。"""
    hook = os.environ.get("GCHAT_WEBHOOK")
    alerted = dict(prev.get("alerted", {}))
    fresh = []
    for d in devices:
        if d["kind"] != "student" or not d["reportedAt"] or "stale" in d["flags"]:
            continue
        if any(f in d["flags"] for f in ("critical", "night", "persist")) and alerted.get(d["id"]) != d["reportedAt"]:
            fresh.append(d)
            alerted[d["id"]] = d["reportedAt"]
    # 只保留還在問題清單上的紀錄，避免無限長大
    live = {d["id"] for d in devices if any(f in d["flags"] for f in ("critical", "night", "persist", "low"))}
    alerted = {k: v for k, v in alerted.items() if k in live}
    if hook and fresh:
        reason = {"critical": "電量危險", "night": "整晚沒充飽", "persist": "連續低電量"}
        lines = [f"🔋 *平板電量警示*（{now.astimezone(TPE):%m/%d %H:%M}）新發現 {len(fresh)} 台需要檢查充電線："]
        for d in sorted(fresh, key=lambda d: (d["cart"], d["slot"] or 0))[:30]:
            tags = "、".join(reason[f] for f in d["flags"] if f in reason)
            lines.append(f"• {d['cartLabel']} {d['slot'] or ''} 號（{d['name']}）{d['battery']}%｜{tags}")
        site = os.environ.get("SITE_URL")
        if site:
            lines.append(f"👉 監控後台：{site}")
        try:
            req = urllib.request.Request(hook, method="POST", data=json.dumps({"text": "\n".join(lines)}).encode(),
                                         headers={"Content-Type": "application/json; charset=UTF-8"})
            urllib.request.urlopen(req, timeout=20).read()
            log(f"已推播 {len(fresh)} 台")
        except Exception as e:  # 推播失敗不影響後台更新
            log("推播失敗：", type(e).__name__)
    return alerted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prev", default="prev.json")
    ap.add_argument("--out", default="site/data.json")
    a = ap.parse_args()
    try:
        prev = json.load(open(a.prev, encoding="utf-8"))
    except Exception:
        prev = {}
    now = dt.datetime.now(UTC)
    jamf = Jamf(os.environ["JAMF_URL"])
    try:
        jamf.login()
        devs = jamf.mobile_devices()
        if os.environ.get("JAMF_SEND_UPDATE_INVENTORY") == "1":
            ids = ",".join(str(d["mobileDeviceId"]) for d in devs)
            try:
                jamf.post(f"/JSSResource/mobiledevicecommands/command/UpdateInventory/id/{ids}")
                log("已請平板回報最新電量")
            except urllib.error.HTTPError as e:
                log("送出更新電量指令失敗：", e.code)
    finally:
        jamf.logout()

    devices = build(devs, prev, now)
    data = {
        "generatedAt": iso(now),
        "thresholds": {"low": LOW, "critical": CRITICAL, "nightFull": NIGHT_FULL, "staleHours": STALE_HOURS},
        "summary": summarize(devices),
        "devices": devices,
        "alerted": notify(devices, prev, now),
    }
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log("完成：", json.dumps(data["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
