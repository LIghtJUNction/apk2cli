# apk2cli

把 Android APK 的实际行为整理成可验证的命令行工具。项目内置 `.agents/skills`，供 AI 编码代理完成分析、编写应用适配、生成 CLI 和验证；确定性的解析、构建、ADB 登录及 HTTP 请求由 Python 标准库实现。

生成的 CLI 必须支持：

```sh
xxx auth --adb
xxx auth status --verify
xxx --help
xxx auth logout
```

`auth --adb` 从已连接手机上、已经登录的指定应用读取适配中声明的会话字段，调用账号接口验证，成功后保存本地会话。后续命令直接调用已配置的 HTTP API。

## 安装

需要 Python 3.11 或更高版本。登录还需要 [Android SDK Platform Tools](https://developer.android.com/tools/releases/platform-tools)。

```sh
git clone https://github.com/LIghtJUNction/apk2cli.git
cd apk2cli
python -m pip install .
apk2cli --help
```

运行时没有第三方 Python 依赖。AI 分析阶段可以按需使用已安装的 REA、Craft Anything、JADX 或 Apktool；`apk2cli tools doctor` 显示可用性及来源。

## 输入 APK，启动 AI 工作流

```sh
apk2cli init /absolute/path/app.apk --name xxx --out work/xxx
```

在 AI 编码代理中打开 `work/xxx`，要求它按 `TASK.md` 和内置技能完成该 APK 的命令行适配。工作区包含包名、版本、权限、启动 Activity、DEX/ABI 清单、待验证 URL、APK 指纹、三个技能及适配格式说明。APK 本身保留在原位置。

| 技能 | 交付内容 |
| --- | --- |
| `inspect-apk` | 分析应用，记录接口、登录态位置和行为证据 |
| `build-apk-cli` | 编写并验证 `app.json`，生成独立 CLI |
| `verify-adb-auth` | 测试已有会话导入、真实请求、失效和退出 |

项目使用当前编码代理执行这些技能，不捆绑模型服务或 API 密钥。APK 中的 URL 只是分析线索；技能要求观察请求和认证行为后再编写适配。

## 已有适配时直接生成

```sh
apk2cli inspect app.apk --json
apk2cli validate app.json
apk2cli build app.apk --name xxx --spec app.json --out dist/xxx

python dist/xxx/xxx auth --adb
python dist/xxx/xxx auth status --verify
python dist/xxx/xxx me
```

将生成目录加入 `PATH` 后可直接运行 `xxx`；Windows 使用同目录的 `xxx.cmd`。生成目录包含自己的运行时代码，移动整个目录后仍可执行，无需安装 apk2cli。

适配支持具名命令、路径参数、查询参数和 JSON 请求体。完整格式及例子见 [适配说明](docs/specification.md)。构建会检查包名、CLI 名称；适配声明 `apk_sha256` 时还会拒绝与分析版本不符的 APK。

## ADB 登录的条件

手机需开启 USB 调试并授权当前电脑，目标应用已经登录。多设备时选择设备：

```sh
xxx auth --adb --serial DEVICE_SERIAL
```

默认通过 Android `run-as` 读取可调试应用。对于普通发行版应用，需要设备已有 root 权限并显式选择：

```sh
xxx auth --adb --root --serial DEVICE_SERIAL
```

Android 的应用沙箱限制普通 ADB 访问发行版应用的私有数据；单独连接 ADB 不会解除这项限制。加密存储、Android Keystore 绑定或额外设备签名仍需要针对应用实现适配，当前通用读取器支持明文 SharedPreferences、JSON 和普通 SQLite 表。详细依据见 [参考项目与 Android 文档](docs/references.md)。

登录只读取适配中列出的文件和键。账号验证失败时不会保存新会话，也不会覆盖原有可用会话。会话位于用户状态目录，POSIX 下目录权限为 `0700`、文件为 `0600`；可用 `APK2CLI_STATE_DIR` 或 `--state-dir` 指定。Windows 使用当前账户的目录 ACL。日志和状态输出不包含导入的凭据。

`auth status` 展示本地会话信息，`--verify` 会向服务端确认。`auth logout` 删除 CLI 本地会话，手机应用的登录状态保留。服务端撤销会话后，API 请求会失败并提示重新导入。

## 参考项目

- [leanrea](https://github.com/aicodeking/leanrea)：直接使用常规工具的分析技能与证据组织方式。
- [REA](https://github.com/morluto/rea)：可选 APK/ADB 分析 CLI 与 MCP 工具。
- [Craft Anything](https://github.com/storytold/craft-anything)：可选 APK 分析及观察、重建、比较的工作流。

仓库记录了核验版本、许可证和实际调用参数，见 [references.md](docs/references.md)。这些项目提供分析能力，apk2cli 实现应用适配契约、CLI 生成和验证后的 ADB 会话导入。

```sh
apk2cli tools doctor
apk2cli tools run rea app.apk --out work/evidence
apk2cli tools run craft-anything app.apk --out work/evidence
apk2cli tools run jadx app.apk --out work/evidence
```

工具输出可能包含应用内容，只保存到本地独立证据目录。默认不安装外部工具，也不上传 APK、分析日志或凭据。

## 测试

```sh
python -m unittest discover -s tests -v
```

CI 在 Linux、macOS、Windows 运行 Python 测试，并在 Android 35 模拟器运行完整流程：构建真实 APK → 安装 → 在应用界面登录测试服务 → 生成 CLI → `auth --adb` → 访问受保护接口 → 撤销/退出检查。模拟器测试使用普通 ADB shell UID 2000，并检查不可调试 APK 的失败行为。

有 Android SDK 和已连接测试设备时，也可执行同一流程。测试会安装并清除测试包 `dev.apk2cli.demo` 的数据：

```sh
bash scripts/build_demo_apk.sh
python scripts/android_e2e.py \
  --apk build/android-demo/demo-debug.apk \
  --release-apk build/android-demo/demo-release.apk \
  --report build/android-e2e.json \
  --screenshot build/android-e2e.png
```

测试报告分别标注本地自动测试、真实 Android 模拟器和实体手机。已完成的结果见 [2026-11-15 验收报告](docs/verification-2026-11-15.md)及 [GitHub Actions](https://github.com/LIghtJUNction/apk2cli/actions)。

## 开发约定

项目内技能与源码一起管理。新增适配必须有对应应用/接口证据与真实命令验证，不使用模拟结果替代手机实测。只提交经过脱敏的测试记录；应用文件、会话和私有分析日志保持在工作目录。

MIT License。
