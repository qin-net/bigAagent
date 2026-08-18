import pytest

from insightagent.retry import (
    ExponentialBackoff,
    NonRetryableError,
    RetryConfig,
    RetryableError,
)


@pytest.mark.asyncio
async def test_exponential_backoff_retries_with_jitter():
    attempts = 0
    delays = []

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RetryableError("temporary")
        return "ok"

    async def fake_sleep(delay):
        delays.append(delay)

    retry = ExponentialBackoff(
        RetryConfig(
            max_retries=3,
            base_delay=1.0,
            backoff_factor=2.0,
            jitter_min=0.5,
            jitter_max=0.5,
        ),
        sleep=fake_sleep,
    )

    assert await retry.execute(operation) == "ok"
    assert attempts == 3
    assert delays == [1.5, 2.5]


@pytest.mark.asyncio
async def test_non_retryable_error_fails_immediately():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        raise NonRetryableError("invalid input")

    retry = ExponentialBackoff()
    with pytest.raises(NonRetryableError):
        await retry.execute(operation)
    assert attempts == 1
