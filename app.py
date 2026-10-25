import os
from pathlib import Path
from uuid import uuid4
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from urllib.parse import quote_plus

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.environ["DB_NAME"]
DB_USER = quote_plus(os.environ["DB_USER"])
DB_PASSWORD = quote_plus(os.environ["DB_PASSWORD"])

app = Flask(__name__)

app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
)

app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY",
    "cloudfind-local-dev-key-change-before-deployment"
)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

S3_BUCKET = os.getenv("S3_BUCKET", "cloudfind-item-images-baalti")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN", "").strip()
s3 = boto3.client("s3", region_name=AWS_REGION)
sns = boto3.client("sns", region_name=AWS_REGION)
db = SQLAlchemy(app)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
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

def upload_image_to_s3(image):
    """Upload a validated image and return its S3 object key."""
    safe_name = secure_filename(image.filename)
    object_key = f"uploads/{uuid4().hex}_{safe_name}"
    content_type = (image.mimetype or "").lower()

    if content_type not in ALLOWED_MIME_TYPES:
        raise ValueError("The uploaded file must be a PNG, JPG, or WEBP image.")

    s3.upload_fileobj(
        image,
        S3_BUCKET,
        object_key,
        ExtraArgs={"ContentType": content_type},
    )
    return object_key


def image_url_for_item(item):
    if not item.image_filename:
        return None

    object_key = item.image_filename

    # Legacy images stored locally use a filename, not an S3 key.
    if not object_key.startswith("uploads/"):
        local_path = UPLOAD_DIR / object_key
        if local_path.is_file():
            return url_for(
                "static",
                filename=f"uploads/{object_key}",
            )

    # S3 images use a private, temporary URL.
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": S3_BUCKET,
                "Key": object_key,
            },
            ExpiresIn=3600,
        )
    except (BotoCoreError, ClientError):
        app.logger.exception(
            "Could not create image URL for report %s", item.id
        )
        return None


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

            try:
                image_filename = upload_image_to_s3(image)

            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("index"))

            except (BotoCoreError, ClientError) as exc:
                app.logger.exception("S3 image upload failed")

                error_code = "Unknown"
                if isinstance(exc, ClientError):
                    error_code = exc.response.get(
                        "Error", {}
                    ).get("Code", "Unknown")

                app.logger.error(
                    "S3 failure details: type=%s, code=%s, message=%s",
                    type(exc).__name__,
                    error_code,
                    str(exc),
                )

                flash(
                    f"Image upload failed ({error_code}). "
                    "Check application logs.",
                    "error",
                )
                return redirect(url_for("index"))

        item = LostItem(
            title=title,
            report_type=report_type,
            category=category,
            location=location,
            description=description,
            image_filename=image_filename,
        )

        try:
            db.session.add(item)
            db.session.commit()

        except Exception:
            db.session.rollback()

            # Delete the S3 object only if saving the database record failed.
            if image_filename:
                try:
                    s3.delete_object(
                        Bucket=S3_BUCKET,
                        Key=image_filename,
                    )
                except (BotoCoreError, ClientError):
                    app.logger.exception(
                        "Could not clean up unused S3 image"
                    )

            app.logger.exception("Failed to save lost-and-found report")
            flash(
                "Could not save your report. Check the application logs.",
                "error",
            )
            return redirect(url_for("index"))
        
        if SNS_TOPIC_ARN:
            try:
                sns.publish(
                    TopicArn=SNS_TOPIC_ARN,
                    Subject="New CloudFind Report",
                    Message=(
                        "A new lost-and-found report was submitted.\n\n"
                        f"Report ID: {item.id}\n"
                        f"Item: {item.title}\n"
                        f"Type: {item.report_type}\n"
                        f"Category: {item.category}\n"
                        f"Location: {item.location}\n"
                        f"Description: {item.description}\n"
                        f"Status: {item.status}"
                    ),
                )
                app.logger.info(
                    "SNS notification sent for report %s", item.id
                )
            except (BotoCoreError, ClientError):
                app.logger.exception(
                    "SNS notification failed for report %s", item.id
                )

        flash("Your report was submitted successfully.", "success")
        return redirect(url_for("index"))

    search = request.args.get("search", "").strip()
    report_type = request.args.get("type", "")
    active_tab = request.args.get("tab", "open")

    if active_tab not in {"open", "resolved", "all"}:
        active_tab = "open"

    query = LostItem.query

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

    items = query.order_by(LostItem.created_at.desc()).all()

    for item in items:
        item.display_image_url = image_url_for_item(item)

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
