"""QQ 官方机器人的接口地址与 intents（2026-11-14，审查细节 10）。

这三个常量以前在 channels/official_qq.py（TOKEN_URL / API）、
channels/qq_inbox.py（API / TOKEN_URL / INTENTS）、channels/qq_setup.py
（TOKEN_API / API / INTENTS）各写了一份，连名字都不统一。域名一旦变，
必须记得三处都改。这里只放常量，不 import 任何业务模块，三方都能引用。
"""
from __future__ import annotations

#: 取 access_token（AppID + AppSecret → access_token，有效期 7200 秒）
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

#: 开放平台 API 根地址（/users/@me、/v2/users/{openid}/messages …）
API = "https://api.sgroup.qq.com"

#: GROUP_AND_C2C_EVENT：群与私聊消息事件（收 openid 只要这一个）
INTENTS = 1 << 25
