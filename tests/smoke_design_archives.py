#!/usr/bin/env python3
"""设计归档接口的端到端冒烟测试（仅用标准库，直连运行中的 HTTP 服务）。

与 tests/test_api_design_archives.py（FastAPI TestClient、临时库）互补：本脚本
针对「Docker 启动下的真实服务」打 HTTP 请求，验证建表、归档、幂等复用与核验
全链路。用法：

    # 1) 启动服务
    docker compose up --build -d
    # 2) 在容器内（或任何能访问服务的机器上）运行
    python3 tests/smoke_design_archives.py
    #    自定义地址：BASE_URL=http://localhost:8000 python3 ...
    #    或：python3 tests/smoke_design_archives.py http://localhost:8000

脚本使用独立的一批测试设计（名称带 smoke-design-archives 前缀），可重复执行：
每次运行都新建设计与归档并自检，不依赖历史数据。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

TWILL = {
    "name": "smoke-design-archives twill",
    "cyclic": True,
    "max_float": 2,
    "threading": [0, 1, 2, 3, 0, 1, 2, 3],
    "tie_up": [
        [1, 1, 0, 0],
        [0, 1, 1, 0],
        [0, 0, 1, 1],
        [1, 0, 0, 1],
    ],
    "treadling": [0, 1, 2, 3, 0, 1, 2, 3],
}

PLAIN = {
    "name": "smoke-design-archives plain",
    "cyclic": False,
    "max_float": 1,
    "threading": [0, 1, 0, 1],
    "tie_up": [[1, 0], [0, 1]],
    "treadling": [0, 1, 0, 1],
}


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def request(method, path, body=None, expect=None):
    url = BASE_URL.rstrip("/") + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8")
        payload = json.loads(raw) if raw else None
    if expect is not None:
        check(status == expect, f"{method} {path} -> {status} (expected {expect})")
    else:
        check(200 <= status < 300, f"{method} {path} -> {status}")
    return status, payload


def main() -> None:
    print(f"smoke-testing design archives against {BASE_URL}")

    status, health = request("GET", "/health")
    check(health == {"status": "ok"}, "service is healthy")

    _, d1 = request("POST", "/designs", TWILL, expect=201)
    _, d2 = request("POST", "/designs", PLAIN, expect=201)
    id1, id2 = d1["id"], d2["id"]
    print(f"  created fixture designs {id1}, {id2}")

    # 首次归档（故意逆序提交）
    status, archive = request(
        "POST", "/design-archives", {"design_ids": [id2, id1]}, expect=201
    )
    check(archive["reused"] is False, "first archive: reused=false")
    check(archive["design_ids"] == [id1, id2], "design_ids normalized to ascending")
    check(archive["item_count"] == 2, "item_count=2")
    check(
        set(archive["manifest"]["designs"]) == {str(id1), str(id2)},
        "manifest keyed by design id",
    )
    check(
        archive["manifest"]["schema"] == "design-archive/v1",
        "manifest schema marker",
    )
    check(
        archive["manifest"]["designs"][str(id1)]["audit"] == d1["audit"],
        "manifest carries stored audit result",
    )
    digest = archive["digest"]
    check(len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
          "digest is 64-char lowercase hex (SHA-256)")
    archive_id = archive["id"]

    # 幂等复用：换顺序再提交
    status, again = request(
        "POST", "/design-archives", {"design_ids": [id1, id2]}, expect=200
    )
    check(again["reused"] is True, "same id set: reused=true")
    check(again["id"] == archive_id, "same archive id reused")
    check(again["digest"] == digest, "same digest reused")
    check(again["created_at"] == archive["created_at"], "created_at preserved")

    # 子集是新归档
    _, sub = request(
        "POST", "/design-archives", {"design_ids": [id1]}, expect=201
    )
    check(sub["digest"] != digest and sub["id"] != archive_id,
          "different id set -> different archive")

    # 核验：刚建的归档必然一致
    _, verify = request(
        "GET", f"/design-archives/{archive_id}/verify", expect=200
    )
    check(verify["digest_match"] is True, "verify: digest_match=true")
    check(verify["digest"] == digest, "verify echoes stored digest")
    check(verify["recomputed_digest"] == digest, "recomputed digest matches")
    check(verify["missing_design_ids"] == [], "no missing designs")
    check(verify["first_diff_path"] is None, "no difference path")

    # 引用不存在的设计：404
    status, err = request(
        "POST", "/design-archives", {"design_ids": [id1, 999999]}, expect=404
    )
    check(err["missing_design_ids"] == [999999],
          "404 lists missing design ids")

    # 入参校验：重复 ID 与超量 -> 422
    request("POST", "/design-archives", {"design_ids": [id1, id1]}, expect=422)
    request(
        "POST",
        "/design-archives",
        {"design_ids": list(range(1, 52))},
        expect=422,
    )

    # 核验未知归档：404
    request("GET", "/design-archives/999999/verify", expect=404)

    print("all design-archives smoke tests passed")


if __name__ == "__main__":
    main()
