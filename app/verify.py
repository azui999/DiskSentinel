"""内容校验（P2）：逐文件读取内容计算 SHA-256，检测静默损坏。

能力（docs/冷备对功能评估.md 的慢通道）：
- 多线程哈希（4~8 线程并发读不同文件，吃满磁盘队列深度）；
- 断点续跑：中断/暂停的校验下次继续时，已完成文件自动沿用，不重读；
- 增量模式（可选，默认关闭）：连已完成的校验也沿用，size+mtime 未变就不重读；
- 暂停：协作式检查点，随时中断、进度已入库；
- 比对：两侧清单按 key 流式归并，sha256 不同 → "内容不同"（你的照片案例就是它）。

关于"要不要沿用历史哈希"——这是本模块最容易写错的地方：
静默损坏**不改变文件大小和修改时间**（磁盘位翻转发生在底层，文件系统不知道），
所以只要跨已完成的校验沿用旧哈希，就等于跳过了这个文件的校验，
把"没检查"伪装成"检查通过"。默认因此只沿用未完成校验（真正的断点续跑），
需要提速时由用户显式选择增量模式，并在界面标注"本次未逐字节读取"。

注意：结构比对发现的差异（缺文件/大小不同）在内容校验里同样会报告，
两类差异一次看全。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass, field
from typing import Any, Callable

from .pairing import (
    CAT_MTIME,
    CAT_ONLY_A,
    CAT_ONLY_B,
    CAT_SIZE,
    DEFAULT_EXCLUDE,
    Pair,
    PairError,
    PairStore,
)

ProgressFn = Callable[[dict[str, Any]], None]

CAT_CONTENT = "content_diff"
CATEGORY_EXTRA = {CAT_CONTENT: "内容不同"}

_READ_BLOCK = 4 << 20      # 4 MB 读块
_HASH_WORKERS = 6          # 并发哈希线程（磁盘型任务）
_QUEUE_DEPTH = 24          # 未完成任务背压上限


class Paused(Exception):
    """用户中断（暂停/退出）。进度已入库，下次校验自动沿用。"""


class Control:
    """协作式暂停控制。"""

    def __init__(self) -> None:
        self._pause = threading.Event()

    def pause(self) -> None:
        self._pause.set()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def check(self) -> None:
        if self._pause.is_set():
            raise Paused()


@dataclass
class VerifyResult:
    counts: dict[str, int] = field(default_factory=lambda: {
        "same": 0, CAT_ONLY_A: 0, CAT_ONLY_B: 0,
        CAT_SIZE: 0, CAT_MTIME: 0, CAT_CONTENT: 0,
    })
    rows: list[dict[str, Any]] = field(default_factory=list)  # 差异行
    reused_files: int = 0        # 沿用历史哈希、未重读的文件数
    hashed_files: int = 0        # 本次实际读取计算的文件数
    read_bytes: int = 0          # 本次实际读取的字节数
    incremental: bool = False    # True = 增量模式（部分文件未逐字节读取）
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def is_clean(self) -> bool:
        keys = ("only_a", "only_b", "size_diff", "mtime_diff", "content_diff")
        return all(self.counts.get(k, 0) == 0 for k in keys)


# --------------------------------------------------------------------------- #
# 表结构（挂在 PairStore 的 index.db 上，兼容旧库自动升级）
# --------------------------------------------------------------------------- #

_VERIFY_SCHEMA = """
CREATE TABLE IF NOT EXISTS verify_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id       INTEGER NOT NULL REFERENCES pairs(id),
    algorithm     TEXT NOT NULL DEFAULT 'sha256',
    status        TEXT NOT NULL DEFAULT 'running',   -- running/paused/finished
    started       TEXT NOT NULL,
    finished      TEXT NOT NULL DEFAULT '',
    counts_json   TEXT NOT NULL DEFAULT '{}',
    reused_files  INTEGER NOT NULL DEFAULT 0,
    hashed_files  INTEGER NOT NULL DEFAULT 0,
    read_bytes    INTEGER NOT NULL DEFAULT 0,
    incremental   INTEGER NOT NULL DEFAULT 0,
    seconds       REAL NOT NULL DEFAULT 0,
    errors_json   TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS verify_files (
    verify_run_id INTEGER NOT NULL REFERENCES verify_runs(id),
    side          TEXT NOT NULL,                     -- 'a' / 'b'
    key           TEXT NOT NULL,
    display       TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    sha256        TEXT NOT NULL DEFAULT '',          -- 空 = 还没算完
    error         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (verify_run_id, side, key)
) WITHOUT ROWID;
"""


def ensure_schema(store: PairStore) -> None:
    """建表 + 旧库补列（可重复执行）。"""
    with store.connect() as conn:
        conn.executescript(_VERIFY_SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(verify_runs)")}
        if "incremental" not in columns:
            conn.execute(
                "ALTER TABLE verify_runs ADD COLUMN incremental INTEGER NOT NULL DEFAULT 0"
            )
    store.ensure_soft_delete()


# --------------------------------------------------------------------------- #
# 哈希
# --------------------------------------------------------------------------- #

def _hash_file(path: str) -> tuple[str, str]:
    """返回 (sha256, error)。error 非空表示读取失败。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(_READ_BLOCK):
                h.update(chunk)
        return h.hexdigest(), ""
    except OSError as exc:
        return "", exc.strerror or str(exc)


