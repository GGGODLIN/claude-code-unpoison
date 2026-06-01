#!/usr/bin/env python3
"""cc-unpoison — recover a Claude Code session poisoned by the Opus 4.7/4.8
"tool call could not be parsed (retry also failed)" bug.

When extended/adaptive thinking is active, Opus 4.7/4.8 intermittently serialize a
tool call as malformed legacy XML instead of a structured tool_use block. The harness
rejects it ("Your tool call was malformed and could not be parsed. Please retry."), and
after the retry also fails the turn dies with "The model's tool call could not be parsed
(retry also failed)." The failed turns pile up at the tail of the transcript as:
empty thinking block + stop_reason=tool_use + NO tool_use block, plus retry-hint and
synthetic-error messages. `--resume` replays that tail, so the session keeps failing.

This tool walks the transcript backwards, drops the contiguous bad tail, and rewinds the
leaf to the last clean node (a user message, a tool_result, or a normally-ended assistant
turn). It backs up first, then truncates. Everything before the failure is preserved
verbatim — strictly more context than /compact, which summarizes.

Usage:
  cc-unpoison.py [SESSION]      detect + back up + truncate, print the resume command
  cc-unpoison.py -n [SESSION]   dry run: show what would be dropped, write nothing
  cc-unpoison.py -r [SESSION]   truncate, then chdir to the session's cwd and `claude --resume`
  SESSION may be a session-id or a path to a .jsonl. Omit it to pick the most recently
  modified session under the project dir for the current working directory.
"""
import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
RETRY_HINT = "malformed and could not be parsed"
FATAL_TEXT = "could not be parsed"
CONV_TYPES = {"user", "assistant"}


def encode_cwd(cwd):
    return str(cwd).replace("/", "-").replace(".", "-")


def resolve_session(arg):
    if arg:
        p = Path(arg)
        if p.is_file():
            return p
        hits = list(PROJECTS.glob(f"**/{glob.escape(arg)}.jsonl"))
        if hits:
            return max(hits, key=lambda f: f.stat().st_mtime)
        sys.exit(f"session not found: {arg}")
    proj = PROJECTS / encode_cwd(Path.cwd())
    if proj.is_dir():
        pool = list(proj.glob("*.jsonl"))
        if not pool:
            sys.exit(f"no session jsonl in {proj}")
        return max(pool, key=lambda f: f.stat().st_mtime)
    pool = list(PROJECTS.glob("**/*.jsonl"))
    if not pool:
        sys.exit("no session jsonl found")
    chosen = max(pool, key=lambda f: f.stat().st_mtime)
    print(
        f"warning: no project dir for {Path.cwd()};\n"
        f"  falling back to newest session across ALL projects:\n  {chosen}",
        file=sys.stderr,
    )
    return chosen


def parse_lines(raw):
    recs = []
    for line in raw.split("\n"):
        if not line.strip():
            recs.append(None)
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            recs.append(None)
    return recs


def is_poison(d):
    """Parse-poison signature: the malformed-tool-call retry hint, or an assistant turn
    that claims stop_reason=tool_use but carries no tool_use block. Narrower than is_bad —
    used to detect poison buried *before* the leaf, where a plain API-error line must NOT
    trigger a false mid-conversation abort."""
    if d is None:
        return False
    msg = d.get("message") or {}
    typ = d.get("type")
    content = msg.get("content")
    if typ == "user" and d.get("isMeta"):
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            txt = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        else:
            txt = ""
        if RETRY_HINT in txt:
            return True
    if typ == "assistant" and msg.get("stop_reason") == "tool_use":
        blocks = content if isinstance(content, list) else []
        if not any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks):
            return True
    return False


def is_bad(d):
    if d is None:
        return False
    msg = d.get("message") or {}
    if msg.get("model") == "<synthetic>" or d.get("isApiErrorMessage"):
        return True
    return is_poison(d)


def is_clean_leaf(d):
    """Safe resume leaf: a user message (not a retry-hint meta), or a normally-ended
    assistant turn. An assistant turn ending in a real tool_use is NOT safe — its
    matching tool_result would be dropped with the tail, leaving a dangling tool_use."""
    if d is None:
        return False
    typ = d.get("type")
    msg = d.get("message") or {}
    if typ == "user" and not d.get("isMeta"):
        return True
    if typ == "assistant" and msg.get("stop_reason") in {"end_turn", "stop_sequence"}:
        if msg.get("model") == "<synthetic>":
            return False
        blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
        if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks):
            return False
        return True
    return False


