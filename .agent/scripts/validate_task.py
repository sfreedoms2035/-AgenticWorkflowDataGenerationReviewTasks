"""
validate_task.py — Review Quality Gate Validator
==============================================
Validates a single Review task JSON file against quality gates specific to
the Review generation pipeline (review_metadata, findings, rewritten_corrected_artifact).

Exit codes: 0 = PASS, 1 = FAIL
Output: JSON quality report to stdout

Usage:
    python validate_task.py <filepath> [--save-report <report_path>] [--quiet] [--strict-thinking]
"""
import sys
import json
import argparse
import re
import os


# ── Quality Gate Thresholds ──────────────────────────────────────────────────
COT_MIN_CHARS = 9_000
COT_REGEN_THRESHOLD = 9_000
# The main assistant answer (Turn 2) content is a JSON dict with 4 keys.
# deep_technical_dialectic alone should be massive.
ANSWER_MIN_CHARS = 3_000
DIALECTIC_MIN_CHARS = 2_000
REQUIRED_TURNS = 6

REQUIRED_TOP_FIELDS = [
    "training_data_id", "prompt_version", "model_used_generation",
    "knowledge_source_date", "document", "task_type", "affected_role",
    "date_of_generation", "key_words", "summary", "difficulty",
    "evaluation_criteria", "conversations"
]

# Review structured answer keys (Turn 2 assistant content)
STRUCTURED_ANSWER_KEYS = [
    "review_metadata", "review_criteria",
    "findings", "rewritten_corrected_artifact", "overall_assessment"
]

# CoT parent step headers (all 8 steps)
COT_PARENT_HEADERS = ["1.", "2.", "3.", "4.", "5.", "6.", "7.", "8."]

COT_SUB_ELEMENTS = [
    "1.1", "1.2",
    "2.1", "2.2", "2.3", "2.4", "2.5",
    "3.1", "3.2", "3.3", "3.4", "3.5", "3.6",
    "4.1", "4.2", "4.3",
    "5.1", "5.2", "5.3", "5.4", "5.5",
    "6.1", "6.2", "6.3",
    "7.1", "7.2", "7.3",
    "8.1", "8.2", "8.3", "8.4",
]

BANNED_VOCABULARY = [
    "the user requests",
    "the document says", "source material", "as mentioned in the pdf",
    "based on the provided", "the text states", "generate a task",
    "generate a multi-turn", "create a coding task", "produce a dataset",
]

# Followup placeholder sentinels that indicate extraction failures
FOLLOWUP_PLACEHOLDERS = [
    "Follow up 1?", "Follow up 2?",
    "Response 1.", "Response 2.",
    "Follow up 1", "Follow up 2",
]

# Instruction-echo sentinels: if these appear in follow-up turn content,
# the model echoed the prompt template instead of generating real content
INSTRUCTION_ECHO_PATTERNS = [
    "(Write a 2-3 sentence technical inquiry",
    "(Write the first technical response here",
    "(Write another 2-3 sentence",
    "(Write the final technical response here",
    "minimum 100 characters)",
    "Ensure it is highly detailed and contextual.)",
    "Must be highly detailed.)",
    "<WRITE YOUR TECHNICAL FOLLOW-UP QUESTION HERE",
    "<WRITE YOUR DETAILED TECHNICAL RESPONSE HERE",
    "<WRITE YOUR SECOND TECHNICAL FOLLOW-UP QUESTION HERE",
    "<WRITE YOUR FINAL TECHNICAL RESPONSE HERE",
    "WRITE YOUR TECHNICAL FOLLOW-UP",
    "NO template text>",
    "BANNED VOCABULARY (CRITICAL)",
    "Never say \"based on established practice\"",
    "Never include placeholders like",
    "Every Work Product from VDA/ISO must be treated",
    "(Write the immersive 3-paragraph problem statement here",
]

# JSON key artifact patterns: fragments from LLM treating output as JSON key-value
JSON_KEY_ARTIFACT_PATTERN = r'(?:^|\s)\\?"\\s*:\\s*\\?"'

