# 测试

`smoke/static.sh` 执行脚本语法、Python 编译、JSON 配置和全部 Compose profile 配置检查；`smoke/run.sh` 在 Docker 一次性环境中验证仓库元数据、签名、架构索引、更新、回滚以及 Web 下载服务。`smoke/repoctl-local.sh` 可以使用指定的 Aptly 二进制单独验证管理和发布链路。管理 Web 的认证和安全测试通过 `./scripts/prvaptmirror admin test` 在隔离容器中运行。
