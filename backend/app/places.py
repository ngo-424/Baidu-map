"""Bounded POI retrieval, explicit completeness, and deterministic UID cleaning."""
import asyncio
import math
import time

import httpx

from .contracts import Facility

QUERIES = {"market": "菜市场", "supermarket": "超市", "pharmacy": "药店", "hospital_pharmacy": "医院药房", "school": "小学"}


def classify(name, tag=""):
    text = name + " " + tag
    if any(word in text for word in ("培训", "补习", "辅导", "幼儿园", "制药", "药业", "医药公司", "兽药")):
        return None
    for category, words in [
        ("hospital_pharmacy", ("医院药房", "门诊药房", "住院药房")),
        ("pharmacy", ("药店", "药房", "大药房")),
        ("school", ("小学", "完全小学")),
        ("market", ("菜市场", "菜场", "农贸市场", "农副产品市场")),
        ("supermarket", ("超市",)),
    ]:
        if any(word in text for word in words):
            return category
    return None


class PlacesClient:
    def __init__(self, client, ak, gate, token):
        self.client, self.ak, self.gate, self.token = client, ak, gate, token
        self.requests = 0
        self.records = []

    async def page(self, origin, query, radius, page, deadline):
        for attempt in range(2):
            if self.token.cancelled or not await self.gate.wait(deadline):
                return None, "cancelled" if self.token.cancelled else "deadline"
            start = time.monotonic()
            self.requests += 1
            status = code = None
            try:
                r = await self.client.get("https://api.map.baidu.com/place/v3/around", params={
                    "ak": self.ak, "query": query, "location": f"{origin[1]:.6f},{origin[0]:.6f}",
                    "coord_type": 3, "radius": math.ceil(radius), "radius_limit": "true",
                    "scope": 2, "page_size": 20, "page_num": page, "output": "json",
                }, timeout=max(.01, min(8, deadline - time.monotonic())))
                status = r.status_code
                payload = r.json() if status == 200 else None
                code = payload.get("status") if isinstance(payload, dict) else None
                retry = status == 429 or status >= 500 or code in (1, 401, 402)
                reason = "rate_limit" if status == 429 or code in (401, 402) else "upstream_error"
                if status == 200 and type(code) is int and code == 0:
                    if isinstance(payload.get("results"), list):
                        self.records.append({"query": query, "page": page, "http_status": status, "baidu_status": code, "elapsed_ms": round((time.monotonic()-start)*1000), "count": len(payload["results"]), "reason": None})
                        return payload, None
                    retry, reason = False, "invalid_response"
            except httpx.RequestError:
                retry, reason = True, "network_error"
            except (ValueError, TypeError):
                retry, reason = False, "invalid_response"
            self.records.append({"query": query, "page": page, "http_status": status, "baidu_status": code, "elapsed_ms": round((time.monotonic()-start)*1000), "reason": reason})
            if not retry or attempt:
                return None, reason
            await asyncio.sleep(min(.5, max(0, deadline-time.monotonic())))

    async def search(self, origin, radius, deadline, max_pages=2):
        by_uid, metadata = {}, []
        conflicts = set()
        for requested, query in QUERIES.items():
            meta = {"category": requested, "query": query, "status": "complete", "pages": 0,
                    "returned": 0, "excluded": 0, "invalid": 0, "total": None, "reason": None}
            for page in range(max_pages):
                payload, reason = await self.page(origin, query, radius, page, deadline)
                if payload is None:
                    meta.update(status="failed", reason=reason)
                    break
                meta["pages"] += 1
                total = payload.get("total")
                meta["total"] = total if type(total) is int and total >= 0 else None
                rows = payload["results"]
                meta["returned"] += len(rows)
                for row in rows:
                    if not isinstance(row, dict):
                        meta["invalid"] += 1
                        continue
                    name, uid = row.get("name"), row.get("uid")
                    location = row.get("location")
                    if not isinstance(name, str) or not name or not isinstance(uid, str) or not uid or not isinstance(location, dict):
                        meta["invalid"] += 1
                        continue
                    tag = (row.get("detail_info") or {}).get("classified_poi_tag", "") if isinstance(row.get("detail_info", {}), dict) else ""
                    category = classify(name, tag if isinstance(tag, str) else "")
                    if category is None:
                        meta["excluded"] += 1
                        continue
                    if category != requested:
                        meta["excluded"] += 1
                    try:
                        item = Facility(id=uid, name=name, category=category, location=location, in_circle=None)
                    except ValueError:
                        meta["invalid"] += 1
                        continue
                    previous = by_uid.get(uid)
                    if previous and (previous.location != item.location or previous.category != item.category):
                        conflicts.add(uid)
                        meta["invalid"] += 1
                    else:
                        by_uid[uid] = item
                # A full page without a reliable total may still have another page.
                exhausted = (meta["returned"] >= meta["total"] and meta["total"] < 150) if meta["total"] is not None else len(rows) < 20
                if exhausted:
                    break
                if page == max_pages-1:
                    meta.update(status="truncated", reason="page_limit")
            if (meta["invalid"] or meta["excluded"]) and meta["status"] == "complete":
                meta.update(status="partial", reason="invalid_excluded_or_conflicting_records")
            metadata.append(meta)
        for uid in conflicts:
            by_uid.pop(uid, None)
        if conflicts:
            for meta in metadata:
                if meta["status"] == "complete":
                    meta.update(status="partial", reason="uid_conflict")
        return sorted(by_uid.values(), key=lambda p: p.id), metadata
