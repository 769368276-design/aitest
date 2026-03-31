# Playwright 离线依赖包（Windows）

适用场景：客户已有源码、Python 环境和项目其它依赖都装好了，但因为网络原因无法执行 `python -m playwright install chromium`（下载浏览器失败）。本方案只打包 Playwright 所需的 **Python wheels** + **浏览器缓存目录 .playwright**，不做整项目打包、不做客户端安装包。

## 生成离线包（在一台能联网下载的机器上）

在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release\build_playwright_offline_bundle.ps1
```

可选参数：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release\build_playwright_offline_bundle.ps1 `
  -PlaywrightVersion 1.58.0 `
  -Browser chromium `
  -PlaywrightDownloadHost https://npmmirror.com/mirrors/playwright
```

产物：
- `dist/playwright_offline_bundle_windows_<version>_<time>.zip`

把这个 zip 发给客户即可。

## 客户侧离线安装（在客户机器上）

1) 解压 zip（例如解压到 `D:\pw_bundle\`，里面应有 `wheelhouse/`、`.playwright/`、`install_playwright_offline.ps1`）。

2) 打开 PowerShell，进入客户的项目根目录（能看到 `manage.py` / `requirements.txt` 的目录），执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\pw_bundle\install_playwright_offline.ps1 -BundleRoot D:\pw_bundle
```

这一步会：
- 把 `D:\pw_bundle\.playwright` 拷贝到项目根目录的 `.playwright`
- 设置 `PLAYWRIGHT_BROWSERS_PATH` 指向该目录
- 如未安装 Playwright Python 包，则从 `wheelhouse/` 进行离线安装
- 做一次 headless 启动自检

如果客户只需要拷贝浏览器目录（不安装 Python 包），可执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\pw_bundle\install_playwright_offline.ps1 -BundleRoot D:\pw_bundle -BrowsersOnly
```

## 备注

- 项目的一键安装脚本 [windows_oneclick.ps1](file:///d:/Test%20AI/qa%20test/qa_platform_src_no_data_20260123_030219/scripts/windows_oneclick.ps1) 已做了优化：如果项目根目录 `.playwright` 里已经存在 `chromium-*`，会自动跳过再次下载，便于离线复用。
- 浏览器缓存目录必须与 Playwright 版本匹配；建议在生成离线包时指定 `-PlaywrightVersion`，并确保客户安装同版本。

