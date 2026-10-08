#!/usr/bin/env python3
"""Configurable fake Pi used by runner tests."""

import json
import os
import select
import sys
import time


mode = os.environ.get("FAKE_PI_MODE", "ok")
args = sys.argv[1:]
# Synthetic model catalog (test-only stand-in for `pi --list-models`, which
# lists every provider that serves a model id). Provider/model ids are fake and
# carry no host configuration.
MODEL_PROVIDERS = {
    "test-model-a": ["test-provider-a", "test-provider-retired"],
    "test-model-b": ["test-provider-a"],
    "test-model-c": ["test-provider-b"],
    "test-model-d": ["test-provider-a"],
    "test-model-legacy": ["test-provider-b", "test-provider-a"],
}
argv_capture = os.environ.get("FAKE_PI_ARGV_FILE")
if argv_capture:
    with open(argv_capture, "w", encoding="utf-8") as out:
        json.dump(sys.argv, out)
if args[:2] == ["auth", "check"]:
    sys.exit(1 if mode == "auth-fail" else 0)
if "--list-models" in args:
    if mode == "model-missing":
        print("test-provider-a/test-model-legacy")
    else:
        query = args[-1]
        providers = MODEL_PROVIDERS.get(query) or [os.environ.get("FAKE_PROVIDER", "")]
        if os.environ.get("FAKE_MODEL_COLUMNS") == "1":
            print("Provider Model Context")
            for provider in providers:
                print(f"{provider} {query} 100000")
        else:
            for provider in providers:
                print(f"{provider}/{query}")
    sys.exit(0)
if "--mode" not in args or "rpc" not in args:
    sys.exit(2)


def emit(payload):
    print(json.dumps(payload), flush=True)


def _read_pending():
    """One short stdin poll; the pending request dict, or None when idle."""
    if not select.select([sys.stdin], [], [], 0.05)[0]:
        return None
    line = sys.stdin.readline()
    if not line.strip():
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


def retry_scenario_events(scenario, pair_seconds=0.05):
    """Opt-in automatic-retry fixtures; returns True when it emitted its own settle.

    Every scenario is inert unless FAKE_PI_RETRY_SCENARIO is set, so no existing
    fake mode changes behaviour. Scenarios:
      burst                - N quick start/end retry pairs, then settle
                             (FAKE_PI_RETRY_COUNT, default 3)
      silent               - one retry that never ends and no further traffic
      stream               - one retry that never ends while unrelated traffic streams
      recovered            - assistant error, then a successful retry, then settle
      recovered-then-error - recovered, then a NEW assistant error before settling
      failed-end           - assistant error, then a FAILED retry end, then settle
      usage-limit-retry    - usage-limit error, recovered retry, then a real
                             usage-limit error again (auto-disable must hold)
    The assistant-error and retry payloads are secrets that must never reach a
    receipt.
    """
    if scenario == "burst":
        count = int(os.environ.get("FAKE_PI_RETRY_COUNT", "3"))
        for attempt in range(1, count + 1):
            emit({"type": "auto_retry_start", "attempt": attempt,
                  "maxAttempts": count, "delayMs": 2000,
                  "errorMessage": "SECRET-PROVIDER-ERROR"})
            time.sleep(pair_seconds)
            emit({"type": "auto_retry_end", "success": True, "attempt": attempt + 1})
        return False
    if scenario in ("silent", "stream"):
        # The retry never ends. The hard cap keeps a mis-wired test bounded; a
        # correct retry budget always fires long before it.
        seconds = float(os.environ.get("FAKE_PI_HANG_SECONDS", "60"))
        started = time.monotonic()
        emit({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
              "delayMs": 2000, "errorMessage": "SECRET-PROVIDER-ERROR"})
        while time.monotonic() - started < seconds:
            if scenario == "stream":
                # Unrelated, steady traffic during the open retry: the budget must
                # still be enforced from the observed span, not from message gaps.
                emit({"type": "message_update",
                      "message": {"role": "assistant", "stopReason": "streaming",
                                  "content": "SECRET-STREAM-CONTENT"}})
                emit({"type": "tool_execution_update", "toolCallId": "call_stream_1",
                      "toolName": "bash", "args": {"command": "pytest -q tests/"},
                      "partialResult": "SECRET-STREAM-PARTIAL"})
            pending = _read_pending()
            if pending and pending.get("type") == "abort":
                marker = os.environ.get("FAKE_ABORT_MARKER")
                if marker:
                    open(marker, "w").write("aborted")
                emit({"type": "response", "id": pending.get("id"),
                      "command": "abort", "success": True})
                sys.exit(0)
        return False
    if scenario in ("recovered", "recovered-then-error", "failed-end",
                    "usage-limit-retry"):
        transient = ("429 Too Many Requests: rate limit exceeded"
                     if scenario == "usage-limit-retry" else "SECRET-TRANSIENT-ERROR")
        emit({"type": "message_end", "message": {"role": "assistant",
                                                  "stopReason": "error",
                                                  "errorMessage": transient}})
        emit({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
              "delayMs": 2000, "errorMessage": transient})
        time.sleep(pair_seconds)
        # Only the recovery scenarios end successfully; a failed retry end must
        # leave the earlier provider error in place.
        emit({"type": "auto_retry_end",
              "success": scenario in ("recovered", "recovered-then-error",
                                      "usage-limit-retry"), "attempt": 2})
        if scenario in ("recovered-then-error", "usage-limit-retry"):
            # A later, independent error still fails: recovery clears the sticky
            # prior error, it does not whitelist the run.
            final = ("429 Too Many Requests: rate limit exceeded"
                     if scenario == "usage-limit-retry" else "secret provider detail")
            emit({"type": "message_end", "message": {"role": "assistant",
                                                      "stopReason": "error",
                                                      "errorMessage": final}})
        emit({"type": "agent_settled"})
        return True
    return False


