"""Tests for bulk staging duplicate-key recovery."""

from app.services.bulk_staging_recovery import is_staging_pk_duplicate_error


def test_is_staging_pk_duplicate_error_truncated_job_message():
    msg = (
        'Partition swap failed (attempt 121); will retry automatically. '
        'IntegrityError: (psycopg2.errors.UniqueViolation) duplicate key value violates '
        'unique constraint "bulk_staging_b3119c1f_93bc_4875_a083_0e6ca02f8337_pkey"\n'
        "DETAIL:  Key (id, collection_id, part_index)=(019f34bb-c747-7a0e-b9a4-a3c61aa2c485, "
        "car-apps-ac, 0) already exists.\n\n[SQL: \n                    IN"
    )
    assert is_staging_pk_duplicate_error(msg)


def test_is_staging_pk_duplicate_error_pgcode():
    class FakeOrig(Exception):
        pgcode = "23505"

    class FakeExc(Exception):
        orig = FakeOrig()

    assert is_staging_pk_duplicate_error(FakeExc())


def test_is_staging_pk_duplicate_error_unrelated():
    assert not is_staging_pk_duplicate_error("deadlock detected")
    assert not is_staging_pk_duplicate_error("would overlap partition")
