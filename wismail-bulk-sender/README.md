# Wismail 批量定时发送 V2（mailcow 账号登录）

这是一个独立运行的 Wismail 网页模块，可与现有 mailcow 部署并存。V2 可通过 mailcow OAuth2 使用原网页邮箱账号登录；每位用户在模块中单独授权自己的发件邮箱。上传“一行一封完整邮件”的 Excel 或 UTF-8 CSV，检查后设置发送时间与间隔，由后台通过 SMTP 逐封发送。发送流程没有任何 AI API 调用，运行时不消耗 AI token。

## 已实现

- 下载 `.xlsx` 上传模板；导入 `.xlsx`、UTF-8 `.csv`。
- 检查收件邮箱、主题、正文、重复邮件及常见未填占位符；失败时指出表格行号。
- 导入后预览全部邮件；明确点击“确认并开始排队”才会发送。
- 按 UTC+8（可配置固定偏移）设置开始时间、每封间隔、每日发送上限；支持暂停、继续、取消。
- 不再联系名单：上传时拦截名单内邮箱；加入名单后，尚未发出的队列邮件会自动跳过。
- 逐封记录待发送、发送中、已提交发信服务器、失败、需人工核对；批次统计和 CSV 明细导出。
- 可选 IMAP 同步：依据邮件头关联回复；依据退信报告或唯一匹配的收件地址关联退信。未能匹配的来信只记为未匹配事件，不会错误标记某封邮件。
- mailcow OAuth2 登录、每个邮箱自己的应用密码、按邮箱隔离批次和发送统计；可随时撤销授权，撤销后该邮箱未完成的批次暂停。
- 应用密码由服务器密钥加密后保存；CSRF 校验、上传大小限制。保留原有单管理员模式供未配置 OAuth2 的本地环境使用。

## 使用流程

1. 使用现有 Wismail/mailcow 网页邮箱账号登录模块；第一次使用时到“发件授权”填写该邮箱的 SMTP 应用密码（可选填 IMAP 应用密码）。模块验证登录，不发送测试邮件。
2. 打开首页下载 Excel 模板。第一行是固定列名：`收件邮箱`、`邮件主题`、`邮件正文`。从第二行开始，一行一封完整邮件。AI Agent 在 Wismail 外部生成内容并填入表格。
3. 上传文件。Wismail 检查并显示逐封预览；有错误则拒绝创建批次。
4. 设置开始时间和间隔，再点击“确认并开始排队”。后台到点以当前用户的邮箱提交邮件。
5. 在批次页面查看统计和逐封状态，必要时暂停、取消或导出明细。其他用户看不到本邮箱的批次。

## 启动

推荐 Docker Compose（Python 3.12）。本地运行也建议使用 Python 3.12，并安装 `requirements.txt`。先复制环境变量文件：

```powershell
Copy-Item .env.example .env
```

### 1. 在 mailcow 注册 OAuth2 客户端

