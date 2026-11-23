# 外部资源包引用与动态切换指南

本文档记录主程序（`hoey94/mfw-licence`）如何在构建时从**外部仓库的 Release** 下载资源包并注入产物根目录，以及如何动态切换不同的资源仓库（例如从 `mfw-xs` 切换到 `mfw-ss`）。

---

## 1. 背景与架构

主程序本身只打包 GUI 框架与原生库，**不内置业务资源**。运行时资源（`interface.json` + `resource/`）通过 GitHub Actions 在构建阶段从外部资源仓库下载并放到产物根目录。

```
资源仓库 (private)                主程序仓库
hoey94/mfw-xs                    hoey94/mfw-licence
└─ Release v1.0.0                 └─ Actions 构建时
   ├─ mfw-xs-resource-v1.0.0.zip      ① 读取 vars.RESOURCE_* 配置
   └─ mfw-xs-resource-v1.0.0.tar.gz   ② 用 PAT 下载对应平台的包
                                      ③ 解压并注入 build/mfw-release/
                                      ④ 产物根目录出现 interface.json + resource/
```

运行时无需任何改动：程序启动时会把工作目录切换到发行根，`InterfaceManager` 自动读取根目录的 `interface.json`，其中 `"resource[].path": ["{PROJECT_DIR}/resource"]` 正好指向根目录的 `resource/`。

### 相关文件

| 文件 | 作用 |
|---|---|
| `.github/workflows/install.yml` | 主构建流程；`Download resource bundle` 步骤下载资源，三个 `Inject resource bundle` 步骤注入资源 |
| `docs/external-resource-bundle.md` | 本文档 |

---

## 2. 主程序仓库的配置项

在 `hoey94/mfw-licence` → Settings → Secrets and variables → Actions 配置：

### Variables（仓库变量）

| 变量名 | 示例值 | 作用 |
|---|---|---|
| `RESOURCE_ENABLED` | `true` | 总开关；`true` 才下载并注入资源，其他值则跳过（保持纯框架构建） |
| `RESOURCE_REPO` | `hoey94/mfw-xs` | 资源仓库的 `owner/repo` |
| `RESOURCE_TAG` | `v1.0.0` | 使用资源仓库的哪个 Release tag（固定版本，保证可复现） |

### Secrets（密钥）

| Secret 名 | 作用 |
|---|---|
| `RESOURCE_TOKEN` | 可读取私有资源仓库 Release 的 PAT。因为 `GITHUB_TOKEN` 是仓库级的，默认无法跨仓库读取私有仓库，必须用 PAT |

### PAT 创建方式（推荐 fine-grained）

1. 右上角头像 → Settings → Developer settings → Personal access tokens → Generate new token
2. Token name：如 `resource-bundle-reader`
3. Resource owner：`hoey94`
4. Repository access：Only select repositories → 勾选需要读取的资源仓库（如 `mfw-xs`）
5. Permissions → Repository permissions → `Contents`: **Read-only**
6. 生成后填入 `RESOURCE_TOKEN` Secret

> 注意：切换到新的资源仓库时，如果新仓库不在 PAT 的授权范围内，需要重新生成 PAT 或把新仓库加入授权。Fine-grained token 创建后可随时编辑授权仓库列表。

---

## 3. 资源仓库的搭建规范

以 `mfw-xs` 为例，资源仓库必须满足以下约定，才能被主程序正确消费。

### 3.1 目录结构（保持原层级）

```
mfw-xs/
├─ assets/
│  ├─ interface.json          ← 必须有，程序入口配置
│  └─ resource/               ← 资源目录，interface.json 中引用
│     ├─ image/
│     ├─ model/
│     └─ pipeline/
└─ .github/workflows/release.yml
```

关键：`interface.json` 中资源路径写 `"path": ["{PROJECT_DIR}/resource"]`，指向**产物根目录**的 `resource/`。因此压缩包解压后，`interface.json` 和 `resource/` 必须位于压缩包**根目录**，不能多套一层 `assets/`。

### 3.2 打包 workflow（`.github/workflows/release.yml`）

逻辑要点：

```yaml
on:
  push:
    tags: ["v*"]       # 打 tag 触发

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      # 关键：working-directory: assets，使压缩包根目录直接是 interface.json + resource/
      - name: Package zip (for Windows)
        working-directory: assets
        run: |
          tag="${GITHUB_REF_NAME}"
          zip -qr "../mfw-xs-resource-${tag}.zip" interface.json resource

      - name: Package tar.gz (for Linux/macOS)
        working-directory: assets
        run: |
          tag="${GITHUB_REF_NAME}"
          tar -czf "../mfw-xs-resource-${tag}.tar.gz" interface.json resource

      - uses: softprops/action-gh-release@v2
        with:
          files: |
            mfw-xs-resource-*.zip
            mfw-xs-resource-*.tar.gz
```

发布流程：修改资源 → commit → push → 打 tag（如 `v1.0.0`）→ 自动发布 Release，包含 zip 与 tar.gz 两个产物。

### 3.3 压缩包命名与平台映射

主程序按平台选择下载格式：

| 平台 | 下载的文件名 |
|---|---|
| Windows | `mfw-xs-resource-<TAG>.zip` |
| Linux / macOS | `mfw-xs-resource-<TAG>.tar.gz` |

文件名由 `install.yml` 中的表达式拼接：

```yaml
fileName: mfw-xs-resource-${{ vars.RESOURCE_TAG }}${{ matrix.platform == 'win' && '.zip' || '.tar.gz' }}
```

---

## 4. 动态切换到另一个资源仓库（如 mfw-ss）

> 这是最关注的场景：今天依赖 `mfw-xs`，明天可能换成 `mfw-ss` 或其他资源包。

### 4.1 切换清单

