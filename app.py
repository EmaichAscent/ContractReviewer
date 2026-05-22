"""Contract Reviewer - Flask web application."""

import json
import os
import uuid
import threading
from datetime import datetime

# Load .env file for local development
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, jsonify
)

import config
from parsing.docx_parser import parse_docx, get_full_text, detect_file_type
from parsing.doc_parser import parse_doc, get_full_text_doc, convert_doc_to_docx
from parsing.pdf_parser import parse_pdf, get_full_text_pdf
from parsing.contract_splitter import split_into_sections
from statutes.jurisdiction_detector import detect_jurisdiction
from statutes.statute_searcher import search_statutes_for_state
from statutes.statute_db import get_statutes_for_state, get_all_states, search_statutes
from analysis.claude_analyzer import analyze_contract
from analysis.scoring_criteria import load_criteria
from output.scorecard_generator import generate_scorecard
from output.scorecard_pdf import generate_scorecard_pdf
from output.executive_summary_pdf import generate_executive_summary_pdf
from output.trackchanges import apply_track_changes
from output.template_revision import generate_client_template_edition
from output.revisions_document import generate_revisions_document

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Job tracking - persisted to disk to survive Flask debug reloads
jobs = {}
JOBS_STATE_FILE = os.path.join(config.RESULTS_FOLDER, "_jobs_state.json")

# On-demand artifact generation tracking. State lives partly on disk (the file
# itself) and partly in this dict (in-flight + error states). Keys: f"{job_id}:{kind}".
generations = {}

ARTIFACT_FILES = {
    "exec-summary": "executive_summary.pdf",
    "marked": "marked_contract.docx",
    "template-edition": "client_template_edition.docx",
    "revisions": "revisions_document.docx",
}


def _persist_job(job_id):
    """Save job state to disk so it survives server restarts."""
    job = jobs.get(job_id)
    if not job:
        return
    state = {
        "id": job.get("id"),
        "client_name": job.get("client_name"),
        "status": job.get("status"),
        "progress": job.get("progress"),
        "status_message": job.get("status_message"),
        "error": job.get("error"),
        "has_results": bool(job.get("results")),
        "primary_docx_filename": job.get("primary_docx_filename"),
    }
    job_state_path = os.path.join(config.RESULTS_FOLDER, job_id, "job_state.json")
    try:
        with open(job_state_path, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def _recover_job(job_id):
    """Try to recover job state from disk after a restart."""
    job_state_path = os.path.join(config.RESULTS_FOLDER, job_id, "job_state.json")
    analysis_path = os.path.join(config.RESULTS_FOLDER, job_id, "analysis.json")

    # If analysis.json exists, the job completed
    if os.path.exists(analysis_path):
        try:
            with open(analysis_path) as f:
                results = json.load(f)
            client_name = "Unknown"
            primary_docx_filename = None
            if os.path.exists(job_state_path):
                with open(job_state_path) as f:
                    state = json.load(f)
                client_name = state.get("client_name", "Unknown")
                primary_docx_filename = state.get("primary_docx_filename")
            # Fall back to the analysis file's mtime if no stored date — keeps
            # old jobs (predating job_state.json) renderable.
            try:
                mtime = os.path.getmtime(analysis_path)
                date_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_str = ""
            jobs[job_id] = {
                "id": job_id,
                "client_name": client_name,
                "status": "complete",
                "progress": 100,
                "status_message": "Review complete!",
                "results": results,
                "primary_docx_filename": primary_docx_filename,
                "date": date_str,
                "jurisdiction": results.get("jurisdiction") if isinstance(results.get("jurisdiction"), dict) else None,
            }
            return jobs[job_id]
        except Exception:
            pass

    # If only job_state exists, return last known state
    if os.path.exists(job_state_path):
        try:
            with open(job_state_path) as f:
                state = json.load(f)
            # If it was processing and we restarted, mark as error
            if state.get("status") == "processing":
                state["status"] = "error"
                state["error"] = "Server restarted during analysis. Please try again."
                state["status_message"] = "Error: Server restarted during analysis. Please try again."
            jobs[job_id] = state
            return jobs[job_id]
        except Exception:
            pass

    return None


@app.route("/")
def index():
    return render_template("upload.html")


@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("contracts")
    if not files or not any(f.filename for f in files):
        flash("Please select at least one file to upload.", "error")
        return redirect(url_for("index"))

    client_name = request.form.get("client_name", "Unknown Client")
    state_override = request.form.get("state", "")

    # Save uploaded files
    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(config.RESULTS_FOLDER, job_id)
    os.makedirs(job_dir, exist_ok=True)

    upload_paths = []
    for file in files:
        if file and file.filename:
            save_path = os.path.join(job_dir, file.filename)
            file.save(save_path)
            upload_paths.append(save_path)

    if not upload_paths:
        flash("No valid files uploaded.", "error")
        return redirect(url_for("index"))

    # Initialize job tracking
    jobs[job_id] = {
        "id": job_id,
        "client_name": client_name,
        "state_override": state_override,
        "upload_paths": upload_paths,
        "status": "processing",
        "progress": 0,
        "status_message": "Starting analysis...",
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "error": None,
        "results": None,
        "jurisdiction": None,
    }

    # Start background analysis
    thread = threading.Thread(target=_run_analysis, args=(job_id,))
    thread.daemon = True
    thread.start()

    return redirect(url_for("status", job_id=job_id))


@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id) or _recover_job(job_id)
    if not job:
        flash("Job not found.", "error")
        return redirect(url_for("index"))
    if job["status"] == "complete":
        return redirect(url_for("results", job_id=job_id))
    return render_template("status.html",
        job_id=job_id,
        client_name=job["client_name"],
        progress=job["progress"],
        status_message=job["status_message"],
    )