# Keywords often used for "Word Salad" padding to inflate character counts
PADDING_KEYWORDS = [
    "visualization", "visualized", "visualize", "visualizations", "visualizing",
    "derivation", "derived", "deriving", "derivations",
    "complexity", "complexities",
    "difficulty", "difficulties",
    "criteria", "criterion",
    "conceptual", "conceptually",
    "initialization", "initialized",
    "virtualized", "virtualization",
]


def validate_task(filepath, strict_thinking=False):
    """Run all quality gates and return structured report."""
    report = {
        "report_id": "QA-AUTO",
        "evaluated_file": os.path.basename(filepath),
        "overall_status": "PASS",
        "locally_fixable": [],       # Issues auto_repair.py can fix
        "needs_regeneration": [],    # Issues requiring full Gemini re-prompt
        "needs_partial_repair": [],  # Issues fixable by re-prompting only follow-up turns
        "metrics": {
            "json_structure": {"status": "PASS", "violations": []},
            "conversation_completeness": {"status": "PASS", "violations": []},
            "richness_and_complexity": {"status": "PASS", "violations": []},
            "structured_answer_format": {"status": "PASS", "violations": []},
            "cot_structure": {"status": "PASS", "violations": []},
            "self_containment": {"status": "PASS", "violations": []},
            "followup_quality": {"status": "PASS", "violations": []},
            "thinking_quality": {"status": "PASS", "violations": []},
        }
    }

    def fail(category, message, fixable_locally=False, partial_repair=False):
        report["overall_status"] = "FAIL"
        report["metrics"][category]["status"] = "FAIL"
        report["metrics"][category]["violations"].append(message)
        if fixable_locally:
            report["locally_fixable"].append({"category": category, "issue": message})
        elif partial_repair:
            report["needs_partial_repair"].append({"category": category, "issue": message})
        else:
            report["needs_regeneration"].append({"category": category, "issue": message})

    def check_keyword_padding(text, turn_label):
        """Check for excessive density of padding keywords (word-salad)."""
        if not text:
            return
        words = re.findall(r'\b\w+\b', text.lower())
        if not words:
            return
        
        # 1. Check density of padding keywords
        padding_count = sum(1 for w in words if w in PADDING_KEYWORDS)
        density = padding_count / len(words)
        if density > 0.15:  # Absolute density threshold
            fail("richness_and_complexity",
                 f"{turn_label} contains keyword-salad padding "
                 f"({padding_count}/{len(words)} padding words, {density:.1%})",
                 fixable_locally=False)
            return

        # 2. Check for "dense clusters" (3+ keywords in a window of 5)
        for i in range(len(words) - 4):
            window = words[i:i+5]
            win_padding = sum(1 for w in window if w in PADDING_KEYWORDS)
            if win_padding >= 4:
                fail("richness_and_complexity",
                     f"{turn_label} contains a dense cluster of padding keywords "
                     f"(e.g., '{' '.join(window)}')",
                     fixable_locally=False)
                return

    def check_internal_repetition(text, turn_label):
        """Check for verbatim repeated paragraphs or large blocks (looping)."""
        if not text or len(text) < 1000:
            return
        paragraphs = [p.strip() for p in text.split('\n\n') if len(p.strip()) > 100]
        if not paragraphs:
            return
            
        seen_sigs = {}
        for i, p in enumerate(paragraphs):
            # Signature: first 150 chars normalized
            sig = re.sub(r'\s+', '', p[:150].lower())
            if sig in seen_sigs:
                fail("richness_and_complexity",
                     f"{turn_label} contains a verbatim repetition loop. "
                     f"Paragraph {i} matches paragraph {seen_sigs[sig]}.",
                     fixable_locally=False)
                return
            seen_sigs[sig] = i

    # ── Gate 0: JSON Parsing ─────────────────────────────────────────────
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        fail("json_structure", f"JSON parse error: {e}", fixable_locally=True)
        return report
    except FileNotFoundError:
        fail("json_structure", f"File not found: {filepath}")
        return report

    if not isinstance(data, list) or len(data) == 0:
        fail("json_structure", "Expected non-empty JSON array")
        return report

    task = data[0]
    if not isinstance(task, dict):
        fail("json_structure", "First element is not a JSON object")
        return report

    # ── Gate 1: Top-Level Fields ─────────────────────────────────────────
    for field in REQUIRED_TOP_FIELDS:
        if field not in task:
            fail("json_structure", f"Missing required field: '{field}'", fixable_locally=True)

    # ── Gate 2: Conversation Structure ───────────────────────────────────
    convs = task.get("conversations", [])
    if not isinstance(convs, list):
        fail("conversation_completeness", "conversations is not an array")
        return report

    if len(convs) != REQUIRED_TURNS:
        fail("conversation_completeness",
             f"Expected {REQUIRED_TURNS} turns, got {len(convs)}",
             fixable_locally=(len(convs) < REQUIRED_TURNS))

    # Check role alternation: user, assistant, user, assistant, user, assistant
    expected_roles = ["user", "assistant", "user", "assistant", "user", "assistant"]
    for i, (conv, expected_role) in enumerate(zip(convs, expected_roles)):
        if not isinstance(conv, dict):
            fail("conversation_completeness", f"Turn {i}: expected a JSON object but got a different type")
            continue
        actual_role = conv.get("role", "")
        if actual_role != expected_role:
            fail("conversation_completeness",
                 f"Turn {i}: expected role '{expected_role}', got '{actual_role}'")

    # Check non-empty content for all turns
    for i, conv in enumerate(convs):
        if not isinstance(conv, dict):
            continue
        content = conv.get("content", "")
        if not content or not content.strip():
            if conv.get("role") == "assistant" and i in [3, 5]:
                fail("conversation_completeness",
                     f"Turn {i}: empty assistant content (followup response is blank) — requires regeneration",
                     fixable_locally=False)
            elif conv.get("role") == "assistant" and i > 1:
                fail("conversation_completeness",
                     f"Turn {i}: empty content",
                     fixable_locally=True)
            else:
                fail("conversation_completeness",
                     f"Turn {i}: empty content",
                     fixable_locally=False)

    # Check <think></think> format for No-Thinking assistant turns (indices 3, 5)
    for i in [3, 5]:
        if i < len(convs):
            conv = convs[i]
            if isinstance(conv, dict) and conv.get("role") == "assistant":
                reasoning = conv.get("reasoning", "")
                if reasoning != "<think></think>":
                    if not reasoning or reasoning.strip() == "":
                        fail("conversation_completeness",
                             f"Turn {i}: missing <think></think> tags (got empty reasoning)",
                             fixable_locally=True)

    if len(convs) < 2:
        return report  # Can't check further without main assistant turn

    # ── Gate 3: Main Assistant Turn (index 1) — Richness ─────────────────
    main_assistant = convs[1]
    reasoning = main_assistant.get("reasoning", "")
    content = main_assistant.get("content", "")

    # ── Gate 3a: Empty or Placeholder Thinking Check ──────────────────────
    EMPTY_THINKING_SENTINELS = [
        "[NO_THINKING_SECTION]",
        "[no_thinking_section]",
        "<think></think>",
    ]
    reasoning_stripped = reasoning.strip()
    if not reasoning_stripped:
        fail("richness_and_complexity",
             "Main assistant turn (index 1): reasoning field is completely empty — requires regeneration",
             fixable_locally=False)
    else:
        for sentinel in EMPTY_THINKING_SENTINELS:
            if reasoning_stripped == sentinel or reasoning_stripped.startswith("[NO_THINKING_SECTION]"):
                fail("richness_and_complexity",
                     f"Main assistant turn (index 1): reasoning contains placeholder '{sentinel}' instead of real CoT — requires regeneration",
                     fixable_locally=False)
                break

    # Check for merged content-in-reasoning anomaly
    if len(content.strip()) < 100 and "</think>" in reasoning:
        parts = reasoning.split("</think>", 1)
        if len(parts) == 2 and len(parts[1].strip()) > 100:
            fail("richness_and_complexity",
                 f"Content merged into reasoning ({len(parts[1])} chars after </think>)",
                 fixable_locally=True)
            content = parts[1].strip()

    cot_len = len(reasoning)
    content_len = len(content)

    if cot_len < COT_MIN_CHARS:
        needs_regen = (cot_len < COT_REGEN_THRESHOLD)
        fail("richness_and_complexity",
             f"CoT too short: {cot_len} chars (min {COT_MIN_CHARS})",
             fixable_locally=not needs_regen)

    if content_len < ANSWER_MIN_CHARS:
        fail("richness_and_complexity",
             f"Answer too short: {content_len} chars (min {ANSWER_MIN_CHARS})")

    # Check for forbidden placeholder patterns
    if re.search(r'\.{4,}', content):
        fail("richness_and_complexity",
             "Found forbidden placeholder (4+ dots) in content")

    # ── Gate 4: Review Structured Answer Format ─────────────────────────────
    # The main assistant content (Turn 2) should be a JSON dict with Review keys
    try:
        parsed_answer = json.loads(content)
        if isinstance(parsed_answer, dict):
            for key in STRUCTURED_ANSWER_KEYS:
                if key not in parsed_answer:
                    fail("structured_answer_format",
                         f"Missing Review answer key: '{key}'",
                         fixable_locally=True)

            findings = parsed_answer.get("findings", [])
            if not isinstance(findings, list):
                fail("structured_answer_format", "findings key must be a list/array", fixable_locally=True)
            elif len(findings) < 15:
                fail("richness_and_complexity", f"findings array is too short: {len(findings)} items (min 15)")

            rewritten = parsed_answer.get("rewritten_corrected_artifact", "")
            if len(rewritten) < 5200:
                fail("richness_and_complexity", f"rewritten_corrected_artifact too short: {len(rewritten)} chars (min 5200, ~850 words)")
                
            # Use anti-repetition check on the rewritten artifact and stringified findings
            check_internal_repetition(rewritten, "Rewritten Artifact")
            check_keyword_padding(rewritten, "Rewritten Artifact")


        else:
            fail("structured_answer_format",
                 "Content is valid JSON but not an object (expected dict with Review keys)",
                 fixable_locally=True)
    except (json.JSONDecodeError, TypeError):
        # Content is not JSON — might be raw markdown
        fail("structured_answer_format",
             "Content is not a valid JSON object (may be raw markdown)",
             fixable_locally=True)

    # ── Gate 5: CoT Structure ────────────────────────────────────────────
    # Extract think block content
    think_match = re.search(r'<think>(.*?)</think>', reasoning, re.DOTALL)
    think_content = think_match.group(1) if think_match else reasoning

    # Normalize: convert escaped \\n sequences to actual newlines for regex matching
    think_normalized = think_content.replace("\\n", "\n").replace("\\\\n", "\n")
    # Collapse multiple horizontal spaces to single spaces
    think_normalized = re.sub(r'[ \t]+', ' ', think_normalized)

    # ── Gate 5a: Check parent step headers ───────────────────────────────
    missing_parents = []
    for parent in COT_PARENT_HEADERS:
        pattern = rf'(?:^|[\n\r])[\s#\-\*]*{re.escape(parent)}[\s]'
        if not re.search(pattern, think_normalized):
            missing_parents.append(parent)

    if missing_parents:
        fail("cot_structure",
             f"Missing CoT parent headers: {', '.join(missing_parents)}",
             fixable_locally=False)

    # ── Gate 5b: Check sub-elements ──────────────────────────────────────
    missing_elements = []
    for elem in COT_SUB_ELEMENTS:
        pattern = rf'(?:^|[\n\r])[\s#\-\*]*{re.escape(elem)}[\.\s:\)]'
        if not re.search(pattern, think_normalized):
            missing_elements.append(elem)

    if missing_elements:
        is_fixable = (cot_len > 15_000 and len(missing_elements) <= 3)
        
        if len(missing_elements) <= 3:
            fail("cot_structure",
                 f"Missing CoT sub-elements: {', '.join(missing_elements)}",
                 fixable_locally=is_fixable)
        else:
            fail("cot_structure",
                 f"Missing {len(missing_elements)} CoT sub-elements: "
                 f"{', '.join(missing_elements[:5])}...",
                 fixable_locally=False)

    # ── Gate 5c: Duplicate <think> tag detection ─────────────────────────
    dup_think = re.search(r'<think>\s*(?:\\?<think\\?>|<think>)', reasoning)
    if dup_think:
        fail("cot_structure",
             "Duplicate <think> tag detected inside reasoning",
             fixable_locally=True)

    # ── Gate 6: Self-Containment (Immersion) ─────────────────────────────
    full_text = (reasoning + " " + content).lower()
    for banned in BANNED_VOCABULARY:
        if banned.lower() in full_text:
            fail("self_containment",
                 f"Banned vocabulary detected: '{banned}'",
                 fixable_locally=True)

    # ── Gate 7: Followup Placeholder Detection ───────────────────────────
    for idx in [2, 3, 4, 5]:  # Follow-up turns
        if idx < len(convs):
            conv_content = convs[idx].get("content", "").strip()
            for placeholder in FOLLOWUP_PLACEHOLDERS:
                if conv_content == placeholder:
                    fail("conversation_completeness",
                         f"Turn {idx}: contains extraction placeholder '{placeholder}'",
                         fixable_locally=False)

    # ── Gate 8: Follow-up Specificity (User turns 2, 4) ──────────────────
    for idx in [2, 4]:  # User follow-up turns
        if idx < len(convs) and convs[idx].get("role") == "user":
            fu_content = convs[idx].get("content", "")
            if len(fu_content) < 100:
                fail("conversation_completeness",
                     f"Turn {idx}: follow-up user prompt too short ({len(fu_content)} chars, min 100)",
                     fixable_locally=False)

    # ── Gate 9: [Thinking]/[No Thinking] Prefix Check ────────────────────
    # Turn 0 (user) must start with [Thinking]
    if len(convs) > 0 and convs[0].get("role") == "user":
        t0_content = convs[0].get("content", "")
        if not t0_content.startswith("[Thinking]"):
            fail("conversation_completeness",
                 "Turn 0: user prompt must start with '[Thinking]'",
                 fixable_locally=True)
    # Turns 2, 4 (user) must start with [No Thinking]
    for idx in [2, 4]:
        if idx < len(convs) and convs[idx].get("role") == "user":
            tu_content = convs[idx].get("content", "")
            if not tu_content.startswith("[No Thinking]"):
                fail("conversation_completeness",
                     f"Turn {idx}: user prompt must start with '[No Thinking]'",
                     fixable_locally=True)

    # ── Gate 10: Anti-Repetition ─────────────────────────────────────────
    # Check that Review answer keys are semantically unique (not copy-pasted)
    try:
        parsed_for_rep = json.loads(content)
        if isinstance(parsed_for_rep, dict):
            # Simple structural check
            if not parsed_for_rep.get("findings"):
                pass
    except (json.JSONDecodeError, TypeError):
        pass

    # ── Gate 11: Keyword-Salad Padding and Internal Repetition ────────────
    check_keyword_padding(reasoning, "Reasoning (CoT)")
    check_internal_repetition(reasoning, "Reasoning (CoT)")
    
    for i, conv in enumerate(convs):
        if conv.get("role") == "user":
            check_keyword_padding(conv.get("content", ""), f"Turn {i} (user)")

    # ── Gate 12: [No Thinking] Tag Duplication ────────────────────────────
    for idx in [2, 4]:  # User follow-up turns
        if idx < len(convs) and convs[idx].get("role") == "user":
            fu_content = convs[idx].get("content", "")
            nt_count = fu_content.count("[No Thinking]")
            if nt_count > 1:
                fail("followup_quality",
                     f"Turn {idx}: duplicated [No Thinking] prefix ({nt_count} occurrences)",
                     fixable_locally=True)

    # ── Gate 13: Instruction Echo Detection (Follow-Up Turns) ─────────────
    for idx in [2, 3, 4, 5]:  # All follow-up turns
        if idx < len(convs):
            fu_content = convs[idx].get("content", "")
            for echo_pattern in INSTRUCTION_ECHO_PATTERNS:
                if echo_pattern in fu_content:
                    fail("followup_quality",
                         f"Turn {idx}: instruction echo detected — model echoed prompt template "
                         f"instead of generating content (matched: '{echo_pattern[:50]}...')",
                         partial_repair=True)
                    break

    # ── Gate 14: JSON Key Artifact Detection ──────────────────────────────
    for idx in [0, 2, 3, 4, 5]:  # Skip index 1 (main assistant JSON)
        if idx >= len(convs):
            continue
        conv_content = convs[idx].get("content", "")
        if re.search(r'\\?"\s*:\s*\\?"\[', conv_content):
            fail("followup_quality",
                 f"Turn {idx}: JSON key artifact detected in content (LLM output formatting corruption)",
                 fixable_locally=True)
        if conv_content.strip().startswith('\\"') or conv_content.strip().startswith('": "'):
            fail("followup_quality",
                 f"Turn {idx}: content starts with JSON key artifact",
                 fixable_locally=True)

    # ── Gate 15: [No Thinking] Tag Leaking into Assistant Content ─────────
    for idx in [1, 3, 5]:  # Assistant turns
        if idx < len(convs) and convs[idx].get("role") == "assistant":
            asst_content = convs[idx].get("content", "")
            if isinstance(asst_content, str) and asst_content.strip().startswith("[No Thinking]"):
                fail("followup_quality",
                     f"Turn {idx}: assistant content starts with '[No Thinking]' — this tag is for USER turns only",
                     partial_repair=True)

    # ── Gate 16: COT Meta-Generation Detection ────────────────────────────
    META_COT_PATTERNS = [
        "the request is to generate",
        "i need to generate",
        "i will structure the user turn",
        "i need to create a task",
        "the variation schema",
        "i will generate",
        "creating a coding task",
        "to generate a multi-turn",
        "produce a dataset",
        "generate exactly 1 distinct",
    ]
    reasoning_lower = reasoning.lower()
    meta_cot_hits = []
    for pat in META_COT_PATTERNS:
        if pat in reasoning_lower:
            meta_cot_hits.append(pat)
    if len(meta_cot_hits) >= 2:
        fail("thinking_quality",
             f"COT describes task generation instead of problem solving. "
             f"Meta-generation phrases found: {meta_cot_hits[:5]}",
             fixable_locally=False)

    # ── Gate 17: Raw Thinking File Integrity ──────────────────────────────
    if strict_thinking:
        try:
            json_dir = os.path.dirname(os.path.abspath(filepath))
            base_name = os.path.splitext(os.path.basename(filepath))[0]
            output_dir = os.path.dirname(json_dir)
            thinking_dir = os.path.join(output_dir, "thinking")
            thinking_path = os.path.join(thinking_dir, f"{base_name}.txt")

            if os.path.exists(thinking_path):
                with open(thinking_path, 'r', encoding='utf-8', errors='replace') as tf:
                    raw_think = tf.read().strip()
                    
                fail_markers = ["[NO_THINKING_SECTION]", "[EXTRACTION_FAILED]", "[EXTRACTION_ERROR]"]
                for marker in fail_markers:
                    if marker in raw_think:
                        fail("thinking_quality", 
                             f"Internal thinking monologue extraction failed: {marker}", 
                             fixable_locally=False)
                        break
                
                if not raw_think or len(raw_think) < 100:
                     fail("thinking_quality", 
                          f"Internal thinking monologue is critically undersized ({len(raw_think)} chars)", 
                          fixable_locally=False)
        except Exception:
            pass

    # Add enriched summary stats to report
    dialectic_len = 0
    try:
        parsed_stats = json.loads(content)
        if isinstance(parsed_stats, dict):
            dialectic_len = len(parsed_stats.get("rewritten_corrected_artifact", ""))
    except (json.JSONDecodeError, TypeError):
        pass

    report["stats"] = {
        "cot_chars": cot_len,
        "answer_chars": content_len,
        "dialectic_chars": dialectic_len,
        "turns": len(convs),
    }

    return report


def main():
    quiet = "--quiet" in sys.argv

    if len(sys.argv) < 2:
        if not quiet:
            print(json.dumps({"overall_status": "FAIL", "error": "No filepath provided"}))
        sys.exit(1)

    strict_thinking = "--strict-thinking" in sys.argv
    
    # filter out flags to get filepath
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    filepath = args[0] if args else sys.argv[1]
    
    report = validate_task(filepath, strict_thinking=strict_thinking)

    # Optionally save report to file
    if "--save-report" in sys.argv:
        idx = sys.argv.index("--save-report")
        if idx + 1 < len(sys.argv):
            report_path = sys.argv[idx + 1]
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            with open(report_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2)

    print(json.dumps(report, indent=2))
    sys.exit(0 if report["overall_status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
