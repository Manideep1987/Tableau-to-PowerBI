import os
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tableau2pbi.converter import convert  # noqa: E402
from tableau2pbi.parser import read_workbook  # noqa: E402
from tableau2pbi.validation.runner import DataBinding, validate  # noqa: E402

SAMPLES = os.path.join(ROOT, "samples", "workbooks")
DB = os.path.join(ROOT, "samples", "data", "superstore.db")
AS_OF = date(2025, 12, 31)
NAMES = ["Superstore_Sales.twb", "Finance_CustomSQL.twb", "HR_Analytics.twbx", "Invoice_Health.twb"]


def _ensure_samples():
    if not all(os.path.exists(os.path.join(SAMPLES, n)) for n in NAMES) or not os.path.exists(DB):
        import runpy
        runpy.run_path(os.path.join(ROOT, "samples", "generate_samples.py"), run_name="__main__")


_ensure_samples()


@pytest.fixture(scope="session")
def workbooks():
    return {n: read_workbook(os.path.join(SAMPLES, n)) for n in NAMES}


@pytest.fixture(scope="session")
def conversions(workbooks):
    return {n: convert(wb) for n, wb in workbooks.items()}


@pytest.fixture(scope="session")
def validations(conversions, workbooks):
    out = {}
    for n, res in conversions.items():
        out[n] = validate(res, DataBinding(sqlite=DB, packaged=workbooks[n].packaged_files), today=AS_OF)
    return out


def binding_for(wb):
    return DataBinding(sqlite=DB, packaged=wb.packaged_files)
