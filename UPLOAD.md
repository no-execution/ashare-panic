# 上传到自己的Git仓库

本目录已经独立，不需要把上一级 `quant` 推送。创建远端空仓库后，向助手提供仓库的SSH或HTTPS地址即可；不要把访问令牌贴进聊天。

准备上传时在**本目录**执行或交给助手执行：

```bash
git status --short
git add .gitignore pyproject.toml README.md UPLOAD.md VALIDATION.md scripts trade .github
git diff --cached --stat
git diff --cached --check
git commit -m "Initial standalone A-share panic monitor"
git remote add origin <你的仓库地址>
git push -u origin main
```

推送前核对暂存列表不含 `data/`、`.venv/`、原始行情、SQLite或密钥。若远端创建时已添加README等文件，应先获取远端内容并正常合并，不要强制覆盖。

当前没有擅自指定开源许可证。若打算公开并允许别人修改、再分发代码，发布前选择代码许可证；第三方行情的许可单独处理。私有仓库可直接邀请朋友访问。

朋友获得仓库后按README安装并运行 `init-data`，历史数据由其电脑自行下载。
