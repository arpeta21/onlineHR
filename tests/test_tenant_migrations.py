from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app import migrate_payslip_schema
from models import Payslip


def test_payslip_schema_migration_repairs_legacy_table_before_orm_query():
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text(
                'CREATE TABLE "user" (id INTEGER PRIMARY KEY, tenant_id INTEGER NULL)'
            ))
            connection.execute(text(
                "CREATE TABLE payslip ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, month INTEGER NOT NULL, "
                "year INTEGER NOT NULL, monthly_ctc_used FLOAT NOT NULL, gross_pay FLOAT NOT NULL, "
                "total_deductions FLOAT NOT NULL, net_pay FLOAT NOT NULL)"
            ))
            connection.execute(text(
                'INSERT INTO "user" (id, tenant_id) VALUES (1, 17), (2, NULL)'
            ))
            connection.execute(text(
                "INSERT INTO payslip (id, user_id, month, year, monthly_ctc_used, gross_pay, "
                "total_deductions, net_pay) "
                "VALUES (101, 1, 1, 2025, 5000, 4200.50, 799.50, 4200.50), "
                "(102, 2, 2, 2025, 2000, 1800.25, 199.75, 1800.25)"
            ))

        migrate_payslip_schema(engine)
        migrate_payslip_schema(engine)

        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("payslip")}
        indexes = {index["name"]: index for index in inspector.get_indexes("payslip")}
        assert set(column.name for column in Payslip.__table__.columns).issubset(columns)
        assert columns["tenant_id"]["nullable"] is True
        assert columns["esi_deduction"]["nullable"] is True
        assert "ix_payslip_tenant_id" in indexes
        assert indexes["ix_payslip_tenant_id"]["column_names"] == ["tenant_id"]

        with Session(engine) as session:
            rows = session.query(Payslip).order_by(Payslip.id).all()
            assert len(rows) == 2
            assert rows[0].tenant_id == 17
            assert rows[0].gross_pay == 4200.5
            assert rows[0].esi_deduction == 0.0
            assert rows[0].tax_regime == "new_regime"
            assert rows[0].statutory_note is None
            assert rows[0].generated_at is None
            assert rows[0].generated_by_id is None
            assert rows[1].tenant_id is None
            assert rows[1].net_pay == 1800.25
    finally:
        engine.dispose()