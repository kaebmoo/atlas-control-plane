"""Atlas Fleet: instance registry, provisioning, health, and usage pull.

Separate from Atlas core — own SQLite registry, no shared tenant DB, no tenant logic
in `atlas/`. See fleet/README.md.
"""
