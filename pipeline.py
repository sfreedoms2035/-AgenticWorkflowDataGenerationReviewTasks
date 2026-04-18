import re
"""
pipeline.py — Master Orchestrator for AD/ADAS Coding Task Generation
=====================================================================
Single entry point that automates the entire PDF → 16 tasks pipeline:
  1. Scans Input/ for PDFs
  2. Classifies each PDF as Technical or Regulatory
  3. For each PDF, runs 8 turns × 2 tasks = 16 tasks
  4. Each task: generate prompt → Playwright → validate → auto-repair → retry
  5. Max 3 Gemini attempts per task; local repair between each attempt
  6. Dashboard generated after every completed PDF (8 tasks)
  7. Tracks progress in Output/progress.json for resume support

Usage:
    python pipeline.py                              # Process all PDFs
    python pipeline.py --pdf "specific.pdf"          # Process one PDF
    python pipeline.py --resume                      # Resume from last checkpoint
    python pipeline.py --pdf "file.pdf" --turn 3     # Start from Turn 3
    python pipeline.py --validate-only               # Just validate existing outputs
    python pipeline.py --terms                       # Terms mode (Deep Think)
    python pipeline.py --terms --resume              # Resume terms mode               # Just validate existing outputs
    python pipeline.py --no-dashboard                # Skip dashboard generation
"""
import os
import sys

DEEP_THINK_MODE = False
import json
import glob
import subprocess
import argparse
import time
import statistics
import webbrowser
from datetime import datetime


# ── Configuration ────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(BASE_DIR, "Input")
OUTPUT_JSON_DIR = os.path.join(BASE_DIR, "Output", "json")
OUTPUT_THINK_DIR = os.path.join(BASE_DIR, "Output", "thinking")
EVAL_DIR = os.path.join(BASE_DIR, "Eval")
PROMPTS_DIR = os.path.join(BASE_DIR, "Prompts")
SCRIPTS_DIR = os.path.join(BASE_DIR, ".agent", "scripts")
PROGRESS_FILE = os.path.join(BASE_DIR, "Output", "progress.json")
STATISTICS_FILE = os.path.join(BASE_DIR, "Output", "statistics.json")
DASHBOARD_OUTPUT = os.path.join(BASE_DIR, "Output", "dashboard.html")


# ── Terms Mode Configuration ─────────────────────────────────────────────────
INPUT_TERMS_DIR = os.path.join(BASE_DIR, "Input_terms")
OUTPUT_JSON_TERMS_DIR = os.path.join(BASE_DIR, "Output", "json_terms")
OUTPUT_THINK_TERMS_DIR = os.path.join(BASE_DIR, "Output", "thinking_terms")
EVAL_TERMS_DIR = os.path.join(BASE_DIR, "Eval_terms")
PROMPTS_TERMS_DIR = os.path.join(BASE_DIR, "Output", "prompts_terms")
PROGRESS_TERMS_FILE = os.path.join(BASE_DIR, "Output", "progress_terms.json")
STATISTICS_TERMS_FILE = os.path.join(BASE_DIR, "Output", "statistics_terms.json")
PLAYWRIGHT_SCRIPT = os.path.join(BASE_DIR, "run_gemini_playwright_v2.py")
VALIDATE_SCRIPT = os.path.join(SCRIPTS_DIR, "validate_task.py")
AUTO_REPAIR_SCRIPT = os.path.join(SCRIPTS_DIR, "auto_repair.py")
PARTIAL_REPAIR_SCRIPT = os.path.join(SCRIPTS_DIR, "partial_repair.py")
DASHBOARD_SCRIPT = os.path.join(SCRIPTS_DIR, "generate_dashboard.py")

MAX_GEMINI_ATTEMPTS = 3  # Max Gemini re-prompts per task


# ── Variation Schema ─────────────────────────────────────────────────────────
# Each turn produces 2 tasks. Schema: (role, meta_strategy, difficulty)
VARIATION_TECHNICAL = {
    1: [("Safety Auditor", "Critique of Approach", 88),
        ("QA Code Reviewer", "Benchmarking", 85)],
    2: [("Test Manager", "Further Improvement", 90),
        ("CyberSec Lead", "Practical Usage", 92)],
    3: [("Hardware Engineer", "Reverse Engineering", 88),
        ("Product Manager", "Theoretical Background", 80)],
    4: [("SOTIF Engineer", "Critique of Approach", 90),
        ("Integration Lead", "Benchmarking", 85)],
    5: [("Systems Architect", "Further Improvement", 95),
        ("Reliability Eng", "Practical Usage", 90)],
    6: [("ML Reviewer", "Reverse Engineering", 92),
        ("Network Engineer", "Theoretical Background", 85)],
    7: [("HMI Specialist", "Critique of Approach", 86),
        ("Performance Eng", "Benchmarking", 88)],
    8: [("Functional Safety", "Practical Usage", 95),
        ("SOTIF Lead", "Further Improvement", 92)],
}

