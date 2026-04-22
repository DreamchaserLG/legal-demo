import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import quote, urlparse

import requests
from websocket import create_connection

from app.core.config import settings


class LLMServiceError(RuntimeError):
    pass


class LLMNotConfiguredError(LLMServiceError):
    pass


_HTTP_SESSION = requests.Session()
_HTTP_SESSION.trust_env = False


def _timeout_value() -> int:
    return max(settings.request_timeout, settings.llm_timeout, 30)


def get_llm_provider() -> str:
    provider = (settings.llm_provider or "spark").strip().lower()
    if provider in {"openai", "spark"}:
        return provider
    return "spark"


def get_llm_model_name() -> str:
    if get_llm_provider() == "openai":
        return settings.openai_model
    return settings.spark_model


def is_llm_configured() -> bool:
    provider = get_llm_provider()
    if provider == "openai":
        return bool(settings.openai_api_key)
    return bool(settings.spark_api_key and settings.spark_api_secret and settings.spark_app_id)


def _extract_openai_output_text(payload: dict) -> str:
    output_text = payload.get("output_text")
    if output_text:
        return output_text

    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                return content["text"]

    raise LLMServiceError("Model response did not contain output_text.")


def _extract_chat_output_text(payload: dict) -> str:
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMServiceError("Model response did not contain chat completion content.") from exc


def _parse_json_text(text: str) -> dict:
    decoder = json.JSONDecoder()
    candidates = []
    stripped = (text or "").strip()
    if stripped:
        candidates.append(stripped)

        if stripped.startswith("```"):
            parts = stripped.split("```")
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if part.lower().startswith("json"):
                    part = part[4:].strip()
                candidates.append(part)

    for candidate in candidates:
        try:
            parsed, end = decoder.raw_decode(candidate)
            if candidate[end:].strip():
                continue
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    for index, char in enumerate(stripped):
        if char not in "{[":
            continue
        try:
            parsed, _ = decoder.raw_decode(stripped[index:])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    raise LLMServiceError("Model returned invalid JSON.")


def _create_openai_structured_response(
    schema_name: str,
    schema: dict,
    instructions: str,
    user_input: str,
) -> dict:
    payload = {
        "model": settings.openai_model,
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": user_input,
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
    }

    if settings.openai_reasoning_effort:
        payload["reasoning"] = {"effort": settings.openai_reasoning_effort}

    try:
        response = _HTTP_SESSION.post(
            f"{settings.openai_base_url}/responses",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_timeout_value(),
        )
    except requests.RequestException as exc:
        raise LLMServiceError(f"OpenAI request failed: {exc}") from exc

    if response.status_code >= 400:
        raise LLMServiceError(
            f"OpenAI API request failed with status {response.status_code}: {response.text}"
        )

    data = response.json()
    output_text = _extract_openai_output_text(data)
    parsed = _parse_json_text(output_text)

    return {
        "data": parsed,
        "model": data.get("model", settings.openai_model),
        "response_id": data.get("id", ""),
        "provider": "openai",
    }


def _spark_rfc1123_date() -> str:
    return format_datetime(datetime.now(timezone.utc), usegmt=True)


def _spark_signed_url() -> str:
    parsed = urlparse(settings.spark_base_url)
    if parsed.scheme not in {"ws", "wss"}:
        raise LLMServiceError("SPARK_BASE_URL must be a ws:// or wss:// URL for Spark WebSocket mode.")

    host = parsed.netloc
    path = parsed.path or "/"
    date = _spark_rfc1123_date()
    signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
    digest = hmac.new(
        settings.spark_api_secret.encode("utf-8"),
        signature_origin.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("utf-8")
    authorization_origin = (
        f'api_key="{settings.spark_api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")
    return (
        f"{settings.spark_base_url}"
        f"?authorization={quote(authorization)}"
        f"&date={quote(date)}"
        f"&host={quote(host)}"
    )


def _spark_messages(schema_name: str, schema: dict, instructions: str, user_input: str) -> list[dict]:
    schema_text = json.dumps(schema, ensure_ascii=False)
    return [
        {
            "role": "system",
            "content": (
                f"{instructions}\n\n"
                "You must return one JSON object only. Do not use markdown fences. "
                "Do not add commentary before or after the JSON. "
                f"The JSON must satisfy schema '{schema_name}'."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Schema name: {schema_name}\n"
                f"JSON schema: {schema_text}\n"
                "Return valid JSON only.\n"
                f"User input:\n{user_input}"
            ),
        },
    ]


def _spark_request_payload(messages: list[dict]) -> dict:
    return {
        "header": {
            "app_id": settings.spark_app_id,
            "uid": "legal-demo",
        },
        "parameter": {
            "chat": {
                "domain": settings.spark_domain,
                "temperature": settings.spark_temperature,
            }
        },
        "payload": {
            "message": {
                "text": messages,
            }
        },
    }


def _read_spark_stream(ws) -> tuple[str, str]:
    parts: list[str] = []
    response_id = ""

    while True:
        try:
            raw = ws.recv()
        except Exception as exc:
            raise LLMServiceError(f"Spark WebSocket receive failed: {exc}") from exc

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMServiceError(f"Spark WebSocket returned invalid JSON frame: {exc}") from exc

        header = data.get("header") or {}
        response_id = response_id or header.get("sid", "")
        code = header.get("code", 0)
        if code:
            message = header.get("message") or "Spark WebSocket returned an error."
            raise LLMServiceError(f"Spark WebSocket error {code}: {message}")

        payload = data.get("payload") or {}
        choices = payload.get("choices") or {}
        for item in choices.get("text") or []:
            content = item.get("content")
            if content:
                parts.append(content)

        if choices.get("status") == 2:
            break

    return "".join(parts).strip(), response_id


def _create_spark_structured_response(
    schema_name: str,
    schema: dict,
    instructions: str,
    user_input: str,
) -> dict:
    messages = _spark_messages(schema_name=schema_name, schema=schema, instructions=instructions, user_input=user_input)
    payload = _spark_request_payload(messages)
    signed_url = _spark_signed_url()

    try:
        ws = create_connection(signed_url, timeout=_timeout_value())
    except Exception as exc:
        raise LLMServiceError(f"Spark WebSocket connect failed: {exc}") from exc

    try:
        ws.send(json.dumps(payload, ensure_ascii=False))
        output_text, response_id = _read_spark_stream(ws)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if not output_text:
        raise LLMServiceError("Spark WebSocket returned an empty response.")

    parsed = _parse_json_text(output_text)

    return {
        "data": parsed,
        "model": settings.spark_model,
        "response_id": response_id,
        "provider": "spark",
    }


def create_structured_response(
    schema_name: str,
    schema: dict,
    instructions: str,
    user_input: str,
) -> dict:
    if not is_llm_configured():
        provider = get_llm_provider()
        if provider == "openai":
            raise LLMNotConfiguredError("OPENAI_API_KEY is not configured.")
        raise LLMNotConfiguredError(
            "Spark WebSocket credentials are not configured. Set SPARK_API_KEY, SPARK_API_SECRET, and SPARK_APP_ID."
        )

    provider = get_llm_provider()
    if provider == "openai":
        return _create_openai_structured_response(schema_name, schema, instructions, user_input)
    return _create_spark_structured_response(schema_name, schema, instructions, user_input)
