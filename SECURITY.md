# 安全政策

## 支持范围

| 版本 | 是否接收安全修复 |
|---|---|
| 最新正式发行版 | 是 |
| 更早版本 | 否，请先升级 |

## 报告漏洞

**请不要开公开 issue**，也不要贴出完整的凭据、日志里的 `SESSDATA` / `QQBOT_APP_SECRET` / openid。

- 请通过 GitHub 仓库的 **Security → Report a vulnerability** 私下报告。若入口未启用，可通过 Gitee 私信或维护者公开的邮件地址联系。
- 报告里**不要**贴完整的凭据、日志里的 `SESSDATA` / `QQBOT_APP_SECRET` / openid。

请在报告里尽量写清：

1. 受影响的版本（`VERSION` 文件内容）、操作系统与 Python 版本；
2. 复现步骤与最小样例；
3. 影响面（能否读到本机其它文件、能否伪造推送、能否绕过管理台的令牌校验等）；
4. 如果涉及配置，**请把凭据值替换成 `xxx` 再贴**。

维护者会在 48 小时内确认收到，并在修复发布后致谢（除非你要求匿名）。

## 本项目持有哪些敏感数据

| 数据 | 位置 | 说明 |
|---|---|---|
| B 站登录态 | `config/bili.env`、`config/livedeck.env`、`config/bili_follow.env` | `SESSDATA` 等同账号密码，泄露即可操作该账号 |
| QQ 机器人凭据 | `BBOT.env` | `QQBOT_APP_ID` + `QQBOT_APP_SECRET`，泄露可用你的机器人额度发消息 |
| 推送目标 | `.p0_ids.json` | 你与机器人的 `user_openid` / `group_openid` |
| 运行数据 | `data/`、`logs/` | 数据库、卡片图、缓存图、日志 |

这些文件**全部**已在 `.gitignore` 中；`scripts/release.py check` 会在打包前扫描一遍是否夹带凭据。

## 使用者须知（部署侧）

- 把项目放在**只有你能访问**的目录；`scripts/secure_config.py` 会用 `icacls` 收紧配置目录的 ACL，安装脚本会调用它。
- 管理台只监听 `127.0.0.1`，并且有 CSRF 令牌 + 环回校验；**不要**把它暴露到公网，也不要改绑 `0.0.0.0` 后再做端口转发。
- 定时识别会把**周表截图原图**发给你配置的模型服务（云端 API）。如果不想让图片离开本机，请改用本机 Ollama / LM Studio，或关掉 Agent（见 `docs/33-AI日程识别.md` §6）。
- 分享日志排查问题时，先搜一遍 `SESSDATA` / `AppSecret` / `openid`，替换后再发。
