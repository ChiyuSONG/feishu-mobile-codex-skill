# 飞书远程 Codex

把飞书变成本地 Codex 的轻量手机入口。

你可以连续发任务，不必等上一条完成；电脑离线时消息继续保留，Codex 恢复后会按顺序处理。简单结果直接回复，复杂结果可以生成私有飞书文档。

这是非官方社区项目，与 OpenAI 或飞书不存在隶属或官方合作关系。

## 能做什么

- 在飞书里远程使用本地 Codex；
- 一个群稳定对应一个本地项目，改群名不影响连接；
- 消息排队、断线保留、恢复后自动补处理；
- 短结果直接回复，长内容使用关闭公开分享的飞书文档；
- 打开或恢复 Codex 时自动启动 Listener；
- 默认每小时自动巡检消息状态并报告 Codex 用量；
- 用自然语言修改巡检、设置提醒或定制自己的用法；
- 支持中文和 English，中文体验优先保留。

它不是新的 Agent 平台，也没有管理后台。Codex 仍负责计划、编辑、运行和测试，飞书只是一个流畅、可靠的移动入口。

## 安装

### 1. 安装 Skill

把本仓库地址发给 Codex，让它安装其中的 `skill/feishu-codex-remote`。

也可以手动将 [`skill/feishu-codex-remote`](skill/feishu-codex-remote) 复制到 Codex Skills 目录，然后重启或刷新 Codex。不要把整个仓库复制成一个 Skill。

### 2. 一句话开始配置

在 Codex 中打开准备连接的本地项目，然后说：

> 使用 `$feishu-codex-remote`，把当前项目连接到我的飞书。尽可能自动完成，只在需要我扫码、登录或授权时提醒我。

Codex 会检查环境、创建独立 Python 环境、协助配置飞书应用、建立群聊绑定、安装 Listener 和启动 Hook，并测试完整消息链路。

通常只需要你完成：

1. 扫码或登录飞书；
2. 选择正确的账号或租户并同意授权；
3. 必要时安全粘贴 App Secret；
4. 首次看到 Hook 提示时，审核并信任 `Starting Feishu Remote Listener`。

不需要自己填写 project key、chat ID、thread ID 或 Listener 路径。

## 开始使用

连接成功后，飞书会收到欢迎消息。之后直接在群里发任务即可，例如：

- `检查一下项目里的改动并运行测试`；
- `先记住这三件事，按顺序处理`；
- `把完整结果整理成飞书文档`；
- `明天上午 10 点提醒我提交材料`；
- `暂停自动巡检`；
- `只在巡检发现异常时通知我`。

电脑或 Codex 关闭时不会执行任务，但消息仍留在飞书；下次 Codex 启动后，SessionStart Hook 会启动 Listener 并处理积压消息。

## 自动巡检

默认每小时运行一次，用于检查 Listener、补拉遗漏消息、处理积压并报告 Codex 用量。

第一次巡检会完整说明用途。此后如果用户没有修改，默认只发三句：

> 状态：Listener 正常，待处理 0，处理中 0，失败 0。
>
> Codex 用量：5 小时额度剩余……；7 天额度剩余……。
>
> 提示：可以用自然语言修改巡检、设置提醒或增加其他用法。

巡检会发起一次轻量 Codex 运行并消耗相应额度。内容、频率和是否启用都可以直接用自然语言调整。

## 卸载

不需要运行命令，直接对 Codex 说：

> 卸载飞书远程 Codex。保留飞书对话和文档，先告诉我会移除什么，然后自动完成。

Codex 会先列出准备移除的内容，确认后再卸载本 Skill 添加的 Listener、SessionStart Hook、自动巡检和本地 Skill，不影响其他任务。

默认保留恢复备份，也不会删除飞书对话、群聊或已经生成的飞书文档。如果还要删除本地凭据、缓存和历史状态，请明确补充：`同时清除全部本地数据`。

## 平台与当前状态

- Windows：已有本地回归测试；
- macOS：已完成适配代码和模拟测试，真实 Mac 验收前属于实验性支持；
- Python：3.10 或更高版本；缺少环境时，Codex 会先说明需要的系统级安装。

当前版本适合先放在 Private GitHub 中进行朋友试点。公开发布前仍需完成干净 Windows/macOS、真实飞书扫码和 SessionStart Hook 验收。

## 隐私

- 不需要公网回调或公开服务；
- App Secret 和 OAuth Token 保存在仓库外，Windows 使用 DPAPI，macOS 使用 Keychain；
- 飞书文档关闭公开链接分享并验证设置，失败时不发送链接；
- 一个群只绑定明确指定的本地目录，不根据群名或消息内容猜项目；
- 仓库不应包含私人路径、飞书 ID、凭据、对话或运行状态。

安全问题请参阅 [SECURITY.md](SECURITY.md)。

## CI 是什么

CI 就是让 GitHub 临时使用干净的 Windows 和 macOS 环境替项目自动考试：安装依赖、检查是否混入明显的私人信息或密钥，并运行自动测试。

它不会连接飞书、不会安装到用户电脑，也不会自动发布。普通用户无需操作；当前只能由维护者手动启动。Private 仓库运行 CI 会消耗 GitHub Actions 额度。

## License

采用 [MIT License](LICENSE)：允许个人使用、商业使用、修改和再分发；再分发时保留许可证和版权声明。

## English quick start

Install `skill/feishu-codex-remote`, open the local project in Codex, and say:

> Use `$feishu-codex-remote` to connect this project to my Feishu account. Automate everything safe and ask only when I need to scan, sign in, approve access, or trust the startup hook.