切换资源仓库需要改动 **3 个 Variables**，必要时再加 **1 个 PAT 授权**：

| 步骤 | 操作 |
|---|---|
| 1 | 新仓库（如 `mfw-ss`）按第 3 节规范搭建，发布一个 Release（如 `v1.0.0`） |
| 2 | 确认 `RESOURCE_TOKEN` 的 PAT 已授权读取新仓库（fine-grained token 编辑授权列表） |
| 3 | 修改 `mfw-licence` 的变量 `RESOURCE_REPO` → `hoey94/mfw-ss` |
| 4 | 修改 `mfw-licence` 的变量 `RESOURCE_TAG` → 新仓库的 tag（如 `v1.0.0`） |
| 5 | 保持 `RESOURCE_ENABLED = true` |
| 6 | 重新构建（打 tag 或手动 Run workflow） |

### 4.2 关于文件名前缀的注意事项（重要）

当前 `install.yml` 的下载文件名前缀 **`mfw-xs-resource-` 是写死的**（见 3.3 节表达式）。切换到 `mfw-ss` 时有两种处理方式：

**方式 A（不改代码，最快）**：让新仓库 `mfw-ss` 的打包 workflow **仍然使用 `mfw-xs-resource-<tag>.zip` 这个前缀命名产物**。这样无需改主程序，只需改 `RESOURCE_REPO` 和 `RESOURCE_TAG` 两个变量即可切换。缺点是命名与新仓库名不一致，仅内部使用无碍。

**方式 B（推荐，真正通用）**：把前缀也做成变量，一劳永逸支持任意资源仓库。改动如下：

1. 新增 Variable `RESOURCE_ASSET_PREFIX`，值为资源包前缀（如 `mfw-xs-resource`、`mfw-ss-resource`）
2. 把 `install.yml` 的 `fileName` 改为：

```yaml
fileName: ${{ vars.RESOURCE_ASSET_PREFIX }}-${{ vars.RESOURCE_TAG }}${{ matrix.platform == 'win' && '.zip' || '.tar.gz' }}
```

之后切换资源仓库只需改 3 个变量（`RESOURCE_REPO` / `RESOURCE_TAG` / `RESOURCE_ASSET_PREFIX`），无需改代码。每个资源仓库用自己的前缀命名产物即可。

> 当前实际配置：采用方式 A（前缀写死 `mfw-xs-resource-`）。若后续引入多个不同前缀的资源仓库，建议按方式 B 改造。

### 4.3 关闭资源注入

若某次构建只需要纯框架（不含资源），把 `RESOURCE_ENABLED` 改为 `false`（或任意非 `true` 的值）。此时下载与注入步骤都会被跳过，构建流程与未引入外部资源前完全一致。

---

## 5. 日常更新资源版本

同一资源仓库内更新资源（不改仓库）：

1. 在资源仓库工作区修改 `assets/interface.json` 或 `assets/resource/`
2. commit → push
3. 打新 tag（递增，如 `v1.0.0` → `v1.0.1`）→ 自动生成新 Release
4. 把 `mfw-licence` 的 `RESOURCE_TAG` 改为新 tag
5. 重新构建

`RESOURCE_TAG` 固定版本的设计保证可复现：不会因为资源仓库发了新版本，导致旧 tag 的主程序重新构建时意外使用新资源。

---

## 6. 验证方法

### 6.1 构建日志检查

在 `mfw-licence` 的 Actions run 中确认：

1. `Download resource bundle` 步骤：
   ```
   Fetching release v1.0.0 from repo hoey94/mfw-xs
   Found release tag: v1.0.0
   Downloading file: mfw-xs-resource-v1.0.0.zip ...
   Extracted ... to build/resource-bundle
   ```
   出现 `DeprecationWarning: url.parse()` 是 Node.js 的弃用警告，可忽略。

2. `Inject resource bundle (Windows/Linux/macOS)` 步骤：打印发行根文件列表，应包含 `interface.json` 与 `resource`。

### 6.2 产物检查

下载 Release 产物并解压，确认根目录包含：

```
interface.json
resource/
  ├─ image/
  ├─ model/
  └─ pipeline/
MFW.exe          (Windows) / MFW (Linux) / MFW.app (macOS)
maafw/
file_list.txt
```

`file_list.txt` 由 `Generate distribution file list` 在资源注入**之后**生成，因此会包含资源文件，更新迁移逻辑可正确识别。

---

## 7. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `Download resource bundle` 报 404 / Not Found | PAT 无权限或 `RESOURCE_REPO` 拼写错误 | 检查 PAT 授权仓库列表；核对 `RESOURCE_REPO` |
| 下载的文件名不匹配 | `RESOURCE_TAG` 与资源仓库 tag 不一致，或前缀不符 | 核对资源仓库 Release 的实际文件名；参考 4.2 节 |
| 注入步骤报 `missing interface.json` | 压缩包内多套了 `assets/` 目录 | 确认资源仓库打包时用了 `working-directory: assets` |
| 运行时找不到资源 | `interface.json` 的路径配置错误 | 确认 `resource[].path` 为 `{PROJECT_DIR}/resource`，且 `bundle.path` 为 `./` |

---

## 8. 已验证记录

| 日期 | 主程序版本 | 资源仓库 | 资源版本 | 结果 |
|---|---|---|---|---|
| 2026-11-23 | `v1.2.1` (commit `f6667d4`) | `hoey94/mfw-xs` (private) | `v1.0.0` | 通过：Windows 产物根目录含 `interface.json` + `resource/` |

验证方式：`v1.2.1` tag push 触发完整 Release 构建，下载 `meiyue-assistant-win-x86_64` 产物，确认根目录结构正确。

---

## 9. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-23 | 建立外部资源包机制；新增本文档 |
