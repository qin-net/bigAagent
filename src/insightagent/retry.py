from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")


class RetryableError(RuntimeError):
    retry_after: Optional[float] = None


class RetryableProviderError(RetryableError):
    pass


class NonRetryableError(RuntimeError):
    pass


class RetryExhaustedError(RuntimeError):
    def __init__(self, attempts: int) -> None:
        super().__init__("Retry attempts exhausted after {} calls".format(attempts))
        self.attempts = attempts


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 1.0
    backoff_factor: float = 2.0
    jitter_min: float = 0.0
    jitter_max: float = 0.25
    max_delay: float = 30.0
    respect_retry_after: bool = True


@dataclass(frozen=True)
class ErrorClassification:
    retryable: bool
    retry_after: Optional[float] = None
    category: str = "unknown"


ErrorClassifier = Callable[[Exception], ErrorClassification]
SleepCallable = Callable[[float], Awaitable[None]]


def default_error_classifier(error: Exception) -> ErrorClassification:
    if isinstance(error, NonRetryableError):
        return ErrorClassification(False, category="non_retryable")
    if isinstance(error, RetryableError):
        return ErrorClassification(
            True,
            retry_after=getattr(error, "retry_after", None),
            category="retryable",
        )

    status_code = getattr(error, "status_code", None)
    if status_code in {429, 500, 503}:
        retry_after = getattr(error, "retry_after", None)
        return ErrorClassification(
            True, retry_after=retry_after, category="http_{}".format(status_code)
        )
    if status_code in {400, 401, 402, 403, 404, 422}:
        return ErrorClassification(
            False, category="http_{}".format(status_code)
        )
    if isinstance(error, (TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return ErrorClassification(True, category="network")
    return ErrorClassification(False, category=type(error).__name__)


class ExponentialBackoff:
    def __init__(
        self,
        config: Optional[RetryConfig] = None,
        *,
        classifier: ErrorClassifier = default_error_classifier,
        sleep: SleepCallable = asyncio.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
        on_retry: Optional[
            Callable[[Exception, ErrorClassification, int, float], Any]
        ] = None,
    ) -> None:
        self.config = config or RetryConfig()
        self.classifier = classifier
        self.sleep = sleep
        self.random_uniform = random_uniform
        self.on_retry = on_retry

    async def execute(
        self, operation: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any
    ) -> T:
        retry_count = 0

        while True:
            try:
                return await operation(*args, **kwargs)
            except Exception as error:
                classification = self.classifier(error)
                if not classification.retryable:
                    raise
                if retry_count >= self.config.max_retries:
                    raise RetryExhaustedError(retry_count + 1) from error

                jitter = self.random_uniform(
                    self.config.jitter_min, self.config.jitter_max
                )
                delay = (
                    self.config.base_delay
                    * (self.config.backoff_factor ** retry_count)
                    + jitter
                )
                delay = min(delay, self.config.max_delay)
                if (
                    self.config.respect_retry_after
                    and classification.retry_after is not None
                ):
                    delay = max(delay, classification.retry_after)

                if self.on_retry:
                    self.on_retry(
                        error, classification, retry_count, delay
                    )
                await self.sleep(delay)
                retry_count += 1