def session_cwd(recs):
    for d in recs:
        if d and isinstance(d.get("cwd"), str) and d["cwd"]:
            return d["cwd"]
    return None


def preview(d, n=80):
    msg = d.get("message") or {}
    c = msg.get("content")
    if isinstance(c, str):
        return c[:n]
    if isinstance(c, list):
        for b in c:
            if b.get("type") == "text":
                return "text: " + b.get("text", "")[:n]
        for b in c:
            if b.get("type") == "tool_use":
                return "tool_use: " + str(b.get("name"))
            if b.get("type") == "tool_result":
                tc = b.get("content")
                snippet = tc if isinstance(tc, str) else json.dumps(tc, ensure_ascii=False)
                return "tool_result: " + (snippet or "")[:n]
        return "(thinking only)"
    return ""


def main():
    ap = argparse.ArgumentParser(
        description="Recover a Claude Code session poisoned by the Opus 4.7/4.8 "
        "'tool call could not be parsed' bug."
    )
    ap.add_argument("session", nargs="?", help="session-id or .jsonl path (default: newest in cwd's project)")
    ap.add_argument("-n", "--dry-run", action="store_true", help="show what would be dropped, write nothing")
    ap.add_argument("-r", "--resume", action="store_true", help="after truncating, chdir to the session cwd and `claude --resume`")
    args = ap.parse_args()

    path = resolve_session(args.session)
    raw = path.read_text(encoding="utf-8")
    recs = parse_lines(raw)

    conv_idx = [i for i, d in enumerate(recs) if d and d.get("type") in CONV_TYPES]
    if not conv_idx:
        sys.exit(f"{path.name}: no conversation messages")

    last = recs[conv_idx[-1]]
    tail_bad = any(is_bad(recs[i]) for i in conv_idx[-6:]) or (
        FATAL_TEXT in json.dumps(last.get("message", {}).get("content", ""))
    )
    if not tail_bad:
        print(f"OK  {path.name}: no parse poisoning at the tail, nothing to do.")
        return

    leaf_pos = None
    for i in reversed(conv_idx):
        if is_bad(recs[i]):
            continue
        if is_clean_leaf(recs[i]):
            leaf_pos = i
            break
    if leaf_pos is None:
        sys.exit("no clean leaf found (whole tail is bad?) — use /compact instead.")

    buried = next((i for i in conv_idx if i < leaf_pos and is_poison(recs[i])), None)
    if buried is not None:
        sys.exit(
            f"{path.name}: poison at L{buried + 1} is buried before the last clean turn "
            f"(L{leaf_pos + 1}); tail truncation can't remove it — use /compact instead."
        )

    keep = leaf_pos + 1
    dropped = len(recs) - keep
    sid = path.stem

    print(f"session   : {path}")
    print(f"clean leaf: L{leaf_pos + 1}  [{recs[leaf_pos].get('type')}]  {preview(recs[leaf_pos])}")
    print(f"will drop : L{keep + 1}~L{len(recs)}  ({dropped} lines: bad turns / retry hints / synthetic errors / trailing local lines)")

    if args.dry_run:
        print("\n[dry-run] nothing written. Drop -n to actually truncate.")
        return

    bak = path.with_suffix(f".jsonl.bak.{int(time.time())}")
    bak.write_text(raw, encoding="utf-8")
    print(f"\nbackup   : {bak}")
    print(f"restore  : cp '{bak}' '{path}'")
    lines = raw.split("\n")
    tmp = path.with_suffix(f".jsonl.tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines[:keep]) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    print("truncated.")

    orig = session_cwd(recs)
    if args.resume:
        if orig and Path(orig).is_dir() and orig != str(Path.cwd()):
            print(f"cd {orig}")
            os.chdir(orig)
        print(f"\nresuming: claude --resume {sid}\n")
        os.execvp("claude", ["claude", "--resume", sid])
    elif orig and orig != str(Path.cwd()):
        print(f"\nnext: cd '{orig}' && claude --resume {sid}")
    else:
        print(f"\nnext: claude --resume {sid}")


if __name__ == "__main__":
    main()
