"""Bounded HTTPS client for Catalog, called through the API Gateway (GET /products/{productId})."""
import contextvars
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from http import HTTPStatus
from typing import Iterable, Optional

from ..domain.errors import CatalogUnavailable
from ..domain.order import CatalogProduct

logger = logging.getLogger(__name__)

# The API Gateway request ID of the request being handled; forwarded to Catalog as x-correlation-id.
# The handler sets it per request; it is read here so Catalog logs can be joined with Orders logs.
correlation_id = contextvars.ContextVar("correlation_id", default="")


class _Retryable(Exception):
    pass


class HttpCatalogClient:
    def __init__(self, base_url: str, *, timeout_seconds: float, max_attempts: int,
                 backoff_seconds: float = 0.1, max_workers: int = 5, opener=None):
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("Catalog base URL must be http(s)")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._max_workers = max_workers
        self._open = opener or urllib.request.build_opener().open

    def get_products(self, product_ids: Iterable[str], deadline: float) -> dict[str, Optional[CatalogProduct]]:
        ids = list(dict.fromkeys(product_ids))
        if not ids:
            return {}
        correlation = correlation_id.get()
        pool = ThreadPoolExecutor(max_workers=min(self._max_workers, len(ids)))
        try:
            futures = {product_id: pool.submit(self._fetch, product_id, deadline, correlation) for product_id in ids}
            results = {}
            for product_id, future in futures.items():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CatalogUnavailable("Catalog did not answer in time.")
                results[product_id] = future.result(timeout=remaining)
            return results
        except FutureTimeout as exc:
            raise CatalogUnavailable("Catalog did not answer in time.") from exc
        finally:
            # Do not wait for stragglers: the deadline is the bound, not the slowest socket.
            pool.shutdown(wait=False, cancel_futures=True)

    def _fetch(self, product_id: str, deadline: float, correlation: str) -> Optional[CatalogProduct]:
        url = f"{self._base_url}/products/{urllib.parse.quote(product_id, safe='')}"
        for attempt in range(1, self._max_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                break
            try:
                return self._get_once(url, product_id, min(self._timeout, remaining), correlation)
            except _Retryable as exc:
                logger.warning(json.dumps({"msg": "catalog attempt failed", "productId": product_id,
                                           "attempt": attempt, "reason": str(exc), "requestId": correlation}))
                if attempt < self._max_attempts and deadline - time.monotonic() > self._backoff:
                    time.sleep(self._backoff)
        raise CatalogUnavailable("Catalog did not answer in time.")

    def _get_once(self, url: str, product_id: str, timeout: float, correlation: str) -> Optional[CatalogProduct]:
        headers = {"accept": "application/json"}
        if correlation:
            headers["x-correlation-id"] = correlation
        request = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with self._open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == HTTPStatus.NOT_FOUND:
                return None
            if exc.code >= HTTPStatus.INTERNAL_SERVER_ERROR or exc.code == HTTPStatus.TOO_MANY_REQUESTS:
                raise _Retryable(f"HTTP {exc.code}") from exc
            raise CatalogUnavailable(f"Catalog answered HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _Retryable(type(exc).__name__) from exc
        except ValueError as exc:
            raise CatalogUnavailable("Catalog returned invalid JSON.") from exc
        return _product_from_payload(payload, product_id)


def _product_from_payload(payload: dict, product_id: str) -> CatalogProduct:
    try:
        price = payload["priceCents"]
        if isinstance(price, bool) or not isinstance(price, int) or payload["productId"] != product_id:
            raise ValueError("unexpected product payload")
        return CatalogProduct(product_id, str(payload["name"]), price, str(payload["currency"]),
                              payload["available"] is True)
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogUnavailable("Catalog returned an unexpected product.") from exc
