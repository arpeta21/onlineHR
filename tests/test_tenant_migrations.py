from sqlalchemy import create_engine, inspect, text

from app import migrate_payslip_tenant_id


def test_payslip_tenant_migration_adds_backfills_and_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text(
                'CREATE TABLE "user" (id INTEGER PRIMARY KEY, tenant_id INTEGER NULL)'
            ))
            connection.execute(text(
                "CREATE TABLE payslip ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "gross_pay FLOAT NOT NULL)"
            ))
            connection.execute(text(
                'INSERT INTO "user" (id, tenant_id) VALUES (1, 17), (2, NULL)'
            ))
            connection.execute(text(
                "INSERT INTO payslip (id, user_id, gross_pay) "
                "VALUES (101, 1, 4200.50), (102, 2, 1800.25)"
            ))

        migrate_payslip_tenant_id(engine)
        migrate_payslip_tenant_id(engine)

        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("payslip")}
        indexes = {index["name"]: index for index in inspector.get_indexes("payslip")}
        assert columns["tenant_id"]["nullable"] is True
        assert "ix_payslip_tenant_id" in indexes
        assert indexes["ix_payslip_tenant_id"]["column_names"] == ["tenant_id"]

        with engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT id, user_id, tenant_id, gross_pay FROM payslip ORDER BY id"
            )).all()
        assert rows == [(101, 1, 17, 4200.5), (102, 2, None, 1800.25)]
    finally:
        engine.dispose()