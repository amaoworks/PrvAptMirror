# 容器

- `repoctl/`：Aptly、GPG、幂等 bootstrap 和仓库管理命令实现。
- `repo-web/`：以只读方式提供已发布的仓库文件，使用非特权用户运行。
- `admin-web/`：第二阶段的首次密码设置、单管理员登录和只读概览；后续接入上传和任务入口。它不能访问 Docker Socket、GPG 私钥或 Aptly 数据库。
- `repo-worker/`：无网络串行任务容器；编排和健康检查已经接入，后续复用 `repoctl` 的校验、签名和发布逻辑。

`bootstrap` 作为 `repoctl` 镜像的一次性 Compose 服务运行。继续实现 Worker 业务任务时必须保持 [Web 管理安全边界](../docs/web-admin.md) 和 [统一多容器编排范围](../docs/unified-deployment.md)。
