# 运行数据

该目录保存本地运行状态，其内容不纳入版本控制：

- `incoming/`：等待导入的手工上传软件包；
- `lib/aptly/`：Aptly 状态、包池和快照；
- `lib/gnupg/`：仓库签名密钥环，包含私密信息；
- `public/`：由 `repo-web` 以只读方式提供的已签名仓库文件。

迁移或升级部署前必须备份 `lib/aptly` 和 `lib/gnupg`。绝不能通过 Web 容器暴露 `incoming` 或 `lib/gnupg`。

