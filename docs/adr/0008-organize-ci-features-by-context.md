# Organize CI features by context

Each CI feature owns a root-level directory containing its implementation, configuration, generated state, tests, domain glossary, and feature-specific ADRs. Repository-wide navigation and decisions remain at the root, while entry points that GitHub only discovers under `.github/` stay there as thin platform adapters. Existing root-level component data paths are moved without compatibility copies so the repository has one source of truth per feature, accepting that external consumers must update their raw file URLs.
