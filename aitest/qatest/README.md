# qatest

面向测试工程师的一体化质量平台：把「用例生成」与「AI 执行」串成闭环，减少人工回归与脚本维护成本。

## 核心能力

- 用例生成：从需求/PRD/文档/页面说明生成结构化用例（步骤、预期、覆盖点），支持二次编辑与沉淀复用
- AI 执行：浏览器自动化按“目标”推进（像真人一样点/填/选/翻页），并在关键动作后自动观察提示与页面状态
- 可解释报告：失败不只一句报错，自动汇聚提示/Toast、关键接口、页面状态、停止原因等证据
- 缺陷闭环：检测到阻塞问题自动停止并登记缺陷；非阻塞提示先记录，步骤完成后仍存在可升级为缺陷
- 可调策略：停止门槛、观察窗口、证据缓存等参数可配置，支持单次执行灰度覆盖便于复现实验

## 快速开始（Windows）

新手最省事：**双击项目根目录的 [安装点我.bat](file:///d:/Test%20AI/qa%20test/qa_platform_src_no_data_20260123_030219/%E5%AE%89%E8%A3%85%E7%82%B9%E6%88%91.bat)**，按提示等待安装并自动启动。

更详细的一键安装/离线/网络排查见：[本地安装说明.md](file:///d:/Test%20AI/qa%20test/qa_platform_src_no_data_20260123_030219/%E6%9C%AC%E5%9C%B0%E5%AE%89%E8%A3%85%E8%AF%B4%E6%98%8E.md)

常见问题处理方案见：[常见问题处理方案.md](file:///d:/Test%20AI/qa%20test/qa_platform_src_no_data_20260123_030219/%E5%B8%B8%E8%A7%81%E9%97%AE%E9%A2%98%E5%A4%84%E7%90%86%E6%96%B9%E6%A1%88.md)

## 快速开始（macOS）

新手最省事：**双击项目根目录的 `安装点我.command`**，按提示等待安装并自动启动。  
更详细的一键安装/离线/网络排查见：`本地安装说明.md`

也可以手动在项目根目录执行：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -U pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py init_data
./.venv/bin/python manage.py runserver 0.0.0.0:8003
```

浏览器访问：`http://localhost:8003/`  
默认会创建 `admin` 账号；可通过环境变量 `INIT_ADMIN_PASSWORD` 指定密码（否则会生成随机密码并在控制台打印）。

如需 AI 执行（浏览器自动化），需额外安装 Playwright 浏览器：

```bash
./.venv/bin/python -m playwright install chromium
```

## 配置（环境变量）

建议用 `.env` 管理环境变量（不要提交到仓库）。常见配置：

- `DJANGO_SECRET_KEY` / `DJANGO_DEBUG` / `DJANGO_ALLOWED_HOSTS`
- 模型相关（按需）：`AI_QWEN_API_KEY` / `AI_QWEN_BASE_URL` / `AI_QWEN_MODEL`，或 `AI_DEEPSEEK_API_KEY` 等

AI 执行策略相关（可选）：

- `AI_EXEC_ENGINE`：执行引擎（`browser_use` 默认 / `playwright_ai`）
- `AI_EXEC_HEADLESS`：是否无头运行浏览器（true/false）
- `AI_EXEC_BROWSER`：Browser 执行时优先使用的浏览器（`chrome` / `edge`，Windows 内网建议用 `edge`）
- `AI_EXEC_CHROME_PATH`：自定义浏览器可执行文件路径（可填 `msedge.exe` 或 `chrome.exe` 绝对路径）
- `AI_EXEC_CHROME_EXTRA_ARGS`：附加浏览器启动参数（用于企业内网代理/证书/策略等）
- `AI_EXEC_PREFLIGHT_TIMEOUT_MS`：项目地址预检超时（默认 45000ms，内网可适当调大）
- `AI_EXEC_STOP_CHECK_MIN_STEP`：最早从第几步开始触发异步停止检查
- `AI_EXEC_SUBMIT_OBSERVE_WAIT_MS`：提交/保存等动作后的观察窗口（减少竞态误判）
- `AI_EXEC_EVIDENCE_MAXLEN`：证据缓存长度（用于报告复盘）
- `AI_EXEC_NON_BLOCKING_NOTE_MAX`：按用例步骤保存的非阻塞提示条数上限

单次执行灰度覆盖：可在数据集变量中提供 `ai_exec_policy` 覆盖策略参数（用于对比实验与复现）。

## Playwright 录制脚本

- 网页入口：`/autotest/record/`（生成命令/下载脚本，本机 GUI 录制）
- 命令行入口：`python manage.py autotest_playwright_codegen --url <URL> --target python --output recordings/xxx.py`

## 运行工作进程（可选）

如果你使用了执行队列/后台执行，可启动 worker（执行器）：

```powershell
.\.venv\Scripts\python manage.py autotest_worker --workers 2
```

如果你需要“定时自动执行”，可启动 scheduler（调度器）：

```powershell
.\.venv\Scripts\python manage.py autotest_scheduler --poll 5
```

定时任务在页面配置入口：`/autotest/schedules/`（每个用户只管理自己的任务）。

## 数据与安全

仓库默认不包含任何业务数据：

- 数据库：`db.sqlite3`、`*.sqlite3`、`*.db` 已被忽略
- 上传文件：`media/` 已被忽略
- 静态收集：`staticfiles/` 已被忽略
- 运行日志：`logs/`、`*.log` 已被忽略
- 密钥与环境：`.env*`、虚拟环境目录已被忽略

如果你需要在机器间迁移本地数据，请手动拷贝 `db.sqlite3` 与 `media/`（仅在你明确需要保留数据时）。

## License

Internal / Demo project.
