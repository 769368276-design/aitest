#!/bin/bash
set -e

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO] $1${NC}"
}

log_warn() {
    echo -e "${YELLOW}[WARN] $1${NC}"
}

log_error() {
    echo -e "${RED}[ERROR] $1${NC}"
}

# 1. 检查并安装 Docker
if ! command -v docker &> /dev/null; then
    log_info "Docker 未安装，正在自动安装..."
    if command -v yum &> /dev/null; then
        # CentOS / Alibaba Cloud Linux / Anolis
        yum install -y yum-utils git curl openssl
        yum-config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
        yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    elif command -v apt &> /dev/null; then
        # Ubuntu / Debian
        apt-get update
        apt-get install -y ca-certificates curl gnupg lsb-release git openssl
        mkdir -p /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
        apt-get update
        apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    else
        log_error "未知的操作系统，请手动安装 Docker。"
        exit 1
    fi
    systemctl enable --now docker
    log_info "Docker 安装完成。"
else
    log_info "Docker 已安装，跳过。"
fi

DOCKER_DAEMON_JSON="/etc/docker/daemon.json"
if [ ! -f "$DOCKER_DAEMON_JSON" ]; then
    mkdir -p /etc/docker
    cat > "$DOCKER_DAEMON_JSON" <<'EOF'
{
  "dns": ["223.5.5.5", "223.6.6.6", "8.8.8.8"],
  "registry-mirrors": ["https://docker.m.daocloud.io", "https://mirror.baidubce.com"]
}
EOF
    if command -v systemctl &> /dev/null; then
        systemctl restart docker || true
    else
        service docker restart || true
    fi
    log_info "已写入 Docker DNS/镜像加速配置。"
else
    log_info "检测到 Docker daemon 配置已存在，跳过 Docker DNS/镜像加速配置。"
fi

# 2. 准备项目环境
PROJECT_DIR="/opt/qatest"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
mkdir -p "$PROJECT_DIR"
cd "$ROOT_DIR"
log_info "当前项目目录: $ROOT_DIR"
if [ "$ROOT_DIR" != "$PROJECT_DIR" ]; then
    log_warn "项目目录不在 $PROJECT_DIR（当前: $ROOT_DIR）。如需固定路径，可将代码放到 $PROJECT_DIR 后再执行。"
fi

# 3. 配置环境变量 (.env)
if [ ! -f .env ]; then
    log_info "未检测到 .env 文件，正在从 .env.example 生成..."
    if [ -f .env.example ]; then
        cp .env.example .env
        
        # 生成随机密码
        SECRET_KEY=$(openssl rand -base64 32 | tr -d /=+ | cut -c -50)
        DB_PASS=$(openssl rand -base64 16 | tr -d /=+)
        ADMIN_PASS=$(openssl rand -base64 12 | tr -d /=+)
        
        # 获取本机公网 IP (备用)
        MY_IP=$(curl -sS --connect-timeout 3 --max-time 3 ifconfig.me 2>/dev/null || echo "127.0.0.1")
        
        # 替换配置
        sed -i "s|DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=$SECRET_KEY|" .env
        sed -i "s|DJANGO_DB_PASSWORD=.*|DJANGO_DB_PASSWORD=$DB_PASS|" .env
        sed -i "s|INIT_ADMIN_PASSWORD=.*|INIT_ADMIN_PASSWORD=$ADMIN_PASS|" .env
        sed -i "s|DJANGO_ALLOWED_HOSTS=.*|DJANGO_ALLOWED_HOSTS=$MY_IP,localhost,127.0.0.1,8.130.85.131|" .env
        
        log_info "已自动生成安全配置："
        echo "  - DJANGO_SECRET_KEY: (已设置)"
        echo "  - DJANGO_DB_PASSWORD: (已设置)"
        echo "  - INIT_ADMIN_PASSWORD: $ADMIN_PASS  <-- 请记录此初始密码！"
    else
        log_warn "未找到 .env.example，跳过自动配置。请手动创建 .env 文件。"
    fi
else
    log_info ".env 文件已存在，跳过生成。"
fi

# 4. 启动服务
log_info "正在构建并启动 Docker 容器..."

if [ -f "docker-compose.prod.yml" ]; then
    if grep -q "pw-browsers:.*:ro" docker-compose.prod.yml; then
        log_warn "检测到 pw-browsers 为只读挂载(:ro)，将自动改为可写(:rw) 以便容器内下载/补齐 Playwright 浏览器。"
        sed -i 's/:ro$/:rw/' docker-compose.prod.yml
    fi
fi

mkdir -p pw-browsers || true
chmod 777 pw-browsers 2>/dev/null || true
if [ -d pw-browsers ] && [ -f pw-browsers.tgz ]; then
    if [ -z "$(ls -A pw-browsers 2>/dev/null || true)" ]; then
        log_info "检测到 pw-browsers.tgz 且 pw-browsers 为空，自动解压离线浏览器目录..."
        tar -xzf pw-browsers.tgz -C pw-browsers || true
    fi
fi

docker compose -f docker-compose.prod.yml up -d --build

log_info "安装 Playwright Linux 依赖（缺库会导致浏览器启动秒退）..."
docker compose -f docker-compose.prod.yml exec -T web bash -lc 'python -m playwright install-deps chromium' || log_warn "web 容器 install-deps 失败（可能网络/源问题），如浏览器自检仍失败请手动排查"
docker compose -f docker-compose.prod.yml exec -T worker bash -lc 'python -m playwright install-deps chromium' || log_warn "worker 容器 install-deps 失败（可能网络/源问题），如 AI 执行仍失败请手动排查"

log_info "检查 Playwright 浏览器目录是否齐全..."
docker compose -f docker-compose.prod.yml exec -T web bash -lc 'ls -la ${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright} | head -n 80' || true

# 5. 检查状态
log_info "部署完成！服务状态如下："
docker compose -f docker-compose.prod.yml ps

echo ""
echo -e "${GREEN}访问地址: http://8.130.85.131/ (或 http://$MY_IP/)${NC}"
if [ -n "$ADMIN_PASS" ]; then
    echo -e "${YELLOW}初始管理员账号: admin / $ADMIN_PASS${NC}"
fi