def _walk(root: str) -> tuple[list[tuple[str, str, int, int]], list[str]]:
    """扫描目录文件（只读元数据）。返回 [(key, display, size, mtime_ns)], skipped。"""
    root_path = os.path.abspath(root)
    if not os.path.isdir(root_path):
        raise PairError(f"无法访问：{root_path}")
    entries: list[tuple[str, str, int, int]] = []
    skipped: list[str] = []
    stack = [root_path]
    while stack:
        folder = stack.pop()
        try:
            it = os.scandir(folder)
        except OSError as exc:
            skipped.append(f"{folder}\t{exc.strerror or exc}")
            continue
        with it:
            try:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() in DEFAULT_EXCLUDE:
                                continue
                            stack.append(entry.path)
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        skipped.append(f"{entry.path}\t{exc.strerror or exc}")
                        continue
                    rel = os.path.relpath(entry.path, root_path).replace("\\", "/")
                    entries.append((rel.lower(), rel, stat.st_size, stat.st_mtime_ns))
            except OSError as exc:
                skipped.append(f"{folder}\t{exc.strerror or exc}")
    return entries, skipped


# --------------------------------------------------------------------------- #
# 校验主流程（UI 子线程调用）
# --------------------------------------------------------------------------- #

def verify_pair(
    store: PairStore,
    pair: Pair,
    control: Control,
    progress: ProgressFn | None = None,
    incremental: bool = False,
) -> VerifyResult:
    """两侧扫描 + 哈希 + 盘间内容比对，一步完成。返回 VerifyResult。

    incremental=False（默认，推荐）：两侧每个文件都重新读一遍内容，
        只有"上次没跑完"的校验才沿用已算好的哈希。慢，但这才是真校验。
    incremental=True：与历史校验相比 size+mtime 未变的文件直接沿用旧哈希，
        快很多，但**无法发现静默损坏**（损坏不改大小和时间），仅适合日常快速看一眼。
    """
    ensure_schema(store)
    tell = progress or (lambda _info: None)
    started = time.perf_counter()
    result = VerifyResult(incremental=incremental)

    conn = store.connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        reuse = _load_reusable_hashes(conn, pair.id, incremental)
        cur = conn.execute(
            "INSERT INTO verify_runs(pair_id, started) VALUES(?,?)",
            (pair.id, time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        run_id = int(cur.lastrowid or 0)
        conn.commit()

        stats: dict[str, dict[str, Any]] = {}
        try:
            for side, root in (("a", pair.path_a), ("b", pair.path_b)):
                stats[side] = _verify_side(
                    conn, run_id, side, root, reuse, result, control, tell
                )
        except Paused:
            result.seconds = time.perf_counter() - started
            _finish_run(conn, run_id, result, stats, status="paused")
            raise

        _compare(conn, run_id, result, tell)
        result.seconds = time.perf_counter() - started
        _finish_run(conn, run_id, result, stats, status="finished")
    finally:
        conn.close()
    return result


def _load_reusable_hashes(
    conn: sqlite3.Connection, pair_id: int, incremental: bool
) -> dict[tuple[str, str], tuple[str, int, int]]:
    """可沿用的历史哈希：(side,key) -> (sha256, size, mtime_ns)。

    默认只取**未完成**校验（running/paused）的哈希 = 断点续跑：
    那些文件本来就在这一轮的待读清单里，只是上次没读完，沿用不会漏检。

    跨已完成校验沿用是有害的：静默损坏不改 size/mtime，沿用即等于跳过校验。
    所以只有用户显式选择增量模式时才这么做。
    """
    reuse: dict[tuple[str, str], tuple[str, int, int]] = {}
    where = "vr.pair_id=? AND vf.sha256 != '' AND vf.error = ''"
    if not incremental:
        where += " AND vr.status IN ('running','paused')"
    rows = conn.execute(
        "SELECT vf.side, vf.key, vf.sha256, vf.size, vf.mtime_ns"
        " FROM verify_files vf JOIN verify_runs vr ON vr.id = vf.verify_run_id"
        f" WHERE {where}",
        (pair_id,),
    )
    for r in rows:
        reuse[(r["side"], r["key"])] = (r["sha256"], r["size"], r["mtime_ns"])
    return reuse


def _verify_side(
    conn: sqlite3.Connection,
    run_id: int,
    side: str,
    root: str,
    reuse: dict[tuple[str, str], tuple[str, int, int]],
    result: VerifyResult,
    control: Control,
    tell: ProgressFn,
) -> dict[str, Any]:
    """扫描 + 哈希一侧。边扫边入线程池（背压控制），完成即入库。"""
    entries, skipped = _walk(root)
    total_bytes = sum(e[2] for e in entries)
    conn.executemany(
        "INSERT OR REPLACE INTO verify_files"
        "(verify_run_id, side, key, display, size, mtime_ns, sha256, error)"
        " VALUES(?,?,?,?,?,?, '', '')",
        [(run_id, side, k, d, s, m) for k, d, s, m in entries],
    )
    conn.commit()

    errors: list[str] = []
    done_bytes = 0
    last_report = time.perf_counter()
    started = time.perf_counter()

    with ThreadPoolExecutor(max_workers=_HASH_WORKERS) as pool:
        pending: dict[Any, tuple[str, str, int, int]] = {}   # future -> entry
        iterator = iter(entries)

        def submit_more() -> bool:
            """队列有空位时继续提交，返回是否还有新提交。"""
            nonlocal done_bytes
            while len(pending) < _QUEUE_DEPTH:
                try:
                    entry = next(iterator)
                except StopIteration:
                    return bool(pending)
                key, display, size, mtime = entry
                cached = reuse.get((side, key))
                if cached and cached[1] == size and cached[2] == mtime:
                    # 沿用历史哈希（增量/断点续跑）：不重读文件
                    conn.execute(
                        "UPDATE verify_files SET sha256=?"
                        " WHERE verify_run_id=? AND side=? AND key=?",
                        (cached[0], run_id, side, key),
                    )
                    result.reused_files += 1
                    done_bytes += size
                    continue
                control.check()
                future = pool.submit(_hash_file, os.path.join(root, display))
                pending[future] = entry
            return True

        keep = submit_more()
        while pending:
            control.check()   # 暂停检查点：批次之间及时响应
            done, _ = futures_wait(pending, return_when="FIRST_COMPLETED")
            for future in done:
                key, display, size, mtime = pending.pop(future)
                sha256, error = future.result()
                conn.execute(
                    "UPDATE verify_files SET sha256=?, error=?"
                    " WHERE verify_run_id=? AND side=? AND key=?",
                    (sha256, error, run_id, side, key),
                )
                result.hashed_files += 1
                result.read_bytes += size
                done_bytes += size
                if error:
                    errors.append(f"{display}\t{error}")
                    result.errors.append(f"{display}\t{error}")
            conn.commit()
            keep = submit_more()

            now = time.perf_counter()
            if now - last_report >= 0.5:
                last_report = now
                elapsed = max(now - started, 1e-6)
                rate = done_bytes / elapsed
                remaining = (total_bytes - done_bytes) / max(rate, 1.0)
                tell({
                    "phase": "hash", "side": side, "files": result.hashed_files,
                    "reused": result.reused_files, "bytes": done_bytes,
                    "total_bytes": total_bytes, "rate": rate, "eta": remaining,
                })
            if not keep and not pending:
                break

    conn.commit()
    return {"files": len(entries), "bytes": total_bytes, "skipped": skipped}


def _compare(
    conn: sqlite3.Connection,
    run_id: int,
    result: VerifyResult,
    tell: ProgressFn,
) -> None:
    """两侧 verify_files 按 key 流式归并比对，差异写 diffs 型结果。"""
    tell({"phase": "compare", "side": "", "files": 0, "reused": result.reused_files,
          "bytes": 0, "total_bytes": 0, "rate": 0.0, "eta": 0.0})
    ca = conn.execute(
        "SELECT key, display, size, mtime_ns, sha256 FROM verify_files"
        " WHERE verify_run_id=? AND side='a' ORDER BY key",
        (run_id,),
    )
    cb = conn.execute(
        "SELECT key, display, size, mtime_ns, sha256 FROM verify_files"
        " WHERE verify_run_id=? AND side='b' ORDER BY key",
        (run_id,),
    )
    fa = ca.fetchone()
    fb = cb.fetchone()
    while fa is not None or fb is not None:
        if fa is None:
            choice = 1
        elif fb is None:
            choice = -1
        else:
            choice = (fa["key"] > fb["key"]) - (fa["key"] < fb["key"])
        if choice < 0:
            result.counts[CAT_ONLY_A] += 1
            result.rows.append({
                "category": CAT_ONLY_A, "path": fa["display"],
                "size_a": fa["size"], "size_b": -1,
                "sha_a": fa["sha256"][:12], "sha_b": "",
            })
            fa = ca.fetchone()
        elif choice > 0:
            result.counts[CAT_ONLY_B] += 1
            result.rows.append({
                "category": CAT_ONLY_B, "path": fb["display"],
                "size_a": -1, "size_b": fb["size"],
                "sha_a": "", "sha_b": fb["sha256"][:12],
            })
            fb = cb.fetchone()
        else:
            if fa["size"] != fb["size"]:
                result.counts[CAT_SIZE] += 1
                result.rows.append({
                    "category": CAT_SIZE, "path": fa["display"],
                    "size_a": fa["size"], "size_b": fb["size"],
                    "sha_a": "", "sha_b": "",
                })
            elif fa["mtime_ns"] != fb["mtime_ns"]:
                result.counts[CAT_MTIME] += 1
                result.rows.append({
                    "category": CAT_MTIME, "path": fa["display"],
                    "size_a": fa["size"], "size_b": fb["size"],
                    "sha_a": "", "sha_b": "",
                })
            elif fa["sha256"] and fb["sha256"] and fa["sha256"] != fb["sha256"]:
                result.counts[CAT_CONTENT] += 1
                result.rows.append({
                    "category": CAT_CONTENT, "path": fa["display"],
                    "size_a": fa["size"], "size_b": fb["size"],
                    "sha_a": fa["sha256"][:12], "sha_b": fb["sha256"][:12],
                })
            else:
                result.counts["same"] += 1
            fa = ca.fetchone()
            fb = cb.fetchone()


def _finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    result: VerifyResult,
    stats: dict[str, dict[str, Any]],
    status: str,
) -> None:
    conn.execute(
        "UPDATE verify_runs SET status=?, finished=?, counts_json=?,"
        " reused_files=?, hashed_files=?, read_bytes=?, incremental=?, seconds=?, errors_json=?"
        " WHERE id=?",
        (
            status,
            time.strftime("%Y-%m-%d %H:%M:%S"),
            json.dumps(result.counts),
            result.reused_files, result.hashed_files, result.read_bytes,
            1 if result.incremental else 0,
            result.seconds,
            json.dumps(result.errors, ensure_ascii=False)[:8000],
            run_id,
        ),
    )
    conn.commit()


def last_verify(store: PairStore, pair_id: int) -> dict[str, Any] | None:
    ensure_schema(store)   # 旧库可能还没有 incremental 列
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM verify_runs WHERE pair_id=? AND status='finished'"
            " ORDER BY id DESC LIMIT 1",
            (int(pair_id),),
        ).fetchone()
    if row is None:
        return None
    try:
        counts = json.loads(row["counts_json"])
    except ValueError:
        counts = {}
    return {
        "verify_run_id": row["id"],
        "timestamp": row["finished"],
        "counts": counts,
        "reused_files": row["reused_files"],
        "hashed_files": row["hashed_files"],
        "read_bytes": row["read_bytes"],
        "incremental": bool(row["incremental"]),
        "seconds": row["seconds"],
    }


