# Limit the pipeline to extension artifact resolution

The repository resolves the extension catalog, packages upstream ZIP archives when necessary, publishes the resulting CRX artifacts, and generates an extension lock containing `name`, `id`, `version`, `url`, and `sha256`. Browser selection, installation, runtime compatibility, and migration of extension data are explicitly outside its boundary; this keeps the artifact pipeline browser-independent at the cost of requiring consumers to validate their own installation environment.
