"""Auto-labeler 'teacher': name the cat in a visit's frames so the gallery
builds itself. Pluggable backend (claude -p haiku now, offline VLM later) +
a strict cross-frame agreement gate so only confident calls are auto-applied.
"""
import json
import subprocess
import sys
from collections import Counter

# Canonical cat names the labeler may return, plus two sentinels:
#   NONE  = the model confidently saw no cat (an empty box)
#   ERROR = the backend FAILED (timeout/crash/missing) — NOT the same as empty;
#           the worker must leave these frames in the queue for a later retry,
#           never retire them as "no cat".
NONE = "none"
ERROR = "error"


class Labeler:
    """Backend interface. Given a visit's frame paths and a {name: ref_path}
    map, return a per-frame list aligned to `frame_paths`:
        [{"file": path, "cat": <name>|"none", "confidence": 0..1}, ...]"""

    def predict_visit(self, frame_paths, refs):
        raise NotImplementedError


def decide(per_frame, valid_cats, established=None, majority=0.66):
    """Cross-frame agreement gate. `per_frame` is the backend's output.

    Rule (precision over recall — a teacher must not poison the gallery):
    take a STRONG-MAJORITY vote among the frames that named a real cat. If one
    cat wins ≥ `majority` of those votes, label the frames the model called that
    cat; otherwise (a genuine ~tie — likely two cats in one visit) send the
    visit to human review. Majority (not unanimity) is what makes rich
    continuous capture an asset: one stray misread in a dozen frames no longer
    blocks the whole visit.

    `established` is the cat a human already confirmed for THIS visit. When set
    it's authoritative — it wins ties and minority noise; only a model MAJORITY
    *against* the human (a possible second cat) is a conflict for review.

    Returns {"status": "labeled"|"empty"|"conflict",
             "cat": name|None,
             "apply": [(file, cat, conf), ...],
             "cats": [names]}  # all cats that got votes
    """
    named = [p for p in per_frame
             if p.get("cat") and p["cat"] != NONE and p["cat"] in valid_cats]
    if not named:
        return {"status": "empty", "cat": None, "apply": [], "cats": []}
    votes = Counter(p["cat"] for p in named)
    winner, winner_n = votes.most_common(1)[0]
    total = len(named)
    cats = sorted(votes)
    winner_has_majority = winner_n / total >= majority
    if established is not None:
        if winner != established and winner_has_majority:
            return {"status": "conflict", "cat": None, "apply": [], "cats": cats}
        cat = established
    elif winner_has_majority:
        cat = winner
    else:
        return {"status": "conflict", "cat": None, "apply": [], "cats": cats}
    apply = [(p["file"], cat, float(p.get("confidence") or 0.0))
             for p in named if p["cat"] == cat]
    return {"status": "labeled", "cat": cat, "apply": apply, "cats": cats}


_PROMPT = """You identify which of three specific cats appears in litter-box camera frames.

Reference photos (read each):
{refs}

Cat descriptions:
- Ucok: brown/gray mackerel tabby, large, heavy striping.
- Garfield: orange mackerel tabby, often wearing a collar.
- Ella: long-haired tortie, fluffy coat, ear tufts.

For EACH frame file below, read the image and decide which cat is visibly present,
or "none" if the box is empty / no cat is in view. Be conservative: if you are not
sure it is a specific cat, use "none". Garfield vs Ucok is the hard pair — both are
tabbies; use the orange-vs-brown tone, collar, and build.

Frames (read each):
{frames}

Output ONLY a JSON object, no prose:
{{"frames": [{{"file": "<path>", "cat": "Ucok|Garfield|Ella|none", "confidence": 0.0-1.0}}]}}"""


