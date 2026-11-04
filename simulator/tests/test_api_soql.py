"""SOQL subset: parsing, typing, error shapes (no database needed)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from harborline.api import soql
from harborline.api.soql import SoqlError, compile_soql, date_literal_range, parse

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)  # a Friday


@pytest.fixture(scope="module")
def schema() -> soql.Schema:
    return soql.base_schema()


def compile_(text: str, schema: soql.Schema) -> soql.Compiled:
    return compile_soql(text, schema, NOW)


def error(text: str, schema: soql.Schema) -> SoqlError:
    with pytest.raises(SoqlError) as exc:
        compile_(text, schema)
    return exc.value


def test_schema_has_system_fields_owner_and_custom_fields(schema: soql.Schema) -> None:
    account = schema.get("account")
    assert account is not None and account.key_prefix == "001"
    names = [f.name for f in account.fields]
    assert names[:2] == ["Id", "IsDeleted"]
    assert {"OwnerId", "CreatedDate", "SystemModstamp", "Health_Score__c"} <= set(names)
    assert account.field("health_score__c").updateable
    assert not account.field("Name").updateable
    assert account.field("OwnerId").relationship_name == "Owner"
    assert schema.get("Rate_Card_Line__c").field("Rate_Card__c").relationship_name == "Rate_Card__r"


def test_select_with_parent_path_builds_join_and_headers(schema: soql.Schema) -> None:
    c = compile_("SELECT Id, Name, Account.Name, Owner.Email FROM Contact", schema)
    assert c.headers == ["Id", "Name", "Account.Name", "Owner.Email"]
    assert set(c.joins) == {"AccountId", "OwnerId"}
    sql = c.page_sql()
    assert "LEFT JOIN sfdc.record p1 ON p1.id = (nullif(r.data->>'AccountId', ''))::bpchar" in sql
    assert sql.endswith("WHERE r.id = ANY(%s::bpchar[])")


def test_field_and_object_names_are_case_insensitive(schema: soql.Schema) -> None:
    c = compile_("select id, NAME, account.name from contact where lastname = 'x'", schema)
    assert c.obj.name == "Contact"
    assert c.headers == ["Id", "Name", "Account.Name"]


def test_object_prefix_and_alias_are_accepted(schema: soql.Schema) -> None:
    c = compile_("SELECT a.Name, Account.Industry FROM Account a WHERE a.Name != null", schema)
    assert c.headers == ["Name", "Industry"]


def test_where_typing_per_field_type(schema: soql.Schema) -> None:
    c = compile_(
        "SELECT Id FROM Opportunity WHERE Amount >= 1000.50 AND IsClosed = false AND CloseDate <= 2026-12-31 "
        "AND CreatedDate > 2026-01-01T00:00:00Z AND StageName IN ('Prospecting', 'closed won') AND Name LIKE 'Acme%'",
        schema,
    )
    w = c.where_sql
    assert "jsonb_typeof(r.data->'Amount') = 'number'" in w and "%s::numeric" in w
    assert "coalesce(CASE WHEN jsonb_typeof(r.data->'IsClosed') = 'boolean'" in w
    assert "pg_input_is_valid(r.data->>'CloseDate', 'date')" in w and "%s::date" in w
    assert "r.created_date > %s::timestamptz" in w
    assert "lower(nullif(r.data->>'StageName', '')) = ANY(%s::text[])" in w
    assert "ILIKE %s" in w
    assert ["prospecting", "closed won"] in c.params
    assert datetime(2026, 1, 1, tzinfo=UTC) in c.params


def test_not_equal_matches_nulls_like_salesforce(schema: soql.Schema) -> None:
    c = compile_("SELECT Id FROM Case WHERE Type != 'Problem'", schema)
    assert "IS DISTINCT FROM" in c.where_sql


def test_null_comparisons_and_boolean_null(schema: soql.Schema) -> None:
    c = compile_("SELECT Id FROM Account WHERE ParentId = null AND Website != null", schema)
    assert "IS NULL" in c.where_sql and "IS NOT NULL" in c.where_sql


def test_id_filters_accept_15_char_ids_and_use_the_primary_key(schema: soql.Schema) -> None:
    c = compile_("SELECT Id FROM Account WHERE Id = '0015f0000000001'", schema)
    assert c.where_sql == "r.id = %s::bpchar"
    assert c.params == ["0015f0000000001AAA"]


def test_date_literals_ranges() -> None:
    today = date(2026, 10, 30)
    lit = parse("SELECT Id FROM Account WHERE CreatedDate = LAST_N_DAYS:7").where.value
    assert date_literal_range(lit, today) == (date(2026, 10, 23), date(2026, 10, 31))
    yesterday = parse("SELECT Id FROM Account WHERE CreatedDate = YESTERDAY").where.value
    assert date_literal_range(yesterday, today) == (date(2026, 10, 29), date(2026, 10, 30))
    this_week = parse("SELECT Id FROM Account WHERE CreatedDate = THIS_WEEK").where.value
    assert date_literal_range(this_week, today) == (date(2026, 10, 25), date(2026, 11, 1))  # Sunday start
    month = parse("SELECT Id FROM Account WHERE CreatedDate = LAST_MONTH").where.value
    assert date_literal_range(month, today) == (date(2026, 9, 1), date(2026, 10, 1))


def test_date_literal_operators_on_datetime_and_date_fields(schema: soql.Schema) -> None:
    c = compile_("SELECT Id FROM Account WHERE CreatedDate = TODAY AND Client_Since__c < LAST_N_DAYS:30", schema)
    assert "r.created_date >= %s::timestamptz AND r.created_date < %s::timestamptz" in c.where_sql
    assert datetime(2026, 10, 30, tzinfo=UTC) in c.params and datetime(2026, 10, 31, tzinfo=UTC) in c.params
    assert date(2026, 9, 30) in c.params  # Client_Since__c < start of the range


def test_mixing_and_or_without_parentheses_is_malformed(schema: soql.Schema) -> None:
    err = error("SELECT Id FROM Account WHERE Name = 'a' AND Type = 'b' OR Rating = 'Hot'", schema)
    assert err.code == "MALFORMED_QUERY"
    assert "unexpected token: 'OR'" in err.message
    ok = compile_("SELECT Id FROM Account WHERE (Name = 'a' AND Type = 'b') OR NOT Rating = 'Hot'", schema)
    assert " OR " in ok.where_sql and "NOT coalesce" in ok.where_sql


def test_error_shapes_match_salesforce(schema: soql.Schema) -> None:
    err = error("SELECT Nme FROM Account", schema)
    assert err.code == "INVALID_FIELD"
    assert err.message.startswith("\nSELECT Nme FROM Account\n       ^\nERROR at Row:1:Column:8\nNo such column 'Nme'")
    assert error("SELECT Id FROM Acount", schema).code == "INVALID_TYPE"
    assert error("SELECT Acount.Name FROM Contact", schema).code == "INVALID_FIELD"
    dt = error("SELECT Id FROM Account WHERE CreatedDate > 2026-01-01", schema)
    assert dt.code == "INVALID_QUERY_FILTER_OPERATOR" and "must be of type dateTime" in dt.message
    assert error("SELECT Id FROM Account WHERE AnnualRevenue = '5'", schema).code == "INVALID_QUERY_FILTER_OPERATOR"
    assert error("SELECT Id FROM Account WHERE Name = 5", schema).code == "INVALID_QUERY_FILTER_OPERATOR"
    assert error("SELECT Id FROM Account WHERE Id = 'nope'", schema).code == "INVALID_QUERY_FILTER_OPERATOR"
    assert error("SELECT Id FROM Account GROUP BY Name", schema).code == "MALFORMED_QUERY"
    assert error("SELECT Id FROM Account OFFSET 2001", schema).code == "NUMBER_OUTSIDE_VALID_RANGE"
    assert error("SELECT Id, Id FROM Account", schema).code == "DUPLICATE_FIELD"
    assert error("", schema).message == "A query string has to be specified"


def test_count_and_count_field(schema: soql.Schema) -> None:
    c = compile_("SELECT COUNT() FROM Case WHERE IsClosed = true LIMIT 5", schema)
    assert c.count_only and c.count_alias is None
    sql, params = c.count_sql(False, NOW)
    assert sql.startswith("SELECT count(*) FROM (SELECT 1 FROM sfdc.record r") and "LIMIT 5" in sql
    assert params[0] == "Case"
    agg = compile_("SELECT COUNT(Id) total FROM Case", schema)
    assert agg.count_alias == "total"


def test_order_limit_offset_and_visibility(schema: soql.Schema) -> None:
    c = compile_("SELECT Id FROM Account ORDER BY Name DESC NULLS LAST, CreatedDate LIMIT 10 OFFSET 20", schema)
    sql, _params = c.ids_sql(False, NOW)
    assert "ORDER BY lower(nullif(r.data->>'Name', '')) DESC NULLS LAST, r.created_date ASC NULLS FIRST, r.id" in sql
    assert sql.endswith("LIMIT 10 OFFSET 20")
    assert "NOT r.is_deleted" in sql
    all_sql, all_params = c.ids_sql(True, NOW)
    assert "coalesce(r.deleted_date, r.system_modstamp) >= %s" in all_sql and all_params[1] == NOW


def test_fields_function_requires_limit(schema: soql.Schema) -> None:
    assert error("SELECT FIELDS(ALL) FROM Account", schema).code == "MALFORMED_QUERY"
    c = compile_("SELECT FIELDS(CUSTOM) FROM Account LIMIT 200", schema)
    assert c.headers and all(h.endswith("__c") for h in c.headers)
    std = compile_("SELECT FIELDS(STANDARD) FROM Account", schema)
    assert "Name" in std.headers and "Health_Score__c" not in std.headers


def test_multipicklist_includes(schema: soql.Schema) -> None:
    c = compile_("SELECT Id FROM Opportunity WHERE Warehouses_Requested__c INCLUDES ('ATL1;DFW1', 'TOR1')", schema)
    assert c.where_sql.count("@>") == 2
    assert ["atl1", "dfw1"] in c.params and ["tor1"] in c.params


def test_schema_drift_fields_merge(schema: soql.Schema) -> None:
    drift = schema.with_custom_fields([("Case", "Carrier_Claim_Number__c",
                                        {"type": "string", "length": 30, "label": "Carrier Claim Number"})])
    f = drift.get("Case").field("Carrier_Claim_Number__c")
    assert f is not None and f.custom and f.length == 30 and f.label == "Carrier Claim Number"
    assert schema.get("Case").field("Carrier_Claim_Number__c") is None  # base schema untouched
    c = compile_soql("SELECT Carrier_Claim_Number__c FROM Case WHERE Carrier_Claim_Number__c != null", drift, NOW)
    assert c.headers == ["Carrier_Claim_Number__c"]


def test_output_formats() -> None:
    f_bool = soql.make_field({"name": "IsActive", "type": "boolean"})
    f_dt = soql.make_field({"name": "ClosedDate", "type": "datetime"})
    f_txt = soql.make_field({"name": "Phone", "type": "phone"})
    assert soql.out_value(f_bool, None) is False
    assert soql.out_value(f_txt, "") is None
    assert soql.out_value(f_dt, datetime(2026, 1, 5, 14, 3, 22, 456000, tzinfo=UTC)) == "2026-01-05T14:03:22.456+0000"
    assert soql.csv_value(f_dt, "2026-01-05T14:03:22.000+0000") == "2026-01-05T14:03:22.000Z"
    assert soql.csv_value(f_bool, True) == "true"


@pytest.mark.parametrize("op", ["=", "!=", "<", "<=", ">", ">="])
def test_placeholders_match_params_for_every_operator(schema: soql.Schema, op: str) -> None:
    for where in (f"CreatedDate {op} LAST_N_DAYS:7", f"CloseDate {op} THIS_MONTH", f"Amount {op} 5",
                  f"CloseDate {op} 2026-01-01", f"Name {op} 'x'"):
        c = compile_(f"SELECT Id FROM Opportunity WHERE {where} AND StageName IN ('a', 'b')", schema)
        sql, params = c.ids_sql(True, NOW)
        assert sql.count("%s") == len(params), where
