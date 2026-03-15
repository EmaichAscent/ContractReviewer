import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"  # default; changeable in admin UI
AVAILABLE_MODELS = {
    "claude-sonnet-4-6": "Sonnet 4.6 (Fast, $0.10-0.30/review)",
    "claude-opus-4-6": "Opus 4.6 (Best quality, $0.50-1.50/review)",
}

REFERENCE_CONTRACTS_FOLDER = os.path.join(BASE_DIR, "data", "reference_contracts")
MAX_TOKENS = 16384

UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
RESULTS_FOLDER = os.path.join(BASE_DIR, "results")
DATA_FOLDER = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_FOLDER, "statutes.db")

# Look for ideal template in data/ first (for deployment), then Samples/ (local dev)
_ideal_in_data = os.path.join(BASE_DIR, "data", "ideal_template.docx")
_ideal_in_samples = os.path.join(BASE_DIR, "Samples", "Management_Agreement_Template.docx")
IDEAL_TEMPLATE_PATH = _ideal_in_data if os.path.exists(_ideal_in_data) else _ideal_in_samples

SCORECARD_TEMPLATE_PATH = os.path.join(
    BASE_DIR, "Samples", "Contract Review Scorecard_V2.docx"
)

ADMIN_PROMPTS_PATH = os.path.join(DATA_FOLDER, "prompts.json")

SECRET_KEY = os.environ.get("SECRET_KEY", "contract-reviewer-dev-key")
