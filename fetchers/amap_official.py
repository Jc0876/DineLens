"""高德开放平台 Web 服务 API（官方）。

官方 API 只提供 POI 搜索/详情（名称、地址、评分、人均、电话、标签等），
不返回评论文本。评论文本请用 browser_fetch.py 的浏览器方案。

申请 key: https://console.amap.com/dev/key/app （类型选 "Web服务"）
"""
import json
import urllib.parse
import urllib.request

from .common import UA

BASE = "https://restapi.amap.com"


def _get(path, params, timeout=15):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if str(data.get("status")) != "1":
        raise RuntimeError(
            f"高德 API 错误: {data.get('info')} (infocode={data.get('infocode')})"
        )
    return data


def search_poi(key, keyword, city=None, page_size=10, page_num=1):
    params = {
        "key": key,
        "keywords": keyword,
        "page_size": page_size,
        "page_num": page_num,
        "extensions": "all",
    }
    if city:
        params["city"] = city
        params["citylimit"] = "true"
    return [_normalize(p) for p in _get("/v3/place/text", params).get("pois", [])]


def poi_detail(key, poi_id):
    data = _get("/v3/place/detail", {"key": key, "id": poi_id, "extensions": "all"})
    pois = data.get("pois", [])
    return _normalize(pois[0]) if pois else None


def _normalize(poi):
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
        "type": poi.get("type"),
        "location": poi.get("location"),
        "tel": tel if isinstance(tel, str) else ";".join(tel or []),
        "rating": biz.get("rating"),
        "cost": biz.get("cost"),
        "tag": tag if isinstance(tag, str) else ";".join(tag or []),
        "raw": poi,
    }
