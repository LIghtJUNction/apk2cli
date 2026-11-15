# 参考项目与集成边界

apk2cli 将 APK 证据整理、应用适配说明和可运行 CLI 分开处理。内置技能负责指导 AI
查清应用行为；可选分析工具提供原始证据；生成器依据明确的适配说明生成命令。一个
分析工具正常退出，只能说明该进程执行完毕，不能证明接口、登录态或生成的命令可用。

## 三个项目各自承担什么

| 项目 | apk2cli 中的用途 | 集成方式 | 上游许可证 |
| --- | --- | --- | --- |
| [leanrea](https://github.com/aicodeking/leanrea) | 先做低成本盘点，再用本地工具定位行为，并区分观察与推断 | `.agents/skills/inspect-apk` 的工作流程引用；可调用 `jadx`、`apktool` | MIT |
| [REA](https://github.com/morluto/rea) | 对 APK 类、方法、Manifest 和设备状态提供分析证据 | 可选本地 `rea` CLI，或用户自行配置 MCP | MIT |
| [Craft Anything](https://github.com/storytold/craft-anything) | APK 结构分析，以及分析、构建、对照验证的工作流程 | 可选本地 `craft-anything-cli`，或用户自行配置 MCP | MIT OR Apache-2.0 |

leanrea 本身是一份技能文件，没有需要启动的 `leanrea` 可执行程序。REA 的 npm
包名是 `rea-agents`，安装后提供 `rea` 命令。Craft Anything 的 CLI 名称是
`craft-anything-cli`。[leanrea 技能][lean-skill]、[REA 安装说明][rea-install]和
[Craft README][craft-readme]给出了各自的使用入口。

本项目没有复制三个项目的实现源码，也没有自动下载或安装它们。使用某个工具前，先
按照其上游说明准备本地依赖。REA 的 Android 反编译还需要其文档指定的 JADX 引擎和
JDK；仅在 PATH 中找到 `rea` 不代表这些依赖已经可用。[REA Android 文档][rea-android]
解释了这些前提。Craft 的 `android_package_show` 使用内置 APK/AXML/DEX 读取器，
这一操作不要求安装 Apktool。[对应实现][craft-android]

## 可执行集成

```sh
apk2cli tools doctor
apk2cli tools run rea ./app.apk --out ./evidence
apk2cli tools run craft-anything ./app.apk --out ./evidence
apk2cli tools run jadx ./app.apk --out ./evidence
apk2cli tools run apktool ./app.apk --out ./evidence
```

`doctor` 只检查可执行文件是否在 PATH 中，不启动分析工具、不连接设备。`run` 仅执行
指定工具，为每次运行创建独立证据目录。适配器将 APK 复制到该目录，记录这份副本的
SHA-256，并把同一副本传给工具，因此记录的哈希对应实际分析的字节。

| 选择的工具 | 实际命令结构 |
| --- | --- |
| `rea` | `rea inspect-android-package INPUT.apk --json` |
| `craft-anything` | `craft-anything-cli android-package-show --json '{"path":"INPUT.apk"}' --output-format json` |
| `jadx` | `jadx -d OUTPUT/decompiled --show-bad-code INPUT.apk` |
| `apktool` | `apktool d --no-src -o OUTPUT/resources INPUT.apk` |

这里的 `INPUT.apk` 和 `OUTPUT` 由运行器替换为绝对路径，并通过参数数组传入，不经
shell 解释。尤其注意 Craft 的 `--json` 是**输入参数**；JSON 输出由
`--output-format json` 选择。这一差异来自其 [CLI 参数解析器][craft-args]。

每个运行默认限时 120 秒，stdout、stderr 分别最多保存 2 MiB。超过限制时终止分析，
并记录 `timeout` 或 `output_limit`；工具非零退出记录为 `failed`，不能启动记录为
`spawn_failed`。日志正文不会直接包含在命令返回对象中。证据目录在 POSIX 系统下为
`0700`，APK 副本、日志和结果文件为 `0600`。外部工具生成的反编译目录包含在这个私有
目录内。Windows 上仍需由当前账户的文件 ACL 保障相同的访问范围。

分析输出可能包含应用常量或私人内容，不能直接提交到公共仓库。报告应只保留所需的
来源位置、摘要和脱敏结果。运行器限制的是日志和运行时间，不是外部反编译器生成的
全部文件大小；大型 APK 仍需要足够的本地磁盘空间。

## 可选 MCP 配置

[mcp.example.json](mcp.example.json) 展示如何启动已经安装的两个 MCP 服务。只有在
使用的 AI 客户端支持该配置结构时，才把相应条目合并到客户端配置。这个示例不会被
apk2cli 自动启用，也不会调用 npm 安装程序。leanrea 无需 MCP 注册。

## ADB 登录的实际条件

`xxx auth --adb` 的含义是读取**已明确配置的应用会话来源**，然后向该应用服务发起
只读身份验证；验证成功后才保存 CLI 会话。连接 ADB、找到应用包、读到一个字符串，
均不等于应用服务已经接受登录。来源文件、字段、请求头和身份接口必须由 APK 分析或
应用文档确定，不能由通用工具猜测。

Android 的应用私有目录有独立访问权限。普通未 root 设备上的 ADB 并不能任意读取
所有应用的私有数据；`run-as` 需要应用支持调试。用户已经授权的 root 设备可使用显式
root 路径，但加密存储、硬件绑定会话等仍可能使直接迁移无效。Android 官方的
[调试文档][android-debug]和 [ADB 文档][android-adb]说明了设备授权与访问限制。
Android 12 对目标 API 31 及以上应用还限制了 `adb backup` 的应用数据导出；不能把它
作为普遍可用的登录后备方案。[Android 12 行为变更][android-backup]

REA 和 Craft 的 ADB 能力提供设备、包、进程与文件的检查及传输接口，并未承诺自动把
任意应用的登录态变成独立 CLI 登录。它们的作用和限制可分别核对
[REA ADB 指南][rea-adb]和 [Craft 工具清单][craft-tools]。apk2cli 的应用适配说明
负责补上具体会话来源和服务验证步骤。

## 验证结果如何表述

| 证据类型 | 能证明什么 | 不能推出什么 |
| --- | --- | --- |
| 静态 APK 分析 | 所选 APK 中确实存在某个声明、方法或字符串 | 服务器仍接受该接口、该分支实际执行过 |
| 适配器进程测试 | 参数传递、超时、日志限制、错误保存正确 | REA/JADX/Craft 本体已在本机成功分析真实 APK |
| 模拟 ADB + HTTP 测试服务 | 登录、验证、保存和请求链路符合协议 | Android 权限、真实设备连接或生产服务兼容 |
| Android 模拟器实测 | 安装的 APK 在真实 Android 系统中的会话读取和验证 | 所有品牌手机或所有第三方应用都兼容 |
| 物理手机实测 | 报告指定手机、Android 版本与 APK 的观测结果 | 未测试应用、版本或设备的兼容性 |

提交的测试报告必须写清使用哪一级证据，给出 APK 哈希、运行环境、执行命令和结果。
缺少设备时，应明确标注设备实测未完成。上游项目自行公布的速度或成本数据不属于
apk2cli 的性能测试结果，本项目不据此声明性能优势。

## 引用版本与许可

2026-11-15 核对的上游源码修订如下。它们固定本文依据，不会强制用户安装某一版本，
也不代表相同修订已经发布为安装包。

| 项目 | 修订 | 许可原文 |
| --- | --- | --- |
| leanrea | `09ba4aa0217f82cd880f855a60b877f59f39b22b` | [MIT](https://github.com/aicodeking/leanrea/blob/09ba4aa0217f82cd880f855a60b877f59f39b22b/LICENSE) |
| REA | `b3cdbb92ee81d00787dd87aefa43c1cdacbe779c` | [MIT](https://github.com/morluto/rea/blob/b3cdbb92ee81d00787dd87aefa43c1cdacbe779c/LICENSE) |
| Craft Anything | `1d8a3b4f60ac8b6cd01b0609cbf2d4d6f65b3da8` | [MIT](https://github.com/storytold/craft-anything/blob/1d8a3b4f60ac8b6cd01b0609cbf2d4d6f65b3da8/LICENSE-MIT) / [Apache-2.0](https://github.com/storytold/craft-anything/blob/1d8a3b4f60ac8b6cd01b0609cbf2d4d6f65b3da8/LICENSE-APACHE) |

leanrea 的版权声明为 `Copyright (c) 2026 AICodeKing`，REA 为
`Copyright (c) 2026 morluto`。本项目仅作方法与接口引用；今后如果复制上游代码或技能
正文，需要同时保留其适用许可。Craft 的图片、字体和其他资产另有归属说明，本项目
不包含这些资产。[Craft 许可说明][craft-license]

[lean-skill]: https://github.com/aicodeking/leanrea/blob/09ba4aa0217f82cd880f855a60b877f59f39b22b/leanrea/SKILL.md
[rea-install]: https://github.com/morluto/rea/blob/b3cdbb92ee81d00787dd87aefa43c1cdacbe779c/docs/getting-started/installation.md
[rea-android]: https://github.com/morluto/rea/blob/b3cdbb92ee81d00787dd87aefa43c1cdacbe779c/docs/guides/android/android-analysis.md
[rea-adb]: https://github.com/morluto/rea/blob/b3cdbb92ee81d00787dd87aefa43c1cdacbe779c/docs/guides/android/adb-device-analysis.md
[craft-readme]: https://github.com/storytold/craft-anything/blob/1d8a3b4f60ac8b6cd01b0609cbf2d4d6f65b3da8/README.md
[craft-tools]: https://github.com/storytold/craft-anything/blob/1d8a3b4f60ac8b6cd01b0609cbf2d4d6f65b3da8/docs/tools.md
[craft-args]: https://github.com/storytold/craft-anything/blob/1d8a3b4f60ac8b6cd01b0609cbf2d4d6f65b3da8/apps/craft-anything-cli/src/tool_args.rs
[craft-android]: https://github.com/storytold/craft-anything/blob/1d8a3b4f60ac8b6cd01b0609cbf2d4d6f65b3da8/crates/engine/src/tools/android_tools.rs
[craft-license]: https://github.com/storytold/craft-anything/blob/1d8a3b4f60ac8b6cd01b0609cbf2d4d6f65b3da8/docs/licences.md
[android-debug]: https://developer.android.com/studio/debug/
[android-adb]: https://developer.android.com/tools/adb
[android-backup]: https://developer.android.com/about/versions/12/behavior-changes-12
