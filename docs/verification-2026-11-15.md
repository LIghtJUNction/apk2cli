# apk2cli 0.1.0 验收报告

日期：2026-11-15，Asia/Taipei（UTC+8）。

**APK 分析、内置 AI 技能、独立 CLI 生成和 `auth --adb` 已实现；Android 35 模拟器完成 12 项端到端检查。** 测试应用通过自己的界面登录，CLI 随后读取该应用已有会话并访问受保护接口。整个流程使用普通 ADB shell，无需 root。

## 代码与证据

| 项目 | 位置 |
| --- | --- |
| 源码仓库 | [LIghtJUNction/apk2cli](https://github.com/LIghtJUNction/apk2cli) |
| 验证代码提交 | [`31484478dd7bef140cfa51ba1ea17345f7206018`](https://github.com/LIghtJUNction/apk2cli/commit/31484478dd7bef140cfa51ba1ea17345f7206018) |
| 完整 CI | [运行 43703011303：全部 5 个任务通过](https://github.com/LIghtJUNction/apk2cli/actions/runs/43703011303) |
| Android 实测日志 | [Android 35 / ADB auth](https://github.com/LIghtJUNction/apk2cli/actions/runs/43703011303/job/132778016200) |
| 原始检查结果 | [android-e2e.json](evidence/android-e2e.json) |
| 登录画面 | [android-e2e.png](evidence/android-e2e.png) |
| 适配格式 | [specification.md](specification.md) |
| 参考项目与许可证 | [references.md](references.md) |

本报告及证据作为文档提交保留；上表中的代码提交是执行测试时的确切版本。

## 已交付功能

`apk2cli init APK --name xxx --out DIR` 创建 AI 工作区，包含 APK 清单分析、输入指纹、任务说明和 `.agents/skills`。三个技能分别负责观察应用、编写 CLI 适配、验证 ADB 登录。AI 编码代理使用这些技能完成应用特定的分析；项目不捆绑模型服务。

`apk2cli build APK --name xxx --spec app.json --out DIR` 根据经过验证的应用契约生成独立 CLI。输出包含自身运行时代码、具名命令、Unix 启动文件和 Windows 启动脚本。路径、查询及 JSON 请求体参数均可声明。构建检查包名、名称和适配声明的 APK 指纹。

`xxx auth --adb` 选择在线且已授权的设备，从指定应用的明确文件和字段读取会话，调用账号接口验证，然后原子保存到本地状态目录。导入失败保留原有效会话。默认通过 `run-as` 读取可调试应用；`--root` 是已有 root 设备的显式选项。

会话读取器支持 SharedPreferences XML、JSON 和普通 SQLite 表。状态输出保留身份与来源信息，隐藏凭据；API 请求拒绝重定向携带凭据。`auth status --verify` 在线核验，`auth logout` 删除本地会话。

项目引用 leanrea 的直接工具分析方法，并实现 REA、Craft Anything、JADX、Apktool 的可选调用适配。核验过的仓库版本、许可证、CLI 参数及 MCP 配置均记录在参考文档。

## 测试结果

| 环境 | 结果 | 范围 |
| --- | --- | --- |
| Linux / Python 3.11 | 56 通过 | 解析、规格、认证、HTTP、工具调用及生成器 |
| Linux / Python 3.14 | 56 通过 | 同上 |
| macOS / Python 3.14 | 56 通过 | 同上，含规范化路径与目录迁移 |
| Windows / Python 3.14 | 45 通过，11 按平台条件跳过 | 跳过 POSIX 权限和依赖 POSIX shebang 的外部工具进程夹具 |
| Android 35 / x86_64 模拟器 | 12 通过 | 真实安装 APK、界面登录、ADB 导入及 HTTP 调用 |

Python 测试同时覆盖了生成目录移位后独立运行、打包后技能与文档可用、拒绝覆盖已有输出、设备状态诊断、凭据格式、会话失效、参数编码和跨来源重定向隔离。外部工具适配测试使用实际子进程夹具验证调用契约；本轮环境未安装 REA、Craft Anything、JADX 或 Apktool，因此没有记录这些上游引擎的真实分析结果。

## Android 实测流程

测试对象是本仓库拥有源码的 `dev.apk2cli.demo`，版本 1.0；使用 Android SDK 构建、签名并安装真实 APK。应用通过可见登录按钮访问本地测试服务，把收到的会话保存到应用私有 SharedPreferences。生成的 CLI 通过 ADB `run-as` 读取这一会话，服务器实际验证每次受保护请求。

| 检查 | 结果 |
| --- | --- |
| 读取真实 APK 的编译版 AndroidManifest | 通过 |
| 生成可独立运行的 CLI | 通过 |
| 未导入会话时拒绝 API 调用 | 通过 |
| Android 应用界面完成 HTTP 登录 | 通过 |
| 服务端拒绝会话时不保存本地认证 | 通过 |
| `auth --adb` 无 root 导入已有会话 | 通过 |
| CLI 成功调用 `/me` 和 `/items` | 通过 |
| 状态输出不暴露导入凭据 | 通过 |
| 服务端撤销会话后请求失败 | 通过 |
| 本地退出后访问被拒绝 | 通过 |
| 重新导入不触发应用再次登录 | 通过 |
| 不可调试 APK 拒绝普通 ADB 读取私有会话 | 通过 |

ADB shell UID 为 `2000`。应用登录次数为 **1**，CLI 产生 **4** 次成功的受保护 HTTP 请求。最新实测运行于 18:15:15 至 18:15:34（UTC+8）；这段时间覆盖测试流程，不包含模拟器启动和 APK 构建，不作为登录性能基准。

`/me` 与 `/items` 的契约来自本仓库测试服务源码，均已通过 HTTP 实际调用。该测试验证工作流、生成器和会话导入机制；它没有自动推导某个第三方应用的全部接口。

## 复现

安装项目后创建工作区：

```sh
python -m pip install .
apk2cli init /absolute/path/app.apk --name xxx --out work/xxx
```

在编码代理中打开生成目录，按 `TASK.md` 和三个技能分析应用并生成 `app.json`。已有经验证适配时可直接构建和登录：

```sh
apk2cli validate app.json
apk2cli build app.apk --name xxx --spec app.json --out dist/xxx
python dist/xxx/xxx auth --adb --serial DEVICE_SERIAL
python dist/xxx/xxx auth status --verify
python dist/xxx/xxx me
```

在配置好 Android SDK 和测试设备的环境复现端到端检查：

```sh
bash scripts/build_demo_apk.sh
python scripts/android_e2e.py \
  --apk build/android-demo/demo-debug.apk \
  --release-apk build/android-demo/demo-release.apk \
  --report build/android-e2e.json \
  --screenshot build/android-e2e.png
```

此脚本会安装测试包并清除 `dev.apk2cli.demo` 的应用数据；请选择测试设备。

## 适用范围

本轮没有用户提供的目标 APK 或实体手机，实测对象为自有测试应用和 Android 模拟器。针对具体应用，需要由 AI 根据 APK、授权设备或可用源码确定认证字段、请求格式和接口，再运行相同验收流程。

普通发行版应用的私有数据受 Android 沙箱保护。默认读取路径要求应用可调试；已有 root 设备可显式选择 `--root`。root 路径本轮有命令约束测试，未进行实体 root 手机实测。Keystore 加密、设备绑定令牌、额外签名或硬件认证需要针对应用扩展。SQLite 读取限于主数据库快照，不合并活动 WAL。

## 结论核验

| 核验点 | 证据与结论 |
| --- | --- |
| 功能交付 | 源码、技能、生成器及 ADB 认证均有对应实现与可执行测试 |
| 可复现性 | 安装、工作流、适配格式及端到端命令已给出 |
| 实验依据 | 保留真实 APK/模拟器/ADB/HTTP 证据，区分进程夹具与设备实测 |
| 覆盖范围 | 已列出平台跳过项、第三方应用与实体手机的未测试范围 |
| 设计条件 | 成功登录依赖具体应用会话可读且服务端接受，验证发生在保存之前 |
