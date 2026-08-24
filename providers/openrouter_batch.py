from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from common.models.errors import NonRetryableAgentError, RetryableAgentError


BATCH_MODEL_SUFFIX = ":batch"
BATCH_ENDPOINT = "/api/beta/batches"
BATCH_CHAT_ENDPOINT = "/v1/chat/completions"
BATCH_TERMINAL_STATUSES = frozenset({"completed", "failed", "expired", "cancelled"})


def is_openrouter_batch_model(model_id: str) -> bool:
    return model_id.strip().lower().endswith(BATCH_MODEL_SUFFIX)


def openrouter_batch_base_model(model_id: str) -> str:
    value = model_id.strip()
    if not is_openrouter_batch_model(value):
        raise ValueError(f"OpenRouter batch model must end with {BATCH_MODEL_SUFFIX}")
    base_model = value[: -len(BATCH_MODEL_SUFFIX)]
    if not base_model:
        raise ValueError("OpenRouter batch model base ID is empty")
    return base_model


class OpenRouterBatchClient:
    """Durable one-request adapter for OpenRouter's asynchronous Batch API.

    PRDCP still reserves one logical provider invocation before this adapter is
    entered.  The sidecar stores only request hashes and remote identifiers; it
    deliberately never stores prompts, API keys, or result bodies.  Once a
    remote ID exists, recovery only polls that ID and cannot submit a duplicate.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        default_timeout_seconds: int,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required")
        if default_timeout_seconds <= 0:
            raise ValueError("OpenRouter timeout must be positive")
        if poll_interval_seconds < 0:
            raise ValueError("OpenRouter batch poll interval cannot be negative")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_timeout_seconds = default_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def execute_chat(
        self,
        *,
        model_id: str,
        request_body: dict,
        reservation_path: Path | None,
        timeout_seconds: int | None,
        invocation_discriminator: str = "structured-output",
    ) -> dict:
        if reservation_path is None:
            raise NonRetryableAgentError(
                "OPENROUTER_BATCH_RESERVATION_REQUIRED: batch submission was blocked",
                provider="openrouter",
                model_id=model_id,
                automatic_retry_allowed=False,
            )
        request_timeout = (
            self.default_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if request_timeout <= 0:
            raise ValueError("OpenRouter request timeout must be positive")

        base_model = openrouter_batch_base_model(model_id)
        body = dict(request_body)
        # OpenRouter documents inheritance from the batch-level model, but its
        # generated model examples also repeat the exact base model in every
        # request body.  Preserve that canonical wire shape because some beta
        # batch backends do not materialize the inherited value consistently.
        body["model"] = base_model
        # Provider routing preferences are a synchronous routing contract.  A
        # batch model already selects the batch-capable provider endpoint.
        body.pop("provider", None)
        custom_seed = json.dumps(
            {
                "model": base_model,
                "body": body,
                "invocation_discriminator": invocation_discriminator,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        custom_digest = hashlib.sha256(custom_seed.encode("utf-8")).hexdigest()
        custom_id = f"prdcp-{custom_digest[:32]}"
        batch_request = {
            "endpoint": BATCH_CHAT_ENDPOINT,
            "model": base_model,
            "requests": [{"custom_id": custom_id, "body": body}],
        }
        request_sha256 = hashlib.sha256(
            json.dumps(
                batch_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        state_path = self.state_path(
            Path(reservation_path),
            invocation_discriminator=invocation_discriminator,
        )
        state = self._load_or_prepare_state(
            state_path=state_path,
            request_sha256=request_sha256,
            custom_id=custom_id,
            model_id=model_id,
            base_model=base_model,
        )
        batch_id = state.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            try:
                created = self._request_json(
                    method="POST",
                    url=self._batch_collection_url(),
                    body=batch_request,
                    timeout_seconds=request_timeout,
                    model_id=model_id,
                    submission=True,
                )
            except Exception as exc:
                self._record_failure(
                    state_path,
                    state,
                    status="submission_ambiguous",
                    detail=type(exc).__name__,
                )
                raise
            batch_id = created.get("id")
            if not isinstance(batch_id, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", batch_id
            ):
                state["submission_response_sha256"] = hashlib.sha256(
                    json.dumps(
                        created,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                state["submission_response_keys"] = sorted(str(key) for key in created)
                for field in ("service", "status"):
                    value = created.get(field)
                    if isinstance(value, (str, int, float, bool)):
                        state[f"submission_{field}"] = str(value)[:120]
                self._record_failure(
                    state_path,
                    state,
                    status="submission_contract_failed",
                    detail="missing_batch_id",
                )
                raise NonRetryableAgentError(
                    "OPENROUTER_BATCH_CONTRACT_ERROR: submission returned no batch ID",
                    provider="openrouter",
                    model_id=model_id,
                    automatic_retry_allowed=False,
                )
            state.update(
                {
                    "batch_id": batch_id,
                    "status": str(created.get("status") or "validating"),
                    "submitted_at": self._now(),
                    "updated_at": self._now(),
                }
            )
            self._replace_json(state_path, state)

        deadline = time.monotonic() + request_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetryableAgentError(
                    "OPENROUTER_BATCH_PENDING: remote batch is still running; recovery may "
                    "poll the saved batch ID without another paid submission",
                    provider="openrouter",
                    model_id=model_id,
                    automatic_retry_allowed=True,
                )
            try:
                batch = self._request_json(
                    method="GET",
                    url=f"{self._batch_collection_url()}/{quote(batch_id, safe='')}",
                    body=None,
                    timeout_seconds=max(1, min(int(remaining), request_timeout)),
                    model_id=model_id,
                    submission=False,
                )
            except RetryableAgentError as exc:
                # Batch creation and read replicas are eventually consistent.
                # Retrying this GET cannot duplicate or bill a generation.
                state["poll_failure_count"] = int(
                    state.get("poll_failure_count") or 0
                ) + 1
                state["last_poll_failure_class"] = type(exc).__name__
                state["last_poll_http_status"] = exc.http_status
                state["updated_at"] = self._now()
                self._replace_json(state_path, state)
                time.sleep(min(self.poll_interval_seconds, max(remaining, 0)))
                continue
            status = str(batch.get("status") or "unknown")
            state.update(
                {
                    "status": status,
                    "updated_at": self._now(),
                    "request_counts": self._safe_request_counts(
                        batch.get("request_counts")
                    ),
                }
            )
            if status in BATCH_TERMINAL_STATUSES:
                state["finalized_at"] = self._now()
                usage = batch.get("usage")
                if isinstance(usage, dict):
                    state["usage"] = {
                        key: usage[key]
                        for key in (
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                            "cost",
                            "is_byok",
                        )
                        if key in usage
                    }
                self._replace_json(state_path, state)
                if status != "completed":
                    raise NonRetryableAgentError(
                        f"OPENROUTER_BATCH_FAILED: terminal status={status}",
                        provider="openrouter",
                        model_id=model_id,
                        automatic_retry_allowed=False,
                    )
                return self._completed_response(
                    batch=batch,
                    custom_id=custom_id,
                    model_id=model_id,
                    state=state,
                    state_path=state_path,
                )
            self._replace_json(state_path, state)
            time.sleep(min(self.poll_interval_seconds, max(remaining, 0)))

    @classmethod
    def has_resumable_state(
        cls,
        reservation_path: Path,
        *,
        invocation_discriminator: str = "structured-output",
    ) -> bool:
        state_path = cls.state_path(
            reservation_path,
            invocation_discriminator=invocation_discriminator,
        )
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return isinstance(state, dict) and isinstance(state.get("batch_id"), str)

    @classmethod
    def has_terminal_failed_state(
        cls,
        reservation_path: Path,
        *,
        model_id: str,
        invocation_discriminator: str = "structured-output",
    ) -> bool:
        """Confirm a correlated remote Batch failure before a new identity is issued."""

        state_path = cls.state_path(
            reservation_path,
            invocation_discriminator=invocation_discriminator,
        )
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(state, dict)
            and state.get("model_id") == model_id
            and isinstance(state.get("batch_id"), str)
            and state.get("status") in {"failed", "expired", "cancelled"}
        )

    @staticmethod
    def state_path(
        reservation_path: Path,
        *,
        invocation_discriminator: str,
    ) -> Path:
        path = Path(reservation_path)
        reservation_root = next(
            (
                parent
                for parent in (path.parent, *path.parents)
                if parent.name in {
                    "provider_call_reservations",
                    "retrieval_call_reservations",
                }
            ),
            None,
        )
        discriminator = hashlib.sha256(
            invocation_discriminator.encode("utf-8")
        ).hexdigest()[:16]
        if reservation_root is None:
            return path.parent / ".openrouter_batch_jobs" / (
                f"{path.stem}.{discriminator}.json"
            )
        relative = path.relative_to(reservation_root)
        return (
            reservation_root.parent
            / "openrouter_batch_jobs"
            / reservation_root.name
            / relative.parent
            / f"{relative.stem}.{discriminator}.json"
        )

    def _load_or_prepare_state(
        self,
        *,
        state_path: Path,
        request_sha256: str,
        custom_id: str,
        model_id: str,
        base_model: str,
    ) -> dict:
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise NonRetryableAgentError(
                    "OPENROUTER_BATCH_STATE_INVALID: saved batch state is unreadable",
                    provider="openrouter",
                    model_id=model_id,
                    automatic_retry_allowed=False,
                ) from exc
            if not isinstance(state, dict) or any(
                state.get(key) != expected
                for key, expected in (
                    ("request_sha256", request_sha256),
                    ("custom_id", custom_id),
                    ("model_id", model_id),
                    ("base_model", base_model),
                )
            ):
                raise NonRetryableAgentError(
                    "OPENROUTER_BATCH_STATE_MISMATCH: stale or mismatched batch result rejected",
                    provider="openrouter",
                    model_id=model_id,
                    automatic_retry_allowed=False,
                )
            if not state.get("batch_id"):
                raise NonRetryableAgentError(
                    "OPENROUTER_BATCH_SUBMISSION_AMBIGUOUS: reservation exists without a "
                    "remote batch ID; explicit operator recovery is required",
                    provider="openrouter",
                    model_id=model_id,
                    automatic_retry_allowed=False,
                )
            return state

        state = {
            "schema_version": 1,
            "transport": "openrouter_batch",
            "model_id": model_id,
            "base_model": base_model,
            "custom_id": custom_id,
            "request_sha256": request_sha256,
            "status": "prepared",
            "prepared_at": self._now(),
            "updated_at": self._now(),
        }
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            with state_path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            return self._load_or_prepare_state(
                state_path=state_path,
                request_sha256=request_sha256,
                custom_id=custom_id,
                model_id=model_id,
                base_model=base_model,
            )
        except OSError as exc:
            raise NonRetryableAgentError(
                "OPENROUTER_BATCH_STATE_ERROR: batch submission blocked before HTTP",
                provider="openrouter",
                model_id=model_id,
                automatic_retry_allowed=False,
            ) from exc
        return state

    def _completed_response(
        self,
        *,
        batch: dict,
        custom_id: str,
        model_id: str,
        state: dict,
        state_path: Path,
    ) -> dict:
        results = batch.get("results")
        if not isinstance(results, list):
            raise NonRetryableAgentError(
                "OPENROUTER_BATCH_CONTRACT_ERROR: completed batch has no results",
                provider="openrouter",
                model_id=model_id,
                automatic_retry_allowed=False,
            )
        matches = [
            item
            for item in results
            if isinstance(item, dict) and item.get("custom_id") == custom_id
        ]
        if len(matches) != 1:
            raise NonRetryableAgentError(
                "OPENROUTER_BATCH_CONTRACT_ERROR: custom_id result correlation failed",
                provider="openrouter",
                model_id=model_id,
                automatic_retry_allowed=False,
            )
        item = matches[0]
        item_error = item.get("error")
        response = item.get("response")
        if item_error is not None or not isinstance(response, dict):
            raise NonRetryableAgentError(
                "OPENROUTER_BATCH_ITEM_FAILED: batch request returned an error",
                provider="openrouter",
                model_id=model_id,
                automatic_retry_allowed=False,
            )
        status_code = response.get("status_code")
        body = response.get("body")
        if status_code != 200 or not isinstance(body, dict):
            raise NonRetryableAgentError(
                f"OPENROUTER_BATCH_ITEM_FAILED: response status={status_code}",
                http_status=status_code if isinstance(status_code, int) else None,
                provider="openrouter",
                model_id=model_id,
                automatic_retry_allowed=False,
            )
        generation_id = body.get("id")
        if isinstance(generation_id, str):
            state["generation_id"] = generation_id
        state["status"] = "completed"
        state["result_correlated_at"] = self._now()
        self._replace_json(state_path, state)
        return body

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        body: dict | None,
        timeout_seconds: int,
        model_id: str,
        submission: bool,
    ) -> dict:
        request = Request(
            url,
            data=(
                json.dumps(body, ensure_ascii=False).encode("utf-8")
                if body is not None
                else None
            ),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            safe_detail = detail.replace(self.api_key, "<redacted>")[:500]
            error_type = NonRetryableAgentError if submission else RetryableAgentError
            raise error_type(
                f"OpenRouter Batch HTTP {exc.code}: {safe_detail}",
                http_status=exc.code,
                provider="openrouter",
                model_id=model_id,
                automatic_retry_allowed=False if submission else True,
            ) from exc
        except (URLError, TimeoutError, ConnectionError, socket.timeout) as exc:
            error_type = NonRetryableAgentError if submission else RetryableAgentError
            raise error_type(
                (
                    "OPENROUTER_BATCH_SUBMISSION_AMBIGUOUS: remote acceptance is unknown"
                    if submission
                    else "OPENROUTER_BATCH_POLL_FAILED: safe GET polling may be retried"
                ),
                provider="openrouter",
                model_id=model_id,
                automatic_retry_allowed=False if submission else True,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            error_type = NonRetryableAgentError if submission else RetryableAgentError
            raise error_type(
                "OPENROUTER_BATCH_CONTRACT_ERROR: invalid JSON envelope",
                provider="openrouter",
                model_id=model_id,
                automatic_retry_allowed=False if submission else True,
            ) from exc
        if not isinstance(payload, dict):
            raise NonRetryableAgentError(
                "OPENROUTER_BATCH_CONTRACT_ERROR: response envelope must be an object",
                provider="openrouter",
                model_id=model_id,
                automatic_retry_allowed=False,
            )
        return payload

    def _batch_collection_url(self) -> str:
        parsed = urlsplit(self.base_url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        return origin + BATCH_ENDPOINT

    @staticmethod
    def _safe_request_counts(value: object) -> dict:
        if not isinstance(value, dict):
            return {}
        return {
            key: value[key]
            for key in ("total", "completed", "failed")
            if isinstance(value.get(key), int)
        }

    @classmethod
    def _record_failure(
        cls,
        state_path: Path,
        state: dict,
        *,
        status: str,
        detail: str,
    ) -> None:
        state.update(
            {
                "status": status,
                "failure_class": detail[:120],
                "updated_at": cls._now(),
            }
        )
        cls._replace_json(state_path, state)

    @staticmethod
    def _replace_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
