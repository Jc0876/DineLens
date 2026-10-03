"""用真实浏览器抓取高德 / 百度地图的店铺评论文本。

为什么需要浏览器：
- 高德、百度官方 Web API 不返回评论文本（只有评分、人均、评论数等）；
- 网页版未公开接口有风控，实测从本机网络访问会要求登录（高德）/ 滑块验证（百度），
  纯 requests 或 headless 浏览器都会被拦。

做法：
- Playwright 驱动真实浏览器，默认使用系统自带 Edge（也可选 Chrome/Chromium）；
- 使用持久化 profile（默认 browser-profile/），登录和验证状态会保存，下次复用；
- 遇到登录/验证码时加 --headed，在弹出的浏览器窗口里手动过一次，程序会自动继续；
- 页面自己发出的 JSON 响应会被原样拦截，保存到 out/<store>/raw/；
- 评论提取采用"尽力而为"的通用规则（找到含正文/评分字段的对象），不做任何筛选。

已知限制：
- 高德/百度接口会变动，若 reviews 为空但 raw/ 里有数据，说明提取规则需要调整；
- 百度 POI 详情页 URL 需要 x,y 坐标（BD09MC），程序会先请求 qt=inf 获取；
- 同一 IP 高频抓取仍可能被临时封禁。
"""
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

from .common import UA, append_jsonl, ensure_dir, save_csv, save_json

REVIEW_TEXT_KEYS = (
    "content",
    "comment",
    "reviewText",
    "review",
    "excerpt",
    "commentText",
    "contentText",
    "summary",
)
REVIEW_COLUMNS = [
    "source",
    "store",
    "poi_id",
    "author",
    "rating",
    "date",
    "content",
    "url",
    "raw",
]

AMAP_HOME = "https://www.amap.com/"
AMAP_DETAIL_URL = "https://www.amap.com/detail/{poi_id}"
AMAP_SEARCH_URL = "https://www.amap.com/search?query={kw}&city={city}"
AMAP_DIRECT_URLS = ["https://www.amap.com/detail/get/detail?id={poi_id}"]
# 高德网页版评论模块已被官方注释掉，但接口仍可用：
# 前端源码 detail.comment.js 里 detail.service.reviewList + poiid/pagenum/pagesize/select_mode
# select_mode: 1=带图 2=差评 4=全部 5=最新
AMAP_REVIEW_URL = (
    "https://www.amap.com/detail/get/reviewList"
    "?poiid={poi_id}&pagesize=20&pagenum={page}&select_mode=4"
)

BAIDU_HOME = "https://map.baidu.com/"
BAIDU_SEARCH_URL = (
    "https://map.baidu.com/?qt=s&wd={kw}&c={city}&rn=10&ie=utf-8&oue=1&res=api&newmap=1"
)
BAIDU_INFO_URL = "https://map.baidu.com/?qt=inf&uid={uid}&ie=utf-8&oue=1&res=api&newmap=1"
BAIDU_UGC_URLS = [
    "https://map.baidu.com/?qt=ugc&uid={uid}&pageno={page}&pn=10&ie=utf-8&oue=1&res=api&newmap=1",
    "https://map.baidu.com/?qt=poi_ugc&uid={uid}&pageno={page}&pn=10&ie=utf-8&oue=1&res=api&newmap=1",
]
BAIDU_POI_URL = "https://map.baidu.com/poi/{name}/@{x},{y},17z?uid={uid}"

RISK_MARKERS = (
    "punish-component",
    "need_recaptcha",
    "请按住滑块",
    "滑动下方滑块",
    "滑块验证",
    "安全验证",
    "短信登录",
)


SLIDER_MARKERS = ("请按住滑块", "滑动下方滑块", "滑块验证", "安全验证", "拖动到最右边")
SLIDER_SELECTORS = (
    "#nc_1_wrapper",
    "#nc_1_n1z",
    ".nc-container",
    ".nc_scale",
    "iframe[src*='punish']",
    "iframe[src*='nocaptcha']",
)


def _has_slider(page):
    for selector in SLIDER_SELECTORS:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible(timeout=200):
                return True
        except Exception:
            continue
    for frame in page.frames:
        try:
            element = frame.frame_element()
            if not element.is_visible():
                continue
            text = frame.evaluate("document.body ? document.body.innerText : ''") or ""
        except Exception:
            continue
        if any(marker in text for marker in SLIDER_MARKERS):
            return True
    text = _page_text(page)
    return any(marker in text for marker in SLIDER_MARKERS)


