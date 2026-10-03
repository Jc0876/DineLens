"""百度地图开放平台 Web 服务 API（官方）。

官方 Place API 只提供 POI 搜索/详情（名称、地址、评分、评论数、人均、标签等），
不返回评论文本。评论文本请用 browser_fetch.py 的浏览器方案。

申请 AK: https://lbsyun.baidu.com/apiconsole/key （应用类型选 "浏览器端" 或
"服务端" 均可调用 Place API；服务端需开通 "地点检索" 服务）
"""
import json
import urllib.parse
import urllib.request

from .common import UA

BASE = "https://api.map.baidu.com"


def _get(path, params, timeout=15):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("status") not in (0, 2):
        raise RuntimeError(
            f"百度 API 错误: {data.get('message')} (status={data.get('status')})"
        )
    return data


def search_place(ak, keyword, region=None, page_size=10, page_num=0, scope=2):
    params = {
        "ak": ak,
        "query": keyword,
        "output": "json",
        "page_size": page_size,
        "page_num": page_num,
        "scope": scope,
    }
    if region:
        params["region"] = region
    return [_normalize(p) for p in _get("/place/v2/search", params).get("results", [])]


def place_detail(ak, uid, scope=2):
    data = _get(
        "/place/v2/detail", {"ak": ak, "uid": uid, "output": "json", "scope": scope}
    )
    return _normalize(data.get("result") or {})


def _normalize(poi):
    info = poi.get("detail_info") or {}
    loc = poi.get("location") or {}
    return {
        "source": "baidu",
        "id": poi.get("uid"),
        "name": poi.get("name"),
        "address": poi.get("address"),
        "city": poi.get("city"),
        "district": poi.get("area"),
        "location": ",".join(str(v) for v in (loc.get("lng"), loc.get("lat")) if v is not None),
        "tel": poi.get("telephone"),
        "rating": info.get("overall_rating"),
        "comment_num": info.get("comment_num"),
        "cost": info.get("price"),
        "tag": info.get("tag"),
        "raw": poi,
    }
