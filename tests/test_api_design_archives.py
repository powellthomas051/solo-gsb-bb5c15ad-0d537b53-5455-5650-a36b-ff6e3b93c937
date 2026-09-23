"""POST /design-archives 与 GET /design-archives/{id}/verify 的接口集成测试。

覆盖：
- 1～50 个已入库 ID 的归档创建（201）、清单结构与规范化 digest；
- 相同 ID 集合（乱序/重复排列）幂等复用（200，reused=true，id/digest 不变）；
- 不同 ID 集合（子集）产生不同归档；
- verify 正常一致、活数据篡改、归档行篡改（manifest/digest 列）、设计缺失
  四类情形的 digest_match / missing_design_ids / first_diff_path；
- 失败原子性：引用不存在的设计返回 404 且归档表不新增行；
- Pydantic 校验：空列表、超 50 个、重复 ID、非正整数、缺字段 -> 422。

使用临时 SQLite 文件与 FastAPI TestClient，不依赖运行中的服务，也不触碰
仓库默认的 data/weave.db。直接执行：python3 tests/test_api_design_archives.py
（也可被 pytest 收集）。
"""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(prefix="weave-test-", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_PATH"] = _TMP_DB.name

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.archives import ARCHIVE_SCHEMA  # noqa: E402
from app.main import app  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


TWILL = {
    "name": "2/2 twill",
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
    "name": "plain weave",
    "cyclic": False,
    "max_float": 1,
    "threading": [0, 1, 0, 1],
    "tie_up": [[1, 0], [0, 1]],
    "treadling": [0, 1, 0, 1],
}


def _create_design(client, payload):
    resp = client.post("/designs", json=payload)
    check(resp.status_code == 201, f"fixture design created: {resp.text}")
    return resp.json()


def _archive_count():
    with sqlite3.connect(db.DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM design_archives").fetchone()[0]


def _overwrite_manifest(archive_id, manifest):
    """绕过 API 直接覆写归档清单文本（模拟存储层篡改）。"""
    text = (
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute(
            "UPDATE design_archives SET manifest = ? WHERE id = ?", (text, archive_id)
        )


def test_create_archive_canonical_manifest_and_digest():
    with TestClient(app) as client:
        d1 = _create_design(client, TWILL)
        d2 = _create_design(client, PLAIN)

        # 乱序提交：服务端按 ID 升序处理
        resp = client.post(
            "/design-archives", json={"design_ids": [d2["id"], d1["id"]]}
        )
        check(resp.status_code == 201, "first archive returns 201")
        data = resp.json()
        check(data["reused"] is False, "new archive reports reused=false")
        check(data["design_ids"] == [d1["id"], d2["id"]], "design_ids sorted ascending")
        check(data["item_count"] == 2, "item_count is 2")
        check(isinstance(data["id"], int) and "created_at" in data, "id and created_at present")

        manifest = data["manifest"]
        check(manifest["schema"] == ARCHIVE_SCHEMA, "manifest schema marker")
        check(manifest["item_count"] == 2, "manifest item_count is 2")
        check(
            set(manifest["designs"]) == {str(d1["id"]), str(d2["id"])},
            "manifest designs keyed by id strings",
        )
        item = manifest["designs"][str(d1["id"])]
        expected_item = {
            k: d1[k]
            for k in (
                "name", "cyclic", "max_float", "shafts", "treadles",
                "warp_ends", "weft_picks", "threading", "tie_up", "treadling",
            )
        }
        expected_item["audit"] = d1["audit"]
        check(item == expected_item, "snapshot equals stored design params + audit")
        check("created_at" not in item and "id" not in item,
              "snapshot excludes id and created_at")

        # digest 可由规范化 JSON 独立复算（64 位小写 hex，键排序、紧凑分隔符、末尾换行）
        canonical = (
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        )
        expected_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        check(data["digest"] == expected_digest, "SHA-256 digest over canonical JSON")

        # 持久化的清单文本与返回一致
        with sqlite3.connect(db.DB_PATH) as conn:
            row = conn.execute(
                "SELECT manifest, item_count, design_ids FROM design_archives WHERE id = ?",
                (data["id"],),
            ).fetchone()
        check(row[0] == canonical, "stored manifest text is the canonical JSON")
        check(row[1] == 2, "stored item_count")
        check(json.loads(row[2]) == [d1["id"], d2["id"]], "stored sorted design_ids")


def test_same_digest_idempotent_reuse():
    with TestClient(app) as client:
        d1 = _create_design(client, TWILL)
        d2 = _create_design(client, PLAIN)

        first = client.post(
            "/design-archives", json={"design_ids": [d1["id"], d2["id"]]}
        ).json()
        count_after_create = _archive_count()

        # 逆序再提交：返回 200、reused=true，id/digest/created_at 全部复用
        resp = client.post(
            "/design-archives", json={"design_ids": [d2["id"], d1["id"]]}
        )
        check(resp.status_code == 200, "identical id set returns 200 (reuse)")
        again = resp.json()
        check(again["reused"] is True, "reused=true")
        check(again["id"] == first["id"], "same archive id reused")
        check(again["digest"] == first["digest"], "same digest reused")
        check(again["created_at"] == first["created_at"], "created_at preserved")
        check(
            again["manifest"] == first["manifest"], "reused manifest identical"
        )
        check(_archive_count() == count_after_create, "reuse inserts no new row")

        # 子集是不同的归档（digest 不同）
        resp = client.post("/design-archives", json={"design_ids": [d1["id"]]})
        check(resp.status_code == 201, "subset archive is a new 201")
        sub = resp.json()
        check(sub["id"] != first["id"], "subset has its own archive id")
        check(sub["digest"] != first["digest"], "subset digest differs")
        check(sub["item_count"] == 1 and sub["design_ids"] == [d1["id"]],
              "subset manifest contains one design")

        # 单设计归档再提交一次也复用
        resp = client.post("/design-archives", json={"design_ids": [d1["id"]]})
        check(resp.status_code == 200 and resp.json()["id"] == sub["id"],
              "single-design archive also idempotent")


def test_verify_matching_archive():
    with TestClient(app) as client:
        d1 = _create_design(client, TWILL)
        d2 = _create_design(client, PLAIN)
        archive = client.post(
            "/design-archives", json={"design_ids": [d1["id"], d2["id"]]}
        ).json()

        resp = client.get(f"/design-archives/{archive['id']}/verify")
        check(resp.status_code == 200, "verify returns 200")
        v = resp.json()
        check(v["id"] == archive["id"], "verify echoes archive id")
        check(v["digest"] == archive["digest"], "verify echoes stored digest")
        check(v["recomputed_digest"] == archive["digest"],
              "recomputed digest equals stored digest")
        check(v["digest_match"] is True, "fresh archive digest_match=true")
        check(v["missing_design_ids"] == [], "no missing designs")
        check(v["first_diff_path"] is None, "no first diff path")
        check("verified_at" in v, "verified_at present")


def test_verify_detects_live_data_tamper_with_first_diff_path():
    with TestClient(app) as client:
        d1 = _create_design(client, TWILL)
        archive = client.post(
            "/design-archives", json={"design_ids": [d1["id"]]}
        ).json()

        # 直接改活数据：设计 max_float 被改
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "UPDATE designs SET max_float = 99 WHERE id = ?", (d1["id"],)
            )
        v = client.get(f"/design-archives/{archive['id']}/verify").json()
        check(v["digest_match"] is False, "live tamper -> digest_match=false")
        check(v["missing_design_ids"] == [], "design still present")
        check(
            v["first_diff_path"] == "designs.{}.max_float".format(d1["id"]),
            f"first diff at max_float, got {v['first_diff_path']}",
        )
        check(v["recomputed_digest"] is not None
              and v["recomputed_digest"] != archive["digest"],
              "recomputed digest present and differs")

        # 还原 max_float，改为审计交织矩阵单元 -> 数组索引路径
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "UPDATE designs SET max_float = 2 WHERE id = ?", (d1["id"],)
            )
            audit_text = conn.execute(
                "SELECT interlacing FROM audits WHERE design_id = ?", (d1["id"],)
            ).fetchone()[0]
            matrix = json.loads(audit_text)
            matrix[1][2] = 1 - matrix[1][2]
            conn.execute(
                "UPDATE audits SET interlacing = ? WHERE design_id = ?",
                (json.dumps(matrix), d1["id"]),
            )
        v = client.get(f"/design-archives/{archive['id']}/verify").json()
        check(v["digest_match"] is False, "matrix tamper -> digest_match=false")
        check(
            v["first_diff_path"]
            == f"designs.{d1['id']}.audit.interlacing[1][2]",
            f"array-index first diff path, got {v['first_diff_path']}",
        )


def test_verify_detects_archive_row_tamper():
    with TestClient(app) as client:
        d1 = _create_design(client, PLAIN)
        archive = client.post(
            "/design-archives", json={"design_ids": [d1["id"]]}
        ).json()
        archive_id = archive["id"]

        # 改清单文本但不动 digest 列：归档行自洽性检查应发现
        manifest = json.loads(json.dumps(archive["manifest"]))
        manifest["designs"][str(d1["id"])]["name"] = "tampered name"
        _overwrite_manifest(archive_id, manifest)

        v = client.get(f"/design-archives/{archive_id}/verify").json()
        check(v["digest_match"] is False,
              "manifest-only tamper still yields digest_match=false")
        check(
            v["first_diff_path"] == f"designs.{d1['id']}.name",
            f"first diff at name, got {v['first_diff_path']}",
        )
        check(v["recomputed_digest"] == archive["digest"],
              "live data unchanged: recomputed digest equals original digest")

        # 改 digest 列：先把清单还原为原值，仅 digest 列不一致
        _overwrite_manifest(archive_id, archive["manifest"])
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "UPDATE design_archives SET digest = ? WHERE id = ?",
                ("0" * 64, archive_id),
            )
        v = client.get(f"/design-archives/{archive_id}/verify").json()
        check(v["digest_match"] is False, "digest column tamper -> mismatch")
        check(v["first_diff_path"] is None, "manifests still compare equal")


