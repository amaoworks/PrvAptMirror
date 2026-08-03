# 容器

- `repoctl/`：Aptly、GPG 和仓库管理命令实现。
- `repo-web/`：以只读方式提供已发布的仓库文件，使用非特权用户运行。
- `admin-web/`：第二阶段的单管理员 Web 页面与认证服务；检查点 1 提供登录和只读概览，后续接入上传和任务入口。它不能访问 Docker Socket、GPG 私钥或 Aptly 数据库。
- `bootstrap/`：下一检查点计划增加的一次性无网络初始化服务，负责单一数据目录、权限、首次设置令牌和 GPG 密钥的幂等初始化。
- `repo-worker/`：第二阶段的无网络串行任务执行器，复用 `repoctl` 的校验、签名和发布逻辑。

`bootstrap/` 和 `repo-worker/` 仍是当前阶段的目标目录；继续实现时必须保持 [Web 管理安全边界](../docs/web-admin.md) 和 [统一多容器编排范围](../docs/unified-deployment.md)。
