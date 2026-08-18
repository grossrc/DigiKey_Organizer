# tests/test_sql_guard.py
import pytest

from mcp_server.sql_guard import SqlRejected, validate, validate_and_wrap

REJECTED = [
    "DROP TABLE parts",
    "TRUNCATE parts",
    "UPDATE parts SET mpn = 'x'",
    "INSERT INTO parts (mpn) VALUES ('a')",
    "DELETE FROM parts",
    "SELECT 1; DELETE FROM parts",
    "WITH x AS (DELETE FROM parts RETURNING *) SELECT * FROM x",
    "WITH x AS (UPDATE parts SET mpn='y' RETURNING *) SELECT * FROM x",
    "COPY parts TO PROGRAM 'sh -c whoami'",
    "SET ROLE postgres",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_sleep(60)",
    "SELECT nextval('parts_part_id_seq')",
    "SELECT dblink('host=evil', 'select 1')",
    "SELECT * FROM pg_shadow",
    "SELECT * FROM pg_authid",
    "SELECT * FROM some_other_table",
    "SELECT * FROM secrets.credentials",
    "",
    "   ",
    "this is not sql",
]

ACCEPTED = [
    "SELECT mpn FROM parts LIMIT 5",
    "SELECT * FROM v_inventory_totals",
    """SELECT p.mpn FROM parts p
       WHERE p.attributes @> '{"dielectric":"X7R"}'::jsonb""",
    """WITH t AS (SELECT part_id, SUM(quantity_delta) q FROM movements GROUP BY part_id)
       SELECT * FROM t JOIN parts USING (part_id)""",
    "SELECT column_name FROM information_schema.columns WHERE table_name = 'parts'",
    "SELECT k.key FROM parts p CROSS JOIN LATERAL jsonb_object_keys(p.attributes) AS k(key)",
    "SELECT mpn FROM parts UNION SELECT mpn FROM parts",
    "SELECT l.position_code FROM locations l LEFT JOIN movements m USING (position_code)",
]


@pytest.mark.parametrize("sql", REJECTED)
def test_rejects_non_readonly_sql(sql):
    with pytest.raises(SqlRejected):
        validate(sql)


@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepts_readonly_selects(sql):
    assert validate(sql)


def test_trailing_semicolon_is_stripped():
    assert validate("SELECT 1 FROM parts;  ") == "SELECT 1 FROM parts"


def test_trailing_comment_cannot_swallow_the_limit():
    wrapped = validate_and_wrap("SELECT mpn FROM parts -- sneaky", 25)
    assert wrapped.splitlines()[-1] == "LIMIT 25"


def test_wrapper_applies_outer_limit():
    wrapped = validate_and_wrap("SELECT mpn FROM parts LIMIT 100000", 10)
    assert wrapped.endswith("LIMIT 10")
    assert "_mcp_q" in wrapped