for line in sys.stdin:
    request = json.loads(line)
    if request.get("type") == "get_commands":
        mode = os.environ.get("FAKE_PI_MODE", "ok")
        if mode == "get-cmds-fail":
            # Correlated response, but the command itself failed.
            emit({"type": "response", "id": request.get("id"),
                  "command": "get_commands", "success": False,
                  "data": {"commands": [{"name": "intercom", "source": "extension"}]}})
            continue
        if mode == "get-cmds-malformed":
            # Correlated success response whose data is not the documented dict.
            emit({"type": "response", "id": request.get("id"),
                  "command": "get_commands", "success": True,
                  "data": {"commands": "intercom"}})
            continue
        commands = [{"name": "prompt", "source": "builtin"},
                    {"name": "get_session_stats", "source": "builtin"},
                    {"name": "abort", "source": "builtin"},
                    {"name": "set_model", "source": "builtin"}]
        if os.environ.get("FAKE_PI_NO_INTERCOM") != "1":
            commands.append({"name": "intercom", "source": "extension"})
        # Real Pi RPC shape: data is a dict wrapping the commands list. Send an
        # unrelated response first, then the correlated one, so the runner's
        # id/command correlation logic is exercised on every happy-path test.
        emit({"type": "response", "id": "unrelated-notice",
              "command": "notify", "success": True, "data": {"note": "ignored"}})
        emit({"type": "response", "id": request.get("id"),
              "command": "get_commands", "success": True,
              "data": {"commands": commands}})
        continue
    prompt_path = os.environ.get("FAKE_PI_PROMPT_FILE")
    if prompt_path and request.get("type") == "prompt":
        with open(prompt_path, "a", encoding="utf-8") as out:
            json.dump({"id": request.get("id"), "message": request.get("message")}, out,
                      ensure_ascii=False)
            out.write("\n")
    if request.get("type") == "prompt":
        emit({"type": "response", "id": request.get("id"), "command": "prompt", "success": True})
        if mode == "exit":
            sys.exit(7)
        if mode == "timeout":
            continue
        emit({"type": "agent_start"})
        hang = os.environ.get("FAKE_PI_HANG_TOOL", "")
        if hang:
            # Opt-in per-tool-timeout fixture. Modes:
            #   quiet  - start a tool and never end it, no partial updates
            #   stream - start a tool and keep emitting partial updates, so a
            #            streaming tool is proven not to reset the timer
            #   short  - same, but the tool ends after FAKE_PI_HANG_SECONDS and
            #            the run settles normally (for the disabled-bound case)
            # The hard cap keeps a mis-wired test from hanging forever.
            #
            # While the tool "runs" the fake keeps polling stdin so it can answer
            # an abort, the way a real Pi does. Polling is line-at-a-time (the
            # tests only ever send an abort here), so a command that arrives
            # together with another line would be seen on the next read.
            seconds = float(os.environ.get("FAKE_PI_HANG_SECONDS", "60"))
            started = time.monotonic()
            emit({"type": "tool_execution_start", "toolCallId": "call_hang_1",
                  "toolName": "bash", "args": {"command": "sleep SECRET-HANG"}})
            while time.monotonic() - started < seconds:
                if hang != "quiet":
                    emit({"type": "tool_execution_update", "toolCallId": "call_hang_1",
                          "toolName": "bash", "args": {"command": "sleep SECRET-HANG"},
                          "partialResult": "SECRET-HANG-PARTIAL"})
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    line = sys.stdin.readline()
                    try:
                        pending = json.loads(line) if line.strip() else {}
                    except json.JSONDecodeError:
                        pending = {}
                    if pending.get("type") == "abort":
                        marker = os.environ.get("FAKE_ABORT_MARKER")
                        if marker:
                            open(marker, "w").write("aborted")
                        emit({"type": "response", "id": pending.get("id"),
                              "command": "abort", "success": True})
                        sys.exit(0)
            emit({"type": "tool_execution_end", "toolCallId": "call_hang_1",
                  "toolName": "bash", "isError": False, "result": "SECRET-HANG-OUTPUT"})
        retry_scenario = os.environ.get("FAKE_PI_RETRY_SCENARIO", "")
        retry_handled = retry_scenario_events(retry_scenario) if retry_scenario else False
        if not retry_handled and os.environ.get("FAKE_PI_TOOL_FLOW") == "1":
            # Opt-in tool lifecycle so telemetry tests can exercise
            # start/end pairing. The secrets here must never reach a receipt.
            emit({"type": "tool_execution_start", "toolCallId": "call_bash_1",
                  "toolName": "bash", "args": {"command": "pytest -q tests/"}})
            emit({"type": "tool_execution_update", "toolCallId": "call_bash_1",
                  "toolName": "bash", "args": {"command": "pytest -q tests/"},
                  "partialResult": "SECRET-PARTIAL-OUTPUT"})
            emit({"type": "tool_execution_end", "toolCallId": "call_bash_1",
                  "toolName": "bash", "isError": False,
                  "result": "SECRET-BASH-OUTPUT"})
            emit({"type": "tool_execution_start", "toolCallId": "call_write_1",
                  "toolName": "write",
                  "args": {"path": "/private/secret/dir/file.txt",
                           "content": "SECRET-FILE-CONTENT"}})
            emit({"type": "tool_execution_end", "toolCallId": "call_write_1",
                  "toolName": "write", "isError": False,
                  "result": "SECRET-WRITE-RESULT"})
            emit({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
                  "delayMs": 2000, "errorMessage": "SECRET-PROVIDER-ERROR"})
            emit({"type": "auto_retry_end", "success": True, "attempt": 2})
        if not retry_handled and mode == "provider-error":
            emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                                        "errorMessage": "secret provider detail"}})
        if not retry_handled and mode == "usage-limit":
            emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                                        "errorMessage": "429 Too Many Requests: rate limit exceeded"}})
        if not retry_handled:
            emit({"type": "agent_settled"})
    elif request.get("type") == "get_session_stats":
        data = {"sessionId": "fake-session", "cost": 0.125}
        if mode != "missing-usage":
            data["tokens"] = {"input": 11, "output": 7, "cacheRead": 3, "cacheWrite": 2, "total": 23}
        emit({"type": "response", "id": request.get("id"), "command": "get_session_stats", "success": True, "data": data})
    elif request.get("type") == "abort":
        marker = os.environ.get("FAKE_ABORT_MARKER")
        if marker:
            open(marker, "w").write("aborted")
        emit({"type": "response", "id": request.get("id"), "command": "abort", "success": True})
        sys.exit(0)
    elif request.get("type") == "steer":
        # Checkpoint steering. Logging happens first so a silent Pi still shows
        # what reached the wire. FAKE_PI_STEER_SILENT=1 ignores the request
        # entirely and FAKE_PI_STEER_REJECT=1 refuses it; neither may fail the
        # run, because the runner never waits for this response.
        log = os.environ.get("FAKE_PI_STEER_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as out:
                out.write(json.dumps({"id": request.get("id"), "type": "steer",
                                      "message": request.get("message")},
                                     ensure_ascii=False) + "\n")
        if os.environ.get("FAKE_PI_STEER_SILENT") == "1":
            continue
        rejected = os.environ.get("FAKE_PI_STEER_REJECT") == "1"
        emit({"type": "response", "id": request.get("id"), "command": "steer",
              "success": not rejected,
              "data": {"error": "steering unavailable"} if rejected
              else {"disposition": "queued"}})
