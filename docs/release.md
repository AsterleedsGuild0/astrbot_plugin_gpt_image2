# 发布流程

本项目通过 GitHub Actions 自动构建插件 zip，并发布到 GitHub Release。

---

## 发布前检查

1. 确认版本号一致：
   - `metadata.yaml` 中的 `version` 使用 `vX.Y.Z`。
   - `pyproject.toml` 中的 `version` 使用 `X.Y.Z`。
   - `CHANGELOG.md` 顶部存在对应 `## vX.Y.Z - YYYY-MM-DD` 小节。
2. 本地运行测试：

```bash
python -m pytest
```

1. 本地验证打包：

```bash
python scripts/package_plugin.py
```

---

## 自动发布

推送版本 tag 会触发 `.github/workflows/release.yml`：

```bash
git tag v0.5.0
git push origin v0.5.0
```

Workflow 会执行以下步骤：

- 安装运行和测试依赖。
- 校验 tag、`metadata.yaml` 与 `pyproject.toml` 版本一致。
- 运行 `python -m pytest`。
- 运行 `python scripts/package_plugin.py` 生成 `dist/*.zip`。
- 从 `CHANGELOG.md` 提取对应 tag 的发布说明。
- 创建或更新 GitHub Release，并上传插件 zip。

---

## 手动触发

如果 tag 已存在，也可以在 GitHub Actions 页面手动运行 `Release` workflow，并填写要发布的 tag，例如 `v0.5.0`。

---

## 手动兜底发布

如果 Actions 不可用，可以使用 GitHub CLI 手动发布：

```bash
python -m pytest
python scripts/package_plugin.py
gh release create v0.5.0 \
  dist/astrbot_plugin_gpt_image2_233-v0.5.0.zip \
  --target master \
  --title "v0.5.0" \
  --notes-file tmp/release-v0.5.0-notes.md
```