@app.route("/status/<job_id>/json")
def status_json(job_id):
    job = jobs.get(job_id) or _recover_job(job_id)
    if not job:
        return jsonify({"status": "error", "error": "Job not found. The server may have restarted."})
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "status_message": job["status_message"],
        "error": job.get("error"),
        "activity_log": job.get("activity_log", []),
    })


@app.route("/results/<job_id>")
def results(job_id):
    job = jobs.get(job_id) or _recover_job(job_id)
    if not job or not job.get("results"):
        flash("Results not available.", "error")
        return redirect(url_for("index"))

    analysis = job["results"]
    overall_score = analysis.get("overall_score", 0)
    overall_pct = round(overall_score * 100)

    score_class = "good" if overall_score >= 0.7 else "warn" if overall_score >= 0.4 else "bad"
    if overall_score >= 0.8:
        score_rating = "Strong Agreement"
    elif overall_score >= 0.6:
        score_rating = "Adequate — Some Improvements Recommended"
    elif overall_score >= 0.4:
        score_rating = "Needs Significant Improvement"
    else:
        score_rating = "Major Revision or Replacement Recommended"

    usage = analysis.get("usage", {})

    return render_template("results.html",
        job_id=job_id,
        client_name=job["client_name"],
        jurisdiction=job.get("jurisdiction"),
        review_date=job["date"],
        categories=analysis.get("categories", {}),
        overall_pct=overall_pct,
        score_class=score_class,
        score_rating=score_rating,
        statute_concerns=analysis.get("statute_concerns", []),
        recommendation=analysis.get("overall_recommendation", ""),
        gap_analysis=analysis.get("gap_analysis", []),
        usage=usage,
        artifact_states={
            kind: _artifact_status(job_id, kind) for kind in ARTIFACT_FILES
        },
        has_primary_docx=bool(job.get("primary_docx_filename")),
    )


def _artifact_status(job_id, kind):
    """Return current status for an on-demand artifact.

    States: 'not_started' | 'generating' | 'ready' | 'error'.
    A file on disk implies 'ready' (handles restarts and old jobs that pre-date
    on-demand generation).
    """
    filename = ARTIFACT_FILES.get(kind)
    if not filename:
        return {"status": "error", "error": "Unknown artifact type"}

    file_path = os.path.join(config.RESULTS_FOLDER, job_id, filename)
    if os.path.exists(file_path):
        return {"status": "ready"}

    state = generations.get(f"{job_id}:{kind}")
    if state:
        return state
    return {"status": "not_started"}


def _set_artifact_state(job_id, kind, status, **extra):
    generations[f"{job_id}:{kind}"] = {"status": status, **extra}


def _generate_exec_summary(job_id):
    job = jobs.get(job_id) or _recover_job(job_id)
    if not job or not job.get("results"):
        _set_artifact_state(job_id, "exec-summary", "error", error="Analysis not available")
        return
    try:
        job_dir = os.path.join(config.RESULTS_FOLDER, job_id)
        out_path = os.path.join(job_dir, "executive_summary.pdf")
        generate_executive_summary_pdf(
            job["results"], job["client_name"], out_path, job.get("jurisdiction")
        )
        _set_artifact_state(job_id, "exec-summary", "ready")
    except Exception as e:
        import traceback
        traceback.print_exc()
        _set_artifact_state(job_id, "exec-summary", "error", error=str(e))


def _generate_marked_contract(job_id):
    job = jobs.get(job_id) or _recover_job(job_id)
    if not job or not job.get("results"):
        _set_artifact_state(job_id, "marked", "error", error="Analysis not available")
        return
    try:
        job_dir = os.path.join(config.RESULTS_FOLDER, job_id)
        primary_filename = job.get("primary_docx_filename")
        if not primary_filename:
            _set_artifact_state(job_id, "marked", "error",
                                error="No .docx contract was uploaded — track changes cannot be applied to PDFs.")
            return
        primary_path = os.path.join(job_dir, primary_filename)
        if not os.path.exists(primary_path):
            _set_artifact_state(job_id, "marked", "error", error="Original contract file not found")
            return
        out_path = os.path.join(job_dir, "marked_contract.docx")
        revisions = job["results"].get("revisions", [])
        if revisions:
            apply_track_changes(primary_path, out_path, revisions)
        else:
            import shutil
            shutil.copy2(primary_path, out_path)
        _set_artifact_state(job_id, "marked", "ready")
    except Exception as e:
        import traceback
        traceback.print_exc()
        _set_artifact_state(job_id, "marked", "error", error=str(e))


