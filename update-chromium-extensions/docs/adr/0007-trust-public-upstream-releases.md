# Trust public upstream releases at acquisition time

GitHub ZIP sources are limited to public repositories, and the workflow trusts each repository's latest non-draft, non-prerelease asset when it is first acquired. Requiring a catalog-pinned checksum would defeat unattended updates, while upstream checksum and attestation formats are not uniform; the workflow therefore records source URLs and observed hashes for audit but cannot detect a repository compromise before acquisition. Private repositories and source-specific verification adapters are outside the current scope.

[ADR-0010](./0010-package-release-source-archives-without-upstream-builds.md) 将获取对象扩展到同一正式 Release 关联的源码归档，并保留上述首获信任与公开下载边界。