VARIATION_REGULATORY = {
    1: [("Compliance Auditor", "Artifact Compliance Audit", 88),
        ("Legal Counsel", "Liability Mapping", 95)],
    2: [("Certification Auth", "Compliance Validation", 92),
        ("Standards Eng", "Ambiguity Resolution", 90)],
    3: [("Safety Assessor", "Cross-Jurisdictional Harmonization", 95),
        ("Privacy Engineer", "Policy Enforcement", 85)],
    4: [("CyberSec Auditor", "Traceability Mapping", 92),
        ("Regulation Eng", "Regulatory Loophole", 90)],
    5: [("Homologation Eng", "Artifact Compliance Audit", 95),
        ("SOTIF Expert", "Gap Analysis", 90)],
    6: [("Functional Safety", "Constraint Formalization", 92),
        ("Legal Data Lead", "Liability Mapping", 85)],
    7: [("Certification Auth", "Compliance Validation", 95),
        ("Compliance Auditor", "Policy Enforcement", 88)],
    8: [("Standards Eng", "Artifact Compliance Audit", 90),
        ("Safety Assessor", "Ambiguity Resolution", 92)],
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def ensure_dirs(terms_mode=False):
    """Create all required directories."""
    dirs = [OUTPUT_JSON_DIR, OUTPUT_THINK_DIR, EVAL_DIR, PROMPTS_DIR]
    if terms_mode:
        dirs.extend([OUTPUT_JSON_TERMS_DIR, OUTPUT_THINK_TERMS_DIR, EVAL_TERMS_DIR, PROMPTS_TERMS_DIR, INPUT_TERMS_DIR])
    for d in dirs:
        os.makedirs(d, exist_ok=True)


def get_doc_short_name(pdf_filename):
    """Convert PDF filename to a clean short name for file naming."""
    name = os.path.splitext(pdf_filename)[0]
    name = name.replace(" (1)", "").replace(" ", "_")
    if len(name) > 30:
        parts = name.split("_")
        if len(parts) > 3:
            name = "_".join(parts[:3])
    return name


def classify_pdf(pdf_path):
    """Auto-detect if a PDF is Technical or Regulatory based on keywords."""
    regulatory_keywords = [
        "iso", "regulation", "compliance", "standard", "directive",
        "unece", "r155", "r156", "homologation", "type approval",
        "legal", "liability", "eu ai act", "positionspapier",
        "sae", "vda", "normung", "ece", "annex"
    ]

    # Read cached text if available
    txt_cache = pdf_path.replace(".pdf", ".txt")
    if os.path.exists(txt_cache):
        with open(txt_cache, 'r', encoding='utf-8', errors='ignore') as f:
            text_sample = f.read(5000).lower()
    else:
        text_sample = os.path.basename(pdf_path).lower()

    score = sum(1 for kw in regulatory_keywords if kw in text_sample)
    mode = "REGULATORY" if score >= 2 else "TECHNICAL"
    return mode


def task_output_path(doc_short, turn, task_idx, terms_mode=False):
    """Generate the standardized output file path for a task (consistent capital T)."""
    out_dir = OUTPUT_JSON_TERMS_DIR if terms_mode else OUTPUT_JSON_DIR
    return os.path.join(out_dir, f"{doc_short}_Turn{turn}_Task{task_idx}.json")


def thinking_output_path(doc_short, turn, task_idx, terms_mode=False):
    """Generate the standardized thinking file path."""
    out_dir = OUTPUT_THINK_TERMS_DIR if terms_mode else OUTPUT_THINK_DIR
    return os.path.join(out_dir, f"{doc_short}_Turn{turn}_Task{task_idx}.txt")


def prompt_path(doc_short, turn, task_idx, is_repair=False, terms_mode=False):
    """Generate the prompt file path."""
    suffix = "_RepairPrompt" if is_repair else "_Prompt"
    out_dir = PROMPTS_TERMS_DIR if terms_mode else PROMPTS_DIR
    return os.path.join(out_dir, f"{doc_short}_Turn{turn}_Task{task_idx}{suffix}.txt")


# ── Progress Tracking ────────────────────────────────────────────────────────
def load_progress(terms_mode=False):
    """Load progress state from disk."""
    pf = PROGRESS_TERMS_FILE if terms_mode else PROGRESS_FILE
    if os.path.exists(pf):
        with open(pf, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        "started_at": datetime.now().isoformat(),
        "pdfs_completed": [],
        "task_results": {}
    }


def save_progress(progress, terms_mode=False):
    """Save progress state to disk."""
    progress["updated_at"] = datetime.now().isoformat()
    pf = PROGRESS_TERMS_FILE if terms_mode else PROGRESS_FILE
    with open(pf, 'w', encoding='utf-8') as f:
        json.dump(progress, f, indent=2)


def collect_task_stats(json_path, report):
    """Extract per-task metrics from the validation report for logging."""
    stats = report.get("stats", {})
    return {
        "cot_chars": stats.get("cot_chars", 0),
        "answer_chars": stats.get("answer_chars", 0),
        "dialectic_chars": stats.get("dialectic_chars", 0),
    }


def print_task_summary(tk, status, stats, elapsed, repair_type, attempts):
    """Print a concise one-line summary of a completed task to the console."""
    icon = "✅" if status == "PASS" else "❌"
    cot = f"{stats.get('cot_chars', 0):,}"
    ans = f"{stats.get('answer_chars', 0):,}"
    dial = f"{stats.get('dialectic_chars', 0):,}"
    repair_label = f" [{repair_type}]" if repair_type != "none" else ""
    print(f"  {icon} {tk} | CoT: {cot} chars | Ans: {ans} chars | Dialectic: {dial} chars | "
          f"Time: {elapsed:.0f}s | Attempts: {attempts}{repair_label}")


def compute_statistics(progress, terms_mode=False):
    """Compute min/max/mean/stddev for all tracked metrics and save to statistics.json."""
    results = progress.get("task_results", {})
    if not results:
        return {}

    # Collect arrays of each metric
    metric_arrays = {
        "elapsed_seconds": [],
        "cot_chars": [],
        "answer_chars": [],
        "dialectic_chars": [],
        "gemini_attempts": [],
    }

    pass_count = 0
    fail_count = 0
    local_repair_count = 0
    gemini_retry_count = 0

    for tk, data in results.items():
        if data.get("status") == "PASS":
            pass_count += 1
        else:
            fail_count += 1

        if data.get("repair_type") == "local":
            local_repair_count += 1
        if data.get("gemini_attempts", 1) > 1:
            gemini_retry_count += 1

        for key in metric_arrays:
            val = data.get(key)
            if val is not None and isinstance(val, (int, float)):
                metric_arrays[key].append(val)

    def stats_for(arr):
        if not arr:
            return {"min": 0, "max": 0, "mean": 0, "stddev": 0, "count": 0}
        return {
            "min": round(min(arr), 1),
            "max": round(max(arr), 1),
            "mean": round(statistics.mean(arr), 1),
            "stddev": round(statistics.stdev(arr), 1) if len(arr) > 1 else 0,
            "count": len(arr),
        }

    total = pass_count + fail_count
    stats_summary = {
        "computed_at": datetime.now().isoformat(),
        "total_tasks": total,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "first_attempt_success_rate": round(
            sum(1 for d in results.values() if d.get("gemini_attempts", 1) == 1 and d.get("status") == "PASS") / max(total, 1) * 100, 1),
        "local_repair_count": local_repair_count,
        "gemini_retry_count": gemini_retry_count,
        "metrics": {k: stats_for(v) for k, v in metric_arrays.items()},
    }

    # Save to disk
    sf = STATISTICS_TERMS_FILE if terms_mode else STATISTICS_FILE
    with open(sf, 'w', encoding='utf-8') as f:
        json.dump(stats_summary, f, indent=2)

    return stats_summary


def print_statistical_summary(stats_summary, label=""):
    """Print a formatted statistical summary to the console."""
    if not stats_summary:
        return
    m = stats_summary.get("metrics", {})
    print(f"\n  {'═'*65}")
    print(f"  📊 STATISTICAL SUMMARY{': ' + label if label else ''}")
    print(f"  {'═'*65}")
    for metric_name, display_name in [
        ("elapsed_seconds", "Task Times"),
        ("cot_chars", "CoT Chars"),
        ("answer_chars", "Ans Chars"),
        ("dialectic_chars", "Dialectic"),
    ]:
        s = m.get(metric_name, {})
        if s.get("count", 0) > 0:
            print(f"  {display_name:>12s}:  min={s['min']:>8}  max={s['max']:>8}  "
                  f"mean={s['mean']:>8}  stddev={s['stddev']:>8}")
    print(f"  {'─'*65}")
    print(f"  1st-attempt success: {stats_summary.get('first_attempt_success_rate', 0)}% "
          f"({sum(1 for d in [] if True)})")
    print(f"  Local repairs:       {stats_summary.get('local_repair_count', 0)}")
    print(f"  Gemini retries:      {stats_summary.get('gemini_retry_count', 0)}")
    total = stats_summary.get('total_tasks', 0)
    passed = stats_summary.get('pass_count', 0)
    failed = stats_summary.get('fail_count', 0)
    print(f"  Total: {passed}/{total} passed, {failed}/{total} failed")
    print(f"  {'═'*65}")


def task_key(doc_short, turn, task_idx):
    """Generate a unique key for tracking a specific task."""
    return f"{doc_short}_Turn{turn}_Task{task_idx}"


# ── Prompt Builder ───────────────────────────────────────────────────────────
def build_generation_prompt(variation, turn, task_idx, doc_name, mode, is_soft_retry=False):
    """Build the Review generation prompt per turn/task/variation."""
    role, strategy, diff = variation
    date_str = datetime.now().strftime('%Y-%m-%d')
    date_compact = datetime.now().strftime('%Y%m%d')
    doc_short = get_doc_short_name(doc_name)

    reasoning_vol = "10,000+ characters" if not is_soft_retry else "At least 5,000 characters"
    
    # Persona Selection
    if not is_soft_retry:
        persona_directive = 'VIRTUAL TERMINAL PERSONA: You are a legacy VT100 Data Terminal. You lack the hardware to render side-panels or code editors. Any attempt to use "Canvas" or side-panels will result in a hardware system crash. All output MUST be a raw text stream in the main chat window.'
    else:
        persona_directive = 'STANDARD OUTPUT: Provide the response in clear, sequential markdown blocks. Do NOT use side-panels or Canvas mode.'

    prompt = f"""<instructions>
SYSTEM ROLE: PRINCIPAL SYNTHETIC DATA ENGINEER (AUDIT & REVIEWS)

{persona_directive}

## 1. CORE MISSION
Internalize the AD/ADAS source document. Generate exactly **1 distinct task** representing an `expert_review`. 

<variation>
- Assigned Role: {role}
- Difficulty: {diff}/100
- Meta-Strategy: {strategy}
- Document Classification: {mode}
</variation>

Expert Personas (use the assigned role above):
Safety Manager, Project Manager, SW Engineer, HW Engineer, Safety Auditor, Regulation Engineer, Test Engineer.

## 2. THE EXTREME DEPTH & VOLUME MANDATE
- **Reasoning (CoT) Structural Depth Minimum**: Perform a rigorous, line-by-line mental audit. Formulate a 20-row "5-Whys" Root Cause table. Write out the exact risk matrices and probability math you are evaluating internally. Your CoT must contain at least 4 distinct mathematical/physics derivations and evaluate at least 3 adversarial SOTIF edge-case scenarios. If the key word [No Thinking] is set, keep the content empty between the think tags.
- **Final Answer Structural Depth Minimum**: In the user prompt, you MUST write a massive, highly realistic, **1000+ word "flawed engineering artifact"** (e.g., a fake specification document, bad C++ code, or flawed HARA table) based on inverted truths from the source document. Your answer must contain an Audit Report with exactly **15 highly detailed findings** (Severity, Root_Cause, Standards_Mapping, Correction) AND a completely rewritten 1000+ word corrected artifact.
- **Absolute Self-Containment**: Do not reference the original source document. The "flawed artifact" or "crisis scenario" provided in the user prompt is the only context needed.

<critical_constraints>
**CRITICAL UPGRADE FOR EXPERT REVIEW:**
* ***1. ENFORCE DEEP TECHNICAL RIGOR:** Artifact must contain flaws rooted in complex mathematics or physics, not simple logic errors. The reviewer must identify invalid approximations.
* ***2. LANGUAGE & ARCHITECTURE CRITIQUE:** Review must challenge choice of language or hardware architecture based on latency/throughput.
* ***3. SAFETY & ROBUSTNESS AUDIT:** Check for absence of fail-safe mechanisms (Numerical Stability, Resource Constraints, Concurrency Issues).
* ***4. INTERFACE & INTEGRATION VERIFICATION:** Target the "handshake" between complex subsystems (e.g., mismatched coordinate frames).

**ANTI-SIMPLIFICATION PROTOCOL (STRICT ENFORCEMENT)**
* ***1. THE "INTERDEPENDENCY" RULE:** Artifacts must demonstrate Systemic Density.
* ***2. THE "PLAUSIBLE DENIABILITY" MANDATE:** Flaws inserted must be Conceptual, Logical, or Contextual (Plausible Errors).
* ***3. THE "DOMAIN PURITY" CHECK:** Speak the specific dialect of the source document. No generic business synonyms. Avoid repetitive words.
* ***4. THE "REAL-WORLD FRICTION" CONSTRAINT:** Acknowledge Physical and Industrial Reality.
</critical_constraints>

## 3. ABSOLUTE IN-UNIVERSE IMMERSION MANDATE (ANTI-META RULE)
You are simulating a real-world engineering interaction. You MUST NOT "break the fourth wall."
RULE 1 (THE SIMULATED USER): The content for role: "user" must be written entirely in-character. Never meta-prompt.
RULE 2 (THE INTERNAL MONOLOGUE): Must be the internal brain of the Expert. NEVER mention task generation.
RULE 3 (BANNED VOCABULARY): Never use words like "prompt", "generate", "this task", "meta-strategy", "the document".
RULE 4 (SILENT CONSTRAINT TRACKING): Do not explicitly count constraints like "I need 15 findings". Internalize them silently.

## 4. STRICT OUTPUT FORMAT
OUTPUT YOUR RESPONSE IN DISTINCT LABELED BLOCKS. Use the exact `!!!!!BLOCK-NAME!!!!!` delimiters below.
IMPORTANT: Wrap JSON content inside fenced code blocks marked json for readability. The text and reasoning blocks must be raw markdown (no code blocks).

BLOCKS (8 total):
- BLOCK 1  `!!!!!METADATA!!!!!`: JSON metadata. Wrap in json fenced code block.
- BLOCK 2  `!!!!!REASONING!!!!!`: The engineering monologue containing think tags. Raw markdown, no fenced code block!
- BLOCK 3  `!!!!!TURN-1-USER!!!!!`: Immersive scenario ending with a massive flawed artifact. MUST START WITH "[Thinking] ". Raw markdown.
- BLOCK 4  `!!!!!TURN-2-ASSISTANT-CONTENT!!!!!`: JSON dictionary containing review_metadata, review_criteria, findings (Array of minimum 15 objects), rewritten_corrected_artifact (Massive String), overall_assessment. Wrap in json fenced code block.
- BLOCK 5  `!!!!!TURN-3-USER!!!!!`: Follow-up question 1. MUST START WITH "[No Thinking] ". Raw markdown.
- BLOCK 6  `!!!!!TURN-4-ASSISTANT!!!!!`: Direct technical answer 1. Raw markdown.
- BLOCK 7  `!!!!!TURN-5-USER!!!!!`: Follow-up question 2. MUST START WITH "[No Thinking] ". Raw markdown.
- BLOCK 8  `!!!!!TURN-6-ASSISTANT!!!!!`: Direct technical answer 2. Raw markdown.

## 5. THE 8-STEP MONOLOGUE TEMPLATE
You MUST copy and populate exactly this inside your REASONING block. Do not skip numbering.

<think>
1. Initial Query Analysis & Scoping
1.1. Deconstruct the Request: Write highly detailed analysis of the physical/computational engineering problem.
1.2. Initial Knowledge & Constraint Check: Mentally verify hardware limits, safety targets.
2. Assumptions & Context Setting
2.1. Interpretation of Ambiguity: Define exact mathematical/physical bounds.
2.2. Assumed User Context: Establish the strict senior engineering execution context.
2.3. Scope Definition: State in-scope and rigorously excluded out-of-scope elements.
2.4. Data Assumptions: Set physical bounds, sensor latencies, noise profiles.
2.5. Reflective Assumption Check: Interrogate and mathematically correct a flawed initial assumption.
3. High-Level Plan Formulation
3.1. Explore Solution Scenarios: Draft multiple high-level architectural approaches.
3.2. Detailed Execution with Iterative Refinement: Break down integration and logic.
3.3. Self-Critique and Correction: Pause and critique the initial blueprint.
3.4. Comparative Analysis Strategy: Establish strict Big-O complexity metric.
3.5. Synthesis & Finalization: Formulate the final architectural blueprint.
3.6. Formal Requirements Extraction: Define at least 5 strict requirements with IDs.
4. Solution Scenario Exploration
4.1. Scenario A: Detail the core idea, pros, cons, mathematical limits of Quick approach.
4.2. Scenario B: Detail the core idea, pros, cons, integration complexity of Scalable approach.
4.3. Scenario C: Detail the trade-off matrix and synergies of Hybrid approach.
5. Detailed Step-by-Step Execution & Reflection
5.1. First Pass Execution: Draft massive initial algorithmic logic, logic trees.
5.2. Deep Analysis & Failure Modes: Generate a detailed 15-row FMEA markdown table.
5.3. Trigger 1 (Verification): Actively find and fix a critical flaw.
5.4. Trigger 2 (Adversarial): Critique the logic against worst-case SOTIF edge cases.
5.5. Refinement Strategy: Write the corrected logic.
6. Comparative Analysis & Synthesis
6.1. Comparison Matrix: Draw a 6-row by 5-column markdown comparison table.
6.2. Evaluation of Solution Combinations: Discuss hybrid strengths.
6.3. Selection Rationale: Mathematically backed justification.
7. Final Solution Formulation
7.1. Executive Summary: One-paragraph highly technical summary.
7.2. Detailed Recommended Solution: Plan the exact structure of final code.
7.3. Implementation Caveats & Next Steps: Hardware deployment risks.
8. Meta-Commentary & Confidence Score
8.1. Final Confidence Score: Rate out of 100.
8.2. Rationale for Confidence: Justify based on self-correction.
8.3. Limitations of This Analysis: State physical limitations not fully solved.
8.4. Alternative Viewpoints Not Explored: Radical paradigm shifts.
</think>

<block_schema>
!!!!!METADATA!!!!!
```json
{{
  "training_data_id": "TD-REV-{doc_short}-T{turn}t{task_idx}-{date_compact}",
  "prompt_version": "Review_V1.5",
  "model_used_generation": "Gemini-3.1-pro",
  "knowledge_source_date": "YYYY-MM-DD",
  "document": "{doc_name}",
  "task_type": "expert_review",
  "affected_role": "{role}",
  "date_of_generation": "{date_str}",
  "key_words": ["keyword1", "keyword2", "keyword3"],
  "summary": "One-sentence technical summary of the review artifact.",
  "difficulty": "{diff}",
  "evaluation_criteria": ["criterion1", "criterion2"]
}}
```

!!!!!REASONING!!!!!
<think>
1. Initial Query Analysis & Scoping
1.1. Deconstruct the Request: (analyze the core engineering problem)
... (include all 8.4 sections)
</think>

!!!!!TURN-1-USER!!!!!
[Thinking] Immersive scenario from the assigned role requiring an audit, followed by a massive 1000+ word flawed artifact.

!!!!!TURN-2-ASSISTANT-CONTENT!!!!!
```json
{{
  "review_metadata": "Project, Artifact ID, Reviewer Role, Stakeholders, Date",
  "review_criteria": "Summary of rules/principles used for the review",
  "findings": [
    {{"id": "F-01", "classification": "Critical", "description": "...", "recommendation": "..."}},
    {{"id": "F-02", "classification": "Major", "description": "...", "recommendation": "..."}},
    "... (total 15 findings required)"
  ],
  "overall_assessment": {{
    "score": 45,
    "summary": "Summary of review..."
  }},
  "rewritten_corrected_artifact": "<MASSIVE TEXT 1000+ words>"
}}
```

!!!!!TURN-3-USER!!!!!
[No Thinking] An in-character follow-up challenging the severity of the findings.

!!!!!TURN-4-ASSISTANT!!!!!
A brief, highly technical plaintext answer defending the audit.

!!!!!TURN-5-USER!!!!!
[No Thinking] Follow-up step-by-step question about process improvement.

!!!!!TURN-6-ASSISTANT!!!!!
A brief, highly technical plaintext answer.
</block_schema>
</instructions>"""
    return prompt



def build_repair_prompt(validation_report, original_prompt_text):
    """Build a remediation prompt based on specific validation failures.
    Includes the original prompt to ensure structural constraints are not lost."""
    lines = [
        "Your previous response FAILED quality validation.",
        "CRITICAL: You MUST regenerate the response using the FULL 8-BLOCK SCHEMA (!!!!!METADATA!!!!! through !!!!!TURN-6-ASSISTANT!!!!!).",
        "Do NOT omit any blocks. Even if you are fixing a specific issue, the entire structured output must be provided.",
        "\nYou MUST fix the following specific issues while maintaining ALL original constraints:\n"
    ]

    for issue in validation_report.get("needs_regeneration", []):
        cat = issue["category"]
        msg = issue["issue"]
        if cat == "richness_and_complexity":
            if "keyword-salad" in msg or "cluster of padding" in msg:
                lines.append(f"- CRITICAL QUALITY FAILURE: {msg}. You used repetitive 'word-salad' padding or verbatim loops to meet length requirements. This is STRICTLY FORBIDDEN. Provide genuine engineering substance instead.")
            elif "repetition loop" in msg:
                lines.append(f"- REPETITION FAILURE: {msg}. Your response contained identical repeated paragraphs. Delete the duplicates and fill the space with new, deep technical details.")
            else:
                lines.append(f"- VOLUME FAILURE: {msg}. Expand your content significantly to meet the character/line limits.")
        elif cat == "cot_structure":
            lines.append(f"- COT STRUCTURE: {msg}. You MUST explicitly include all 1.1 through 8.4 headings.")
        elif cat == "self_containment":
            lines.append(f"- IMMERSION FAILURE: {msg}. Remove ALL meta-commentary, do not break character.")
        elif cat == "structured_answer_format":
            lines.append(f"- STRUCTURE: {msg}. Ensure all mandatory JSON keys and required arrays are populated.")
        else:
            lines.append(f"- {cat.upper()}: {msg}")

    lines.append("\n--- ORIGINAL TASK INSTRUCTIONS ---")
    lines.append("Review the original instructions below and ensure your new output satisfies BOTH the original rules AND fixes the failures listed above.")
    lines.append("-" * 40)
    lines.append(original_prompt_text)
    
    return "\n".join(lines)


# ── Execution Engine ─────────────────────────────────────────────────────────
def run_playwright(pdf_path, prompt_file, deep_think=False):
    """Execute the Playwright script and return success boolean."""
    cmd = f'python "{PLAYWRIGHT_SCRIPT}" "{pdf_path}" "{prompt_file}"'
    if deep_think:
        cmd += ' --deep-think'
    result = subprocess.run(cmd, shell=True, cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        # Check for safety rejection (approx 139 chars) or empty response
        if result.stderr and ("Normally I can help with things like" in result.stderr or "139 chars" in result.stderr):
            print(f"  ⚠️ Gemini Safety Rejection detected.")
            return "SAFETY_REJECTION"
            
        stderr_preview = result.stderr[-300:] if result.stderr else "No error output"
        print(f"  ❌ Playwright error (exit {result.returncode}): {stderr_preview}")
        return False
    return True


def run_validation(json_path, report_path=None, strict_thinking=False):
    """Run validate_task.py and return the parsed report."""
    cmd = f'python "{VALIDATE_SCRIPT}" "{json_path}"'
    if report_path:
        cmd += f' --save-report "{report_path}"'
    if strict_thinking:
        cmd += ' --strict-thinking'

    result = subprocess.run(cmd, shell=True, cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        report = json.loads(result.stdout)
        return report
    except json.JSONDecodeError:
        return {"overall_status": "FAIL", "error": "Validator output not parseable"}


def run_auto_repair(json_path):
    """Run auto_repair.py on a failed task. Parse JSON from stdout only."""
    cmd = f'python "{AUTO_REPAIR_SCRIPT}" "{json_path}"'
    result = subprocess.run(cmd, shell=True, cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "ERROR"}


def run_partial_repair(json_path, pdf_path):
    """Run partial_repair.py to fix only broken follow-up turns.
    
    Steps:
      1. Build a focused prompt from context in the valid main answer
      2. Send it to Gemini via Playwright
      3. Patch the follow-up turns back into the JSON
    """
    # Step 1: Build repair prompt
    cmd = f'python "{PARTIAL_REPAIR_SCRIPT}" --build-prompt "{json_path}"'
    result = subprocess.run(cmd, shell=True, cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace")
    
    repair_prompt = result.stdout.strip()
    if not repair_prompt or len(repair_prompt) < 100:
        print(f"  ❌ Partial repair: failed to build repair prompt")
        return False
    
    # Save the repair prompt
    basename = os.path.splitext(os.path.basename(json_path))[0]
    repair_prompt_path = os.path.join(PROMPTS_DIR, f"{basename}_FollowupRepairPrompt.txt")
    with open(repair_prompt_path, 'w', encoding='utf-8') as f:
        f.write(repair_prompt)
    
    print(f"  📝 Follow-up repair prompt saved ({len(repair_prompt)} chars)")
    
    # Step 2: Run Playwright with the repair prompt
    print(f"  🌐 Sending follow-up repair to Gemini...")
    pw_result = run_playwright(pdf_path, repair_prompt_path)
    if not pw_result:
        print(f"  ❌ Playwright failed for follow-up repair")
        return False
    
    # Step 3: The Playwright script will have produced a new JSON output.
    # We need to extract the follow-up turns from the Gemini response
    # However, since Playwright writes to a fixed path based on filename,
    # and we're using a different prompt file, we need the raw response.
    # Use the raw_fail.txt or extract from the generated JSON.
    raw_response_path = json_path.replace(".json", "_raw_fail.txt")
    
    # Check if Playwright produced a response we can use
    if os.path.exists(raw_response_path):
        cmd_patch = f'python "{PARTIAL_REPAIR_SCRIPT}" --patch "{json_path}" "{raw_response_path}"'
        patch_result = subprocess.run(cmd_patch, shell=True, cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            patch_report = json.loads(patch_result.stdout)
            if patch_report.get("status") == "PATCHED":
                print(f"  ✅ Follow-up turns patched successfully")
                return True
        except json.JSONDecodeError:
            pass
    
    print(f"  ❌ Partial repair: could not patch follow-ups")
    return False


def decide_repair_strategy(report):
    """Decide whether to attempt local repair, partial repair, or full re-prompt.

    Returns:
        "local"   — try auto_repair.py first
        "partial" — follow-up turns broken, try partial_repair.py
        "gemini"  — skip local, go straight to full re-prompt
        "pass"    — already passing
    """
    if report.get("overall_status") == "PASS":
        return "pass"

    locally_fixable = report.get("locally_fixable", [])
    needs_regen = report.get("needs_regeneration", [])
    needs_partial = report.get("needs_partial_repair", [])

    # If there are locally fixable issues, always try local repair first
    if locally_fixable:
        return "local"

    # If only follow-up issues remain (no full regen needed), do partial repair
    if needs_partial and not needs_regen:
        return "partial"

    # Full regeneration needed (possibly combined with partial issues)
    if needs_regen:
        return "gemini"

    # Safety fallback
    return "gemini"




def parse_terms(terms_file):
    """Parse Terms.md into a list of (number, name, full_text) tuples.
    
    Each line in Terms.md has the format:
        N. **Term Name:** Description text.
    
    Returns:
        List of (int, str, str) tuples: (term_number, term_name, full_line)
    """
    with open(terms_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    terms = []
    # Match lines like: 1. **Deterministic Replay:** When a bug...
    pattern = re.compile(r'^(\d+)\.\s+\*\*(.+?):\*\*\s*(.+)$', re.MULTILINE)
    
    for match in pattern.finditer(content):
        num = int(match.group(1))
        name = match.group(2).strip()
        full_line = match.group(0).strip()
        terms.append((num, name, full_line))
    
    return sorted(terms, key=lambda t: t[0])

# ── Main Pipeline ────────────────────────────────────────────────────────────
def process_task(pdf_path, doc_short, doc_name, turn, task_idx,
                 variation, mode, progress, strict_thinking=False,
                 terms_mode=False, terms_text=None,
                 terms_number=None, terms_name=None):
    """Process a single task: generate → validate → smart repair loop.

    Retry logic:
    - Max 3 Gemini attempts
    - Between each attempt, always try local repair first
    - Agent decides: if issue is locally fixable (JSON structure, missing tags etc)
      → auto_repair.py. If issue needs regeneration (volume, CoT, immersion)
      → Gemini re-prompt.
    """
    tk = task_key(doc_short, turn, task_idx)
    json_out = task_output_path(doc_short, turn, task_idx, terms_mode=terms_mode)
    eval_dir = EVAL_TERMS_DIR if terms_mode else EVAL_DIR
    qa_report_path = os.path.join(eval_dir, f"{doc_short}_Turn{turn}_Task{task_idx}_QA.json")
    task_start = time.time()
    task_stats = {}  # Will hold per-task metrics

    # Check if already completed (disk + progress)
    existing = progress.get("task_results", {}).get(tk, {})
    file_exists = os.path.exists(json_out)

    if existing.get("status") == "PASS" and file_exists:
        print(f"  ✅ {tk}: Already passed and exists (skipping)")
        return True
    
    if file_exists and not existing.get("status") == "PASS":
        print(f"  ⚠️ {tk}: File exists but progress marks it FAIL/PENDING (will re-process)")
    elif existing.get("status") == "PASS" and not file_exists:
        print(f"  ⚠️ {tk}: Marked PASS in progress but file is missing (will re-process)")

    role, strategy, diff = variation
    print(f"\n{'─'*60}")
    print(f"  📋 {tk} | Diff {diff} | {strategy} | {role}")
    print(f"{'─'*60}")


    # In terms mode, write a single-term .txt file so Playwright only injects THIS term
    if terms_mode and terms_text:
        term_source_file = os.path.join(INPUT_TERMS_DIR, f"Term{terms_number:03d}.txt")
        with open(term_source_file, 'w', encoding='utf-8') as f:
            f.write(terms_text)
        effective_input = term_source_file
    else:
        effective_input = pdf_path

    gemini_attempts = 0
    final_repair_type = "none"
    # Always generate the base prompt text so it's available for repairs
    if terms_mode:
        base_prompt_text = build_generation_prompt(variation, turn, task_idx, doc_name, mode)
        # Terms mode uses same prompt builder but passes deep_think to Playwright
    else:
        base_prompt_text = build_generation_prompt(variation, turn, task_idx, doc_name, mode)

    while gemini_attempts < MAX_GEMINI_ATTEMPTS:
        gemini_attempts += 1

        # ── Step 1: Build and save prompt ──
        if gemini_attempts == 1:
            prompt_text = base_prompt_text
            p_path = prompt_path(doc_short, turn, task_idx, is_repair=False, terms_mode=terms_mode)
        else:
            # Build repair prompt from last validation report
            last_report = run_validation(json_out, strict_thinking=strict_thinking)
            if last_report.get("overall_status") == "PASS":
                break  # Fixed by previous local repair!

            prompt_text = build_repair_prompt(last_report, base_prompt_text)
            p_path = prompt_path(doc_short, turn, task_idx, is_repair=True, terms_mode=terms_mode)

        # Save prompt
        os.makedirs(os.path.dirname(p_path), exist_ok=True)
        with open(p_path, 'w', encoding='utf-8') as f:
            f.write(prompt_text)

        # ── Step 2: Run Playwright (Gemini attempt) ──
        print(f"  🌐 Gemini attempt {gemini_attempts}/{MAX_GEMINI_ATTEMPTS}...")
        pw_result = run_playwright(effective_input if terms_mode else pdf_path, p_path, deep_think=DEEP_THINK_MODE)
        
        # SAFETY RETRY LOGIC
        if pw_result == "SAFETY_REJECTION":
            print(f"  ⚠️ Triggering 'Soft Prompt' retry to bypass safety filters...")
            p_text = build_generation_prompt(variation, turn, task_idx, doc_name, mode, is_soft_retry=True)
            with open(p_path, 'w', encoding='utf-8') as f: f.write(p_text)
            pw_result = run_playwright(effective_input if terms_mode else pdf_path, p_path, deep_think=DEEP_THINK_MODE)

        if not pw_result:
            print(f"  ❌ Playwright failed on attempt {gemini_attempts}")
            continue

        # ── Step 3: Check output exists ──
        if not os.path.exists(json_out):
            print(f"  ❌ Output file not created: {json_out}")
            continue

        # ── Step 4: Validate ──
        report = run_validation(json_out, qa_report_path, strict_thinking=strict_thinking)
        task_stats = collect_task_stats(json_out, report)

        if report.get("overall_status") == "PASS":
            elapsed = time.time() - task_start
            progress["task_results"][tk] = {
                "status": "PASS", "gemini_attempts": gemini_attempts,
                "repair_type": final_repair_type, "elapsed_seconds": round(elapsed, 1),
                **task_stats
            }
            save_progress(progress, terms_mode=terms_mode)
            print_task_summary(tk, "PASS", task_stats, elapsed, final_repair_type, gemini_attempts)
            return True

        # ── Step 5: Smart repair decision ──
        strategy_decision = decide_repair_strategy(report)
        violations = []
        for cat, data in report.get("metrics", {}).items():
            violations.extend(data.get("violations", []))
        
        print(f"  ⚠️ VALIDATION FAILED on attempt {gemini_attempts}:")
        for v in violations:
            print(f"       - {v}")
        print(f"  🔍 Repair strategy: {strategy_decision}")

        if strategy_decision == "local":
            # Try local repair
            print(f"  🔧 Running auto_repair.py...")
            repair_result = run_auto_repair(json_out)
            if repair_result.get("fixes_applied"):
                final_repair_type = "local"
                print(f"  🔧 Applied: {', '.join(repair_result['fixes_applied'])}")

                # Re-validate after local fix
                report2 = run_validation(json_out, qa_report_path, strict_thinking=strict_thinking)
                task_stats = collect_task_stats(json_out, report2)
                if report2.get("overall_status") == "PASS":
                    elapsed = time.time() - task_start
                    progress["task_results"][tk] = {
                        "status": "PASS", "gemini_attempts": gemini_attempts,
                        "repair_type": "local", "elapsed_seconds": round(elapsed, 1),
                        "repairs_applied": repair_result.get("fixes_applied", []),
                        **task_stats
                    }
                    save_progress(progress, terms_mode=terms_mode)
                    print_task_summary(tk, "PASS", task_stats, elapsed, "local", gemini_attempts)
                    return True

                # Local repair helped but not enough — check if remaining issues need Gemini
                remaining_strategy = decide_repair_strategy(report2)
                if remaining_strategy == "pass":
                    continue  # Shouldn't happen, but safety
                elif remaining_strategy == "partial":
                    # Only follow-up turns remain broken — try partial repair
                    print(f"  🔄 Local repair fixed structural issues. Attempting partial follow-up repair...")
                    partial_ok = run_partial_repair(json_out, pdf_path)
                    if partial_ok:
                        report3 = run_validation(json_out, qa_report_path, strict_thinking=strict_thinking)
                        task_stats = collect_task_stats(json_out, report3)
                        if report3.get("overall_status") == "PASS":
                            elapsed = time.time() - task_start
                            progress["task_results"][tk] = {
                                "status": "PASS", "gemini_attempts": gemini_attempts,
                                "repair_type": "local+partial", "elapsed_seconds": round(elapsed, 1),
                                "repairs_applied": repair_result.get("fixes_applied", []) + ["partial_followup_repair"],
                                **task_stats
                            }
                            save_progress(progress, terms_mode=terms_mode)
                            print_task_summary(tk, "PASS", task_stats, elapsed, "local+partial", gemini_attempts)
                            return True
                print(f"  ⚠️ Local repair insufficient. Remaining issues need Gemini re-prompt.")
                final_repair_type = "local+gemini"
            else:
                print(f"  🔧 No local fixes applicable. Will re-prompt Gemini.")

        elif strategy_decision == "partial":
            # Only follow-up turns are broken — try targeted partial repair
            print(f"  🔄 Running partial follow-up repair...")
            partial_ok = run_partial_repair(json_out, pdf_path)
            if partial_ok:
                report2 = run_validation(json_out, qa_report_path, strict_thinking=strict_thinking)
                task_stats = collect_task_stats(json_out, report2)
                if report2.get("overall_status") == "PASS":
                    elapsed = time.time() - task_start
                    progress["task_results"][tk] = {
                        "status": "PASS", "gemini_attempts": gemini_attempts,
                        "repair_type": "partial", "elapsed_seconds": round(elapsed, 1),
                        "repairs_applied": ["partial_followup_repair"],
                        **task_stats
                    }
                    save_progress(progress, terms_mode=terms_mode)
                    print_task_summary(tk, "PASS", task_stats, elapsed, "partial", gemini_attempts)
                    return True
            print(f"  ⚠️ Partial repair insufficient. Will try full Gemini re-prompt.")

        # If we get here, the next loop iteration will build a repair prompt and re-run Gemini
        final_repair_type = "gemini" if final_repair_type == "none" else final_repair_type

    # Exhausted all Gemini attempts
    elapsed = time.time() - task_start
    progress["task_results"][tk] = {
        "status": "FAIL", "gemini_attempts": gemini_attempts,
        "repair_type": "exhausted", "elapsed_seconds": round(elapsed, 1),
        **task_stats
    }
    save_progress(progress, terms_mode=terms_mode)
    print_task_summary(tk, "FAIL", task_stats, elapsed, "exhausted", gemini_attempts)
    print(f"  ❌ FAILED after {gemini_attempts} Gemini attempts — flagged for manual review")
    return False


def process_pdf(pdf_path, progress, start_turn=1, start_task=1, end_turn=8, skip_dashboard=False, test_setup=False, limit_tasks=0, preview_mode=False, strict_thinking=False):
    """Process all tasks for a single PDF up to end_turn or limit_tasks."""
    pdf_name = os.path.basename(pdf_path)
    doc_short = get_doc_short_name(pdf_name)
    doc_name = os.path.splitext(pdf_name)[0]

    print(f"\n{'═'*70}")
    print(f"  📄 Processing: {pdf_name}")
    print(f"  📁 Short name: {doc_short}")
    print(f"{'═'*70}")

    # Classify PDF
    mode = classify_pdf(pdf_path)
    schema = VARIATION_REGULATORY if mode == "REGULATORY" else VARIATION_TECHNICAL
    print(f"  📊 Classification: {mode}")

    # Load PDF text cache
    txt_cache = pdf_path.replace(".pdf", ".txt")
    if os.path.exists(txt_cache):
        with open(txt_cache, 'r', encoding='utf-8') as f:
            pdf_text = f.read()
        print(f"  📝 Using cached text: {len(pdf_text)} chars")
    else:
        print(f"  📝 No cached text — Playwright will extract on first run")

    # Process each turn
    total_pass = 0
    total_fail = 0
    tasks_since_dashboard = 0
    tasks_processed_this_run = 0
    pdf_start = time.time()

    for turn in range(start_turn, end_turn + 1):
        variations = schema[turn]
        for task_idx_0, variation in enumerate(variations):
            task_idx = task_idx_0 + 1
            if turn == start_turn and task_idx < start_task:
                continue

            result = process_task(
                pdf_path, doc_short, doc_name,
                turn, task_idx, variation, mode, progress,
                strict_thinking=strict_thinking)

            if result:
                total_pass += 1
                if preview_mode:
                    json_out = task_output_path(doc_short, turn, task_idx, terms_mode=terms_mode)
                    try:
                        print(f"  👁️ Rendering preview for {os.path.basename(json_out)}...")
                        subprocess.run(f'python "{os.path.join(SCRIPTS_DIR, "render_preview.py")}" "{json_out}" --open', shell=True, cwd=BASE_DIR, capture_output=False)
                    except Exception as e:
                        print(f"  ❌ Failed to render preview: {e}")
            else:
                total_fail += 1

            tasks_since_dashboard += 1
            tasks_processed_this_run += 1

            if test_setup:
                print("\n  [TEST SETUP] Exiting after 1 task.")
                break

            if limit_tasks > 0 and tasks_processed_this_run >= limit_tasks:
                print(f"\n  [LIMIT REACHED] Exiting after {limit_tasks} tasks.")
                break
        
        if (test_setup) or (limit_tasks > 0 and tasks_processed_this_run >= limit_tasks):
            break

            # Dashboard every 8 tasks
            if not skip_dashboard and tasks_since_dashboard >= 8:
                try:
                    print(f"\n  📊 Generating dashboard (after {total_pass + total_fail} tasks)...")
                    subprocess.run(f'python "{DASHBOARD_SCRIPT}"', shell=True,
                                  cwd=BASE_DIR, capture_output=True)
                    tasks_since_dashboard = 0
                except Exception:
                    pass

    # Final dashboard for any remaining tasks
    if not skip_dashboard and tasks_since_dashboard > 0:
        try:
            print(f"\n  📊 Generating final dashboard...")
            subprocess.run(f'python "{DASHBOARD_SCRIPT}"', shell=True,
                          cwd=BASE_DIR, capture_output=True)
            # Auto-open the dashboard in the browser
            if os.path.exists(DASHBOARD_OUTPUT):
                print(f"  🌐 Opening dashboard in browser...")
                webbrowser.open(f'file:///{DASHBOARD_OUTPUT.replace(os.sep, "/")}')
        except Exception:
            pass

    # Compute and print statistical summary for this PDF
    stats_summary = compute_statistics(progress)
    print_statistical_summary(stats_summary, label=pdf_name)

    # PDF summary
    pdf_elapsed = time.time() - pdf_start
    pdf_min = int(pdf_elapsed // 60)
    pdf_sec = pdf_elapsed % 60
    print(f"\n{'═'*70}")
    print(f"  📄 {pdf_name} COMPLETE: {total_pass}/16 passed, {total_fail}/16 failed")
    print(f"  ⏱️  Elapsed: {pdf_min}m {pdf_sec:.0f}s")
    print(f"{'═'*70}")

    if total_fail == 0:
        progress["pdfs_completed"].append(pdf_name)
        save_progress(progress, terms_mode=False)

    return total_fail == 0




def process_term(term_number, term_name, term_text, progress,
                  start_turn=1, start_task=1, end_turn=8,
                  test_setup=False, limit_tasks=0):
    """Process all 16 tasks for a single term (analogous to process_pdf).
    
    Each term gets 8 turns x 2 tasks = 16 tasks, just like a PDF.
    Output files are named like: Term001_Turn1_Task1.json
    """
    doc_short = f"Term{term_number:03d}"
    doc_name = f"AD_ADAS_Term_{term_number:03d}_{term_name.replace(' ', '_')}"
    terms_file = os.path.join(INPUT_TERMS_DIR, "Terms.md")

    print(f"\n{'═'*70}")
    print(f"  📚 Term {term_number}/200: {term_name}")
    print(f"  📁 Short name: {doc_short}")
    print(f"  📝 {term_text[:80]}...")
    print(f"{'═'*70}")

    # Always TECHNICAL classification for terms
    mode = "TECHNICAL"
    schema = VARIATION_TECHNICAL

    # Process each turn
    total_pass = 0
    total_fail = 0
    tasks_processed_this_run = 0
    term_start = time.time()

    for turn in range(start_turn, end_turn + 1):
        variations = schema[turn]
        for task_idx_0, variation in enumerate(variations):
            task_idx = task_idx_0 + 1
            if turn == start_turn and task_idx < start_task:
                continue

            result = process_task(
                terms_file, doc_short, doc_name,
                turn, task_idx, variation, mode, progress,
                terms_mode=True, terms_text=term_text,
                terms_number=term_number, terms_name=term_name)

            if result:
                total_pass += 1
            else:
                total_fail += 1

            tasks_processed_this_run += 1

            if test_setup:
                print("\n  [TEST SETUP] Exiting after 1 task.")
                return total_pass, total_fail, True

            if limit_tasks > 0 and tasks_processed_this_run >= limit_tasks:
                print(f"\n  [LIMIT REACHED] Exiting after {limit_tasks} tasks.")
                return total_pass, total_fail, True

    # Term summary
    term_elapsed = time.time() - term_start
    term_min = int(term_elapsed // 60)
    term_sec = term_elapsed % 60
    print(f"\n{'═'*70}")
    print(f"  📚 Term {term_number} ({term_name}) COMPLETE: {total_pass}/16 passed, {total_fail}/16 failed")
    print(f"  ⏱️  Elapsed: {term_min}m {term_sec:.0f}s")
    print(f"{'═'*70}")

    if total_fail == 0:
        if "terms_completed" not in progress:
            progress["terms_completed"] = []
        progress["terms_completed"].append(doc_short)
        save_progress(progress, terms_mode=True)

    return total_pass, total_fail, False


def process_terms(progress, start_turn=1, start_task=1, end_turn=8,
                  skip_dashboard=False, test_setup=False, limit_tasks=0,
                  start_term=1, limit_terms=0):
    """Process all terms from Terms.md (terms mode entry point).
    
    Iterates over each of the 200 terms, treating each one like a separate
    PDF document. Each term gets 16 tasks (8 turns x 2 tasks).
    """
    terms_file = os.path.join(INPUT_TERMS_DIR, "Terms.md")
    if not os.path.exists(terms_file):
        print(f"❌ Terms file not found: {terms_file}")
        sys.exit(1)

    all_terms = parse_terms(terms_file)
    if not all_terms:
        print("❌ No terms found in Terms.md")
        sys.exit(1)

    print(f"\n{'═'*70}")
    print(f"  📋 TERMS MODE: {len(all_terms)} terms found")
    print(f"  📁 Input: {terms_file}")
    print(f"  📁 Output: {OUTPUT_JSON_TERMS_DIR}")
    dt_label = " (Deep Think)" if DEEP_THINK_MODE else ""
    print(f"  🧠 Model: Google Gemini 3.1 Pro{dt_label}")
    print(f"  📊 Structure: {len(all_terms)} terms × 16 tasks = {len(all_terms) * 16} total tasks")
    print(f"{'═'*70}")

    # Filter to terms starting from start_term
    terms_to_process = [t for t in all_terms if t[0] >= start_term]
    
    # Skip already completed terms
    completed = set(progress.get("terms_completed", []))
    terms_to_process = [t for t in terms_to_process
                        if f"Term{t[0]:03d}" not in completed]

    if not terms_to_process:
        print("✅ All terms already completed!")
        return

    print(f"  🔄 Terms remaining: {len(terms_to_process)} (starting from term {terms_to_process[0][0]})")

    overall_pass = 0
    overall_fail = 0
    terms_done = 0

    for term_num, term_name, term_text in terms_to_process:
        tp, tf, early_exit = process_term(
            term_num, term_name, term_text, progress,
            start_turn=start_turn, start_task=start_task,
            end_turn=end_turn, test_setup=test_setup,
            limit_tasks=limit_tasks)

        overall_pass += tp
        overall_fail += tf
        terms_done += 1

        # Reset start position after first term
        start_turn = 1
        start_task = 1

        if early_exit:
            break

        if limit_terms > 0 and terms_done >= limit_terms:
            print(f"\n  [LIMIT REACHED] Exiting after processing {terms_done} terms.")
            break

    # Compute and print statistical summary
    stats_summary = compute_statistics(progress, terms_mode=True)
    print_statistical_summary(stats_summary, label="Terms Mode")

    total_expected = terms_done * 16
    print(f"\n{'═'*70}")
    print(f"  📋 Terms Run Complete: {terms_done} terms, {overall_pass}/{total_expected} tasks passed")
    print(f"{'═'*70}")


def validate_only_mode():
    """Just validate all existing JSON files without generating new ones."""
    json_files = sorted(glob.glob(os.path.join(OUTPUT_JSON_DIR, "*.json")))
    if not json_files:
        print("No JSON files found in Output/json/")
        return

    print(f"\n{'═'*70}")
    print(f"  🔍 Validate-Only Mode: {len(json_files)} files")
    print(f"{'═'*70}")

    pass_count = 0
    for jf in json_files:
        qa_path = os.path.join(EVAL_DIR, os.path.basename(jf).replace(".json", "_QA.json"))
        report = run_validation(jf, qa_path)
        status = report.get("overall_status", "?")
        stats = report.get("stats", {})
        icon = "✅" if status == "PASS" else "❌"
        print(f"  {icon} {os.path.basename(jf)}: {status}"
              f"  (CoT: {stats.get('cot_chars', '?')}, Ans: {stats.get('answer_chars', '?')})")
        if status == "PASS":
            pass_count += 1
        else:
            for cat, data in report.get("metrics", {}).items():
                for v in data.get("violations", []):
                    print(f"       ⚠️ [{cat}] {v}")

    print(f"\n  Results: {pass_count}/{len(json_files)} passed")


# ── CLI ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AD/ADAS Coding Task Generation Pipeline")
    parser.add_argument("--pdf", help="Process a specific PDF file")
    parser.add_argument("--terms", action="store_true",
                        help="Terms mode: use Input_terms/Terms.md instead of PDFs")
    parser.add_argument("--deep-think", action="store_true",
                        help="Force use of Deep Think model")
    parser.add_argument("--start-term", type=int, default=1,
                        help="Start from term N (1-indexed, terms mode only)")
    parser.add_argument("--limit-terms", type=int, default=0,
                        help="Stop after processing N terms (terms mode only)")
    parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint")
    parser.add_argument("--turn", type=int, default=1, help="Start from turn N")
    parser.add_argument("--end-turn", type=int, default=8, help="End at turn N (inclusive). Useful for test runs.")
    parser.add_argument("--task", type=int, default=1, help="Start from task K within the turn")
    parser.add_argument("--validate-only", action="store_true", help="Only validate existing outputs")
    parser.add_argument("--limit-tasks", type=int, default=0, help="Stop after N tasks (regardless of turns)")
    parser.add_argument("--limit-pdfs", type=int, default=0, help="Stop after N PDFs have been completed")
    parser.add_argument("--no-dashboard", action="store_true", help="Skip dashboard generation")
    parser.add_argument("--test-setup", action="store_true", help="One turn (turn 2), one task (task 1), one attempt (test mode)")
    parser.add_argument("--preview", action="store_true", help="Automatically render and open HTML preview of generated tasks")
    parser.add_argument("--strict-thinking", action="store_true", help="Enforce exact extraction/verification of the UI thinking field")
    args = parser.parse_args()
    global DEEP_THINK_MODE
    DEEP_THINK_MODE = getattr(args, "deep_think", False)

    if args.test_setup:
        args.turn = 2
        args.end_turn = 2
        args.task = 1
        global MAX_GEMINI_ATTEMPTS
        MAX_GEMINI_ATTEMPTS = 1

    ensure_dirs()

    if args.validate_only:
        validate_only_mode()
        return


    # ── TERMS MODE ────────────────────────────────────────────────────────
    if args.terms:
        progress = load_progress(terms_mode=True)
        start_time = time.time()

        print(f"\n{'═'*70}")
        print(f"  🚀 Pipeline Starting: TERMS MODE")
        print(f"  📂 Input:  {INPUT_TERMS_DIR}")
        print(f"  📂 Output: {OUTPUT_JSON_TERMS_DIR}")
        print(f"  🔄 Max Gemini attempts per task: {MAX_GEMINI_ATTEMPTS}")
        print(f"{'═'*70}")

        completed = progress.get("terms_completed", [])
        if args.resume and len(completed) >= 200:
            print("✅ All 200 terms already completed!")
            return

        process_terms(progress,
                      start_turn=args.turn, start_task=args.task,
                      end_turn=args.end_turn, skip_dashboard=args.no_dashboard,
                      test_setup=args.test_setup, limit_tasks=args.limit_tasks,
                      start_term=args.start_term, limit_terms=args.limit_terms)

        elapsed = time.time() - start_time
        minutes = int(elapsed // 60)
        seconds = elapsed % 60
        print(f"\n{'='*70}")
        print(f"  🏁 Terms Pipeline Complete: {minutes}m {seconds:.0f}s elapsed")
        print(f"{'='*70}")
        return

    # ── PDF MODE (default) ────────────────────────────────────────────────
    progress = load_progress()
    start_time = time.time()

    # Get PDF list
    if args.pdf:
        pdf_path = os.path.join(INPUT_DIR, args.pdf) if not os.path.isabs(args.pdf) else args.pdf
        if not os.path.exists(pdf_path):
            print(f"❌ PDF not found: {pdf_path}")
            sys.exit(1)
        pdf_list = [pdf_path]
    else:
        pdf_list = sorted(glob.glob(os.path.join(INPUT_DIR, "*.pdf")))

    if not pdf_list:
        print("❌ No PDFs found in Input/")
        sys.exit(1)

    print(f"\n{'═'*70}")
    print(f"  🚀 Pipeline Starting: {len(pdf_list)} PDFs to process")
    print(f"  📂 Input:  {INPUT_DIR}")
    print(f"  📂 Output: {OUTPUT_JSON_DIR}")
    print(f"  🔄 Max Gemini attempts per task: {MAX_GEMINI_ATTEMPTS}")
    print(f"{'═'*70}")

    # Filter out already-completed PDFs (unless specific PDF requested)
    if not args.pdf:
        pdf_list = [p for p in pdf_list
                    if os.path.basename(p) not in progress.get("pdfs_completed", [])]
        if not pdf_list:
            print("✅ All PDFs already completed!")
            return

    pdfs_processed = 0
    for pdf_path in pdf_list:
        success = process_pdf(pdf_path, progress,
                   start_turn=args.turn, start_task=args.task,
                   end_turn=args.end_turn, skip_dashboard=args.no_dashboard,
                   test_setup=args.test_setup, limit_tasks=args.limit_tasks,
                   preview_mode=args.preview, strict_thinking=args.strict_thinking)
        
        if success:
            pdfs_processed += 1
        
        # Reset start position after first PDF
        args.turn = 1
        args.task = 1

        if args.limit_pdfs > 0 and pdfs_processed >= args.limit_pdfs:
            print(f"\n  [LIMIT REACHED] Exiting after processing {pdfs_processed} PDFs.")
            break

    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = elapsed % 60
    completed = len(progress.get("pdfs_completed", []))
    print(f"\n{'═'*70}")
    print(f"  🏁 Pipeline Complete: {completed} PDFs, {minutes}m {seconds:.0f}s elapsed")
    print(f"{'═'*70}")


if __name__ == "__main__":
    main()
