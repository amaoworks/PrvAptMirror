# 运行数据根目录

默认部署把所有 PrvAptMirror 应用状态保存在本目录。除本说明外，内容全部被 Git 忽略，并由 `bootstrap` 幂等创建：

- `admin/auth`：一次性设置令牌和管理员 Argon2id 密码哈希；
- `admin/uploads`：浏览器上传和待处理文件；
- `admin/jobs`：结构化任务与 Worker 状态；
- `admin/audit`：追加式审计日志；
- `aptly`：Aptly 数据库、包池和快照；
- `gnupg`：仓库 OpenPGP 签名私钥环；
- `incoming`：高级 SSH/CLI 导入入口；
- `public`：公开的签名 APT 仓库和公钥；
- `state`：数据布局版本和仓库发布状态。

可以在 `.env` 中用 `PRVAPTMIRROR_DATA_DIR` 把整个数据根目录放到其他磁盘。不要只移动其中一部分，也不要把密码、设置令牌或私钥提交到 Git。
