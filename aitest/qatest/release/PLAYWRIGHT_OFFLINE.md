## 目标

只把 Playwright（Python 包 + 依赖 wheel）打成离线包，发给客户在本地离线安装；不打整个项目，不做客户端安装包。

## 你这边（能联网的机器）生成离线包

在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release\build_playwright_offline_bundle.ps1
```

默认会优先从本机 Playwright 缓存目录复制浏览器（常见路径：`%LOCALAPPDATA%\ms-playwright`）。如果缓存里已有 `chromium-*`，离线包里就会把浏览器一起带上，不需要再下载。

注意：Playwright 的 headless 模式通常还会用到 `chromium_headless_shell-*`（以及可能的 `ffmpeg-*`）。如果缓存里只有 `chromium-*` 没有 `chromium_headless_shell-*`，客户那边 headless 启动仍可能失败，这种情况下必须在一台能正常下载 Playwright 浏览器的机器上先执行一次下载后再打包。

### 推荐最稳的做法（先补齐浏览器，再打包）

1）先把缺失浏览器下载到项目根目录的 `.playwright/`（此目录可直接打包复用）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release\download_playwright_browsers.ps1 -Browser chromium -BrowsersPath .\.playwright -PlaywrightDownloadHost https://npmmirror.com/mirrors/playwright
```

2）确认你本机项目当前实际使用的 Playwright 版本（AI 运行会跟它一致）：

```powershell
.\.venv\Scripts\python.exe .\release\print_playwright_version.py
```

3）用同版本打包（避免“Python 包版本”和“浏览器版本”不匹配）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release\build_playwright_offline_bundle.ps1 -PlaywrightVersion 1.57.0 -BrowsersSourcePath .\.playwright
```

如果你想指定浏览器缓存来源（比如你已经从别的机器拷贝了一份 ms-playwright），可以显式指定：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release\build_playwright_offline_bundle.ps1 -BrowsersSourcePath "D:\ms-playwright"
```

如果你这边也下不到浏览器（chromium），先只打 wheel：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release\build_playwright_offline_bundle.ps1 -SkipBrowserDownload
```

产物在 `dist/` 下：`playwright_offline_bundle_windows_*.zip`

## 客户机器离线安装

1）解压你给的 zip，得到：

- `wheelhouse/`（离线 wheels）
- `.playwright/`（浏览器目录；如果你生成时跳过了下载，这里会是空的）
- `install_playwright_offline.ps1`

2）在客户的项目根目录（有 `.venv` 的那份源码目录）执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_playwright_offline.ps1 -BundleRoot <解压后的目录> -ProjectRoot <客户项目根目录>
```

这个脚本会：

- 把 bundle 里的 `.playwright/` 拷贝到客户项目根目录下的 `.playwright/`
- 从 bundle 里的 `wheelhouse/` 离线安装 `playwright`
- 做一次 headless 启动自检

## 浏览器还是下不到怎么办

Playwright 的 Python 包离线装好后，真正卡的通常是 `python -m playwright install chromium` 下载浏览器。

解决思路只有两条：

1）在一台能下载的机器把 `.playwright/` 准备好（脚本不加 `-SkipBrowserDownload`），然后把 zip 发给客户；客户直接复用即可。

2）公司内有镜像的话，生成离线包时指定：`-PlaywrightDownloadHost <你们的镜像地址>`。

## Linux/服务器注意事项（很常见）

1）Windows 打出来的 `.playwright/` 不能直接拿到 Linux 服务器用（平台不一致，必然启动失败）。

2）Linux 上建议用以下顺序准备（示例以项目根目录为工作目录）：

- 安装 Python 包（用虚拟环境 python 执行，避免跑到系统/用户级 playwright.exe）：
  - `./.venv/bin/python -m pip install -r requirements.txt`
- 下载浏览器到项目目录（或统一目录 `/ms-playwright`）：
  - `PLAYWRIGHT_BROWSERS_PATH=./.playwright ./.venv/bin/python -m playwright install chromium chromium-headless-shell ffmpeg`
- 安装 Linux 依赖（缺库会导致“Browser has been closed/Target closed”等类似错误）：
  - `./.venv/bin/python -m playwright install-deps chromium`

3）如果你在 Docker/容器或 root 用户下运行，需要 `--no-sandbox`。本项目的“系统自检”与执行器已经默认加了该参数；如果你自写脚本启动浏览器，也要加。