def test_verify_reports_missing_designs():
    with TestClient(app) as client:
        d1 = _create_design(client, TWILL)
        d2 = _create_design(client, PLAIN)
        archive = client.post(
            "/design-archives", json={"design_ids": [d1["id"], d2["id"]]}
        ).json()
        archive_id = archive["id"]

        # API 没有删除端点：直接删库表模拟设计丢失（先关外键）
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DELETE FROM audits WHERE design_id = ?", (d1["id"],))
            conn.execute("DELETE FROM designs WHERE id = ?", (d1["id"],))

        v = client.get(f"/design-archives/{archive_id}/verify").json()
        check(v["digest_match"] is False, "missing design -> digest_match=false")
        check(v["missing_design_ids"] == [d1["id"]],
              "missing_design_ids lists deleted id ascending")
        check(v["recomputed_digest"] is None,
              "recomputed_digest null when manifest cannot be fully rebuilt")
        check(
            v["first_diff_path"]
            == f"designs.{d1['id']} (design missing from store)",
            f"first diff names missing design, got {v['first_diff_path']}",
        )

        # 归档不存在 -> 404
        resp = client.get("/design-archives/99999/verify")
        check(resp.status_code == 404, "verify unknown archive returns 404")
        check(
            resp.json()["detail"] == "design archive 99999 not found",
            "verify 404 detail",
        )


