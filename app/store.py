"""文件存储层：不依赖数据库，全部落在项目 data/ 目录。

目录结构：
    data/
    ├─ disks.json                        硬盘档案索引
    └─ snapshots/<硬盘目录>/
       ├─ attributes.csv                 追加写，画图/Excel 直接用
       └─ raw/<快照ID>.json              完整原始 JSON，永不修改

写入一律用"临时文件 + 原子替换"，避免断电留下半个文件。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import normalize
from .collector import DiskDevice, Snapshot
from .paths import data_dir

INDEX_NAME = "disks.json"
CSV_NAME = "attributes.csv"
RAW_DIR = "raw"

_BASE_COLUMNS = ("timestamp", "snapshot_id", "severity", "exit_status")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(text: str) -> str:
    cleaned = _UNSAFE.sub("_", text.strip()).strip(" ._")
    return cleaned[:64]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else data_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / INDEX_NAME
        index = self._load_index()
        if self._migrate_forks(index):
            self._save_index(index)

    # ------------------------------------------------------------------ #
    # 索引
    # ------------------------------------------------------------------ #
    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"version": 1, "disks": {}}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": 1, "disks": {}}
        data.setdefault("version", 1)
        data.setdefault("disks", {})
        return data

    def _save_index(self, data: dict[str, Any]) -> None:
        _atomic_write(self.index_path, json.dumps(data, ensure_ascii=False, indent=2))

    def disks(self) -> dict[str, dict[str, Any]]:
        return self._load_index()["disks"]

    def disk_dir(self, key: str) -> Path:
        return self.root / "snapshots" / key

    def find_key(self, device: DiskDevice) -> str | None:
        """查这个设备是否已有历史档案。

        序列号是唯一身份，只按序列号匹配；序列号缺失时才退回 型号+容量。
        容量不参与判断：WMI 与 smartctl IDENTIFY 对同一块盘报出的容量可能不同。
        """
        serial = device.serial.strip()
        for key, entry in self.disks().items():
            if serial:
                if entry.get("serial") == serial:
                    return key
            elif (
                entry.get("model") == device.model
                and entry.get("capacity_bytes") == device.capacity_bytes
            ):
                return key
        return None

    @staticmethod
    def _is_conflict(entry: dict[str, Any], device: DiskDevice) -> bool:
        """是否真的是"另一块盘"。

        只有型号对不上才算冲突。容量不参与判断：同一块盘，
        WMI 报 1000202273280 而 IDENTIFY 报 1000204886016，相差数 GB 是正常的。
        """
        return (entry.get("model") or "") != (device.model or "")

    # ------------------------------------------------------------------ #
    # 历史脏数据合并
    # ------------------------------------------------------------------ #
    def _migrate_forks(self, index: dict[str, Any]) -> bool:
        """把历史上因容量误判产生的 `<序列号>__哈希` 分叉条目合并回主条目。"""
        disks = index["disks"]
        changed = False
        for key in [k for k in disks if "__" in k]:
            base = key.split("__", 1)[0]
            fork = disks[key]
            target = disks.get(base)

            if target is None:
                # 主条目反而缺失：直接改名为主条目
                if self.disk_dir(key).exists():
                    self.disk_dir(key).rename(self.disk_dir(base))
                disks[base] = disks.pop(key)
                changed = True
                continue

            if (
                target.get("serial") != fork.get("serial")
                or target.get("model") != fork.get("model")
            ):
                continue  # 真的是另一块盘，保留

            self._merge_snapshot_dir(base, key)
            target["snapshots"] = len(self.list_snapshots(base))
            del disks[key]
            changed = True
        return changed

    def _merge_snapshot_dir(self, base_key: str, fork_key: str) -> None:
        """把分叉目录的快照并入主目录。"""
        base_dir = self.disk_dir(base_key)
        fork_dir = self.disk_dir(fork_key)
        base_dir.mkdir(parents=True, exist_ok=True)

        existing_ids = set(self.list_snapshots(base_key))
        fork_raw = fork_dir / RAW_DIR
        if fork_raw.exists():
            (base_dir / RAW_DIR).mkdir(exist_ok=True)
            for src in sorted(fork_raw.glob("*.json")):
                if src.stem in existing_ids:
                    continue
                src.replace(base_dir / RAW_DIR / src.name)

        self._merge_csv(base_dir / CSV_NAME, fork_dir / CSV_NAME)
        shutil.rmtree(fork_dir, ignore_errors=True)

    def _merge_csv(self, base_csv: Path, fork_csv: Path) -> None:
        base_rows = self._read_rows(base_csv)
        fork_rows = self._read_rows(fork_csv)
        if not fork_rows:
            return

        merged: dict[str, dict[str, str]] = {}
        for row in base_rows + fork_rows:
            merged.setdefault(row.get("snapshot_id", ""), row)

        columns = list(_BASE_COLUMNS) + list(normalize.METRIC_KEYS)
        extra = sorted({c for row in merged.values() for c in row if c not in columns})
        columns += extra
        lines = [",".join(_csv_cell(c) for c in columns)]
        for row in sorted(merged.values(), key=lambda r: r.get("timestamp", "")):
            lines.append(",".join(_csv_cell(row.get(c, "")) for c in columns))
        _atomic_write(base_csv, "\n".join(lines) + "\n")


    def register(self, device: DiskDevice) -> str:
        """登记硬盘档案，返回目录名（即稳定身份）。

        序列号唯一优先；序列号缺失时退化为 型号+容量，并附加短哈希避免撞车。
        """
        index = self._load_index()
        disks = index["disks"]

        serial = _safe_name(device.serial)
        if serial:
            base = serial
        else:
            digest = hashlib.sha1(
                f"{device.model}|{device.capacity_bytes}".encode()
            ).hexdigest()[:6]
            base = f"{_safe_name(device.model) or 'UNKNOWN'}-{device.capacity_bytes // 1_000_000_000}G-{digest}"

        key = base
        existing = disks.get(key)
        if existing and self._is_conflict(existing, device):
            # 同一个序列号对应不同型号/容量：多是 USB 桥接器给出固定假序列号
            digest = hashlib.sha1(
                f"{device.model}|{device.capacity_bytes}".encode()
            ).hexdigest()[:6]
            key = f"{base}__{digest}"

        entry = disks.setdefault(
            key,
            {
                "key": key,
                "serial": device.serial.strip(),
                "model": device.model,
                "capacity_bytes": device.capacity_bytes,
                "protocol": device.protocol,
                "first_seen": datetime.now().isoformat(timespec="seconds"),
                "snapshots": 0,
            },
        )
        entry["model"] = device.model or entry.get("model", "")
        entry["capacity_bytes"] = device.capacity_bytes or entry.get("capacity_bytes", 0)
        entry["protocol"] = device.protocol or entry.get("protocol", "")
        entry["firmware"] = device.firmware or entry.get("firmware", "")
        entry["last_device"] = device.name
        entry["last_seen"] = datetime.now().isoformat(timespec="seconds")
        entry["snapshots"] = int(entry.get("snapshots", 0)) + 1
        self._save_index(index)
        return key

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #
    def save_snapshot(self, snapshot: Snapshot, metrics: dict[str, Any]) -> tuple[str, Path]:
        key = self.register(snapshot.device)
        folder = self.disk_dir(key)
        raw_path = folder / RAW_DIR / f"{snapshot.snapshot_id}.json"

        _atomic_write(
            raw_path,
            json.dumps(snapshot.raw, ensure_ascii=False, indent=1),
        )
        self._append_row(folder / CSV_NAME, snapshot, metrics)
        return key, raw_path

    def _append_row(self, csv_path: Path, snapshot: Snapshot, metrics: dict[str, Any]) -> None:
        row: dict[str, Any] = {
            "timestamp": snapshot.collected_at.isoformat(timespec="seconds"),
            "snapshot_id": snapshot.snapshot_id,
            "severity": snapshot.severity,
            "exit_status": snapshot.exit_status,
        }
        row.update(metrics)

        columns = list(_BASE_COLUMNS) + [k for k in normalize.METRIC_KEYS if k in metrics]

        if not csv_path.exists():
            lines = [columns, [str(row.get(c, "")) for c in columns]]
            _atomic_write(csv_path, "\n".join(",".join(_csv_cell(v) for v in line) for line in lines) + "\n")
            return

        header = self._header(csv_path)
        missing = [c for c in row if c not in header]
        if missing:
            # 出现了新指标：重写整表加列，老行留空
            new_header = header + missing
            old_rows = self._read_rows(csv_path)
            body = [",".join(_csv_cell(r.get(c, "")) for c in new_header) for r in old_rows]
            body.append(",".join(_csv_cell(row.get(c, "")) for c in new_header))
            _atomic_write(
                csv_path,
                "\n".join([",".join(_csv_cell(c) for c in new_header), *body]) + "\n",
            )
            return

        with csv_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([row.get(c, "") for c in header])
            handle.flush()

    def _header(self, csv_path: Path) -> list[str]:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            first = handle.readline().strip()
        return first.split(",") if first else []

    def _read_rows(self, csv_path: Path) -> list[dict[str, str]]:
        if not csv_path.exists():
            return []
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def history(self, key: str) -> list[dict[str, Any]]:
        """按时间升序返回历史记录，数值列已转换为数字。"""
        rows = self._read_rows(self.disk_dir(key) / CSV_NAME)
        result: list[dict[str, Any]] = []
        for row in rows:
            parsed: dict[str, Any] = {"timestamp": row.get("timestamp", "")}
            for column, value in row.items():
                if column in ("timestamp", "snapshot_id", "severity"):
                    parsed[column] = value
                    continue
                if value in (None, ""):
                    continue
                try:
                    number = float(value)
                    parsed[column] = int(number) if number.is_integer() else number
                except (TypeError, ValueError):
                    parsed[column] = value
            result.append(parsed)
        result.sort(key=lambda item: item.get("timestamp", ""))
        return result

    def list_snapshots(self, key: str) -> list[str]:
        raw_dir = self.disk_dir(key) / RAW_DIR
        if not raw_dir.exists():
            return []
        return sorted((p.stem for p in raw_dir.glob("*.json")), reverse=True)

    def load_raw(self, key: str, snapshot_id: str) -> dict[str, Any] | None:
        path = self.disk_dir(key) / RAW_DIR / f"{snapshot_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def known_keys(self) -> Iterable[str]:
        return self.disks().keys()


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text