def _wait_risk(page, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _has_slider(page):
            return True
        page.wait_for_timeout(1000)
    return False


def _pause(msg, page=None, timeout=600):
    print("\n" + "=" * 64)
    print(msg)
    print("=" * 64)
    try:
        if sys.stdin and sys.stdin.isatty():
            input("完成后按回车继续...")
            return True
    except EOFError:
        pass
    if page is None:
        return False
    print(f"未检测到交互终端，自动等待验证完成（最多 {timeout} 秒）...")
    deadline = time.time() + timeout
    last_report = 0
    while time.time() < deadline:
        time.sleep(3)
        if not _has_slider(page):
            print("验证已通过，继续。")
            return True
        remaining = int(deadline - time.time())
        if time.time() - last_report >= 30:
            last_report = time.time()
            print(f"仍在等待验证... 剩余 {remaining}s")
    print("等待超时，继续尝试（如果仍被拦截，可重跑或换网络）。")
    return False


def _risk_help(headed, out_dir, page=None):
    msg = (
        "平台要求登录/验证码。\n"
        f"请在浏览器里完成验证（登录或拖动滑块），状态会保存在 {out_dir}。\n"
        "完成后程序会自动继续。"
    )
    if not headed:
        raise RuntimeError(
            "平台要求登录/验证码，headless 模式无法完成。\n"
            "请加 --headed 参数重跑：浏览器会打开，手动过一次验证即可，\n"
            "验证状态保存在 --profile-dir（默认 browser-profile/），下次可复用。"
        )
    _pause(msg, page=page)


class BrowserSession:
    def __init__(self, headed=False, profile_dir=None, channel="auto"):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SystemExit(
                "未安装 playwright，请先执行：\n"
                "  pip install -r requirements.txt\n"
                "  python -m playwright install chromium"
            ) from exc
        self._pw = sync_playwright().start()
        if channel in (None, "", "auto"):
            candidates = ["msedge", "chrome", None]
        elif channel == "chromium":
            candidates = [None]
        else:
            candidates = [channel]

        self.browser = None
        self.context = None
        last_error = None
        launch_args = ["--disable-blink-features=AutomationControlled"]
        for candidate in candidates:
            try:
                if profile_dir:
                    self.context = self._pw.chromium.launch_persistent_context(
                        str(profile_dir),
                        headless=not headed,
                        channel=candidate,
                        locale="zh-CN",
                        user_agent=UA,
                        viewport={"width": 1366, "height": 900},
                        args=launch_args,
                        ignore_default_args=["--enable-automation"],
                    )
                else:
                    self.browser = self._pw.chromium.launch(
                        headless=not headed,
                        channel=candidate,
                        args=launch_args,
                        ignore_default_args=["--enable-automation"],
                    )
                    self.context = self.browser.new_context(
                        locale="zh-CN",
                        user_agent=UA,
                        viewport={"width": 1366, "height": 900},
                    )
                self.context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                break
            except Exception as exc:
                last_error = exc
                self.context = None
                self.browser = None
        if self.context is None:
            self._pw.stop()
            raise SystemExit(
                "浏览器启动失败，请确认已安装 Edge/Chrome 或执行 "
                "`python -m playwright install chromium`。\n"
                f"最后错误: {last_error}"
            )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.context.close()
            if self.browser:
                self.browser.close()
        finally:
            self._pw.stop()
        return False


def _page_text(page):
    try:
        return page.evaluate("document.body ? document.body.innerText : ''")
    except Exception:
        return ""


def _detect_risk(page):
    if _has_slider(page):
        return True
    try:
        html = page.content()
    except Exception:
        return False
    return any(marker in html for marker in RISK_MARKERS)


def _needs_recaptcha(data):
    if not isinstance(data, dict):
        return False
    if data.get("need_recaptcha"):
        return True
    result = data.get("result")
    if isinstance(result, dict):
        anti = result.get("anti_session")
        if isinstance(anti, dict) and anti.get("need_recaptcha"):
            return True
    return False


def _goto_json(page, url, headed, out_dir, home_url=None, retries=2):
    last_snippet = ""
    for attempt in range(retries + 1):
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(800)
        text = _page_text(page).strip()
        data = None
        try:
            data = json.loads(text)
        except Exception:
            last_snippet = text[:200]
        if isinstance(data, dict):
            if _needs_recaptcha(data):
                if not headed:
                    _risk_help(headed=False, out_dir=out_dir, page=page)
                if home_url:
                    try:
                        page.goto(home_url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
                _risk_help(headed=True, out_dir=out_dir, page=page)
                continue
            return data
        if _detect_risk(page):
            if not headed:
                _risk_help(headed=False, out_dir=out_dir, page=page)
            _risk_help(headed=True, out_dir=out_dir, page=page)
            continue
        if attempt < retries:
            page.wait_for_timeout(2500)
    raise RuntimeError(f"无法获取 JSON（可能被风控），响应片段: {last_snippet!r}")


def _attach_capture(page, url_pattern):
    captured = []

    def on_response(resp):
        try:
            url = resp.url
            if not re.search(url_pattern, url, re.I):
                return
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" not in ctype.lower() and not re.search(
                r"(json|api|comment|review|ugc|detail)", url, re.I
            ):
                return
            body = resp.json()
        except Exception:
            return
        captured.append({"url": url, "body": body})

    page.on("response", on_response)
    return captured, on_response


def _looks_like_review(obj):
    if not isinstance(obj, dict):
        return False
    for key in REVIEW_TEXT_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and len(value.strip()) >= 4:
            return True
    return False


def _walk_reviews(obj, out):
    if isinstance(obj, dict):
        if _looks_like_review(obj):
            out.append(obj)
        for value in obj.values():
            _walk_reviews(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _walk_reviews(value, out)


def extract_reviews(obj):
    found = []
    _walk_reviews(obj, found)
    seen, rows = set(), []
    for item in found:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        rows.append(item)
    return rows


def _first_str(obj, names):
    for name in names:
        value = obj.get(name)
        if value in (None, ""):
            continue
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        return json.dumps(value, ensure_ascii=False)
    return ""


def _normalize_review(obj, source, store, poi_id, url):
    return {
        "source": source,
        "store": store,
        "poi_id": poi_id,
        "author": _first_str(
            obj,
            (
                "author",
                "userName",
                "username",
                "nickName",
                "nickname",
                "user_name",
                "user",
                "nick",
            ),
        ),
        "rating": _first_str(
            obj,
            (
                "rating",
                "score",
                "star",
                "stars",
                "overall_rating",
                "avgScore",
                "review_rating",
            ),
        ),
        "date": _first_str(
            obj,
            (
                "date",
                "time",
                "createTime",
                "create_time",
                "publishTime",
                "reviewTime",
                "commentTime",
                "timestamp",
            ),
        ),
        "content": _first_str(obj, REVIEW_TEXT_KEYS),
        "url": url,
        "raw": json.dumps(obj, ensure_ascii=False),
    }


def _collect(captured, source, store, poi_id):
    rows, seen_raw, seen_text = [], set(), set()
    for item in captured:
        for review in extract_reviews(item.get("body")):
            raw_key = json.dumps(review, ensure_ascii=False, sort_keys=True)
            if raw_key in seen_raw:
                continue
            seen_raw.add(raw_key)
            row = _normalize_review(review, source, store, poi_id, item.get("url", ""))
            text = (row.get("content") or "").strip()
            if text:
                text_key = (text, row.get("author"), row.get("date"))
                if text_key in seen_text:
                    continue
                seen_text.add(text_key)
            rows.append(row)
    return rows


def _write_outputs(out_dir, rows):
    out_dir = Path(out_dir)
    save_json(out_dir / "reviews.json", rows)
    append_jsonl(out_dir / "reviews.jsonl", rows)
    save_csv(out_dir / "reviews.csv", rows, REVIEW_COLUMNS)
    print(f"\n共提取到 {len(rows)} 条疑似评论（未做任何筛选）")
    print(f"  评论: {out_dir / 'reviews.json'}")
    print(f"  原始响应: {out_dir / 'raw'}")
    if not rows:
        print(
            "  提示: 没有提取到评论。先看 raw/ 里有没有平台返回的评论数据，\n"
            "        如果 raw/ 也是空的，通常是登录/验证码没通过，请加 --headed 重跑。"
        )


def _extract_poi_list(captured):
    found, seen = [], set()
    for item in captured:
        stack = [item.get("body")]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                pois = obj.get("poi_list") or obj.get("pois")
                if isinstance(pois, list):
                    for poi in pois:
                        if not (isinstance(poi, dict) and poi.get("name")):
                            continue
                        key = poi.get("id") or poi.get("name")
                        if key not in seen:
                            seen.add(key)
                            found.append(poi)
                stack.extend(obj.values())
            elif isinstance(obj, list):
                stack.extend(obj)
    return found


def _amap_poi_row(poi):
    biz = poi.get("biz_ext") or {}
    if isinstance(biz, list):
        biz = biz[0] if biz else {}
    tel = poi.get("tel")
    tag = poi.get("tag")
    return {
        "source": "amap",
        "id": poi.get("id"),
        "name": poi.get("name"),
        "address": poi.get("address"),
        "city": poi.get("cityname"),
        "district": poi.get("adname"),
        "location": poi.get("location"),
        "tel": tel if isinstance(tel, str) else ";".join(tel or []),
        "rating": biz.get("rating") or poi.get("rating"),
        "cost": biz.get("cost") or poi.get("cost"),
        "tag": tag if isinstance(tag, str) else ";".join(tag or []),
    }


def _baidu_flatten(data):
    found, seen = [], set()

    def walk(obj):
        if isinstance(obj, dict):
            uid = obj.get("uid")
            if uid and obj.get("name"):
                if uid not in seen:
                    seen.add(uid)
                    found.append(obj)
            else:
                for value in obj.values():
                    walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    content = data.get("content") if isinstance(data, dict) else data
    walk(content if content is not None else data)
    return found


def _baidu_poi_row(poi):
    return {
        "source": "baidu",
        "id": poi.get("uid"),
        "name": poi.get("name"),
        "address": poi.get("addr") or poi.get("address"),
        "city": poi.get("cityname") or poi.get("city"),
        "district": poi.get("areaname") or poi.get("area"),
        "location": f"{poi.get('x')},{poi.get('y')}",
        "x": poi.get("x"),
        "y": poi.get("y"),
        "tel": poi.get("tel") or poi.get("phone"),
        "rating": poi.get("overall_rating") or poi.get("score"),
        "comment_num": poi.get("comment_num") or poi.get("commentNum"),
        "cost": poi.get("price"),
        "tag": poi.get("std_tag") or poi.get("tag"),
    }


def search_amap(keyword, city_code, out_dir, headed=False, profile_dir=None, channel="auto"):
    out_dir = ensure_dir(Path(out_dir))
    captured = []
    with BrowserSession(headed=headed, profile_dir=profile_dir, channel=channel) as session:
        page = session.page
        search_url = AMAP_SEARCH_URL.format(kw=quote(keyword), city=city_code or "")
        cap, handler = _attach_capture(page, r"amap\.com")
        page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        if _wait_risk(page, 15) or _detect_risk(page):
            _risk_help(headed, out_dir, page=page)
            page.goto(AMAP_HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
        for _ in range(3):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1200)
        captured.extend(cap)
        page.remove_listener("response", handler)
    save_json(out_dir / "raw" / "amap_search_responses.json", captured)
    rows = [_amap_poi_row(poi) for poi in _extract_poi_list(captured)]
    print(f"浏览器搜索到 {len(rows)} 个候选店铺")
    if not rows:
        print("  没抓到候选。多为登录/滑块未通过；请加 --headed 重跑并手动完成验证。")
    return rows


def fetch_amap(poi_id, out_dir, headed=False, max_scroll=8, profile_dir=None, channel="auto"):
    if not poi_id:
        raise SystemExit("缺少 --poi-id")
    out_dir = ensure_dir(Path(out_dir))
    captured = []
    with BrowserSession(headed=headed, profile_dir=profile_dir, channel=channel) as session:
        page = session.page
        detail_url = AMAP_DETAIL_URL.format(poi_id=poi_id)
        cap, handler = _attach_capture(page, r"amap\.com")
        page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        if _wait_risk(page, 15) or _detect_risk(page):
            _risk_help(headed, out_dir, page=page)
            page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
        page.remove_listener("response", handler)
        captured.extend(cap)

        fetched_pages = 0
        for pagenum in range(1, max(1, max_scroll) + 1):
            url = AMAP_REVIEW_URL.format(poi_id=poi_id, page=pagenum)
            try:
                resp = session.context.request.get(
                    url, headers={"Referer": detail_url}, timeout=20000
                )
                data = resp.json()
            except Exception as exc:
                print(f"[warn] 第 {pagenum} 页评论请求失败: {exc}")
                break
            captured.append({"url": url, "body": data})
            if str(data.get("status")) != "1":
                print(
                    f"[warn] 评论接口返回异常（第 {pagenum} 页）: "
                    f"{json.dumps(data, ensure_ascii=False)[:150]}"
                )
                break
            page_data = data.get("data") or {}
            fetched_pages += 1
            page_total = int(page_data.get("page_total") or 1)
            count = len(page_data.get("review_list") or [])
            print(f"  第 {pagenum}/{page_total} 页，{count} 条评论")
            if pagenum >= page_total:
                break
        print(f"评论接口共抓取 {fetched_pages} 页")

        if not any("reviewList" in item.get("url", "") for item in captured):
            for template in AMAP_DIRECT_URLS:
                url = template.format(poi_id=poi_id)
                try:
                    captured.append(
                        {"url": url, "body": _goto_json(page, url, headed, out_dir, home_url=AMAP_HOME)}
                    )
                except Exception as exc:
                    print(f"[warn] 直接请求 {url} 失败: {exc}")
    save_json(out_dir / "raw" / "captured.json", captured)
    rows = _collect(captured, "amap", poi_id, poi_id)
    _write_outputs(out_dir, rows)
    return rows


def search_baidu(keyword, city_code, out_dir, headed=False, profile_dir=None, channel="auto"):
    out_dir = ensure_dir(Path(out_dir))
    with BrowserSession(headed=headed, profile_dir=profile_dir, channel=channel) as session:
        page = session.page
        page.goto(BAIDU_HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        data = _goto_json(
            page,
            BAIDU_SEARCH_URL.format(kw=quote(keyword), city=city_code or "0"),
            headed,
            out_dir,
            home_url=BAIDU_HOME,
        )
    save_json(out_dir / "raw" / "baidu_search.json", data)
    rows = [_baidu_poi_row(poi) for poi in _baidu_flatten(data)]
    print(f"浏览器搜索到 {len(rows)} 个候选店铺")
    if not rows:
        print("  没抓到候选。多为验证码未通过；请加 --headed 重跑并手动完成验证。")
    return rows


def fetch_baidu(
    uid,
    out_dir,
    headed=False,
    max_pages=5,
    name=None,
    xy=(None, None),
    profile_dir=None,
    channel="auto",
):
    if not uid:
        raise SystemExit("缺少 --uid")
    out_dir = ensure_dir(Path(out_dir))
    captured = []
    x, y = xy
    with BrowserSession(headed=headed, profile_dir=profile_dir, channel=channel) as session:
        page = session.page
        page.goto(BAIDU_HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)

        if not (x and y):
            info_url = BAIDU_INFO_URL.format(uid=uid)
            try:
                info = _goto_json(page, info_url, headed, out_dir, home_url=BAIDU_HOME)
                captured.append({"url": info_url, "body": info})
                pois = _baidu_flatten(info)
                if pois:
                    x, y = pois[0].get("x"), pois[0].get("y")
                    name = name or pois[0].get("name")
                    save_json(out_dir / "raw" / "baidu_info.json", pois[0])
            except Exception as exc:
                print(f"[warn] 获取百度 POI 坐标失败: {exc}")

        cap, handler = _attach_capture(page, r"map\.baidu\.com|map\.bdimg\.com")
        if x and y:
            url = BAIDU_POI_URL.format(name=quote(name or ""), x=x, y=y, uid=uid)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)
                if _wait_risk(page, 15) or _detect_risk(page):
                    _risk_help(headed, out_dir, page=page)
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4000)
                for _ in range(max(1, max_pages)):
                    page.mouse.wheel(0, 2000)
                    page.wait_for_timeout(1200)
                    for text in ("查看全部评论", "全部评论", "评论"):
                        try:
                            locator = page.get_by_text(text, exact=False).first
                            if locator.is_visible(timeout=300):
                                locator.click(timeout=1500)
                                page.wait_for_timeout(1500)
                                break
                        except Exception:
                            pass
                page.wait_for_timeout(1500)
            except Exception as exc:
                print(f"[warn] 打开百度 POI 页面失败: {exc}")
        page.remove_listener("response", handler)
        captured.extend(cap)

        for template in BAIDU_UGC_URLS:
            for pageno in range(max(1, max_pages)):
                url = template.format(uid=uid, page=pageno)
                try:
                    data = _goto_json(page, url, headed, out_dir, home_url=BAIDU_HOME)
                except Exception as exc:
                    print(f"[warn] {url} -> {exc}")
                    break
                captured.append({"url": url, "body": data})
                if not extract_reviews(data):
                    break
    save_json(out_dir / "raw" / "captured.json", captured)
    rows = _collect(captured, "baidu", name or uid, uid)
    _write_outputs(out_dir, rows)
    return rows
