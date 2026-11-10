常用启动参数（MFW 开关写在分隔符 `--` 之前；之后仅传给 Qt）：

- `--config-id <ID>`（或 `--config-id=<ID>`）：启动后切换到指定配置
- `--direct-run`：启动后直接运行任务流
- `--dev`：显示测试页面（开发调试开关）
- `--force-restart`：强制重启同目录下已有实例

示例：

```powershell
python main.py --config-id default --direct-run --dev
python main.py --config-id=default --direct-run -- -platform windows:darkmode=1
MFW.exe --direct-run --force-restart --config-id c_08b08298fcf340d8a028ee63503125e2
```

查看完整说明：`python main.py --help`

## 打包

Windows、Linux 与 macOS 均由 `.github/workflows/install.yml` 中的 Nuitka job 构建主程序并组装为独立分发版（不再下载或打包外部更新器）。Linux 主程序使用 `onefile`；macOS 主程序使用 `app-dist`。macOS 还需 `imageio`（见 `requirements-nuitka.txt`）以将 PNG 图标转为 `.icns`：

```bash
python -m pip install -r requirements-nuitka.txt
python -m nuitka --mode=onefile --enable-plugin=pyside6 \
  --linux-icon=app/assets/icons/logo.png --output-filename=MFW main.py
python -m nuitka --mode=app-dist --enable-plugin=pyside6 \
  --macos-app-mode=gui --macos-app-icon=app/assets/icons/logo.png main.py
```

当前发行版不含 UI 全量更新组件 `MFWUpdater/`；`tools/packaging/fetch_mfw_updater.py` 仅保留为可选的手动工具，CI 不会调用。升级主程序需整体替换发行目录。

Linux CI 会把 onefile 的 `MFW` 与外置 `maafw/` 组装为发行根。macOS 则为 `MFW.app` 与 `maafw/` 同级。`interface.json(c)`、`resource/`、`bundle/` 和 `config/` 必须保留在主程序外部；调试资源热更新流程时也应从该发行根启动程序。

## 版本与发布

本仓库 `hoey94/mfw-licence` 独立维护：不配置 `upstream` remote、不与上游 `overflow65537/MFW-PyQt6` 同步。仓库以当前提交为唯一 root，不保留上游提交历史与旧 tag，自 `v1.0.0` 起单独编号。

版本号规则集中在 [`tools/release_meta.py`](../tools/release_meta.py)，基线版本来自 [`app/common/__version__.py`](../app/common/__version__.py)：

| 场景 | 版本值 | 是否发布 Release |
|------|--------|------------------|
| 推送 `vMAJOR.MINOR.PATCH` | `v1.0.0` | 是 |
| 推送预发布 `v1.1.0-rc.2`（也支持 `-alpha.N` / `-beta.N`） | `v1.1.0-rc.2` | 是，标记为 prerelease |
| 日常提交 / PR / 手动触发 | `v1.0.0-ci.261110.abcdef1` | 否，仅产出 CI 构建产物 |

- 不读取任何 Release API，也不从上游仓库获取版本号；改起始版本只需修改 `app/common/__version__.py`。
- `.github/workflows/install.yml` 负责编译、组装、打包与发布，产物名为 `mfw-licence-{platform}-{arch}-{tag}.zip/tar.gz`；`cliff.toml` 按同一 tag 规则生成 Release 说明。
- `deploy/install.yml` 是资源包组装模板，共用 `tools/release_meta.py` 的版本规则；两个 workflow 都只引用 `hoey94/mfw-licence` 的 Release 资产。
- 本地校验版本输出：`python3 tools/release_meta.py --ref refs/heads/main --sha <sha> --date 2026-11-10`。

