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
from output.trackchanges import apply_track_changes

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Job tracking - persisted to disk to survive Flask debug reloads
jobs = {}
JOBS_STATE_FILE = os.path.join(config.RESULTS_FOLDER, "_jobs_state.json")


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
            if os.path.exists(job_state_path):
                with open(job_state_path) as f:
                    state = json.load(f)
                client_name = state.get("client_name", "Unknown")
            jobs[job_id] = {
                "id": job_id,
                "client_name": client_name,
                "status": "complete",
                "progress": 100,
                "status_message": "Review complete!",
                "results": results,
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
        usage=usage,
    )


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
    elif file_type == "marked":
        path = os.path.join(job_dir, "marked_contract.docx")
        name = f"Marked Contract - {client}.docx"
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
    return render_template("admin.html",
        prompts=prompts,
        criteria=criteria,
        models=config.AVAILABLE_MODELS,
        current_model=settings.get("model", config.CLAUDE_MODEL),
        reference_contracts=reference_contracts,
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
        flash("Ideal template replaced. Previous version backed up.", "success")
    return redirect(url_for("admin"))


def _run_analysis(job_id):
    """Background analysis worker."""
    job = jobs[job_id]
    try:
        upload_paths = job.get("upload_paths", [])
        if not upload_paths:
            raise ValueError("No files to analyze")

        # Step 1: Parse all uploaded documents
        _update_job(job_id, 5, f"Parsing {len(upload_paths)} document(s)...")
        all_text_parts = []
        all_paragraphs = []
        primary_docx_path = None  # For track changes markup

        parse_errors = []
        for i, upload_path in enumerate(upload_paths):
            fname = os.path.basename(upload_path)
            _update_job(job_id, 5 + (15 * i // len(upload_paths)),
                        f"Parsing document {i + 1}/{len(upload_paths)}: {fname}")

            try:
                file_type = detect_file_type(upload_path)

                if file_type == "pdf":
                    text = get_full_text_pdf(upload_path)
                    paragraphs = parse_pdf(upload_path)
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
            _update_job(job_id, 20,
                        f"Parsed {len(all_text_parts)}/{len(upload_paths)} document(s). "
                        f"Skipped: {'; '.join(parse_errors)}")
        else:
            _update_job(job_id, 20, f"Parsed {len(upload_paths)} document(s) successfully.")

        contract_text = "\n".join(all_text_parts)

        # Step 2: Split into sections
        sections = split_into_sections(all_paragraphs)
        _update_job(job_id, 25, f"Identified {len(sections)} contract sections.")

        # Step 3: Detect jurisdiction
        _update_job(job_id, 30, "Detecting jurisdiction...")
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
        _update_job(job_id, 40, f"Jurisdiction: {state_info}")

        # Step 4: Search statutes
        _update_job(job_id, 45, "Searching for relevant statutes...")
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
                _update_job(job_id, 55, f"Found {len(statutes)} relevant statutes.")
            else:
                _update_job(job_id, 55, "No cached statutes found; proceeding with analysis.")
        else:
            _update_job(job_id, 55, "No jurisdiction detected; skipping statute search.")

        # Step 5: Load ideal template and reference contracts
        _update_job(job_id, 60, "Loading ideal template for comparison...")
        ideal_template_text = get_full_text(config.IDEAL_TEMPLATE_PATH)
        reference_texts = _get_reference_texts()
        if reference_texts:
            ideal_template_text += "\n\n## ADDITIONAL REFERENCE CONTRACTS\n" + reference_texts

        # Step 6: AI Analysis (using selected model)
        settings = _load_settings()
        selected_model = settings.get("model", config.CLAUDE_MODEL)
        _update_job(job_id, 65, f"Running AI analysis with {selected_model}...")
        analysis = analyze_contract(
            contract_text, ideal_template_text,
            statutes_context=statutes_context,
            jurisdiction=jurisdiction,
            model=selected_model,
        )
        usage = analysis.get("usage", {})
        if usage:
            total_in = usage.get("total_input_tokens", 0)
            total_out = usage.get("total_output_tokens", 0)
            cost = usage.get("estimated_cost", 0)
            _log_activity(job_id, f"API usage: {total_in:,} input + {total_out:,} output tokens — estimated cost: ${cost:.4f}")
        num_revisions = len(analysis.get("revisions", []))
        _log_activity(job_id, f"Generated {num_revisions} suggested revisions for track changes")
        _update_job(job_id, 80, "Analysis complete. Generating reports...")

        # Step 7: Generate scorecard (docx + pdf)
        _update_job(job_id, 85, "Generating scorecard...")
        job_dir = os.path.join(config.RESULTS_FOLDER, job_id)
        scorecard_path = os.path.join(job_dir, "scorecard.docx")
        generate_scorecard(analysis, job["client_name"], scorecard_path, jurisdiction)
        scorecard_pdf_path = os.path.join(job_dir, "scorecard.pdf")
        try:
            generate_scorecard_pdf(analysis, job["client_name"], scorecard_pdf_path, jurisdiction)
        except Exception as e:
            print(f"PDF generation error: {e}")

        # Step 8: Generate marked-up contract
        _update_job(job_id, 90, "Generating marked-up contract...")
        marked_path = os.path.join(job_dir, "marked_contract.docx")
        revisions = analysis.get("revisions", [])
        if revisions and primary_docx_path:
            _log_activity(job_id, f"Applying {len(revisions)} track changes to contract...")
            try:
                apply_track_changes(primary_docx_path, marked_path, revisions)
                _log_activity(job_id, "Track changes applied successfully")
            except Exception as e:
                print(f"Track changes error: {e}")
                import traceback
                traceback.print_exc()
                import shutil
                shutil.copy2(primary_docx_path, marked_path)
        elif primary_docx_path:
            import shutil
            shutil.copy2(primary_docx_path, marked_path)

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


def _load_prompts():
    """Load prompts from file."""
    try:
        with open(config.ADMIN_PROMPTS_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "system_prompt": "",
            "analysis_prompt": "",
            "statute_search_prompt": "",
            "revision_prompt": "",
        }


def _load_settings():
    """Load app settings."""
    settings_path = os.path.join(config.DATA_FOLDER, "settings.json")
    try:
        with open(settings_path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"model": config.CLAUDE_MODEL}


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
