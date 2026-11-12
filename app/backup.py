"""一键备份：把整个 data 目录打包成 zip。

两个要点：
1. SQLite 用 backup API 复制一份快照，而不是直接拷 .db 文件——
   WAL 模式下正在写入时直接拷可能拿到半截数据；快照保证一致。
2. 其余文件（disks.json、snapshots/）都是小 JSON，直接写进 zip。
"""
from __future__ import annotations

import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

from .paths import data_dir

ProgressFn = Callable[[str], None]


class BackupError(Exception):
    """备份失败。"""


def default_backup_name() -> str:
    import time

    return f"DiskSentinel-数据备份-{time.strftime('%Y%m%d-%H%M')}.zip"


def _snapshot_sqlite(source: Path, target: Path) -> None:
    """用 SQLite 在线备份 API 生成一致快照。"""
    src = sqlite3.connect(str(source), timeout=30)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def backup_data(target: str | Path, progress: ProgressFn | None = None) -> tuple[int, int]:
    """把 data 目录打包到 target。

    返回 (文件数, 总字节数)。只读数据目录，不修改任何原始文件。
    """
    root = data_dir()
    target_path = Path(target).resolve()
    if not root.is_dir():
        raise BackupError(f"数据目录不存在：{root}")
    if target_path == root or root in target_path.parents:
        raise BackupError("备份文件不能放在数据目录里面，请换一个位置（比如桌面或另一块盘）。")

    tell = progress or (lambda _msg: None)
    files: list[Path] = [
        p for p in root.rglob("*")
        if p.is_file() and p.resolve() != target_path
    ]
    total = len(files)
    written = 0
    total_bytes = 0

    try:
        with tempfile.TemporaryDirectory(prefix="disksentinel-bak-") as temp_dir:
            temp = Path(temp_dir)
            db_snapshot: Path | None = None
            source_db = root / "fileindex" / "index.db"
            if source_db.is_file():
                tell("正在生成数据库快照…")
                db_snapshot = temp / "index.db"
                _snapshot_sqlite(source_db, db_snapshot)

            with zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for index, path in enumerate(files, 1):
                    rel = path.relative_to(root)
                    # 数据库走 snapshot 单独写入，避免 zip 里出现两份同名 index.db；
                    # WAL/SHM 是运行期临时文件，快照里不需要
                    if rel.as_posix() == "fileindex/index.db":
                        continue
                    if rel.name in ("index.db-wal", "index.db-shm"):
                        continue
                    zf.write(path, arcname=str(Path("data") / rel))
                    written += 1
                    total_bytes += path.stat().st_size
                    if index % 200 == 0:
                        tell(f"正在打包：{index:,}/{total:,} 个文件")
                if db_snapshot is not None:
                    zf.write(db_snapshot, arcname="data/fileindex/index.db")
                    written += 1
                    total_bytes += db_snapshot.stat().st_size
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        # 失败时别留下半截 zip，否则用户以为备份成功了
        try:
            target_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise BackupError(f"备份失败：{exc}") from exc

    return written, total_bytes
