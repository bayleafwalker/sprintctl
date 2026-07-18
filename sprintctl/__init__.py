__version__ = "0.2.0"

# Keep these identifiers stable: the doctor command compares the running
# package with the capabilities declared by a checked-out source tree.
CLI_CAPABILITIES = (
    "normalized-scope-refs/v1",
    "portable-aggregate-uuid-json/v1",
    "remote-backend/v1",
)