def _generate_template_edition(job_id):
    """Build the Client Template Edition — a revised copy of the master template
    with track changes and Word comments sourced from the client contract analysis.
    This runs a second, focused LLM call."""
    job = jobs.get(job_id) or _recover_job(job_id)
    if not job or not job.get("results"):
        _set_artifact_state(job_id, "template-edition", "error", error="Analysis not available")
        return
    try:
        job_dir = os.path.join(config.RESULTS_FOLDER, job_id)
        out_path = os.path.join(job_dir, "client_template_edition.docx")
        settings = _load_settings()
        model = settings.get("model", config.CLAUDE_MODEL)
        generate_client_template_edition(
            template_path=config.IDEAL_TEMPLATE_PATH,
            output_path=out_path,
            client_name=job["client_name"],
            analysis=job["results"],
            jurisdiction=job.get("jurisdiction"),
            model=model,
        )
        _set_artifact_state(job_id, "template-edition", "ready")
    except Exception as e:
        import traceback
        traceback.print_exc()
        _set_artifact_state(job_id, "template-edition", "error", error=str(e))


def _generate_revisions_document(job_id):
    """Build the standalone Suggested Contract Revisions .docx. Works for any
    input format (PDF, .doc, .docx) since it's generated from the analysis JSON
    rather than the source file."""
    job = jobs.get(job_id) or _recover_job(job_id)
    if not job or not job.get("results"):
        _set_artifact_state(job_id, "revisions", "error", error="Analysis not available")
        return
    try:
        job_dir = os.path.join(config.RESULTS_FOLDER, job_id)
        out_path = os.path.join(job_dir, "revisions_document.docx")
        generate_revisions_document(
            job["results"], job["client_name"], out_path, job.get("jurisdiction")
        )
        _set_artifact_state(job_id, "revisions", "ready")
    except Exception as e:
        import traceback
        traceback.print_exc()
        _set_artifact_state(job_id, "revisions", "error", error=str(e))


_ARTIFACT_GENERATORS = {
    "exec-summary": _generate_exec_summary,
    "marked": _generate_marked_contract,
    "template-edition": _generate_template_edition,
    "revisions": _generate_revisions_document,
}


@app.route("/generate/<job_id>/<kind>", methods=["POST"])
def generate_artifact(job_id, kind):
    if kind not in _ARTIFACT_GENERATORS:
        return jsonify({"status": "error", "error": "Unknown artifact type"}), 400

    job = jobs.get(job_id) or _recover_job(job_id)
    if not job:
        return jsonify({"status": "error", "error": "Job not found"}), 404

    current = _artifact_status(job_id, kind)
    if current["status"] in ("ready", "generating"):
        return jsonify(current)

    _set_artifact_state(job_id, kind, "generating")
    thread = threading.Thread(target=_ARTIFACT_GENERATORS[kind], args=(job_id,))
    thread.daemon = True
    thread.start()
    return jsonify({"status": "generating"})


@app.route("/generate/<job_id>/<kind>/status")
def generate_artifact_status(job_id, kind):
    return jsonify(_artifact_status(job_id, kind))


@app.route("/download/<job_id>/<file_type>")
def download(job_id, file_type):
    job_dir = os.path.join(config.RESULTS_FOLDER, job_id)
    client = jobs.get(job_id, {}).get('client_name', 'Client')

    if file_type == "scorecard":
        path = os.path.join(job_dir, "scorecard.docx")
        name = f"Scorecard - {client}.docx"
    elif file_type == "scorecard_pdf":
        path = os.path.join(job_dir, "scorecard.pdf")
        name = f"Scorecard - {client}.pdf"
    elif file_type == "executive_summary":
        path = os.path.join(job_dir, "executive_summary.pdf")
        name = f"Executive Summary - {client}.pdf"
    elif file_type == "marked":
        path = os.path.join(job_dir, "marked_contract.docx")
        name = f"Marked Contract - {client}.docx"
    elif file_type == "template_edition":
        path = os.path.join(job_dir, "client_template_edition.docx")
        name = f"Client Template Edition - {client}.docx"
    elif file_type == "revisions":
        path = os.path.join(job_dir, "revisions_document.docx")
        name = f"Suggested Revisions - {client}.docx"
    else:
        flash("Invalid download type.", "error")
        return redirect(url_for("results", job_id=job_id))

    if not os.path.exists(path):
        flash("File not found.", "error")
        return redirect(url_for("results", job_id=job_id))

    return send_file(path, as_attachment=True, download_name=name)


@app.route("/history")
def history():
    job_list = []
    for job_id, job in sorted(jobs.items(), key=lambda x: x[1]["date"], reverse=True):
        if job["status"] == "complete":
            job_list.append({
                "id": job_id,
                "date": job["date"],
                "client_name": job["client_name"],
                "state": job.get("jurisdiction", {}).get("state_abbrev") if job.get("jurisdiction") else None,
                "overall_score": round(job["results"].get("overall_score", 0) * 100) if job.get("results") else 0,
            })
    return render_template("history.html", jobs=job_list)


