import pytest

from pinchana_tiktok import main


class ImmediateRunner:
    async def run(self, function, *args, **_kwargs):
        return function(*args)


@pytest.fixture(autouse=True)
def immediate_upstream_runner(monkeypatch):
    """Keep unit tests deterministic and free of production pacing delays."""
    monkeypatch.setattr(main, "UPSTREAM_RUNNER", ImmediateRunner())
