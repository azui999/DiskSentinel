"""smartctl 调用层。

职责边界：只负责"把 smartctl 跑起来、把 JSON 拿回来、把退出码翻译成人话"。
不做指标归一化（normalize.py），不落盘（store.py）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import project_root

# 单次调用超时。坏盘或桥接器无响应时必须能中断，否则界面会卡死。
CMD_TIMEOUT = 40

# smartctl 退出码是位掩码，不是递进错误码。
EXIT_FLAGS: tuple[tuple[int, str], ...] = (
    (0x01, "命令行参数错误"),
    (0x02, "设备无法打开"),
    (0x04, "部分 SMART 命令执行失败"),
    (0x08, "硬盘处于失效预警状态"),
    (0x10, "曾有属性值越过阈值"),
    (0x20, "当前属性值均高于阈值"),
    (0x40, "错误日志中存在记录"),
    (0x80, "自检日志中存在错误"),
)

# 真正代表"这次读取没成功"的位。其余位是硬盘状态信息，不是调用失败。
FATAL_MASK = 0x01 | 0x02


class CollectError(RuntimeError):
    """采集失败，消息面向用户可直接展示。"""


@dataclass
class DiskDevice:
    name: str
    dev_type: str = "auto"
    protocol: str = ""
    model: str = ""
    serial: str = ""
    firmware: str = ""
    capacity_bytes: int = 0
    rotation_rate: int = 0
    smart_available: bool = True
    smart_enabled: bool = True
    passed: bool | None = None
    reachable: bool = True
    limited: bool = False          # 非管理员时部分识别信息不可用
    letters: tuple[str, ...] = ()  # 挂载的盘符（C:、D:...）
    error: str = ""

    @property
    def is_ssd(self) -> bool:
        if self.protocol.upper() == "NVME":
            return True
        # smartctl: rotation_rate 为 1 表示 SSD，为 0 表示未上报
        return self.rotation_rate in (0, 1)

    @property
    def capacity_text(self) -> str:
        if not self.capacity_bytes:
            return "未知"
        gb = self.capacity_bytes / 1_000_000_000
        return f"{gb:.0f} GB" if gb < 1000 else f"{gb / 1000:.2f} TB"

    @property
    def display_name(self) -> str:
        return self.model or "(未知型号)"

    @property
    def identity(self) -> str:
        return self.serial.strip() or f"{self.model}|{self.capacity_bytes}"

    @property
    def letters_text(self) -> str:
        return ", ".join(self.letters) if self.letters else ""


@dataclass
class Snapshot:
    device: DiskDevice
    collected_at: datetime
    exit_status: int
    raw: dict[str, Any]
    severity: str = "ok"            # ok / warning / error，只反映硬盘健康
    flags: list[str] = field(default_factory=list)
    partial: bool = False           # 数据不完整（通常是权限导致）
    needs_admin: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def smartctl_version(self) -> str:
        ver = self.raw.get("smartctl", {}).get("version") or []
        return ".".join(str(v) for v in ver) or "unknown"

    @property
    def overall_passed(self) -> bool | None:
        status = self.raw.get("smart_status") or {}
        return status.get("passed")

    @property
    def snapshot_id(self) -> str:
        return self.collected_at.strftime("%Y%m%dT%H%M%S")


# --------------------------------------------------------------------------- #
# 可执行文件定位
# --------------------------------------------------------------------------- #

_smartctl_cache: Path | None = None


def find_smartctl() -> Path:
    """按优先级查找 smartctl.exe，找不到就给出可操作的提示。"""
    global _smartctl_cache
    if _smartctl_cache and _smartctl_cache.exists():
        return _smartctl_cache

    candidates: list[Path] = []
    env_path = os.environ.get("SMARTCTL_PATH")
    if env_path:
        candidates.append(Path(env_path))
    # 绿色版：随项目走
    candidates.append(project_root() / "tools" / "smartctl.exe")
    candidates.append(Path(r"C:\Program Files\smartmontools\bin\smartctl.exe"))
    candidates.append(Path(r"C:\Program Files (x86)\smartmontools\bin\smartctl.exe"))
    which = shutil.which("smartctl")
    if which:
        candidates.append(Path(which))

    for path in candidates:
        try:
            if path.is_file():
                _smartctl_cache = path
                return path
        except OSError:
            continue

    raise CollectError(
        "未找到 smartctl.exe。\n"
        "请安装 smartmontools 64 位版本，或把 smartctl.exe 放到项目 tools/ 目录下。\n"
        "也可以用环境变量 SMARTCTL_PATH 指定完整路径。"
    )


def _decode(payload: bytes) -> str:
    for enc in ("utf-8", "cp936", "latin-1"):
        try:
            return payload.decode(enc)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", "replace")


def _run(args: list[str]) -> tuple[int, str, str]:
    exe = find_smartctl()
    cmd = [str(exe), *args]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=CMD_TIMEOUT,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectError(f"smartctl 超过 {CMD_TIMEOUT} 秒无响应，已中止。") from exc
    except OSError as exc:
        raise CollectError(f"无法执行 smartctl：{exc}") from exc

    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)


def _run_json(args: list[str]) -> tuple[int, dict[str, Any], str]:
    rc, out, err = _run(args)
    text = out.strip()
    if not text:
        raise CollectError((err or "smartctl 没有返回任何内容。").strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        head = text[:200].replace("\n", " ")
        raise CollectError(
            f"smartctl 输出无法解析为 JSON：{head}\n{err}".strip()
        ) from exc
    return rc, data, err


# --------------------------------------------------------------------------- #
# 设备扫描
# --------------------------------------------------------------------------- #

def _apply_identity(device: DiskDevice, info: dict[str, Any]) -> None:
    device.model = (info.get("model_name") or info.get("device", {}).get("model") or "").strip()
    device.serial = (info.get("serial_number") or "").strip()
    device.firmware = (info.get("firmware_version") or "").strip()
    device.protocol = (info.get("device", {}).get("protocol") or device.protocol or "").strip()

    capacity = info.get("user_capacity") or {}
    device.capacity_bytes = int(capacity.get("bytes") or info.get("nvme_total_capacity") or 0)
    device.rotation_rate = int(info.get("rotation_rate") or 0)

    if not device.capacity_bytes:
        # 非管理员权限下 smartctl 拿不到完整的 IDENTIFY 数据（容量、转速都会缺），
        # 这里用 WMI 兜底，保证界面上容量不至于显示成"未知"。
        index = _physical_index(device.name)
        if index is not None:
            device.capacity_bytes = _wmi_sizes().get(index, 0)
        device.limited = True

    if os.name == "nt":
        index = _physical_index(device.name)
        if index is not None:
            device.letters = tuple(_drive_letters().get(index, ()))

    support = info.get("smart_support") or {}
    if support:
        device.smart_available = bool(support.get("available", True))
        device.smart_enabled = bool(support.get("enabled", True))
    device.passed = (info.get("smart_status") or {}).get("passed")


def _type_args(dev_type: str) -> list[str]:
    if dev_type and dev_type not in ("auto",):
        return ["-d", dev_type]
    return []


def read_identity(name: str, dev_type: str = "auto") -> dict[str, Any]:
    """只读身份信息，不开 SMART 日志，速度快且不需要管理员权限。"""
    _, info, _ = _run_json(["-j", "-i", *_type_args(dev_type), name])
    return info


def scan_devices() -> list[DiskDevice]:
    """枚举本机磁盘。

    Windows 上直接探测 /dev/pd0..N：比 `--scan-open` 给出的 /dev/sdX 更可靠
    （后者在部分机器上会被解析成磁盘分区，导致读取失败），而且 pdN 的编号
    可以直接对上 WMI 的 PhysicalDrive 序号，方便取容量兜底。
    """
    devices: list[DiskDevice] = []
    if os.name == "nt":
        devices = _probe_physical_drives()
    if not devices:
        devices = _scan_open()
    return devices


def _scan_open() -> list[DiskDevice]:
    _, data, _ = _run_json(["-j", "--scan-open"])
    devices: list[DiskDevice] = []
    for item in data.get("devices") or []:
        name = item.get("name") or ""
        if not name:
            continue
        dev = DiskDevice(
            name=name,
            dev_type=item.get("type") or "auto",
            protocol=item.get("protocol") or "",
        )
        try:
            _apply_identity(dev, read_identity(dev.name, dev.dev_type))
        except CollectError:
            try:
                _apply_identity(dev, read_identity(dev.name))
            except CollectError as exc:
                dev.reachable = False
                dev.error = str(exc)
        devices.append(dev)
    return devices


def _probe_physical_drives(limit: int = 16, max_misses: int = 3) -> list[DiskDevice]:
    devices: list[DiskDevice] = []
    misses = 0
    for index in range(limit):
        name = f"/dev/pd{index}"
        try:
            info = read_identity(name)
        except CollectError:
            misses += 1
            if devices and misses >= max_misses:
                break
            continue

        dev = DiskDevice(name=name)
        _apply_identity(dev, info)
        if not dev.model and not dev.serial:
            misses += 1
            if devices and misses >= max_misses:
                break
            continue

        misses = 0
        devices.append(dev)
    return devices


# --------------------------------------------------------------------------- #
# WMI 兜底：容量信息
# --------------------------------------------------------------------------- #

_PHYSICAL_INDEX = re.compile(r"/dev/(?:pd|sd)(\d+|[a-z])", re.IGNORECASE)
_wmi_cache: dict[int, int] | None = None
_letter_cache: dict[int, list[str]] | None = None


def _drive_letters() -> dict[int, list[str]]:
    """盘号映射：PhysicalDrive 序号 → 盘符列表（对标 CrystalDiskInfo 的"驱动器号"）。"""
    global _letter_cache
    if _letter_cache is not None:
        return _letter_cache
    _letter_cache = {}
    if os.name != "nt":
        return _letter_cache

    script = (
        "Get-Partition | Where-Object DriveLetter | "
        "ForEach-Object { \"$($_.DiskNumber)|$($_.DriveLetter)\" }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = _decode(proc.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return _letter_cache

    for line in output.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 2 and parts[0].isdigit() and parts[1]:
            _letter_cache.setdefault(int(parts[0]), []).append(f"{parts[1]}:")
    return _letter_cache


def _physical_index(name: str) -> int | None:
    match = _PHYSICAL_INDEX.fullmatch(name.strip())
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token)
    return ord(token.lower()) - ord("a")


def _wmi_sizes() -> dict[int, int]:
    global _wmi_cache
    if _wmi_cache is not None:
        return _wmi_cache
    _wmi_cache = {}
    if os.name != "nt":
        return _wmi_cache

    script = (
        "Get-CimInstance Win32_DiskDrive | "
        "ForEach-Object { \"$($_.Index)|$($_.Size)\" }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return _wmi_cache

    for line in _decode(proc.stdout).splitlines():
        parts = line.strip().split("|")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            _wmi_cache[int(parts[0])] = int(parts[1])
    return _wmi_cache



# --------------------------------------------------------------------------- #
# 快照读取
# --------------------------------------------------------------------------- #

def read_snapshot(device: DiskDevice) -> Snapshot:
    """读取完整 SMART 信息，不写入任何东西。"""
    rc, data, err = _run_json(
        ["-j", "-x", *_type_args(device.dev_type), device.name]
    )

    snapshot = Snapshot(
        device=device,
        collected_at=datetime.now().replace(microsecond=0),
        exit_status=rc,
        raw=data,
    )
    snapshot.flags = [text for bit, text in EXIT_FLAGS if rc & bit]

    if rc & FATAL_MASK:
        detail = err.strip() or "；".join(snapshot.flags) or "未知原因"
        raise CollectError(f"{device.name} 读取失败：{detail}")

    has_attrs = "ata_smart_attributes" in data
    has_nvme = "nvme_smart_health_information_log" in data
    snapshot.partial = bool(rc & 0x04) and (has_attrs or has_nvme)

    # JSON 里不会记录"权限不足"这件事（只在 stdout 文本里），
    # 所以用"命令失败 + 属性在但日志缺"来推断。
    if snapshot.partial and has_attrs and not any(
        key in data for key in ("ata_smart_error_log", "ata_smart_selftest_log")
    ):
        snapshot.needs_admin = True
        snapshot.notes.append("错误日志与自检日志未读取，需要管理员权限。")

    # severity 只描述硬盘健康，不把"数据不全"混进来，否则非管理员会一直看到告警。
    if rc & 0x08 or rc & 0x10:
        snapshot.severity = "error" if rc & 0x08 else "warning"
    if data.get("smart_status", {}).get("passed") is False:
        snapshot.severity = "error"


    support = data.get("smart_support") or {}
    if support and not support.get("available", True):
        snapshot.notes.append("该设备不支持 SMART。")

    return snapshot


def smartctl_version() -> str:
    try:
        rc, data, _ = _run_json(["-j", "--version"])
    except CollectError:
        return "unknown"
    ver = data.get("smartctl", {}).get("version") or []
    text = ".".join(str(v) for v in ver)
    return text or "unknown"
