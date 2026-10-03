#!/usr/bin/env python3
"""餐厅评论抓取工具（高德 / 百度地图），先只负责把评论抓到本地，不做筛选。

用法示例（Windows 下把 py 换成 python）:
  # 1) 搜店，列出候选（有 key 走官方 API，没 key 自动用浏览器搜索）
  py main.py search --source amap --keyword 楼外楼 --city 杭州
  py main.py search --source baidu --keyword 楼外楼 --city 杭州 --city-code 179

  # 2) 按 ID 抓评论（浏览器方案；遇到验证码加 --headed 手动过一次）
  py main.py reviews --source amap --poi-id B0FFHXXXX --headed
  py main.py reviews --source baidu --uid xxxxx --headed

  # 3) 一条龙：给店名 -> 搜索 -> 选序号 -> 抓评论
  py main.py run --source amap --keyword 楼外楼 --city 杭州 --headed
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fetchers import amap_official, baidu_official, browser_fetch  # noqa: E402
from fetchers.common import ensure_dir, now_stamp, safe_name, save_json  # noqa: E402

OUT_ROOT = ROOT / "out"


def load_config():
    cfg = {"amap_key": "", "baidu_ak": ""}
    path = ROOT / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    cfg["amap_key"] = cfg.get("amap_key") or os.environ.get("AMAP_KEY", "")
    cfg["baidu_ak"] = cfg.get("baidu_ak") or os.environ.get("BAIDU_AK", "")
    return cfg


def search(source, keyword, city, city_code, headed, out_dir, cfg, profile_dir, channel):
    if source == "amap":
        if cfg["amap_key"]:
            return amap_official.search_poi(cfg["amap_key"], keyword, city=city)
        print("[提示] 未配置 amap_key（config.json 或环境变量 AMAP_KEY），改用浏览器搜索")
        return browser_fetch.search_amap(
            keyword,
            city_code or city,
            out_dir,
            headed=headed,
            profile_dir=profile_dir,
            channel=channel,
        )
    if cfg["baidu_ak"]:
        return baidu_official.search_place(cfg["baidu_ak"], keyword, region=city)
    print("[提示] 未配置 baidu_ak（config.json 或环境变量 BAIDU_AK），改用浏览器搜索")
    return browser_fetch.search_baidu(
        keyword,
        city_code,
        out_dir,
        headed=headed,
        profile_dir=profile_dir,
        channel=channel,
    )


def print_candidates(rows):
    if not rows:
        print("没有找到候选店铺")
        return
    for index, row in enumerate(rows, 1):
        rating = row.get("rating") or "-"
        print(f"{index:>3}. {row.get('name')}  [评分 {rating}]  {row.get('address') or ''}")
        print(
            f"     id={row.get('id') or '-'}  tel={row.get('tel') or '-'}  "
            f"tag={row.get('tag') or '-'}"
        )


def pick(rows, prefer=None):
    print_candidates(rows)
    if not rows:
        return None
    interactive = sys.stdin is not None and sys.stdin.isatty()
    if interactive:
        try:
            raw = input("\n选择要抓评论的店铺序号（直接回车取消）: ").strip()
        except EOFError:
            raw = ""
        if not raw:
            return None
        if raw.isdigit():
            index = int(raw) - 1
            return rows[index] if 0 <= index < len(rows) else None
        return None
    if prefer:
        tokens = [t for t in re.split(r"[\s（）()·]+", prefer) if t]
        def score(row):
            name = str(row.get("name") or "")
            return sum(1 for token in tokens if token in name)
        rows = sorted(rows, key=score, reverse=True)
    choice = rows[0]
    print(f"\n非交互模式，自动选择: {choice.get('name')} (id={choice.get('id')})")
    return choice


def fetch_reviews(args, cfg, row=None):
    row = row or {}
    keyword = args.keyword or row.get("name")
    poi_id = args.poi_id or row.get("id")
    uid = args.uid or row.get("id")
    store = keyword or poi_id or uid or "store"
    out_dir = ensure_dir(OUT_ROOT / f"{now_stamp()}-{safe_name(store)}")
    if args.source == "amap":
        browser_fetch.fetch_amap(
            poi_id,
            out_dir,
            headed=args.headed,
            max_scroll=args.pages,
            profile_dir=args.profile_dir,
            channel=args.browser_channel,
        )
    else:
        browser_fetch.fetch_baidu(
            uid,
            out_dir,
            headed=args.headed,
            max_pages=args.pages,
            name=keyword,
            xy=(row.get("x"), row.get("y")),
            profile_dir=args.profile_dir,
            channel=args.browser_channel,
        )


def cmd_search(args, cfg):
    out_dir = ensure_dir(OUT_ROOT / f"{now_stamp()}-search-{safe_name(args.keyword)}")
    rows = search(
        args.source,
        args.keyword,
        args.city,
        args.city_code,
        args.headed,
        out_dir,
        cfg,
        args.profile_dir,
        args.browser_channel,
    )
    print_candidates(rows)
    save_json(out_dir / "search.json", rows)
    print(f"\n已保存 {len(rows)} 条候选到 {out_dir / 'search.json'}")


def cmd_reviews(args, cfg):
    if args.poi_id or args.uid:
        fetch_reviews(args, cfg)
        return
    if not args.keyword:
        print("请提供 --keyword（先搜索）或 --poi-id / --uid（直接抓取）")
        return
    out_dir = ensure_dir(OUT_ROOT / f"{now_stamp()}-search-{safe_name(args.keyword)}")
    rows = search(
        args.source,
        args.keyword,
        args.city,
        args.city_code,
        args.headed,
        out_dir,
        cfg,
        args.profile_dir,
        args.browser_channel,
    )
    save_json(out_dir / "search.json", rows)
    row = pick(rows, prefer=args.keyword)
    if row:
        args.poi_id = row.get("id")
        args.uid = row.get("id")
        fetch_reviews(args, cfg, row=row)


def build_parser():
    parser = argparse.ArgumentParser(
        description="抓取高德/百度地图店铺评论到本地（暂不做筛选）"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(sub_parser, keyword_required=False):
        sub_parser.add_argument("--source", choices=["amap", "baidu"], required=True)
        sub_parser.add_argument("--keyword", required=keyword_required, help="店名")
        sub_parser.add_argument("--city", help="城市名（官方 API 用，如 杭州）")
        sub_parser.add_argument(
            "--city-code",
            help="浏览器搜索用城市代码：高德 adcode（杭州 330100）/ 百度城市代码（杭州 179）",
        )
        sub_parser.add_argument(
            "--headed", action="store_true", help="显示浏览器窗口，便于手动过验证码"
        )
        sub_parser.add_argument("--pages", type=int, default=5, help="最多滚动/翻页次数（默认 5）")
        sub_parser.add_argument(
            "--profile-dir",
            default=str(ROOT / "browser-profile"),
            help="浏览器用户目录，保存登录/验证状态（默认 browser-profile/）",
        )
        sub_parser.add_argument(
            "--browser-channel",
            default="auto",
            choices=["auto", "msedge", "chrome", "chromium"],
            help="浏览器内核，auto 优先使用系统 Edge（默认 auto）",
        )

    sp = sub.add_parser("search", help="搜索店铺，列出候选")
    add_common(sp, keyword_required=True)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("reviews", help="抓评论：可给 --poi-id/--uid，也可给店名先搜索")
    add_common(sp)
    sp.add_argument("--poi-id", help="高德 POI ID")
    sp.add_argument("--uid", help="百度 POI uid")
    sp.set_defaults(func=cmd_reviews)

    sp = sub.add_parser("run", help="同 reviews，给店名一条龙")
    add_common(sp)
    sp.add_argument("--poi-id", help="高德 POI ID")
    sp.add_argument("--uid", help="百度 POI uid")
    sp.set_defaults(func=cmd_reviews)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args, load_config())


if __name__ == "__main__":
    main()
