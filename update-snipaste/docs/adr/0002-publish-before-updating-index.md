# 所有目标完成校验和发布后再更新索引

为避免发布索引引用缺失或未经校验的安装包，所有目标都必须完成下载和校验，才开始发布镜像修订；全部发布成功后，才将 JSON 发布索引提交到 main。各目标独立维护版本，并拒绝自动降级。任一下载、校验或发布失败都保留旧索引，接受一个目标故障会暂时阻止整批索引更新。

GitHub Release 发布与 Git 推送无法组成一个事务。中途失败时保留已经发布的镜像修订，重跑校验并复用已有发布后继续，而不通过删除历史内容回滚。

发布索引保存为 update-snipaste/snipaste.json，顶层固定为 aarch64-darwin 和 x86_64-linux。每项包含 version、url 和 hash；url 指向本仓库不可变 Release 中的具体安装包，hash 使用可直接作为 Nix fetchurl.hash 的 SHA-256 SRI。字段名对齐 nixpkgs sources.nix 中 fetchurl 的 hash，元数据采用扁平结构，便于 JSON 消费方使用。
