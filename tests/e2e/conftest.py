"""E2E tier guard.

Every test in this directory drives the real CLI against live Facebook — a
bare `pytest -m e2e` would otherwise fire actual scrapes against whatever
account sits in `db/accounts.db`. To make that impossible by accident, e2e
tests are opt-in: they skip unless `FBSCRAPE_RUN_E2E=1` is set. Account
availability is still required on top of the opt-in (via `require_active_account`
in the individual tests).
"""


import os

import pytest


RUN_E2E_ENV = "FBSCRAPE_RUN_E2E"


@pytest.fixture(autouse=True)
def _require_e2e_opt_in():
    if os.environ.get(RUN_E2E_ENV) != "1":
        pytest.skip(
            f"e2e tests launch real Facebook scrapes; set {RUN_E2E_ENV}=1 to run them"
        )