@app.route("/statutes")
def statutes_view():
    selected_state = request.args.get("state")
    states = get_all_states()

    if selected_state:
        statute_list = get_statutes_for_state(selected_state, max_age_days=3650)
    else:
        statute_list = []
        for state in states:
            statute_list.extend(get_statutes_for_state(state, max_age_days=3650))

    return render_template("statutes_view.html",
        states=states,
        statutes=statute_list,
        selected_state=selected_state,
    )


@app.route("/admin")
def admin():
    prompts = _load_prompts()
    criteria = load_criteria()
    settings = _load_settings()
    reference_contracts = _list_reference_contracts()
    template_name = settings.get("ideal_template_name", os.path.basename(config.IDEAL_TEMPLATE_PATH)) if os.path.exists(config.IDEAL_TEMPLATE_PATH) else "Not uploaded"
    template_size = ""
    if os.path.exists(config.IDEAL_TEMPLATE_PATH):
        sz = os.path.getsize(config.IDEAL_TEMPLATE_PATH)
        template_size = f"{sz / 1024:.0f} KB" if sz < 1048576 else f"{sz / 1048576:.1f} MB"
    return render_template("admin.html",
        prompts=prompts,
        criteria=criteria,
        models=config.AVAILABLE_MODELS,
        current_model=settings.get("model", config.CLAUDE_MODEL),
        reference_contracts=reference_contracts,
        template_name=template_name,
        template_size=template_size,
    )


@app.route("/admin/model", methods=["POST"])
def admin_model():
    model = request.form.get("model", config.CLAUDE_MODEL)
    if model in config.AVAILABLE_MODELS:
        settings = _load_settings()
        settings["model"] = model
        _save_settings(settings)
        flash(f"Model switched to {config.AVAILABLE_MODELS[model]}", "success")
    return redirect(url_for("admin"))


@app.route("/admin/save", methods=["POST"])
def admin_save():
    prompts = {
        "system_prompt": request.form.get("system_prompt", ""),
        "analysis_prompt": request.form.get("analysis_prompt", ""),
        "statute_search_prompt": request.form.get("statute_search_prompt", ""),
        "revision_prompt": request.form.get("revision_prompt", ""),
        "template_revision_system_prompt": request.form.get("template_revision_system_prompt", ""),
        "template_revision_prompt": request.form.get("template_revision_prompt", ""),
    }
    os.makedirs(os.path.dirname(config.ADMIN_PROMPTS_PATH), exist_ok=True)
    with open(config.ADMIN_PROMPTS_PATH, "w") as f:
        json.dump(prompts, f, indent=2)
    flash("Prompts saved successfully.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/reset")
def admin_reset():
    default_prompts_path = os.path.join(os.path.dirname(__file__), "data", "prompts_default.json")
    if os.path.exists(default_prompts_path):
        import shutil
        shutil.copy2(default_prompts_path, config.ADMIN_PROMPTS_PATH)
    flash("Prompts reset to defaults.", "success")
    return redirect(url_for("admin"))


# --- Criteria Management ---

@app.route("/admin/criteria/weight", methods=["POST"])
def admin_criteria_weight():
    category = request.form.get("category")
    weight = float(request.form.get("weight", 0))
    criteria = load_criteria()
    if category in criteria["categories"]:
        criteria["categories"][category]["weight"] = weight
        _save_criteria(criteria)
        flash(f"Weight for {category} updated to {weight}.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/criteria/add", methods=["POST"])
def admin_criteria_add():
    category = request.form.get("category")
    criteria = load_criteria()
    if category in criteria["categories"]:
        new_crit = {
            "id": request.form.get("id", "").strip(),
            "name": request.form.get("name", "").strip(),
            "description": request.form.get("description", "").strip(),
            "ideal": request.form.get("ideal", "").strip(),
        }
        if new_crit["id"] and new_crit["name"]:
            criteria["categories"][category]["criteria"].append(new_crit)
            _save_criteria(criteria)
            flash(f"Added criterion '{new_crit['name']}' to {category}.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/criteria/delete", methods=["POST"])
def admin_criteria_delete():
    category = request.form.get("category")
    criterion_id = request.form.get("criterion_id")
    criteria = load_criteria()
    if category in criteria["categories"]:
        crit_list = criteria["categories"][category]["criteria"]
        criteria["categories"][category]["criteria"] = [
            c for c in crit_list if c["id"] != criterion_id
        ]
        _save_criteria(criteria)
        flash(f"Removed criterion from {category}.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/criteria/edit/<category>/<criterion_id>")
def admin_criteria_edit(category, criterion_id):
    criteria = load_criteria()
    if category in criteria["categories"]:
        for crit in criteria["categories"][category]["criteria"]:
            if crit["id"] == criterion_id:
                return render_template("criterion_edit.html",
                    category_name=category,
                    criterion=crit,
                )
    flash("Criterion not found.", "error")
    return redirect(url_for("admin"))


