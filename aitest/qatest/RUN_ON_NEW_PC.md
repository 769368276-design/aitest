# 在另一台电脑运行（Windows）

## 0. 最省事（推荐）
- 直接双击项目根目录的 `安装点我.bat`，按提示等待安装并自动启动。
- 大白话说明见：`部署指南傻瓜版.md`

## 1. 复制与解压
- 把压缩包拷到新电脑后解压到任意目录（路径尽量不要含中文与空格）。

## 2. 安装 Python（建议 3.12+）
- 确保命令行能运行：`python --version`

## 3. 创建虚拟环境并安装依赖

在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\pip install -r requirements.txt
```

如果需要使用“AI 执行测试”（自动化浏览器），还需要安装 Playwright 浏览器：

```powershell
.\.venv\Scripts\python -m playwright install chromium
```

## 4. 配置环境变量（必须）
- 复制 `.env.example` 为 `.env`，按需填写：
  - `DJANGO_SECRET_KEY`
  - `DJANGO_DEBUG`
  - `DJANGO_ALLOWED_HOSTS`
  - `AI_QWEN_API_KEY`（如果使用千问）
  - `AI_QWEN_BASE_URL`
  - `AI_QWEN_MODEL`

## 5. 初始化数据库并启动

```powershell
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py runserver 0.0.0.0:8003
```

浏览器访问：`http://localhost:8003/`

## 可选：带数据迁移
- 如果你需要保留现有数据：把旧电脑的 `db.sqlite3` 拷贝到新电脑项目根目录覆盖即可（先停服务）。
- 如需保留上传文件/截图：把旧电脑的 `media/` 目录一并拷贝到新电脑项目根目录。

