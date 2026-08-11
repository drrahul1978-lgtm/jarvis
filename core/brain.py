"""The reasoning loop: talks to a local Ollama server, runs tools, streams back.

`Brain.ask` is a generator of events rather than a blob of text. That keeps the
display layer dumb and swappable — the console prints tokens as they arrive, and
a future voice layer can buffer them into sentences for speech instead.

Events yielded:
    ("token",  str)   a fragment of the visible answer
    ("think",  str)   a fragment of the model's private reasoning
    ("tool",   dict)  {"name":..., "arguments":..., "result":...} after a call
    ("error",  str)   something went wrong; the turn is over
    ("done",   str)   the complete answer text
"""

import json
import re
import urllib.error
import urllib.request

from . import config, tools

_THINK_TAG = re.compile(r"<think>.*?</think>", re.DOTALL)
_OPEN_THINK = re.compile(r"<think>.*", re.DOTALL)


class BrainOffline(Exception):
    """Ollama is not reachable."""


class Brain:
    def __init__(self, model: str | None = None, host: str | None = None):
        self.model = model or config.MODEL
        self.host = (host or config.OLLAMA_HOST).rstrip("/")
        # Only reasoning models accept a `think` field; the rest reject the
        # request outright. Dropped permanently the first time one complains.
        self.supports_think = True

    # -- server checks ----------------------------------------------------
    def _request(self, path: str, payload: dict | None = None, stream: bool = False):
        url = f"{self.host}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        # A cold model load on a Pi can take a while; be patient on first token.
        return urllib.request.urlopen(req, timeout=None if stream else 15)

    def is_up(self) -> bool:
        try:
            with self._request("/api/tags") as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    def installed_models(self) -> list[str]:
        try:
            with self._request("/api/tags") as resp:
                data = json.loads(resp.read())
            return [m["name"] for m in data.get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    def prefer_own_build(self, name: str = "jarvis") -> bool:
        """Switch to the purpose-built model if it has been created.

        Falls back silently to the base model, so a machine where the build was
        never run still works — it just gets its identity from the prompt alone.
        """
        available = self.installed_models()
        for candidate in (name, f"{name}:latest"):
            if candidate in available:
                self.model = candidate
                return True
        return False

    def has_model(self) -> bool:
        installed = self.installed_models()
        # "qwen3:8b" should match an installed "qwen3:8b" exactly, but Ollama
        # also reports bare names as "name:latest".
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        return wanted in installed or self.model in installed

    # -- the loop ---------------------------------------------------------
    def ask(self, messages: list[dict]):
        """Run one user turn to completion, yielding events as they happen.

        `messages` is mutated in place so the caller keeps the full transcript,
        including assistant tool calls and their results.
        """
        answer_parts: list[str] = []

        for _ in range(config.MAX_TOOL_HOPS):
            try:
                content, thinking, tool_calls = yield from self._stream_once(messages)
            except BrainOffline as exc:
                yield ("error", str(exc))
                return
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:400]
                yield ("error", f"Ollama returned HTTP {exc.code}: {detail}")
                return
            except Exception as exc:  # noqa: BLE001
                yield ("error", f"{type(exc).__name__}: {exc}")
                return

            assistant_msg: dict = {"role": "assistant", "content": content}
            if thinking:
                assistant_msg["thinking"] = thinking
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if content:
                answer_parts.append(content)

            if not tool_calls:
                yield ("done", "".join(answer_parts).strip())
                return

            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                result = tools.dispatch(name, args)
                yield ("tool", {"name": name, "arguments": args, "result": result})
                messages.append(
                    {"role": "tool", "tool_name": name, "content": result}
                )

        yield (
            "done",
            "".join(answer_parts).strip()
            or "I got stuck in a loop chasing that one. Try narrowing the question.",
        )

    def _stream_once(self, messages: list[dict]):
        """One streaming completion. Returns (content, thinking, tool_calls)."""
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools.schemas(),
            "stream": True,
            "options": {
                "temperature": config.TEMPERATURE,
                "num_ctx": config.CONTEXT_TOKENS,
                "num_predict": config.MAX_TOKENS,
            },
        }
        if self.supports_think:
            payload["think"] = config.THINK

        try:
            resp = self._request("/api/chat", payload, stream=True)
        except urllib.error.HTTPError as exc:
            # A model with no reasoning mode rejects the field. Forget it and
            # retry once, rather than failing the turn over a tuning hint.
            if self.supports_think and exc.code == 400:
                self.supports_think = False
                payload.pop("think", None)
                resp = self._request("/api/chat", payload, stream=True)
            else:
                raise
        except urllib.error.URLError as exc:
            raise BrainOffline(
                f"Cannot reach Ollama at {self.host} ({exc.reason}). "
                "Is the service running?"
            ) from exc

        content_parts: list[str] = []
        think_parts: list[str] = []
        tool_calls: list[dict] = []
        in_think = False

        with resp:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if "error" in chunk:
                    raise RuntimeError(chunk["error"])

                message = chunk.get("message") or {}

                # Ollama exposes native reasoning separately on models that
                # support it; older builds inline it as <think> tags instead.
                if message.get("thinking"):
                    think_parts.append(message["thinking"])
                    yield ("think", message["thinking"])

                for call in message.get("tool_calls") or []:
                    tool_calls.append(call)

                fragment = message.get("content") or ""
                if fragment:
                    visible, in_think = _strip_think(fragment, in_think)
                    content_parts.append(visible)
                    if visible:
                        yield ("token", visible)

                if chunk.get("done"):
                    break

        content = _THINK_TAG.sub("", "".join(content_parts))
        return content.strip(), "".join(think_parts).strip(), tool_calls


def _strip_think(fragment: str, in_think: bool) -> tuple[str, bool]:
    """Filter inline <think> reasoning out of a streamed fragment.

    Handles tags that straddle chunk boundaries by carrying the open state.
    """
    if in_think:
        end = fragment.find("</think>")
        if end == -1:
            return "", True
        fragment, in_think = fragment[end + len("</think>") :], False

    fragment = _THINK_TAG.sub("", fragment)
    if "<think>" in fragment:
        fragment = _OPEN_THINK.sub("", fragment)
        in_think = True
    return fragment, in_think
