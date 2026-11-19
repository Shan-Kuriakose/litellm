import re
from pathlib import Path
from typing import Final

import pytest

REPO: Final = Path(__file__).resolve().parents[3]
PACKAGE: Final = REPO / "litellm-proxy-extras" / "litellm_proxy_extras"
SCHEMA_COPIES: Final = (
    REPO / "schema.prisma",
    REPO / "litellm" / "proxy" / "schema.prisma",
    PACKAGE / "schema.prisma",
)


def _migration_sql() -> str:
    return "\n".join(path.read_text() for path in sorted((PACKAGE / "migrations").glob("*/migration.sql")))


def _tables_left_by_migrations(sql: str) -> frozenset[str]:
    created: Final = frozenset(re.findall(r'CREATE TABLE (?:IF NOT EXISTS )?"(\w+)"', sql))
    dropped: Final = frozenset(re.findall(r'DROP TABLE (?:IF EXISTS )?"(\w+)"', sql))
    renames: Final = tuple(re.findall(r'ALTER TABLE (?:IF EXISTS )?"(\w+)" RENAME TO "(\w+)"', sql))
    renamed_from: Final = frozenset(old for old, _ in renames)
    renamed_to: Final = frozenset(new for _, new in renames)
    return (created - dropped - renamed_from) | renamed_to


def _prisma_tables(schema: str) -> frozenset[str]:
    models: Final = frozenset(re.findall(r"^model (\w+) \{", schema, re.MULTILINE))
    mapped: Final = frozenset(re.findall(r'@@map\("(\w+)"\)', schema))
    return models | mapped


def test_every_table_the_migrations_create_has_a_prisma_model():
    prisma_tables: Final = _prisma_tables((PACKAGE / "schema.prisma").read_text())
    assert _tables_left_by_migrations(_migration_sql()) - prisma_tables == frozenset()


@pytest.mark.parametrize("copy", SCHEMA_COPIES[1:], ids=lambda path: str(path.relative_to(REPO)))
def test_every_schema_copy_matches_the_root_schema(copy: Path):
    assert copy.read_text().strip() == SCHEMA_COPIES[0].read_text().strip()
