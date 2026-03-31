# 发布包（新装 & 升级）

本目录提供两类脚本：

- `install.ps1`：新客户从零安装（创建虚拟环境、安装依赖、执行迁移、收集静态文件）
- `upgrade.ps1`：老客户使用新包升级旧包（备份数据库/配置、更新依赖、执行迁移、收集静态文件）

> 说明：项目采用 Django 迁移机制管理数据库结构。升级时请务必执行 `python manage.py migrate`，脚本已包含该步骤。

## 0. 前置条件

- Windows 环境（PowerShell 5+）
- Python 3.12+
- 已解压项目代码到某个目录（该目录下应包含 `manage.py`）

## 1. 新客户：从零安装（install.ps1）

在项目根目录执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\release\install.ps1
```

首次安装后：

- 请按提示检查/填写 `.env`（纯本地建议优先用 `.env.local.example` 生成；`.env.example` 主要用于 Docker/服务器场景）
- 如需启动服务可执行：

```powershell
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8003
```

## 2. 老客户：用新包升级旧包（upgrade.ps1）

建议流程：

1) 停止旧版本服务（如果正在运行）  
2) 用新包代码覆盖旧目录（建议先完整备份旧目录）  
3) 在项目根目录执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\release\upgrade.ps1
```

脚本会：

- 备份 `db.sqlite3`（如存在）到 `backup\`
- 备份 `.env`（如存在）到 `backup\`
- 安装/更新依赖
- 执行数据库迁移（把新版本新增字段同步到数据库）
- 收集静态文件

## 3. 生成发布 ZIP（build_release.ps1）

在项目根目录执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\release\build_release.ps1
```

脚本会在 `dist\` 下生成 `qa_platform_release_*.zip`。如果当前代码目录存在未提交改动，会自动使用复制打包方式以包含最新改动。

## 3.1 生成纯本地安装包（给客户拷贝安装）

在项目根目录执行（会生成包含 `wheelhouse/` 的离线依赖缓存，便于企业网络/离线安装）：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\release\build_local_package.ps1
```

如不想构建 wheelhouse（更快，但客户安装需要联网）：

```powershell
.\release\build_local_package.ps1 -NoWheelhouse
```

## 4. 常见问题

### 4.1 用例扩写/AI 执行报错

这两块依赖 `.env` 中的 AI Key 配置（见 `.env.example`）。未配置 Key 会导致接口报错或无法生成/执行。

### 4.2 Playwright 浏览器未安装

如果环境首次使用 Playwright，可能需要安装浏览器内核：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```
