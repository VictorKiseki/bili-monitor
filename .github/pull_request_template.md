## 改了什么

<!-- 一句话概括；涉及多个模块时按模块列 -->

## 为什么

<!-- 遇到的问题 / 动机。修复类请写清触发条件 -->

## 怎么验证

<!-- 命令 + 关键输出，或管理台上的操作步骤 -->

```
python -m unittest discover -s tests -p "test_*.py"
python scripts/release.py check
```

## 自查

- [ ] 单元测试全绿
- [ ] `scripts/release.py check` 输出「干净，可以 build」
- [ ] 新增/修改的行为有离线用例
- [ ] 新路由 / 新配置项已写进 `docs/12` 或 `docs/31`
- [ ] `CHANGELOG.md` 的 `[Unreleased]` 有对应条目
- [ ] 没有提交任何真实凭据（`BBOT.env` / `config/*.env` / `.p0_ids.json` / `data/` / `logs/`）
- [ ] 改了 PowerShell 脚本的话，仍是 UTF-8 with BOM
