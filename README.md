# Agentic Workflow: Autonomous Expert Review Data Generation

This repository contains an autonomous pipeline driving a Playwright agent connected to Gemini. It performs high-fidelity strategic and technical **Expert Reviews** mapped intelligently from user-provided source PDF documents. 

The core output is synthetically generated JSON tasks representing an Expert Review artifact audit. Each generation spans roughly 10,000+ output characters containing rich audit scenarios, mathematical derivations, strict `findings` array structures (15+ elements), and natively reconstructed flawed-to-corrected >1000-word engineering artifacts.

## Overview
1. **Input**: A source PDF (either Technical or Regulatory).
2. **Analysis**: The orchestrator assigns up to 16 distinct role/strategy variations to each PDF (e.g., Code Reviewer vs Cybersecurity Auditor).
3. **Execution (`run_gemini_playwright_v2.py`)**: Submits deep-prompts to Gemini using Playwright-driven UI automation.
4. **Validation (`validate_task.py`)**: Runs strict schema checking ensuring absolute semantic density, >15 audit findings, and valid artifact lengths.
5. **Auto-Repair (`auto_repair.py`)**: Gracefully applies structured local-patching if minor keys fail. Returns bad outputs back to the Playwright driver for regeneration.
6. **Rendering**: Auto-compiles human-readable HTML visualizations to visually audit the structured payloads (`--preview`).

## System Requirements & Installation

If deploying this pipeline to a completely new machine, follow these steps:

### 1. Prerequisites
- **Python 3.10+**
- (Optional but Recommended) A Python virtual environment (`venv` or `conda`).

### 2. Install Dependencies
Install the required python packages. The primary dependency is Microsoft Playwright.
```powershell
pip install playwright markdown json-repair

# Install the Playwright browser binaries (Required!)
playwright install chromium
```

### 3. Setup Gemini Credentials / Profile
This script simulates human usage against the Gemini web UI utilizing Playwright. You must initiate a login session for the bot:
1. Make sure your `.playwright_profile` directory is located in the root (copied over, or generated natively).
2. To regenerate or manually authenticate, temporarily run standard Playwright with `headless=False` on the target machine and log into your Google account. The token state is saved automatically.

### 4. Folder Structure Preparation
Ensure all input PDFs that you wish the agent to audit are copied inside the `Input/` folder.
*If the folder does not exist, running the pipeline will generate the directories automatically.*

* `Input/`: Place `.pdf` files here.
* `Prompts/`: Holds text files actively fed to Gemini.
* `Output/json/`: Final destination for successful validated generation JSONs.
* `Output/thinking/`: Stores raw extracted CoT traces.
* `Eval/Previews/`: The HTML rendered visual previews.
* `Raw_output/`: Playwright diagnostic and fail dumps.

## Execution Guidelines

Run the master orchestrator to sequentially process all PDFs in the `Input` folder:

```powershell
# Basic run: Processes all PDFs, applying 16 variations to each.
python pipeline.py

# Safe-resume mode: Resumes from the last completed Turn/Task automatically.
python pipeline.py --resume

# Specific document targeting
python pipeline.py --pdf "Specific_File.pdf"

# Preview mode: Automatically open browser preview of exactly what was generated.
python pipeline.py --preview

# Test setup mode: Run a single task iteration as a dry-run
python pipeline.py --test-setup
```

## Variation Matrix Schema
The agent automatically adapts tone and strategy dynamically based on the topic.
* **Technical Variation Profiles:** Safety Auditor, QA Code Reviewer, Systems Architect, CyberSec Lead. Focuses on logic benchmarking, hardware critique, and SOTIF modeling.
* **Regulatory Variation Profiles:** Certification Authority, Legal Counsel, Compliance Auditor. Focuses on artifact compliance, liability mapping, and legal loophole identification.

## Quality Gates
The output is subjected to strict anti-repetition, keyword-salad (word spam) filtering, and explicit array bounding (min. 15 technical findings in each JSON). Generations failing this gate are auto-scheduled for full architectural re-prompting.
