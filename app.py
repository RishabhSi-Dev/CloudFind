import os
from pathlib import Path
from uuid import uuid4

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY", "local-dev-only-change-before-deploy"
)
app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"sqlite:///{BASE_DIR / 'cloudfind.db'}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
db = SQLAlchemy(app)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
CATEGORIES = ["Electronics", "Bags", "Clothing", "Books", "Accessories", "Other"]

class LostItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    report_type = db.Column(db.String(10), nullable=False)
    category = db.Column(db.String(40), nullable=False)
    location = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(1000), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Open")
    image_filename = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=db.func.now())
def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        report_type = request.form.get("report_type", "")
        category = request.form.get("category", "")
        location = request.form.get("location", "").strip()
        description = request.form.get("description", "").strip()
        if not title or not location or not description:
            flash("Please fill in all required fields.", "error")
            return redirect(url_for("index"))
        if report_type not in {"Lost", "Found"} or category not in CATEGORIES:
            flash("Choose a valid report type and category.", "error")
            return redirect(url_for("index"))
        image = request.files.get("image")
        image_filename = None
        if image and image.filename:
            if not allowed_file(image.filename):
                flash("Upload a PNG, JPG, JPEG or WEBP image.", "error")
                return redirect(url_for("index"))
            safe_name = secure_filename(image.filename)
            image_filename = f"{uuid4().hex}_{safe_name}"
            image.save(UPLOAD_DIR / image_filename)
        item = LostItem(
            title=title,
            report_type=report_type,
            category=category,
            location=location,
            description=description,
            image_filename=image_filename,
        )
        db.session.add(item)
        db.session.commit()
        flash("Your report was submitted successfully.", "success")
        return redirect(url_for("index"))
    search = request.args.get("search", "").strip()
    report_type = request.args.get("type", "")
    active_tab = request.args.get("tab", "open")
    #status = request.args.get("status", "")
    if active_tab not in {"open", "resolved", "all"}:
        active_tab = "open"
    query = LostItem.query

    #the Open and Resolved tabs show only their respective reports
    if active_tab == "open":
        query = query.filter_by(status="Open")
    elif active_tab == "resolved":
        query = query.filter_by(status="Resolved")
    
    if search:
        term = f"%{search}%"
        query = query.filter(
            db.or_(
                LostItem.title.ilike(term),
                LostItem.location.ilike(term),
                LostItem.description.ilike(term),
            )
        )

    if report_type in {"Lost", "Found"}:
        query = query.filter_by(report_type=report_type)
    '''
    if status in {"Open", "Resolved"}:
        query = query.filter_by(status=status)
    '''
    items = query.order_by(LostItem.created_at.desc()).all()
    total = LostItem.query.count()
    open_count = LostItem.query.filter_by(status="Open").count()
    resolved_count = LostItem.query.filter_by(status="Resolved").count()
    return render_template(
        "index.html",
        items=items,
        categories=CATEGORIES,
        search=search,
        selected_type=report_type,
        active_tab=active_tab,
        #selected_status=status,
        total=total,
        open_count=open_count,
        resolved_count=resolved_count,
    )

@app.post("/items/<int:item_id>/status")
def update_status(item_id):
    item = db.get_or_404(LostItem, item_id)
    new_status = request.form.get("status", "")
    if new_status not in {"Open", "Resolved"}:
        flash("Invalid status.", "error")
        return redirect(url_for("index"))
    item.status = new_status
    db.session.commit()
    flash(f"Report #{item.id} updated to {new_status}.", "success")
    return redirect(url_for("index"))
with app.app_context():
    db.create_all()
if __name__ == "__main__":
    app.run(debug=False)