def recent_runs(store: PairStore, pair_id: int, limit: int = 2) -> list[dict[str, Any]]:
    """最近几次完成的内容校验（新→旧），用于"与上次对比"。"""
    ensure_schema(store)
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, started, finished FROM verify_runs"
            " WHERE pair_id=? AND status='finished' ORDER BY id DESC LIMIT ?",
            (int(pair_id), int(limit)),
        ).fetchall()
    return [
        {"run_id": r["id"], "timestamp": r["finished"] or r["started"]} for r in rows
    ]


def run_rows(store: PairStore, run_id: int) -> list[dict[str, Any]]:
    """事后重算某次内容校验的差异明细（历史报告用）。只读数据库，不碰磁盘。"""
    result = VerifyResult()
    with store.connect() as conn:
        _compare(conn, int(run_id), result, lambda _info: None)
    return result.rows


def all_runs(store: PairStore, limit: int = 200) -> list[dict[str, Any]]:
    """全部盘对内容校验记录，按时间倒序（含各次统计）。"""
    ensure_schema(store)
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT vr.*, p.name AS pair_name FROM verify_runs vr"
            " JOIN pairs p ON p.id = vr.pair_id"
            " WHERE p.deleted_at='' ORDER BY vr.id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    out = []
    for row in rows:
        try:
            counts = json.loads(row["counts_json"])
        except ValueError:
            counts = {}
        out.append({
            "source": "pair",
            "run_id": row["id"],
            "pair_id": row["pair_id"],
            "title": row["pair_name"],
            "status": row["status"],
            "kind": "incremental" if row["incremental"] else "full",
            "started": row["started"],
            "finished": row["finished"],
            "counts": counts,
            "files": row["hashed_files"] + row["reused_files"],
            "seconds": row["seconds"],
        })
    return out