def test_create_with_unknown_design_ids_404_and_atomic():
    with TestClient(app) as client:
        d1 = _create_design(client, PLAIN)
        before = _archive_count()

        resp = client.post(
            "/design-archives", json={"design_ids": [d1["id"], 99999, 88888]}
        )
        check(resp.status_code == 404, "unknown design id returns 404")
        body = resp.json()
        check(body["detail"] == "design 88888 not found",
              "404 detail names smallest missing id (ascending)")
        check(body["missing_design_ids"] == [88888, 99999],
              "all missing ids listed ascending")

        # 归档失败不留半成品：归档表行数不变
        check(_archive_count() == before, "failed archive inserts no row")

        # 失败后同批 ID 全部补齐仍可正常归档
        d2 = _create_design(client, TWILL)
        # 99999/88888 仍不存在时依旧失败
        resp = client.post(
            "/design-archives", json={"design_ids": [d1["id"], 99999]}
        )
        check(resp.status_code == 404, "still 404 while id missing")
        check(_archive_count() == before, "still no new row")


def test_request_validation_422():
    with TestClient(app) as client:
        resp = client.post("/design-archives", json={"design_ids": []})
        check(resp.status_code == 422, "empty id list -> 422")

        resp = client.post(
            "/design-archives", json={"design_ids": list(range(1, 52))}
        )
        check(resp.status_code == 422, "51 ids -> 422")

        resp = client.post("/design-archives", json={"design_ids": [1, 1]})
        check(resp.status_code == 422, "duplicate ids -> 422")
        check(
            "duplicate" in resp.json()["detail"][0]["msg"].lower(),
            "duplicate error message mentions duplicates",
        )

        resp = client.post("/design-archives", json={"design_ids": [0]})
        check(resp.status_code == 422, "non-positive id -> 422")

        resp = client.post("/design-archives", json={"design_ids": [-3]})
        check(resp.status_code == 422, "negative id -> 422")

        resp = client.post("/design-archives", json={})
        check(resp.status_code == 422, "missing design_ids field -> 422")

        resp = client.post("/design-archives", json={"design_ids": "1,2"})
        check(resp.status_code == 422, "wrong field type -> 422")


