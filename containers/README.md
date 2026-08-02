# 容器

- `repoctl/`：Aptly、GPG 和仓库管理命令实现。
- `repo-web/`：以只读方式提供已发布的仓库文件，使用非特权用户运行。
- `admin-web/`：第二阶段的单管理员 Web 页面、认证、上传和任务入口，不能访问 Docker Socket、GPG 私钥或 Aptly 数据库。
- `repo-worker/`：第二阶段的无网络串行任务执行器，复用 `repoctl` 的校验、签名和发布逻辑。

后两项是当前阶段的目标目录，创建实现时必须保持 [Web 管理安全边界](../docs/web-admin.md)。