class AgyLabeler(Labeler):
    """Antigravity (`agy`) vision backend — the strong teacher. Validated 82%
    on 11 human-labeled frames vs claude-haiku's 45% (haiku biases to Ucok and
    even misreads longhair Ella). One `agy` call per frame: agy's flag parser
    is order-sensitive and `--dangerously-skip-permissions` is blocked here, so
    the only reliable form is `agy --print "<prompt with absolute path>"`; agy
    reads the image paths via its file tools."""

    CATS = ("Ucok", "Garfield", "Ella")

    def __init__(self, timeout=240):
        self.timeout = timeout

    def _prompt(self, frame_path, refs):
        ref_lines = ""
        for name, paths in refs.items():
            ps = paths if isinstance(paths, (list, tuple)) else [paths]
            ref_lines += f"{name} reference(s): {', '.join(ps)}\n"
        return (
            "Identify which of three specific cats is in a litter-box photo.\n"
            "Ucok = brown/gray mackerel tabby, short hair, large, heavy stripes.\n"
            "Garfield = ORANGE mackerel tabby, often wearing a collar.\n"
            "Ella = long-haired tortie, fluffy coat, ear tufts.\n"
            + (ref_lines if ref_lines else "")
            + f"Look at this photo (read the file): {frame_path}\n"
            "Reply with ONLY one word: Ucok, Garfield, Ella, or none "
            "(none = empty box / no cat visible)."
        )

    def _classify(self, frame_path, refs):
        try:
            out = subprocess.run(["agy", "--print", self._prompt(frame_path, refs)],
                                 capture_output=True, text=True,
                                 timeout=self.timeout, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError) as e:
            print(f"[labeler/agy] {frame_path} failed ({e}); ERROR (will retry)",
                  file=sys.stderr)
            return {"file": frame_path, "cat": ERROR, "confidence": 0.0}
        text = out.stdout.lower()
        # Pick the cat/none token that appears earliest in agy's reply.
        hits = {tok: text.find(tok.lower())
                for tok in (*self.CATS, NONE) if text.find(tok.lower()) != -1}
        if not hits:
            return {"file": frame_path, "cat": NONE, "confidence": 0.0}
        cat = min(hits, key=hits.get)
        return {"file": frame_path, "cat": cat,
                "confidence": 0.0 if cat == NONE else 1.0}

    def predict_visit(self, frame_paths, refs):
        return [self._classify(p, refs) for p in frame_paths]


class ClaudeCliLabeler(Labeler):
    """Shells `claude -p` with a vision-capable model. The model reads the
    reference photos + frame files (via its Read tool) and returns JSON."""

    def __init__(self, model="haiku", timeout=180,
                 extra_args=("--allowedTools=Read",)):  # =form: --allowedTools is variadic
                                                        # and would otherwise eat the prompt
        self.model = model
        self.timeout = timeout
        self.extra_args = list(extra_args)

    def _build_prompt(self, frame_paths, refs):
        # refs: {name: [paths]} — list every reference angle per cat.
        def _fmt(paths):
            return ", ".join(paths) if isinstance(paths, (list, tuple)) else str(paths)
        ref_lines = "\n".join(f"- {name}: {_fmt(paths)}" for name, paths in refs.items())
        frame_lines = "\n".join(f"- {p}" for p in frame_paths)
        return _PROMPT.format(refs=ref_lines, frames=frame_lines)

    def _run_cli(self, prompt):
        cmd = ["claude", "-p", "--model", self.model,
               "--output-format", "json", *self.extra_args, prompt]
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=self.timeout, check=True)
        return out.stdout

    @staticmethod
    def _parse(stdout, frame_paths):
        """Pull the model's JSON out of claude's result envelope, tolerating
        markdown fences. Falls back to all-'none' if parsing fails (a parse
        failure must never fabricate a label)."""
        text = stdout
        try:
            env = json.loads(stdout)
            text = env.get("result", stdout) if isinstance(env, dict) else stdout
        except json.JSONDecodeError:
            pass
        s = text.strip()
        if s.count("```") >= 2:
            s = s.split("```")[1].removeprefix("json").strip()  # not lstrip (char-set footgun)
        start, end = s.find("{"), s.rfind("}")
        try:
            data = json.loads(s[start:end + 1])
            frames = {f["file"]: f for f in data.get("frames", [])}
            return [frames.get(p, {"file": p, "cat": NONE, "confidence": 0.0})
                    for p in frame_paths]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(f"[labeler] parse failed ({e}); treating as no-cat", file=sys.stderr)
            return [{"file": p, "cat": NONE, "confidence": 0.0} for p in frame_paths]

    def predict_visit(self, frame_paths, refs):
        if not frame_paths:
            return []
        prompt = self._build_prompt(frame_paths, refs)
        try:
            stdout = self._run_cli(prompt)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError) as e:
            print(f"[labeler] claude -p failed ({e}); ERROR (will retry)", file=sys.stderr)
            return [{"file": p, "cat": ERROR, "confidence": 0.0} for p in frame_paths]
        return self._parse(stdout, frame_paths)
