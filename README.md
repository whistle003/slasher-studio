# Slasher Studio

Slasher Studio 是一个 Windows x64 工具，用于查找《Call of Duty》资源 hash 对应的原名，并生成 Saluki 可读取的 CDB。它支持 MWII、MWIII、BO6、BO7 和 MW7 的资产快照，可处理动画、声音文件、声音别名、材质和图片。模型查名目前适用于 MW7。

程序用 hash-slinging-slasher 的 CPU 引擎或 OpenCL GPU 验证候选。确认的名称会先写成 TXT，再由 NameIndex 转成合并 CDB，并完整回读核验。模型名称输出到本地 CSV。PR 工作流只保存本地预览。 [hash-slinging-slasher](https://github.com/KingslayerKyle/hash-slinging-slasher) · [cod-name-db](https://github.com/echo000/cod-name-db)

## 运行

使用便携版时，解压 Windows x64 ZIP 后打开 `SlasherStudio.exe`，并保留同目录的 `_internal`。从本仓库运行源码需要 Windows x64 和 Python 3.13，Tkinter 随常规 Windows Python 安装提供。仓库附带预编译的 CPU 引擎和 CDB 转换器。

```powershell
git clone https://github.com/whistle003/slasher-studio.git
Set-Location slasher-studio
py -3.13 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe studio.py
```

GPU 模式需要支持 OpenCL 的显卡和驱动。引擎选「自动（按任务选择）」时，会按实际批次选择 CPU 或 GPU，GPU 不可用时可用 CPU 继续。

## 从资产快照查找

1. 在「目标来源」选「资产快照」，选择 `.ids` 和同名 `.pools.txt` 所在位置。搜索声音别名时，同目录提供 `sound_alias.raw64.txt.gz` 或 `sound_alias.raw64.txt`。
2. 选择 `cod-name-db` 仓库或其 `csv` 子目录。准备独立的结果目录。若使用 Saluki，选其安装目录或 `hash_pkg`，程序会把原库纳入合并。
3. 勾选要搜索的类型，点击「开始查找并生成 CDB」。处理顺序为模型、动画、声音、材质、图片。勾选「自动换方法」后，新命中会为后续候选提供种子。
4. 可随时点「暂停」，等当前批保存后退出。下次载入同一任务，再点「继续搜索」。每次开始或继续时会先在线核对上游主分支和开放 PR，并读取所选原库和本地历史 CDB。

「高级选项」中的「额外 TXT 名单」接收每行一个完整候选名。「额外名称线索目录」接收 `confirmed_names.csv` 等以 `names.csv` 结尾的名称清单，表头为 `type,hash,name`，动画使用 `xanim`。

启动查找前请准备对应游戏和版本的快照。上游提供了示例快照和采集格式说明。 [上游快照目录](https://github.com/KingslayerKyle/hash-slinging-slasher/tree/main/snapshots) · [现代快照说明](https://github.com/KingslayerKyle/hash-slinging-slasher/blob/main/docs/MODERN_CAPTURES.md)

## 用 Saluki 导出查找 MW7 动画

「Saluki 导出目录」模式可以选择 MW7 导出根目录或 `animpkgs` 子目录。同级 `animations` 中的 CAST 文件可提供音效事件线索。程序使用包内已知动作的武器代码、配件、版本和槽位生成候选，再逐条核对目标 hash。

资产快照模式开启「自动换方法」时，MW7 任务进入动画阶段会尝试读取 Saluki 的 `exported_files/mw7`。如果导出放在其他位置，可以在「高级选项」指定含 `animpkgs` 的 MW7 根目录。现有任务可以沿用原断点。

## 结果文件

| 文件 | 用途 |
| --- | --- |
| `txt/<type>.txt` | 各类型的新增名称，每行 `hash,name` |
| `merged-csv/` | 传给 NameIndex 的完整名称表 |
| `saluki-ready/hash_pkg/` | 通过回读验证的合并 CDB |
| `new_names.csv` | 当前任务累计新增名称，含模型名 |
| `live/` | 最近一次完成验证的自动导出 |
| `pr-preview.txt`、`local-pr/` | 本地 PR 草稿和投稿文件 |
| `run.log`、`performance.json` | 查找、导出及耗时记录 |

如需在 Saluki 里使用结果，关闭 Saluki，备份旧库，再将生成的同名 CDB 放入 `hash_pkg`。Studio 将合并结果写在你选择的结果目录内。

## 测试和打包

在源码目录运行测试：

```powershell
& .\.venv\Scripts\python.exe -B -m unittest discover
```

打包脚本会把引擎和转换器源码随附到便携包。先准备两个相邻的完整仓库，`hash-slinging-slasher` 的历史需包含 [CPU 引擎提交](https://github.com/KingslayerKyle/hash-slinging-slasher/tree/375cbfb81ab2ff0db474d18184e29dc50b02d6e5) 和 [方法提交](https://github.com/KingslayerKyle/hash-slinging-slasher/tree/208c7d641e665475bc385e8446e0189d7f407bc5)。

```powershell
git clone https://github.com/KingslayerKyle/hash-slinging-slasher.git ..\hash-slinging-slasher
git clone https://github.com/echo000/cod-name-db.git ..\cod-name-db
git -C ..\cod-name-db checkout d561dabe7b73c766b8ea04643af07ecca52a9422
& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
$env:Path = (Resolve-Path .\.venv\Scripts).Path + ';' + $env:Path
.\build.ps1
```

构建产物在 `release/`，包括 Windows x64 便携版、源码 ZIP 和 `SHA256SUMS.txt`。打包会使用仓库附带的引擎和社区缓存。候选方法、依赖来源和许可说明见 [METHOD_SOURCES.md](METHOD_SOURCES.md)，运行性能见 [PERFORMANCE.md](PERFORMANCE.md)。本地任务数据库和结果应保存在独立目录，便于续跑和备份。
