#!/usr/bin/env python3
"""人気帯別・ロジック版別の ROI 集計.

  versions : track_record.json の git 履歴（日次の累計スナップショット）を差分して、
             ロジック版ごとの レース数・的中率・ROI を出す。外部データ不要。
  bands    : live_free.json の git 履歴から各レースの発走前最終予想（帯・買い目）を復元し、
             結果CSV と突き合わせて 帯 × ロジック版 の ROI を出す。
  fetch    : bands 用の結果CSV を boatrace.jp から取る（ネットに出られる手元で実行）。

結果CSV: date,venue,race,trifecta,payout  （payout は100円あたり。返還・不成立は行を作らない）
例:      2026-09-30,鳴門,1,1-2-3,1230

  python3 tools/roi_breakdown.py versions
  python3 tools/roi_breakdown.py fetch --since 2026-07-20 --out results.csv
  python3 tools/roi_breakdown.py bands --results results.csv
"""
import argparse
import bisect
import csv
import datetime as dt
import json
import random
import re
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (開始日, 名前, 1レースあたり点数)  track_record.json の logic_note に合わせる
VERSIONS = [
    ("2026-07-04", "V3・1号軸6点", 6),
    ("2026-09-02", "着順モデル6点", 6),
    ("2026-09-09", "着順モデル10点", 10),
]
BANDS = ["本命", "中本命", "拮抗", "穴", "大穴"]
JCD = {n: i + 1 for i, n in enumerate(
    "桐生 戸田 江戸川 平和島 多摩川 浜名湖 蒲郡 常滑 津 三国 びわこ 住之江 "
    "尼崎 鳴門 丸亀 児島 宮島 徳山 下関 若松 芦屋 福岡 唐津 大村".split())}


def version_of(date):
    cur = None
    for start, name, pts in VERSIONS:
        if date >= start:
            cur = (name, pts)
    return cur