@app.route("/admin/criteria/update", methods=["POST"])
def admin_criteria_update():
    category = request.form.get("category")
    original_id = request.form.get("original_id")
    criteria = load_criteria()
    if category in criteria["categories"]:
        for i, crit in enumerate(criteria["categories"][category]["criteria"]):
            if crit["id"] == original_id:
                criteria["categories"][category]["criteria"][i] = {
                    "id": request.form.get("id", "").strip(),
                    "name": request.form.get("name", "").strip(),
                    "description": request.form.get("description", "").strip(),
                    "ideal": request.form.get("ideal", "").strip(),
                }
                _save_criteria(criteria)
                flash(f"Updated criterion '{request.form.get('name')}'.", "success")
                break
    return redirect(url_for("admin"))


@app.route("/admin/criteria/add-category", methods=["POST"])
def admin_criteria_add_category():
    name = request.form.get("name", "").strip()
    weight = float(request.form.get("weight", 0.25))
    description = request.form.get("description", "").strip()
    if name:
        criteria = load_criteria()
        criteria["categories"][name] = {
            "weight": weight,
            "description": description,
            "criteria": [],
        }
        _save_criteria(criteria)
        flash(f"Added category '{name}'.", "success")
    return redirect(url_for("admin"))


# --- Reference Contract Management ---

@app.route("/admin/reference/upload", methods=["POST"])
def admin_reference_upload():
    file = request.files.get("reference")
    if file and file.filename:
        os.makedirs(config.REFERENCE_CONTRACTS_FOLDER, exist_ok=True)
        save_path = os.path.join(config.REFERENCE_CONTRACTS_FOLDER, file.filename)
        file.save(save_path)
        description = request.form.get("description", "")
        # Save metadata
        meta_path = save_path + ".meta"
        with open(meta_path, "w") as f:
            json.dump({"description": description}, f)
        flash(f"Reference contract '{file.filename}' uploaded.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/reference/delete", methods=["POST"])
def admin_reference_delete():
    filename = request.form.get("filename")
    if filename:
        path = os.path.join(config.REFERENCE_CONTRACTS_FOLDER, filename)
        if os.path.exists(path):
            os.remove(path)
            meta_path = path + ".meta"
            if os.path.exists(meta_path):
                os.remove(meta_path)
            flash(f"Removed '{filename}'.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/template/replace", methods=["POST"])
def admin_template_replace():
    file = request.files.get("template")
    if file and file.filename:
        import shutil
        # Backup existing template
        if os.path.exists(config.IDEAL_TEMPLATE_PATH):
            backup_path = config.IDEAL_TEMPLATE_PATH + ".backup"
            shutil.copy2(config.IDEAL_TEMPLATE_PATH, backup_path)
        file.save(config.IDEAL_TEMPLATE_PATH)
        # Save the original filename for display
        settings = _load_settings()
        settings["ideal_template_name"] = file.filename
        _save_settings(settings)
        flash("Ideal template replaced. Previous version backed up.", "success")
    return redirect(url_for("admin"))


