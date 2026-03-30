# Trae 客户环境安装部署指南（AI执行版）

## 1. 适用范围

- 目标：让客户在 Trae 中由 AI 直接完成安装、启动、验证。
- 系统：Windows（PowerShell 5+）。
- 包类型：源码包（包含 `release/install.ps1` 与 `release/upgrade.ps1`）。

## 2. 交付物

- 最新源码包：`dist/qatest-src-*.zip`
- 客户解压后目录中应包含：
  - `manage.py`
  - `requirements.txt`
  - `release/install.ps1`
  - `release/upgrade.ps1`

## 3. 给 Trae AI 的执行指令（可直接粘贴）

将下面整段发给客户的 Trae AI：

```text
你是部署助手，请在 Windows 上执行以下步骤，全部用 PowerShell 命令、非交互方式完成：

1) 进入我提供的项目目录（目录下有 manage.py）。
2) 执行：
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\release\install.ps1
3) 若 install 成功，检查并补齐 .env（若不存在则从 .env.local.example 复制生成）。
4) 启动服务：
   .\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8003
5) 输出以下结果给我：
   - Python 版本
   - 迁移是否成功
   - 服务地址（http://localhost:8003）
   - 若失败，给出最后 80 行完整错误日志和修复建议

要求：
- 不要使用交互式命令；
- 不要修改业务代码；
- 所有命令在项目根目录执行。
```

## 4. 新装标准流程（人工/AI一致）

1. 解压源码包到目标目录（路径避免中文与空格更稳妥）。
2. 在项目根目录执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\release\install.ps1
```

3. 启动服务：

```powershell
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
```

4. 浏览器访问：

```text
http://localhost:8000/
```

## 5. 升级标准流程

1. 停止旧服务。
2. 用新包覆盖代码目录（保留 `db.sqlite3` 和 `.env` 亦可）。
3. 在项目根目录执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\release\upgrade.ps1
```

4. 重新启动服务并验证页面可访问。

## 6. 验收检查（必须通过）

- `python --version` ≥ 3.12
- `install/upgrade` 脚本执行完成无中断
- `migrate` 成功
- `collectstatic` 成功
- 首页可打开：`http://localhost:8003/`

## 7. 常见问题快速处理

- AI能力不可用：检查 `.env` 中 AI Key 与 Base URL。
- 首次浏览器自动化失败：执行

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

- 端口冲突：改为

```powershell
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8001
```

## 8. 交付建议

- 给客户两份内容：
  - 源码包 `qatest-src-*.zip`
  - 本文档 `release/Trae_AI_安装部署指南.md`
- 同时附一句话：  
  “请将本文第3节整段复制给 Trae AI 执行，可自动完成安装与启动。”