# ---------------------------------------------------------------- git
def file_history(path):
    """path の全版を古い順に (コミット時刻, dict) で返す。"""
    out = subprocess.check_output(
        ["git", "-C", str(REPO), "log", "--reverse", "--format=%H %cI", "--", path], text=True)
    revs = [l.split() for l in out.splitlines()]
    p = subprocess.Popen(["git", "-C", str(REPO), "cat-file", "--batch"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    for sha, ts in revs:
        p.stdin.write(f"{sha}:{path}\n".encode())
        p.stdin.flush()
        head = p.stdout.readline().split()
        if head[-1] == b"missing":
            continue
        body = p.stdout.read(int(head[2]))
        p.stdout.read(1)
        try:
            yield dt.datetime.fromisoformat(ts), json.loads(body)
        except ValueError:
            continue
    p.stdin.close()
    p.wait()


def ensure_full_history():
    shallow = subprocess.check_output(
        ["git", "-C", str(REPO), "rev-parse", "--is-shallow-repository"], text=True).strip()
    if shallow == "true":
        sys.exit("shallow clone です。先に `git fetch --unshallow` を実行してください。")


# ---------------------------------------------------------------- versions
def cmd_versions(a):
    ensure_full_history()
    snaps = {}  # generated日 -> その日の最終スナップショット
    for _, d in file_history("track_record.json"):
        if d.get("first_date") != VERSIONS[0][0]:
            continue  # 旧ロジック期間（first_date 違い）の版は除外
        snaps[d["generated"]] = d["cumulative"]
    days = sorted(snaps)
    rows, prev = [], {"races": 0, "hits": 0, "stake": 0.0, "pay": 0.0}
    for g in days:
        c = snaps[g]
        name, pts = version_of(g)
        races = c["races"] - prev["races"]
        stake = prev["stake"] + races * pts
        pay = c["roi"] / 100 * stake  # 累計ROI×累計投資 = 累計払戻（単位: 100円）
        r = {"date": g, "version": name, "races": races, "hits": c["hits"] - prev["hits"],
             "stake": races * pts, "pay": pay - prev["pay"], "cum_roi": c["roi"]}
        r["roi"] = 100 * r["pay"] / r["stake"] if r["stake"] else None
        # 異常: 1日ROIが負/極端 → 累計の再計算（過去分の書き換え）が疑われる
        r["flag"] = "!" if r["roi"] is not None and not (0 <= r["roi"] <= 400) else ""
        rows.append(r)
        prev = {"races": c["races"], "hits": c["hits"], "stake": stake, "pay": pay}

    print("## ロジック版別（track_record.json の累計差分）\n")
    print("| 版 | 期間 | レース | 的中率 | 投資(点) | ROI | 異常日より後だけのROI |")
    print("|---|---|---|---|---|---|---|")
    for start, name, pts in VERSIONS:
        vs = [r for r in rows if r["version"] == name]
        if not vs:
            continue
        n = sum(r["races"] for r in vs)
        h = sum(r["hits"] for r in vs)
        s = sum(r["stake"] for r in vs)
        p = sum(r["pay"] for r in vs)
        # 異常日（累計の書き換え）より前の差分は書き換え前の数字なので、最後の異常日より後だけでも出す
        last_bad = max((i for i, r in enumerate(vs) if r["flag"]), default=None)
        if last_bad is None:
            after = "-"
        else:
            ok = vs[last_bad + 1:]
            ok_s = sum(r["stake"] for r in ok)
            after = (f"{100*sum(r['pay'] for r in ok)/ok_s:.1f}%（{ok[0]['date']}〜, "
                     f"{sum(r['races'] for r in ok)}R）") if ok_s else "-"
        print(f"| {name} | {start}〜{vs[-1]['date']} | {n} | {100*h/n:.1f}% | {s:.0f} "
              f"| {100*p/s:.1f}% | {after} |")
    if a.daily:
        print("\n| 日 | 版 | レース | 的中 | 日次ROI | 累計ROI | |")
        print("|---|---|---|---|---|---|---|")
        for r in rows:
            roi = f"{r['roi']:.1f}%" if r["roi"] is not None else "-"
            print(f"| {r['date']} | {r['version']} | {r['races']} | {r['hits']} | {roi} | {r['cum_roi']}% | {r['flag']} |")
    bad = [r["date"] for r in rows if r["flag"]]
    print(f"\n注: 累計ROIは小数1桁丸めのため日次ROIは誤差が大きい（版単位の合計は端点2つで決まるので誤差小）。")
    print("注: 投資は「レース数×点数×100円」と仮定（track_record の累計ROIが投資額加重である前提）。")
    if bad:
        print(f"注: 日次ROIが 0〜400% を外れた日 {bad} → 累計の再計算（過去分の書き換え）が疑われる。"
              "それより前の日次差分は書き換え前の数字なので、「異常日より後だけのROI」が最も整合的。")


# ---------------------------------------------------------------- bands
def load_predictions():
    """各レースの発走前最終予想を復元する。キー: (date, venue, race)"""
    ensure_full_history()
    sched = {}  # (date, venue, race) -> 発走 datetime(JST)
    for _, v in file_history("venues_today.json"):
        for venue, lst in v.get("schedule", {}).items():
            for r, hm in lst:
                sched[(v["date"], venue, int(r))] = dt.datetime.fromisoformat(
                    f"{v['date']}T{hm}:00+09:00")
    preds = {}
    for ts, d in file_history("live_free.json"):
        for key, v in d.get("races", {}).items():
            if not v.get("kaime") or "_" not in key:
                continue
            venue, r = key.rsplit("_", 1)
            k = (d["date"], venue, int(r))
            post = sched.get(k)
            if post is not None and ts >= post:
                continue  # 発走後の更新は使わない
            preds[k] = v
    return preds


def load_results(path):
    res = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            res[(row["date"], row["venue"], int(row["race"]))] = (row["trifecta"], int(row["payout"]))
    return res


def boot_ci(stakes, pays, n=2000, seed=0):
    rng = random.Random(seed)
    m = len(stakes)
    vals = []
    for _ in range(n):
        idx = [rng.randrange(m) for _ in range(m)]
        s = sum(stakes[i] for i in idx)
        vals.append(100 * sum(pays[i] for i in idx) / s)
    vals.sort()
    return vals[int(0.025 * n)], vals[int(0.975 * n)]


def cmd_bands(a):
    preds = load_predictions()
    res = load_results(a.results)
    cells = defaultdict(lambda: ([], [], [0]))  # (行キー) -> (stakes, pays, hits)
    nores = 0
    for k, v in preds.items():
        if k not in res:
            nores += 1
            continue
        win, payout = res[k]
        stake = len(v["kaime"]) * 100
        pay = payout if win in v["kaime"] else 0
        ver = version_of(k[0])
        if ver is None:
            continue
        for key in [(v.get("gap"), ver[0]), (v.get("gap"), "全期間"), ("全帯", ver[0]), ("全帯", "全期間")]:
            c = cells[key]
            c[0].append(stake)
            c[1].append(pay)
            c[2][0] += pay > 0
    print(f"## 人気帯 × ロジック版（live_free 履歴 {len(preds)}R、結果突合 {len(preds)-nores}R、結果なし {nores}R）\n")
    print("| 帯 | 版 | レース | 的中率 | 平均点数 | ROI | 95%CI |")
    print("|---|---|---|---|---|---|---|")
    vnames = [v[1] for v in VERSIONS] + ["全期間"]
    for band in BANDS + ["全帯"]:
        for vn in vnames:
            if (band, vn) not in cells:
                continue
            st, py, h = cells[(band, vn)]
            n = len(st)
            roi = 100 * sum(py) / sum(st)
            ci = "{:.0f}〜{:.0f}%".format(*boot_ci(st, py)) if n >= 30 else "n<30"
            print(f"| {band} | {vn} | {n} | {100*h[0]/n:.1f}% | {sum(st)/n/100:.1f} | {roi:.1f}% | {ci} |")
    print("\n注: 対象は無料フィード（live_free.json）に載ったレースのみ。track_record の全レースとは母集団が違う。")
    print("注: CI はレース単位ブートストラップ。下限が100%を超えない限り「プラス」とは言えない。")


# ---------------------------------------------------------------- fetch
def fetch_one(date, venue, race):
    url = (f"https://www.boatrace.jp/owpc/pc/race/raceresult?rno={race}"
           f"&jcd={JCD[venue]:02d}&hd={date.replace('-', '')}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    i = html.find("3連単")
    j = html.find("3連複", i)
    if i < 0 or j < 0:
        return None
    seg = html[i:j]
    nums = re.findall(r'numberSet1_number[^>]*>\s*(\d)\s*<', seg)
    pay = re.search(r'is-payout1[^>]*>\s*(?:&yen;|¥|￥)?\s*([\d,]+)', seg)
    if len(nums) < 3 or not pay:
        return None  # 不成立・返還など
    return "-".join(nums[:3]), int(pay.group(1).replace(",", ""))


def cmd_fetch(a):
    preds = load_predictions()
    out = Path(a.out)
    have = set(load_results(out)) if out.exists() else set()
    todo = sorted(k for k in preds if k[0] >= a.since and k not in have
                  and k[0] < dt.date.today().isoformat() and k[1] in JCD)
    print(f"取得対象 {len(todo)}R（既存 {len(have)}R）", file=sys.stderr)
    new = not out.exists()
    with open(out, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "venue", "race", "trifecta", "payout"])
        for n, k in enumerate(todo, 1):
            try:
                r = fetch_one(*k)
            except Exception as e:
                print(f"失敗 {k}: {e}", file=sys.stderr)
                r = None
            if r:
                w.writerow([*k, *r])
                f.flush()
            if n % 50 == 0:
                print(f"{n}/{len(todo)}", file=sys.stderr)
            time.sleep(a.wait)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("versions")
    p.add_argument("--daily", action="store_true", help="日次の内訳も出す")
    p.set_defaults(f=cmd_versions)
    p = sp.add_parser("bands")
    p.add_argument("--results", required=True)
    p.set_defaults(f=cmd_bands)
    p = sp.add_parser("fetch")
    p.add_argument("--since", default="2026-07-01")
    p.add_argument("--out", default="results.csv")
    p.add_argument("--wait", type=float, default=1.0, help="1リクエストごとの待ち秒")
    p.set_defaults(f=cmd_fetch)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
