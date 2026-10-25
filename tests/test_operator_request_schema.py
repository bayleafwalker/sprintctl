from __future__ import annotations

from sprintctl import operator_request_schema as schema


class CatalogCursor:
    def __init__(self):
        self.tables = set(schema.TABLES)
        self.indexes = set(schema._INDEX_DEFINITIONS)
        self.schema_name = "public"
        self.columns = {table: list(values) for table, values in schema._COLUMNS.items()}
        self.constraints = dict(schema._CONSTRAINTS)
        self.index_definitions = dict(schema._INDEX_DEFINITIONS)
        self.triggers = {name: {**value} for name, value in schema._EXPECTED_TRIGGERS.items()}
        self.functions = {name: {**value} for name, value in schema._EXPECTED_FUNCTIONS.items()}
        self._rows = []
        self._row = None

    def execute(self, query, _params=None):
        if "c.relkind IN ('r','i')" in query:
            self._rows = ([{"relname": name, "relkind": "r"} for name in self.tables]
                          + [{"relname": name, "relkind": "i"} for name in self.indexes])
        elif "FROM pg_attribute a" in query:
            self._rows = [
                {"relname": table, "attname": name, "data_type": data_type,
                 "attnotnull": not_null, "attidentity": "", "default_expr": ""}
                for table, values in self.columns.items()
                for name, data_type, not_null in values
            ]
        elif "FROM pg_constraint con" in query:
            self._rows = [
                {"conname": name, "definition": definition, "convalidated": True}
                for name, definition in self.constraints.items()
            ]
        elif "pg_get_indexdef(c.oid)" in query:
            self._rows = []
            for name, definition in self.index_definitions.items():
                table_name = definition.split(" ON ", 1)[1].split(" USING ", 1)[0]
                qualified = definition.replace(
                    f" ON {table_name} ",
                    f" ON {self.schema_name}.{table_name} ",
                    1,
                )
                self._rows.append({
                    "relname": name,
                    "schema_name": self.schema_name,
                    "indexdef": qualified,
                })
        elif "FROM pg_trigger t" in query:
            self._rows = [
                {"tgname": name, "table_name": value["table"], "function_name": value["function"],
                 "tgenabled": value["enabled"], "tgtype": value["type"]}
                for name, value in self.triggers.items()
            ]
        elif "FROM pg_proc p" in query:
            self._rows = [
                {"proname": name, "result_type": value["result_type"], "language": value["language"],
                 "security_definer": value["security_definer"], "body": value["body"]}
                for name, value in self.functions.items()
            ]
        else:
            raise AssertionError(f"unexpected catalog query: {query}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


def test_schema28_exact_catalog_is_admitted():
    result = schema.inspect_catalog(CatalogCursor())

    assert result["available"] is True
    assert result["complete"] is True
    assert result["table_count"] == 3
    assert result["trigger_count"] == 2
    assert result["function_count"] == 1
    assert result["catalog_fingerprint"] == result["expected_catalog_fingerprint"]


def test_catalog_index_definitions_are_qualified_to_the_current_schema():
    cursor = CatalogCursor()
    cursor.schema_name = "tenant"

    result = schema.inspect_catalog(cursor)

    assert result["available"] is True
    assert result["catalog_fingerprint"] == result["expected_catalog_fingerprint"]


def test_changed_fk_or_unvalidated_constraint_fails_closed():
    cursor = CatalogCursor()
    cursor.constraints["operator_request_dependant_request_fk"] = (
        cursor.constraints["operator_request_dependant_request_fk"].replace(
            "ON DELETE RESTRICT", "ON DELETE CASCADE"
        )
    )
    assert schema.inspect_catalog(cursor)["available"] is False

    cursor = CatalogCursor()
    cursor.constraints.pop("operator_request_event_revision_positive")
    assert schema.inspect_catalog(cursor)["available"] is False


def test_partial_inventory_or_unexpected_trigger_is_not_admitted():
    cursor = CatalogCursor()
    cursor.tables.remove("operator_request_event")
    assert schema.inspect_catalog(cursor)["complete"] is False

    cursor = CatalogCursor()
    cursor.triggers["sprintctl_operator_request_event_append_only"]["type"] = 3
    result = schema.inspect_catalog(cursor)
    assert result["available"] is False
    assert result["complete"] is False


def test_changed_guard_function_is_not_admitted():
    cursor = CatalogCursor()
    cursor.functions[schema.FUNCTIONS[0]]["body"] += " RETURN NEW;"
    result = schema.inspect_catalog(cursor)
    assert result["available"] is False
    assert result["complete"] is False