def _run_analysis(job_id):
    """Background analysis worker."""
    job = jobs[job_id]
    try:
        upload_paths = job.get("upload_paths", [])
        if not upload_paths:
            raise ValueError("No files to analyze")

        # Progress bands — chosen so the bar reflects actual wall time:
        #   0-12%   parsing + section split (seconds)
        #   12-15%  jurisdiction detection
        #   15-20%  statute lookup + template load
        #   20-75%  LLM analysis (this is the slow phase, streamed live)
        #   75-85%  revisions LLM call (streamed live)
        #   85-100% scorecard + pdf generation
        _update_job(job_id, 3, f"Parsing {len(upload_paths)} document(s)...")
        all_text_parts = []
        all_paragraphs = []
        primary_docx_path = None  # For track changes markup
        pdf_attachments = []  # PDFs sent to Claude as multimodal documents

        parse_errors = []
        for i, upload_path in enumerate(upload_paths):
            fname = os.path.basename(upload_path)
            _update_job(job_id, 3 + (8 * i // max(1, len(upload_paths))),
                        f"Parsing document {i + 1}/{len(upload_paths)}: {fname}")

            try:
                file_type = detect_file_type(upload_path)

                if file_type == "pdf":
                    text = get_full_text_pdf(upload_path)
                    paragraphs = parse_pdf(upload_path)
                    # Always send PDFs as multimodal documents — pdfplumber
                    # silently returns empty text on scanned/OCR'd PDFs, which
                    # would make the LLM think no contract was provided.
                    pdf_attachments.append(upload_path)
                    if len(text.strip()) < 200:
                        _log_activity(job_id, f"WARNING: pdfplumber extracted only {len(text.strip())} chars from {fname} — using direct PDF analysis as the source of truth")
                elif file_type == "doc":
                    text = get_full_text_doc(upload_path)
                    paragraphs = parse_doc(upload_path)
                    if not primary_docx_path:
                        try:
                            primary_docx_path = convert_doc_to_docx(upload_path)
                        except Exception:
                            pass
                elif file_type == "docx":
                    text = get_full_text(upload_path)
                    paragraphs = parse_docx(upload_path)
                    if not primary_docx_path:
                        primary_docx_path = upload_path
                else:
                    try:
                        text = get_full_text(upload_path)
                        paragraphs = parse_docx(upload_path)
                        if not primary_docx_path:
                            primary_docx_path = upload_path
                    except Exception:
                        try:
                            text = get_full_text_doc(upload_path)
                            paragraphs = parse_doc(upload_path)
                        except Exception:
                            text = get_full_text_pdf(upload_path)
                            paragraphs = parse_pdf(upload_path)

                word_count = len(text.split())
                _log_activity(job_id, f"Parsed {fname} ({file_type}) — {word_count:,} words, {len(paragraphs)} paragraphs")
                if len(upload_paths) > 1:
                    all_text_parts.append(f"\n=== DOCUMENT: {fname} ===\n{text}")
                else:
                    all_text_parts.append(text)
                all_paragraphs.extend(paragraphs)

            except Exception as e:
                import traceback
                traceback.print_exc()
                error_msg = _friendly_parse_error(fname, file_type if 'file_type' in dir() else "unknown", e)
                parse_errors.append(error_msg)
                print(f"Parse error for {fname}: {e}")

        if not all_text_parts:
            error_detail = "\n".join(parse_errors) if parse_errors else "Unknown parsing error"
            raise ValueError(f"Could not read any of the uploaded files.\n{error_detail}")

        if parse_errors:
            _update_job(job_id, 11,
                        f"Parsed {len(all_text_parts)}/{len(upload_paths)} document(s). "
                        f"Skipped: {'; '.join(parse_errors)}")
        else:
            _update_job(job_id, 11, f"Parsed {len(upload_paths)} document(s) successfully.")

        contract_text = "\n".join(all_text_parts)

        # Step 2: Split into sections
        sections = split_into_sections(all_paragraphs)
        _update_job(job_id, 12, f"Identified {len(sections)} contract sections.")

        # Step 3: Detect jurisdiction
        _update_job(job_id, 13, "Detecting jurisdiction...")
        if job["state_override"]:
            from statutes.jurisdiction_detector import US_STATES
            state_name = None
            for name, abbrev in US_STATES.items():
                if abbrev == job["state_override"]:
                    state_name = name.title()
                    break
            jurisdiction = {
                "state": state_name or job["state_override"],
                "state_abbrev": job["state_override"],
                "statutes_mentioned": [],
                "confidence": "manual",
            }
        else:
            jurisdiction = detect_jurisdiction(contract_text)

        job["jurisdiction"] = jurisdiction
        state_info = f"{jurisdiction['state']} ({jurisdiction['state_abbrev']})" if jurisdiction.get("state") else "Unknown"
        if jurisdiction.get("statutes_mentioned"):
            _log_activity(job_id, f"Found {len(jurisdiction['statutes_mentioned'])} statute references in contract text")
        _update_job(job_id, 15, f"Jurisdiction: {state_info}")

        # Step 4: Search statutes
        _update_job(job_id, 16, "Searching for relevant statutes...")
        statutes_context = ""
        if jurisdiction.get("state_abbrev"):
            statutes = search_statutes_for_state(
                jurisdiction["state_abbrev"], contract_text
            )
            if statutes:
                statutes_context = "\n".join(
                    f"- {s.get('statute_number', 'N/A')}: {s.get('title', '')}\n  {s.get('summary', '')}"
                    for s in statutes
                )
                _update_job(job_id, 18, f"Found {len(statutes)} relevant statutes.")
            else:
                _update_job(job_id, 18, "No cached statutes found; proceeding with analysis.")
        else:
            _update_job(job_id, 18, "No jurisdiction detected; skipping statute search.")

        # Step 5: Load ideal template and reference contracts
        _update_job(job_id, 19, "Loading ideal template for comparison...")
        ideal_template_text = get_full_text(config.IDEAL_TEMPLATE_PATH)
        reference_texts = _get_reference_texts()
        if reference_texts:
            ideal_template_text += "\n\n## ADDITIONAL REFERENCE CONTRACTS\n" + reference_texts

        # Step 6: AI Analysis (using selected model)
        settings = _load_settings()
        selected_model = settings.get("model", config.CLAUDE_MODEL)
        _update_job(job_id, 20, f"Starting AI analysis with {selected_model}...")

        # Streaming progress: the analyzer streams text chunks back. We translate
        # bytes-received → percent within the band so the bar moves smoothly
        # instead of sitting at one value for 2-5 minutes.
        def _stream_progress(phase, chars_so_far):
            state = jurisdiction.get("state") if isinstance(jurisdiction, dict) else None
            msg = _llm_narrative_msg(phase, chars_so_far, state)
            if phase == "analysis":
                pct = 20 + int(55 * min(1.0, chars_so_far / _EXPECTED_ANALYSIS_CHARS))
            else:  # revisions
                pct = 75 + int(10 * min(1.0, chars_so_far / _EXPECTED_REVISION_CHARS))
            _update_job_progress(job_id, pct, msg)

        analysis = analyze_contract(
            contract_text, ideal_template_text,
            statutes_context=statutes_context,
            jurisdiction=jurisdiction,
            model=selected_model,
            log_fn=lambda msg: _log_activity(job_id, msg),
            pdf_attachments=pdf_attachments,
            progress_fn=_stream_progress,
        )

        # If text-based jurisdiction detection failed but the LLM identified a
        # state from the attached PDF, adopt the LLM's value so the results
        # page and statute compliance can use it on follow-up runs.
        llm_juris = analysis.get("jurisdiction") if isinstance(analysis.get("jurisdiction"), dict) else None
        if llm_juris and llm_juris.get("state") and (not jurisdiction.get("state") or jurisdiction.get("state") in ("Unknown", "")):
            from statutes.jurisdiction_detector import US_STATES
            state_name = (llm_juris.get("state") or "").strip()
            abbrev = ""
            for name, ab in US_STATES.items():
                if name.lower() == state_name.lower() or ab == state_name.upper():
                    abbrev = ab
                    state_name = name.title()
                    break
            if state_name:
                jurisdiction = {
                    "state": state_name,
                    "state_abbrev": abbrev,
                    "statutes_mentioned": llm_juris.get("statutes_referenced", []),
                    "confidence": "llm_extracted",
                }
                job["jurisdiction"] = jurisdiction
                _log_activity(job_id, f"Jurisdiction recovered from PDF analysis: {state_name} ({abbrev})")
        usage = analysis.get("usage", {})
        if usage:
            total_in = usage.get("total_input_tokens", 0)
            total_out = usage.get("total_output_tokens", 0)
            cost = usage.get("estimated_cost", 0)
            _log_activity(job_id, f"API usage: {total_in:,} input + {total_out:,} output tokens — estimated cost: ${cost:.4f}")
        num_revisions = len(analysis.get("revisions", []))
        _log_activity(job_id, f"Generated {num_revisions} suggested revisions for track changes")
        _update_job(job_id, 88, "Analysis complete. Generating scorecard...")

        # Step 7: Generate scorecard (docx + pdf) — the only auto-generated artifact.
        # Executive summary, marked-up contract, client template edition, and the
        # standalone revisions document are generated on demand from the results page.
        job_dir = os.path.join(config.RESULTS_FOLDER, job_id)
        scorecard_path = os.path.join(job_dir, "scorecard.docx")
        generate_scorecard(analysis, job["client_name"], scorecard_path, jurisdiction)
        scorecard_pdf_path = os.path.join(job_dir, "scorecard.pdf")
        try:
            generate_scorecard_pdf(analysis, job["client_name"], scorecard_pdf_path, jurisdiction)
        except Exception as e:
            print(f"PDF generation error: {e}")

        # Remember which uploaded file is the primary .docx so on-demand artifact
        # generation (marked contract, template edition) can find it later.
        if primary_docx_path:
            job["primary_docx_filename"] = os.path.basename(primary_docx_path)

        # Save analysis results
        results_path = os.path.join(job_dir, "analysis.json")
        with open(results_path, "w") as f:
            json.dump(analysis, f, indent=2, default=str)

        job["results"] = analysis
        _update_job(job_id, 100, "Review complete!")
        job["status"] = "complete"
        _persist_job(job_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        job["status"] = "error"
        friendly = _friendly_error(e)
        job["error"] = friendly
        job["status_message"] = f"Error: {friendly}"
        _persist_job(job_id)


def _friendly_parse_error(filename, file_type, error):
    """Convert a parsing exception into a user-friendly message."""
    err_str = str(error).lower()
    if "open.paragraphs" in err_str or "documents.open" in err_str:
        return (f'"{filename}" could not be opened by Word. '
                f"Make sure it is a valid Word document (.doc or .docx).")
    if "no such file" in err_str or "not found" in err_str:
        return f'"{filename}" was not found on disk after upload.'
    if "password" in err_str or "encrypt" in err_str:
        return f'"{filename}" appears to be password-protected.'
    if "corrupt" in err_str or "bad zip" in err_str or "not a zip" in err_str:
        return f'"{filename}" appears to be corrupted or is not a valid document.'
    return f'"{filename}" could not be parsed ({type(error).__name__}: {error})'


def _friendly_error(error):
    """Convert any analysis exception into a user-friendly message."""
    err_str = str(error)
    err_lower = err_str.lower()
    if "open.paragraphs" in err_lower or "documents.open" in err_lower:
        return ("One of the uploaded files could not be opened by Word. "
                "Please ensure all files are valid .doc, .docx, or .pdf files.")
    if "api" in err_lower and ("key" in err_lower or "auth" in err_lower):
        return "API authentication failed. Please check the Anthropic API key in config."
    if "rate" in err_lower and "limit" in err_lower:
        return "API rate limit reached. Please wait a minute and try again."
    if "timeout" in err_lower or "timed out" in err_lower:
        return "The analysis timed out. The contract may be too long — try uploading fewer files."
    if "could not read any" in err_lower:
        return err_str
    return f"An error occurred during analysis: {err_str}"


def _log_activity(job_id, message):
    """Append a timestamped entry to the job's activity log."""
    job = jobs.get(job_id)
    if not job:
        return
    if "activity_log" not in job:
        job["activity_log"] = []
    timestamp = datetime.now().strftime("%H:%M:%S")
    job["activity_log"].append(f"[{timestamp}] {message}")


def _update_job(job_id, progress, message):
    """Update job progress."""
    job = jobs.get(job_id)
    if job:
        job["progress"] = progress
        job["status_message"] = message
        _log_activity(job_id, message)
        _persist_job(job_id)


def _update_job_progress(job_id, progress, message):
    """Update progress without spamming the activity log.

    Used during streaming, when the bar moves frequently but the displayed
    status message only changes when the narrative phase shifts. Logs to the
    activity log only when the message actually changes.
    """
    job = jobs.get(job_id)
    if not job:
        return
    job["progress"] = progress
    if message and message != job.get("status_message"):
        job["status_message"] = message
        _log_activity(job_id, message)


# Expected output sizes for narrative + progress math. These are rough averages
# observed in practice; they only affect pacing of the progress bar, not the
# actual analysis. Streaming progress is capped so it can't overrun the band.
_EXPECTED_ANALYSIS_CHARS = 18000
_EXPECTED_REVISION_CHARS = 5000


def _llm_narrative_msg(phase, chars, jurisdiction_state=None):
    """Build a phase-aware narrative status message based on streamed output.

    Returned strings show the user *what Claude is doing right now* instead of
    a single frozen "Running AI analysis..." that sits for minutes.
    """
    if phase == "revisions":
        return f"Drafting specific text revisions for weak areas ({chars:,} chars generated)..."

    expected = _EXPECTED_ANALYSIS_CHARS
    pct = chars / expected if expected else 0
    state = jurisdiction_state if jurisdiction_state and jurisdiction_state.lower() != "unknown" else None

    if pct < 0.15:
        return "Claude is reading the contract and comparing against the master template..."
    if pct < 0.35:
        return "Scoring profitability and empowerment clauses..."
    if pct < 0.55:
        if state:
            return f"Reviewing risk transference and {state} statute compliance..."
        return "Reviewing risk transference and statute compliance..."
    if pct < 0.75:
        return "Drafting suggested revisions and assessing manager protection gaps..."
    return f"Finalizing the analysis ({chars:,} characters generated)..."


def _load_prompts():
    """Load prompts from file, with packaged defaults as a fallback so the admin
    UI shows the effective values even when prompts.json predates new keys."""
    from analysis.claude_analyzer import load_prompts as _load_merged
    merged = _load_merged()
    for k in ("system_prompt", "analysis_prompt", "statute_search_prompt",
              "revision_prompt", "template_revision_system_prompt",
              "template_revision_prompt"):
        merged.setdefault(k, "")
    return merged


def _load_settings():
    """Load app settings. Falls back to the default model when the saved value
    is a retired model that's no longer in AVAILABLE_MODELS (e.g. a stale
    selection persisted on the volume from before a model upgrade)."""
    settings_path = os.path.join(config.DATA_FOLDER, "settings.json")
    try:
        with open(settings_path, "r") as f:
            settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {}
    if settings.get("model") not in config.AVAILABLE_MODELS:
        settings["model"] = config.CLAUDE_MODEL
    return settings


def _save_settings(settings):
    """Save app settings."""
    settings_path = os.path.join(config.DATA_FOLDER, "settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)


def _save_criteria(criteria):
    """Save scoring criteria to JSON file."""
    path = os.path.join(config.DATA_FOLDER, "scoring_criteria.json")
    with open(path, "w") as f:
        json.dump(criteria, f, indent=2)


def _list_reference_contracts():
    """List uploaded reference contracts."""
    folder = config.REFERENCE_CONTRACTS_FOLDER
    if not os.path.exists(folder):
        return []
    refs = []
    for fname in os.listdir(folder):
        if fname.endswith(".meta"):
            continue
        fpath = os.path.join(folder, fname)
        size = os.path.getsize(fpath)
        size_str = f"{size / 1024:.0f} KB" if size < 1048576 else f"{size / 1048576:.1f} MB"
        refs.append({"name": fname, "size": size_str, "path": fpath})
    return refs


def _get_reference_texts():
    """Get text content from all reference contracts for analysis context."""
    refs = _list_reference_contracts()
    texts = []
    for ref in refs:
        try:
            file_type = detect_file_type(ref["path"])
            if file_type == "docx":
                text = get_full_text(ref["path"])
            elif file_type == "doc":
                text = get_full_text_doc(ref["path"])
            else:
                text = get_full_text(ref["path"])
            texts.append(f"--- Reference: {ref['name']} ---\n{text[:5000]}")
        except Exception:
            pass
    return "\n\n".join(texts)


if __name__ == "__main__":
    os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(config.RESULTS_FOLDER, exist_ok=True)
    os.makedirs(config.DATA_FOLDER, exist_ok=True)
    os.makedirs(config.REFERENCE_CONTRACTS_FOLDER, exist_ok=True)
    app.run(debug=True, port=5000)
