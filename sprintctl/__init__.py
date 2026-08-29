__version__ = "0.3.5"

# Keep these identifiers stable: the doctor command compares the running
# package with the capabilities declared by a checked-out source tree.
CLI_CAPABILITIES = (
    "item-evidence-observations/v1",
    "normalized-scope-refs/v1",
    "portable-aggregate-uuid-json/v1",
    "remote-backend/v1",
    "remote-schema-compatibility/v1",
    "sprintctl-repository-ingest-cursor/v1",
    "volatile-context-item-projection/v1",
)
