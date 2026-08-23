# Public CRX signing keys

Every `*.pem` file in this directory is deliberately public. The update workflow generates one RSA private key per GitHub Release source so that every version of that re-signed extension keeps the same CRX extension ID.

These keys provide identity stability, not publisher authentication: anyone can use a committed key to sign a CRX with the same extension ID. Never reuse them for SSH, TLS, Git signing, package signing, secrets, or any purpose that assumes confidentiality. Replacing or deleting a key changes the extension ID and breaks continuity with previously published artifacts.

The dedicated path is excluded from GitHub secret scanning by `.github/secret_scanning.yml`. Do not place any other private material in this directory.