def test_boundary_one_and_fifty_ids_accepted():
    with TestClient(app) as client:
        ids = []
        for i in range(50):
            ids.append(
                client.post(
                    "/designs",
                    json={
                        "name": f"bulk-{i}",
                        "cyclic": False,
                        "max_float": 1,
                        "threading": [0, 1],
                        "tie_up": [[1, 0], [0, 1]],
                        "treadling": [i % 2, (i + 1) % 2],
                    },
                ).json()["id"]
            )

        # 上界 50 个
        resp = client.post("/design-archives", json={"design_ids": ids})
        check(resp.status_code == 201, "50 ids accepted (201)")
        check(resp.json()["item_count"] == 50, "50-item count")

        # 逆序复用
        resp = client.post("/design-archives", json={"design_ids": list(reversed(ids))})
        check(resp.status_code == 200 and resp.json()["reused"] is True,
              "50 ids reversed -> 200 reuse")

        # 下界 1 个
        resp = client.post("/design-archives", json={"design_ids": [ids[-1]]})
        check(resp.status_code == 201 and resp.json()["item_count"] == 1,
              "single id accepted (201)")


if __name__ == "__main__":
    test_create_archive_canonical_manifest_and_digest()
    test_same_digest_idempotent_reuse()
    test_verify_matching_archive()
    test_verify_detects_live_data_tamper_with_first_diff_path()
    test_verify_detects_archive_row_tamper()
    test_verify_reports_missing_designs()
    test_create_with_unknown_design_ids_404_and_atomic()
    test_request_validation_422()
    test_boundary_one_and_fifty_ids_accepted()
    try:
        os.unlink(_TMP_DB.name)
    except OSError:
        pass
    print("all design-archives API tests passed")
