#!/usr/bin/env bash
# Deploy only the Wismail integration from the feature branch.
set -Eeuo pipefail

BRANCH=feature/wismail-bulk-sender-v2
STATE=/opt/wismail-deploy-state.txt
BASE=/opt/mailcow-dockerized
REMOTE=https://github.com/zengzhengxinandrew-blip/mailcow-dockerized.git
BACKUP_ROOT=/var/backups/mailcow-private
ADMIN_URL=https://mail.wiskind.top/admin/

cd "$BASE"
if [[ "$(id -u)" -ne 0 ]]; then
  echo "必须以 root 运行" >&2
  exit 1
fi
if [[ "$(git branch --show-current)" != "master" ]]; then
  echo "服务器不在 master 分支，停止部署" >&2
  exit 1
fi

git remote set-url origin "$REMOTE"
if [[ "$(git remote get-url origin)" != "$REMOTE" ]]; then
  echo "Git 远程地址不符，停止部署" >&2
  exit 1
fi
git fetch origin "$BRANCH"
NEW_REV=$(git rev-parse FETCH_HEAD)
OLD_REV=$(cat "$STATE" 2>/dev/null || true)

admin_renders() {
  local body
  body=$(curl -fsS --max-time 10 --resolve mail.wiskind.top:443:127.0.0.1 "$ADMIN_URL") || return 1
  [[ "$body" == *'name="login_user"'* && "$body" == *'name="pass_user"'* ]]
}

if [[ "$NEW_REV" == "$OLD_REV" ]]; then
  if ! admin_renders; then
    docker compose restart php-fpm-mailcow
    admin_renders || { echo "管理页仍无法渲染" >&2; exit 1; }
  fi
  echo "Wismail 已是最新（$NEW_REV），管理页正常"
  exit 0
fi

if [[ ! -f wismail-bulk-sender/.env ]]; then
  echo "缺少 wismail-bulk-sender/.env，停止部署" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "服务器 tracked 文件有未提交改动，停止部署" >&2
  exit 1
fi
if [[ ! -d "$BACKUP_ROOT" ]]; then
  echo "备份目录不存在，停止部署：$BACKUP_ROOT" >&2
  exit 1
fi

# Back up secrets and SQLite through its online backup API before any file update.
backup_dir=$(mktemp -d "$BACKUP_ROOT/wismail-$(date +%Y%m%d-%H%M%S)-XXXXXX")
chmod 700 "$backup_dir"
cp -a wismail-bulk-sender "$backup_dir/source"
container_id=$(docker compose ps -q wismail-bulk-sender)
if [[ -z "$container_id" ]]; then
  echo "Wismail 容器不存在，停止部署；源码备份：$backup_dir" >&2
  exit 1
fi
docker compose exec -T wismail-bulk-sender rm -f /tmp/wismail-predeploy.sqlite3 </dev/null
docker compose exec -T wismail-bulk-sender python -c '
import sqlite3
source = sqlite3.connect("file:/app/data/wismail.sqlite3?mode=ro", uri=True)
backup = sqlite3.connect("/tmp/wismail-predeploy.sqlite3")
source.backup(backup)
assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
backup.close()
source.close()
' </dev/null
docker cp "$container_id:/tmp/wismail-predeploy.sqlite3" "$backup_dir/database.sqlite3"
docker compose exec -T wismail-bulk-sender rm -f /tmp/wismail-predeploy.sqlite3 </dev/null
python3 - "$backup_dir/database.sqlite3" <<'PY'
import sqlite3
import sys
db = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
PY
echo "Wismail 备份完成：$backup_dir"

# The feature branch must never replace mailcow's PHP, templates, or core config.
# Those files are updated only by the master deployment.
paths=(
  wismail-bulk-sender
  docker-compose.override.yml
  data/conf/nginx/site.bulk.custom
  data/web/inc/vars.local.inc.php
)
for path in "${paths[@]}"; do
  git cat-file -e "FETCH_HEAD:$path"
done
git archive --format=tar FETCH_HEAD "${paths[@]}" | tar -xf -
docker compose config --quiet
docker compose build wismail-bulk-sender
docker compose up -d wismail-bulk-sender
docker compose up -d php-fpm-mailcow

docker compose exec -T nginx-mailcow nginx -t </dev/null
docker compose exec -T nginx-mailcow nginx -s reload </dev/null
docker compose restart php-fpm-mailcow
if [[ "$(docker compose exec -T php-fpm-mailcow php -r 'echo ini_get("opcache.validate_timestamps");' </dev/null)" != "1" ]]; then
  echo "PHP 缓存时间戳检查未生效，停止部署" >&2
  exit 1
fi

admin_ok=0
for attempt in $(seq 1 10); do
  if admin_renders; then
    admin_ok=1
    break
  fi
  sleep 2
done
if [[ "$admin_ok" -ne 1 ]]; then
  echo "管理登录页未正常渲染，部署失败；备份：$backup_dir" >&2
  exit 1
fi
if [[ "$(curl -fsS --max-time 10 --resolve mail.wiskind.top:443:127.0.0.1 -o /dev/null -w '%{http_code}' https://mail.wiskind.top/bulk/team/login)" != "200" ]]; then
  echo "Wismail 登录页检查失败；备份：$backup_dir" >&2
  exit 1
fi
docker compose ps wismail-bulk-sender

# Install the reviewed script for the next deployment without changing this run.
install -m 755 wismail-bulk-sender/deploy/wismail-deploy.sh /usr/local/sbin/wismail-deploy.next
mv /usr/local/sbin/wismail-deploy.next /usr/local/sbin/wismail-deploy
printf '%s\n' "$NEW_REV" > "$STATE.next"
mv "$STATE.next" "$STATE"
echo "Wismail 部署完成：$OLD_REV -> $NEW_REV"