在 `https://mail.wiskind.top/` 的 mailcow 管理页面 **Configuration → Access → OAuth2** 新增客户端，回调地址填写 **`https://mail.wiskind.top/bulk/oauth/callback`**。记录客户端 ID 和密钥。mailcow 官方的 [OAuth2 接入说明](https://docs.mailcow.email/third_party/nextcloud/third_party-nextcloud/) 展示了相同的注册流程与 `/oauth/authorize`、`/oauth/token`、`/oauth/profile` 接口。已通过只读网络检查确认该站点和 OAuth 身份接口可访问；尚未在实际管理员后台注册客户端。

### 2. 设置模块密钥和服务器地址

生成两个不同的长期密钥，填入 `.env`：

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

第一行用于 `WISMAIL_CREDENTIAL_KEY`，第二行用于 `WISMAIL_SESSION_SECRET`。这两个值必须长期保留；丢失凭证加密密钥后，已保存的应用密码无法解密，需要用户重新授权。

`.env.example` 已填好 `https://mail.wiskind.top`、`/bulk` 回调，以及同主机的 SMTP 587 与 IMAP 993 地址。只读 TCP 检查确认这两个端口可连接。仍需填写 OAuth 客户端 ID/密钥和两项长期密钥。所有 OAuth2 项必须一起配置，且 OAuth2 地址必须使用 HTTPS。

### 3. 启动与访问

从本 GitHub 分支部署时，源码位于 `mailcow-dockerized/wismail-bulk-sender`；仓库根目录的 `docker-compose.override.yml` 已指向该目录，并读取 `wismail-bulk-sender/.env`。请在服务器上单独创建 `.env`，不要提交到 Git。仓库已包含 `data/conf/nginx/site.bulk.custom` 和 `data/web/inc/vars.local.inc.php`；如果服务器上已有同名自定义文件，应合并内容，不要直接覆盖。

在服务器的 `mailcow-dockerized` 目录运行：

```bash
docker compose up -d --build wismail-bulk-sender
docker compose exec nginx-mailcow nginx -t
docker compose restart nginx-mailcow php-fpm-mailcow
```

然后打开 `https://mail.wiskind.top/bulk/`。模块会跳转到 mailcow 登录，再返回批量发送页面。`site.bulk.custom` 使用 mailcow 官方支持的 `site.*.custom` 扩展方式；如站点前还有一层反向代理，须确保 `/bulk/` 请求原样转发给 mailcow。详见 [mailcow 自定义站点文档](https://docs.mailcow.email/manual-guides/Nginx/u_e-nginx_custom/)。

Python 本地启动时，`.env` 不会自动加载；需要在当前进程环境中设置相同变量。未配置 OAuth2 时仍可使用旧版单管理员模式，例如：

```powershell
$env:WISMAIL_ADMIN_PASSWORD = '替换为至少12位的随机密码'
$env:WISMAIL_SMTP_HOST = 'smtp.example.com'
$env:WISMAIL_SMTP_PORT = '587'
$env:WISMAIL_SMTP_USER = 'sales@example.com'
$env:WISMAIL_SMTP_PASSWORD = '邮箱应用密码'
$env:WISMAIL_SMTP_FROM = 'sales@example.com'
python server.py
```

本地单管理员模式通过 `http://127.0.0.1:8765` 访问；此模式仍使用服务器环境变量中的一个固定发件账号。不要将未加 HTTPS 的端口直接暴露到公网。

OAuth2 模式下，每个用户只能授权与自己登录身份相同的发件邮箱；SMTP/IMAP 服务器地址由管理员统一配置。启用双重验证的 mailcow 邮箱通常需要创建应用密码供 SMTP/IMAP 使用；网页登录密码不会交给定时任务。不同外部邮箱服务商的单独 OAuth 授权尚未实现。

管理员配置 IMAP 服务器地址、用户在“发件授权”中填写 IMAP 应用密码后，模块每两分钟读取一次该邮箱 INBOX 来识别回复或退信。第一次连接会从较早邮件开始读取，逐步推进；识别属于“尽力匹配”，具体效果取决于服务商保留的邮件头及退信格式。旧版单管理员模式仍通过环境变量配置 IMAP 用户与密码。

## 统计口径与限制

- **已提交**：外部 SMTP 接受了 Wismail 的邮件提交。它不代表对方服务器接受，更不代表进入客户收件箱。
- **退信 / 回复**：只有配置并成功读取 IMAP，且能可靠关联原邮件时才标记。无数据不等于零退信或零回复。
- 本版不提供“打开 IP / 客户所在地”统计。邮件应用可能通过代理加载图片或提前加载，结果不能可靠代表客户所在地。
- SMTP 成功后的邮件保存在 Wismail 批次明细中；是否出现在外部邮箱的“已发送”文件夹取决于服务商，本版不主动写入该文件夹。
- 本版已支持 mailcow 账号登录和每个登录邮箱的独立发信授权；已准备通过 `vars.local.inc.php` 增加 mailcow 页面入口，待服务器部署后生效。尚未接入 mailcow 角色管理、附件、HTML 正文、不同外部邮箱服务商的 OAuth 授权或最终投递日志 API。
- 同时只运行**一个**应用实例。SQLite 和后台任务线程目前不支持水平扩容。

## 安全与运维

请使用服务商允许的联系人名单，发送前核对 AI 生成的产品、价格和承诺。需要长期运行时应备份 `data/wismail.sqlite3`；这里存有邮件正文、收件信息和加密后的应用密码。请限制谁能访问该数据库与 `.env`。本版对处于“发送中”时突然中断的邮件标记为“需人工核对”，不会自动重发，避免重复投递。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试使用模拟 OAuth2、SMTP 和 IMAP，不会给真实邮箱发送邮件。
