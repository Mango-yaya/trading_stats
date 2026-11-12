#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资金面日报渲染器：接口取数 -> 渲染手机版 HTML -> 推送 GitHub Pages -> 推送飞书
用法: python3 daily.py --analysis /tmp/analysis.json [--no-upload] [--no-feishu]
只依赖标准库。
"""
import argparse, base64, datetime, json, os, re, subprocess, sys, time, urllib.parse, urllib.request

REPO = "Mango-yaya/trading_stats"
PAGE = "https://mango-yaya.github.io/trading_stats/reports/latest.html"
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/09351a6c-5ea2-45c7-a797-50338c4dfd6d"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
POLICY = 1.25  # 隔夜逆回购操作利率

def log(*a):
    print("[%s]" % datetime.datetime.now().strftime("%H:%M:%S"), *a, flush=True)

def fetch(url, headers=None, timeout=25, retry=3):
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise last

# ---------------- 数据 ----------------
def shibor_series(indicator="001", n=16):
    p = urllib.parse.urlencode({
        "reportName": "RPT_IMP_INTRESTRATEN", "columns": "REPORT_DATE,IR_RATE,CHANGE_RATE",
        "filter": '(MARKET_CODE="001")(CURRENCY_CODE="CNY")(INDICATOR_ID="%s")' % indicator,
        "pageNumber": 1, "pageSize": n, "sortColumns": "REPORT_DATE", "sortTypes": -1,
        "source": "WEB", "client": "WEB"})
    try:
        j = json.loads(fetch("https://datacenter-web.eastmoney.com/api/data/v1/get?" + p))
        rows = [(d["REPORT_DATE"][:10], float(d["IR_RATE"]), d.get("CHANGE_RATE")) for d in j["result"]["data"]]
        return rows[::-1]
    except Exception as e:
        log("Shibor fail", indicator, repr(e)[:80]); return []

def _curl(url, headers=None, timeout=15):
    cmd = ["curl", "-s", "--max-time", str(timeout)]
    for k, v in (headers or {}).items():
        cmd += ["-H", "%s: %s" % (k, v)]
    cmd.append(url)
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout + 5).stdout
    except Exception:
        return b""

def gc_quote(secid):
    """上交所质押式回购价：curl 直连（新浪优先，东财兜底）。返回 {'now','prev'}"""
    code = {"1.204001": "sh204001", "1.204007": "sh204007"}.get(secid, "")
    if code:
        for _ in range(2):
            raw = _curl("https://hq.sinajs.cn/list=" + code,
                        {"User-Agent": UA["User-Agent"], "Referer": "https://finance.sina.com.cn/"})
            m = re.search(r'"([^"]+)"', raw.decode("gbk", "ignore"))
            if m:
                p = m.group(1).split(",")
                try:
                    return {"now": float(p[3]), "prev": float(p[2]), "name": p[0]}
                except Exception:
                    pass
            time.sleep(1)
    for host in ["push2.eastmoney.com", "82.push2.eastmoney.com"]:
        raw = _curl("https://%s/api/qt/stock/get?secid=%s&fields=f43,f60,f58" % (host, secid),
                    {"User-Agent": UA["User-Agent"], "Referer": "https://quote.eastmoney.com/"})
        try:
            d = (json.loads(raw.decode("utf-8", "ignore")).get("data") or {})
            if d.get("f43"):
                return {"now": d["f43"] / 1000.0, "prev": d.get("f60", 0) / 1000.0, "name": d.get("f58")}
        except Exception:
            pass
    log("GC fail", secid); return {}

def frr_data(days=10):
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    url = ("https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/FrrHis?lang=CN"
           "&startDate=%s&endDate=%s" % (start, end))
    try:
        j = json.loads(fetch(url, {"Referer": "https://www.chinamoney.com.cn/chinese/bkfrr/"}))
        recs = j.get("records") or j["data"].get("records") or []
        latest = recs[0].get("frValueMap") or {}
        return latest
    except Exception as e:
        log("FrrHis fail", repr(e)[:80]); return {}

# ---------------- SVG ----------------
def bar_svg(items, policy=POLICY):
    """当日利率横向对比。items: [(label, value)]"""
    items = [(k, v) for k, v in items if isinstance(v, (int, float)) and v > 0]
    if not items:
        return '<p style="color:#7a8794;font-size:13px">当日利率数据待发布</p>'
    vals = [v for _, v in items] + [policy]
    lo, hi = min(vals) - 0.05, max(vals) + 0.05
    h = 46 + 34 * len(items)
    X = lambda v: 96 + (v - lo) / (hi - lo) * 258
    out = ['<svg viewBox="0 0 420 %d" width="100%%" role="img" aria-label="当日主要货币市场利率对比">' % h]
    # 网格
    for g in [lo + (hi - lo) * k / 4 for k in range(1, 4)]:
        out.append('<line x1="%g" y1="16" x2="%g" y2="%g" stroke="#eef1f4"/>' % (X(g), X(g), h - 26))
    # 政策利率
    out.append('<line x1="%g" y1="16" x2="%g" y2="%g" stroke="#b3352b" stroke-dasharray="5 3" stroke-width="1.2"/>' % (X(policy), X(policy), h - 26))
    out.append('<text x="%g" y="%d" font-size="11" fill="#b3352b" text-anchor="middle">政策利率 %.2f%%</text>' % (X(policy), h - 10, policy))
    for i, (k, v) in enumerate(items):
        y = 22 + i * 34
        color = "#a8802c" if k.startswith("GC") else ("#2e7d6f" if v < policy else "#1b3f66")
        out.append('<text x="90" y="%d" font-size="12.5" fill="#4a5866" text-anchor="end">%s</text>' % (y + 13, k))
        out.append('<rect x="96" y="%d" width="%g" height="15" fill="%s" rx="1.5"/>' % (y + 2, max(X(v) - 96, 2), color))
        out.append('<text x="%g" y="%d" font-size="12" fill="#1a2430" font-weight="600">%.4f</text>' % (X(v) + 6, y + 14, v))
    out.append('</svg>')
    return "".join(out)

def line_svg(series, policy=POLICY, color="#1b3f66", w=400, h=210, label=""):
    series = [(d, v) for d, v, *_ in series if isinstance(v, (int, float))]
    if len(series) < 2:
        return '<p style="color:#7a8794;font-size:13px">走势数据不足</p>'
    vals = [v for _, v in series]
    lo = min(min(vals), policy) - 0.03
    hi = max(max(vals), policy) + 0.03
    Y = lambda v: 24 + (h - 62) * (hi - v) / (hi - lo)
    out = ['<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="%s">' % (w, h, label)]
    for g in [lo + 0.01 * k for k in range(1, 6) if lo + 0.01 * k < hi]:
        out.append('<line x1="34" y1="%g" x2="%d" y2="%g" stroke="#eef1f4"/>' % (Y(g), w - 16, Y(g)))
    out.append('<line x1="34" y1="%g" x2="%d" y2="%g" stroke="#b3352b" stroke-dasharray="5 3" stroke-width="1.2"/>' % (Y(policy), w - 16, Y(policy)))
    out.append('<text x="%d" y="%g" font-size="11" fill="#b3352b" text-anchor="end">政策利率 %.2f%%</text>' % (w - 18, Y(policy) - 5, policy))
    pts = " ".join("%g,%g" % (34 + (w - 52) * i / (len(vals) - 1), Y(v)) for i, v in enumerate(vals))
    out.append('<polyline fill="none" stroke="%s" stroke-width="1.8" points="%s"/>' % (color, pts))
    out.append('<circle cx="%g" cy="%g" r="2.8" fill="%s"/>' % (w - 18, Y(vals[-1]), color))
    out.append('<text x="%g" y="%g" font-size="11.5" fill="#1a2430" font-weight="600" text-anchor="end">%.4f</text>' % (w - 18, Y(vals[-1]) - 7, vals[-1]))
    out.append('<text x="34" y="%d" font-size="11" fill="#8b98a5">%s</text>' % (h - 12, series[0][0][5:]))
    out.append('<text x="%g" y="%d" font-size="11" fill="#8b98a5" text-anchor="end">%s</text>' % (w - 18, h - 12, series[-1][0][5:]))
    out.append("</svg>")
    return "".join(out)

# ---------------- 渲染 ----------------
def esc(s):
    return str(s if s is not None else "")

def render(tpl, a, d):
    tags = a.get("tags") or []
    header = [
        '<header class="hd">',
        '  <div class="hd-kicker">MACRO LIQUIDITY · DAILY</div>',
        '  <h1>宏观资金面一页纸</h1>',
        '  <div class="hd-meta">%s</div>' % esc(a.get("meta", "")),
        '  <div class="hd-tags">%s</div>' % "".join(
            '<span class="tag%s">%s</span>' % (" gold" if i == len(tags) - 1 else "", t) for i, t in enumerate(tags)),
        '</header>',
    ]
    summary = "\n".join("      <li>%s</li>" % s for s in (a.get("summary") or []))
    dims = []
    for dm in (a.get("dims") or []):
        rows = "".join(
            '<div class="row"><span class="k">%s</span><span class="v">%s</span></div>' % (r[0], r[1])
            for r in (dm.get("rows") or []) if len(r) >= 2)
        dims.append(
            '    <div class="dim">\n      <div class="dim-h"><span class="dim-t">%s</span>'
            '<span class="pill %s">%s</span></div>\n      %s\n    </div>'
            % (dm.get("t", ""), dm.get("pill", "neutral"), dm.get("pill_text", "中性"), rows))
    figs = [
        '    <div class="fig">',
        '      <div class="fig-t">图 1　当日主要货币市场利率对比</div>',
        '      <div class="fig-s">单位：%　红色虚线为隔夜逆回购操作利率（政策利率锚）</div>',
        '      ' + bar_svg([(k, v) for k, v in d["bars"]]),
        '      <div class="fig-note">%s</div>' % esc(a.get("fig1_note", "")),
        '    </div>',
        '    <div class="fig">',
        '      <div class="fig-t">图 2　Shibor 隔夜（近 16 个交易日）</div>',
        '      <div class="fig-s">单位：%%　红色虚线为政策利率锚 %.2f%%</div>' % POLICY,
        '      ' + line_svg(d["shib_on"], label="Shibor隔夜走势"),
        '      <div class="fig-note">%s</div>' % esc(a.get("fig2_note", "数据来源：东方财富数据中心接口直连。")),
        '    </div>',
    ]
    scen = []
    for s in (a.get("scenarios") or []):
        scen.append('    <div class="hzn">\n      <h3>%s</h3>\n      <p>%s</p>\n      <div class="kpi">%s</div>\n    </div>'
                    % (s.get("h", ""), s.get("p", ""), s.get("kpi", "")))
    html = (tpl.replace("{{HEADER}}", "\n".join(header))
               .replace("{{SUMMARY}}", summary)
               .replace("{{VERDICT}}", esc(a.get("verdict", "")))
               .replace("{{DIMS}}", "\n".join(dims))
               .replace("{{FIGS}}", "\n".join(figs))
               .replace("{{READ}}", esc(a.get("read", "")))
               .replace("{{SCENARIOS}}", "\n".join(scen))
               .replace("{{FOOTER}}", esc(a.get("footer", "")))
               .replace("{{DATE}}", a.get("date", ""))
               .replace("{{TITLE_DATE}}", a.get("cn_date", a.get("date", ""))))
    return html

# ---------------- GitHub ----------------
def gh_put(path, content_str, token, msg):
    api = "https://api.github.com/repos/%s/contents/%s" % (REPO, path)
    hdr = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}
    sha = None
    try:
        j = json.loads(fetch(api, hdr))
        sha = j.get("sha")
    except Exception:
        pass
    body = {"message": msg, "branch": "main",
            "content": base64.b64encode(content_str.encode("utf-8")).decode()}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(api, data=json.dumps(body).encode(), headers=hdr, method="PUT")
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.status

# ---------------- 飞书 ----------------
def feishu(title, lines):
    content = [[{"tag": "text", "text": t}] for t in lines]
    content.append([{"tag": "a", "text": "▶ 点此查看完整网页版报告", "href": PAGE}])
    payload = {"msg_type": "post", "content": {"post": {"zh_cn": {"title": title, "content": content}}}}
    req = urllib.request.Request(HOOK, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis", default="/tmp/analysis.json")
    ap.add_argument("--template", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html"))
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN", ""))
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--no-feishu", action="store_true")
    args = ap.parse_args()

    a = json.load(open(args.analysis, encoding="utf-8"))
    today = a.get("date") or datetime.date.today().isoformat()

    so = shibor_series("001", 16)
    s1 = shibor_series("101", 16)
    gc1 = gc_quote("1.204001")
    gc7 = gc_quote("1.204007")
    frr = frr_data()

    dr = a.get("dr") or {}
    def num(x):
        try: return float(x)
        except Exception: return None

    bars = [("DR001", num(dr.get("dr001"))), ("FDR001", num(frr.get("FDR001"))),
            ("Shibor 隔夜", so[-1][1] if so else None), ("GC001", gc1.get("now")),
            ("R001", num(dr.get("r001")))]
    data = {"bars": bars, "shib_on": so}
    log("bars", bars)

    tpl = open(args.template, encoding="utf-8").read()
    html = render(tpl, a, data)
    for p in ["/tmp/report.html"]:
        open(p, "w", encoding="utf-8").write(html)
    log("html bytes", len(html))

    ok = True
    if not args.no_upload:
        if not args.token:
            log("!! 缺少 GH_TOKEN，跳过上传"); ok = False
        else:
            for path in ["reports/latest.html", "reports/%s.html" % today]:
                try:
                    st = gh_put(path, html, args.token, "daily report %s" % today)
                    log("upload", path, st)
                except Exception as e:
                    ok = False; log("upload fail", path, repr(e)[:160])
            try:
                gh_put("data/lastrun.json", json.dumps({"date": today, "ok": ok,
                       "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False), args.token, "lastrun %s" % today)
            except Exception as e:
                log("lastrun fail", repr(e)[:100])

    if not args.no_feishu:
        f = a.get("feishu") or {}
        try:
            r = feishu("宏观资金面一页纸 · %s %s" % (today, a.get("weekday", "")), [
                "【研判】" + f.get("verdict", a.get("verdict", "待发布")),
                "【利率】" + f.get("rates", "待发布"),
                "【OMO】" + f.get("omo", "待发布"),
                "【明日】" + f.get("tomorrow", "待发布"),
            ])
            log("feishu", r)
        except Exception as e:
            ok = False; log("feishu fail", repr(e)[:160])

    print("RESULT", "OK" if ok else "PARTIAL")

if __name__ == "__main__":
    main()
