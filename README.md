# DiskSentinel

磁盘哨兵
Disk + Sentinel（磁盘 + 哨兵）

寓意：长期守护硬盘，监测健康状态，提前发现潜在风险。

## 运行

双击 `run.bat`。

- 首次运行会自动创建项目虚拟环境 `.venv` 并安装依赖（需要联网一次，之后不再需要）。
- 平时启动用 `pythonw`，不会留黑色控制台窗口；程序异常会写入
  `data\logs\error.log` 并弹窗提示。
- 需要 Windows 系统；smartctl.exe 已内置在 `tools/` 下，无需安装 smartmontools，
  也不需要配置任何环境变量。
- 普通权限即可读取 SMART 属性；`run_as_admin.bat` 以管理员启动可额外读取
  错误日志、自检日志以及容量等识别信息。

## 数据

全部保存在项目目录 `data/` 下，不使用数据库：

```
data/
├─ disks.json                        # 硬盘档案索引
└─ snapshots/<硬盘序列号>/
   ├─ attributes.csv                 # 指标时间序列，Excel 可直接打开
   └─ raw/<快照ID>.json              # 每次检测的完整原始 JSON
```

## 设计与评估文档

- `docs/设计.md` —— 原始设计讨论
- `docs/可行性评估.md` —— 技术选型、实测结论与风险清单
