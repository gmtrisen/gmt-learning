"""
GMT_Learning backend
--------------------
Flask app that:
  1. Serves the resource catalog
  2. Triggers an M-Pesa STK push via IntaSend when a buyer hits "Unlock"
  3. Receives IntaSend's webhook when payment completes
  4. Unlocks the download once payment is confirmed

Run locally:
    1. Copy .env.example to .env and fill in real values
    2. pip install -r requirements.txt
    3. flask --app app run --debug
   (.env is loaded automatically — no need to export variables manually)
"""

import os
import uuid
import secrets
from datetime import datetime
from functools import wraps

from dotenv import load_dotenv
load_dotenv()  # reads .env in this folder and sets the variables below automatically

from flask import (
    Flask, request, jsonify, render_template, abort,
    send_from_directory, session, redirect, url_for,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from intasend import Collect
from pypdf import PdfReader

# ---------------------------------------------------------------------------
# App + config
# ---------------------------------------------------------------------------

app = Flask(__name__)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")  # set this in your .env — required to log into /admin

# Used to sign the admin session cookie. MUST be set to a real random value
# in production (Render: set as an env var). Falls back to a random value
# per-process for local dev so it still works without extra setup.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 12  # 12 hours

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///gmt_learning.db")
# Render/Heroku-style Postgres URLs sometimes start with postgres:// — SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

INTASEND_PUBLISHABLE_KEY = os.environ.get("INTASEND_PUBLISHABLE_KEY", "")
INTASEND_SECRET_KEY = os.environ.get("INTASEND_SECRET_KEY", "")
INTASEND_TEST_MODE = os.environ.get("INTASEND_TEST_MODE", "true").lower() == "true"

RESOURCE_PRICE_KES = 100
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "protected_files"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_UPLOAD_MB = 25
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_LEVELS = [
    "PP1", "PP2", "Grade 1", "Grade 2", "Grade 3", "Grade 4", "Grade 5",
    "Grade 6", "Grade 7", "Grade 8", "Grade 9", "Grade 10", "Form 3", "Form 4",
]
ALLOWED_TYPES = ["Past Paper", "Notes", "Revision", "Assessment", "Exam"]


def get_intasend_collect():
    """Create an IntaSend Collect client. Raises clearly if keys are missing."""
    if not INTASEND_SECRET_KEY or not INTASEND_PUBLISHABLE_KEY:
        raise RuntimeError(
            "INTASEND_SECRET_KEY / INTASEND_PUBLISHABLE_KEY are not set. "
            "Set them as environment variables before starting the server."
        )
    return Collect(
        token=INTASEND_SECRET_KEY,
        publishable_key=INTASEND_PUBLISHABLE_KEY,
        test=INTASEND_TEST_MODE,
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Resource(db.Model):
    """A single downloadable item: a past paper, notes set, revision pack, etc."""
    __tablename__ = "resources"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    level = db.Column(db.String(50), nullable=False)        # e.g. "Grade 6", "Form 3", "PP1"
    subject = db.Column(db.String(80), nullable=False)
    resource_type = db.Column(db.String(40), nullable=False)  # Past Paper / Notes / Revision / Assessment / Exam
    description = db.Column(db.String(300), default="")
    page_count = db.Column(db.Integer, default=0)
    file_path = db.Column(db.String(300), nullable=False)    # path inside UPLOAD_DIR, never served directly
    price_kes = db.Column(db.Integer, default=RESOURCE_PRICE_KES)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "level": self.level,
            "subject": self.subject,
            "resource_type": self.resource_type,
            "description": self.description,
            "page_count": self.page_count,
            "price_kes": self.price_kes,
        }


class Order(db.Model):
    """
    One purchase attempt for one resource by one phone number.
    Tracks the full lifecycle: pending -> paid / failed.
    """
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    order_ref = db.Column(db.String(64), unique=True, nullable=False, index=True)  # shown to buyer, used in polling
    resource_id = db.Column(db.Integer, db.ForeignKey("resources.id"), nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)
    amount_kes = db.Column(db.Integer, nullable=False)

    intasend_invoice_id = db.Column(db.String(120))   # returned by IntaSend on STK push
    status = db.Column(db.String(20), default="pending")  # pending / paid / failed / expired
    download_token = db.Column(db.String(64), unique=True)  # generated once paid; required to download

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            "order_ref": self.order_ref,
            "status": self.status,
            "amount_kes": self.amount_kes,
            "download_token": self.download_token if self.status == "paid" else None,
        }


# ---------------------------------------------------------------------------
# Catalog routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/resources")
def list_resources():
    """List active resources, optionally filtered by level/subject/type."""
    query = Resource.query.filter_by(is_active=True)

    level = request.args.get("level")
    subject = request.args.get("subject")
    resource_type = request.args.get("type")

    if level:
        query = query.filter_by(level=level)
    if subject:
        query = query.filter_by(subject=subject)
    if resource_type:
        query = query.filter_by(resource_type=resource_type)

    resources = query.order_by(Resource.created_at.desc()).all()
    return jsonify([r.to_dict() for r in resources])


@app.route("/api/resources/<int:resource_id>")
def get_resource(resource_id):
    resource = Resource.query.get_or_404(resource_id)
    return jsonify(resource.to_dict())


# ---------------------------------------------------------------------------
# Checkout routes — this is the part that talks to IntaSend
# ---------------------------------------------------------------------------

@app.route("/api/checkout/start", methods=["POST"])
def start_checkout():
    """
    Step 1 of the buy flow.
    Buyer has picked a resource and entered their phone number.
    We trigger an STK push and create a pending Order to track it.

    Expects JSON: { "resource_id": 1, "phone_number": "0712345678" }
    Returns: { "order_ref": "...", "status": "pending" }
    """
    data = request.get_json(silent=True) or {}
    resource_id = data.get("resource_id")
    phone_number = (data.get("phone_number") or "").strip()

    if not resource_id or not phone_number:
        return jsonify({"error": "resource_id and phone_number are required"}), 400

    resource = Resource.query.get(resource_id)
    if not resource or not resource.is_active:
        return jsonify({"error": "Resource not found or no longer available"}), 404

    phone_number = normalize_phone(phone_number)
    if not phone_number:
        return jsonify({"error": "Enter a valid Safaricom number, e.g. 0712345678"}), 400

    order_ref = f"GMT-{uuid.uuid4().hex[:10].upper()}"

    order = Order(
        order_ref=order_ref,
        resource_id=resource.id,
        phone_number=phone_number,
        amount_kes=resource.price_kes,
        status="pending",
    )
    db.session.add(order)
    db.session.commit()

    try:
        collect = get_intasend_collect()
        response = collect.mpesa_stk_push(
            phone_number=phone_number,
            amount=resource.price_kes,
            narrative=f"GMT_Learning: {resource.title[:40]}",
            api_ref=order_ref,
        )
        # IntaSend returns an invoice object containing an invoice_id we poll later
        invoice_id = (response.get("invoice") or {}).get("invoice_id") or response.get("id")
        order.intasend_invoice_id = invoice_id
        db.session.commit()
    except Exception as exc:
        order.status = "failed"
        db.session.commit()
        return jsonify({"error": f"Could not start payment: {exc}"}), 502

    return jsonify({"order_ref": order.order_ref, "status": order.status})


@app.route("/api/checkout/status/<order_ref>")
def checkout_status(order_ref):
    """
    Step 2: frontend polls this every couple of seconds while the buyer
    is approving the STK push on their phone.
    """
    order = Order.query.filter_by(order_ref=order_ref).first_or_404()

    # If we're still pending and have an invoice id, double-check with IntaSend directly.
    # This protects against a missed/late webhook.
    if order.status == "pending" and order.intasend_invoice_id:
        try:
            collect = get_intasend_collect()
            result = collect.status(invoice_id=order.intasend_invoice_id)
            remote_state = (result.get("invoice") or {}).get("state", "").upper()
            apply_remote_state(order, remote_state)
        except Exception:
            # Don't fail the poll just because a live re-check failed;
            # the webhook may still resolve it.
            pass

    return jsonify(order.to_dict())


@app.route("/api/webhooks/intasend", methods=["POST"])
def intasend_webhook():
    """
    IntaSend calls this URL automatically when a payment's state changes.
    This is the reliable, server-to-server confirmation — polling above is
    just a fallback for a snappier UI while we wait for this to land.

    NOTE: In production, verify the request signature/challenge IntaSend
    sends (see their webhook docs) before trusting this payload.
    """
    payload = request.get_json(silent=True) or {}

    invoice_id = payload.get("invoice_id") or (payload.get("invoice") or {}).get("invoice_id")
    state = (payload.get("state") or payload.get("status") or "").upper()

    if not invoice_id:
        return jsonify({"error": "missing invoice_id"}), 400

    order = Order.query.filter_by(intasend_invoice_id=invoice_id).first()
    if not order:
        # Nothing to do — could be a webhook for an order not in our DB yet (race condition).
        return jsonify({"received": True}), 200

    apply_remote_state(order, state)
    return jsonify({"received": True}), 200


def apply_remote_state(order, remote_state):
    """Map IntaSend's payment state onto our Order, exactly once."""
    if order.status == "paid":
        return  # already settled, never downgrade

    if remote_state in ("COMPLETE", "COMPLETED", "SUCCESS", "PAID"):
        order.status = "paid"
        order.paid_at = datetime.utcnow()
        order.download_token = uuid.uuid4().hex
        db.session.commit()
    elif remote_state in ("FAILED", "CANCELLED"):
        order.status = "failed"
        db.session.commit()
    # any other state (PENDING, PROCESSING) -> leave as pending


# ---------------------------------------------------------------------------
# Download route — only works once an order is actually marked "paid"
# ---------------------------------------------------------------------------

@app.route("/api/download/<order_ref>/<download_token>")
def download_resource(order_ref, download_token):
    order = Order.query.filter_by(order_ref=order_ref).first_or_404()

    if order.status != "paid" or order.download_token != download_token:
        abort(403)

    resource = Resource.query.get_or_404(order.resource_id)
    return send_from_directory(
        UPLOAD_DIR,
        resource.file_path,
        as_attachment=True,
        download_name=f"{resource.title}.pdf",
    )


# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------

def admin_required(view_func):
    """Gate a view behind the admin session. Redirects HTML pages to login;
    returns 401 JSON for API calls."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            if request.path.startswith("/admin/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("admin_login"))
        return view_func(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        return render_template("admin_login.html", error=None)

    if not ADMIN_PASSWORD:
        return render_template(
            "admin_login.html",
            error="ADMIN_PASSWORD is not set on the server — set it as an environment variable first.",
        )

    submitted = request.form.get("password", "")
    if submitted and secrets.compare_digest(submitted, ADMIN_PASSWORD):
        session["is_admin"] = True
        session.permanent = True
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_login.html", error="Incorrect password.")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


# ---------------------------------------------------------------------------
# Admin pages
# ---------------------------------------------------------------------------

@app.route("/admin")
@admin_required
def admin_dashboard():
    return render_template(
        "admin_dashboard.html",
        levels=ALLOWED_LEVELS,
        types=ALLOWED_TYPES,
    )


@app.route("/admin/orders")
@admin_required
def admin_orders():
    return render_template("admin_orders.html")


# ---------------------------------------------------------------------------
# Admin API — resource management
# ---------------------------------------------------------------------------

@app.route("/admin/api/resources")
@admin_required
def admin_list_resources():
    """Returns ALL resources (active + inactive) for the admin table."""
    resources = Resource.query.order_by(Resource.created_at.desc()).all()
    return jsonify([{**r.to_dict(), "is_active": r.is_active} for r in resources])


@app.route("/admin/api/resources", methods=["POST"])
@admin_required
def admin_create_resource():
    """
    Upload a new resource. Expects multipart/form-data:
      file, title, level, subject, resource_type, description, price_kes
    """
    file = request.files.get("file")
    title = (request.form.get("title") or "").strip()
    level = (request.form.get("level") or "").strip()
    subject = (request.form.get("subject") or "").strip()
    resource_type = (request.form.get("resource_type") or "").strip()
    description = (request.form.get("description") or "").strip()
    price_raw = request.form.get("price_kes", str(RESOURCE_PRICE_KES))

    errors = []
    if not file or file.filename == "":
        errors.append("A PDF file is required.")
    elif not file.filename.lower().endswith(".pdf"):
        errors.append("Only PDF files are accepted.")
    if not title:
        errors.append("Title is required.")
    if level not in ALLOWED_LEVELS:
        errors.append("Choose a valid level.")
    if resource_type not in ALLOWED_TYPES:
        errors.append("Choose a valid resource type.")
    if not subject:
        errors.append("Subject is required.")

    try:
        price_kes = int(price_raw)
        if price_kes <= 0:
            errors.append("Price must be a positive number.")
    except (TypeError, ValueError):
        errors.append("Price must be a whole number.")
        price_kes = RESOURCE_PRICE_KES

    if errors:
        return jsonify({"error": " ".join(errors)}), 400

    safe_name = secure_filename(file.filename)
    stored_name = f"{uuid.uuid4().hex}_{safe_name}"
    full_path = os.path.join(UPLOAD_DIR, stored_name)
    file.save(full_path)

    page_count = 0
    try:
        page_count = len(PdfReader(full_path).pages)
    except Exception:
        # Not fatal — a corrupt/encrypted PDF still gets stored,
        # admin can fix page_count manually if it matters.
        pass

    resource = Resource(
        title=title,
        level=level,
        subject=subject,
        resource_type=resource_type,
        description=description,
        page_count=page_count,
        file_path=stored_name,
        price_kes=price_kes,
        is_active=True,
    )
    db.session.add(resource)
    db.session.commit()

    return jsonify(resource.to_dict()), 201


@app.route("/admin/api/resources/<int:resource_id>", methods=["PATCH"])
@admin_required
def admin_update_resource(resource_id):
    """Edit metadata or toggle active/inactive. JSON body, any subset of fields."""
    resource = Resource.query.get_or_404(resource_id)
    data = request.get_json(silent=True) or {}

    if "title" in data:
        title = (data["title"] or "").strip()
        if not title:
            return jsonify({"error": "Title cannot be empty."}), 400
        resource.title = title
    if "level" in data:
        if data["level"] not in ALLOWED_LEVELS:
            return jsonify({"error": "Invalid level."}), 400
        resource.level = data["level"]
    if "subject" in data:
        subject = (data["subject"] or "").strip()
        if not subject:
            return jsonify({"error": "Subject cannot be empty."}), 400
        resource.subject = subject
    if "resource_type" in data:
        if data["resource_type"] not in ALLOWED_TYPES:
            return jsonify({"error": "Invalid resource type."}), 400
        resource.resource_type = data["resource_type"]
    if "description" in data:
        resource.description = (data["description"] or "").strip()
    if "price_kes" in data:
        try:
            price = int(data["price_kes"])
            if price <= 0:
                raise ValueError
            resource.price_kes = price
        except (TypeError, ValueError):
            return jsonify({"error": "Price must be a positive whole number."}), 400
    if "is_active" in data:
        resource.is_active = bool(data["is_active"])

    db.session.commit()
    return jsonify({**resource.to_dict(), "is_active": resource.is_active})


@app.route("/admin/api/resources/<int:resource_id>", methods=["DELETE"])
@admin_required
def admin_delete_resource(resource_id):
    """
    Hard-deletes the catalog entry. Does NOT delete past orders that
    reference it (so sales history stays intact) — it just stops new
    purchases. The underlying file is removed from disk too.
    """
    resource = Resource.query.get_or_404(resource_id)

    file_path = os.path.join(UPLOAD_DIR, resource.file_path)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass  # don't block deletion of the DB record on a filesystem hiccup

    db.session.delete(resource)
    db.session.commit()
    return jsonify({"deleted": True})


# ---------------------------------------------------------------------------
# Admin API — orders / sales view
# ---------------------------------------------------------------------------

@app.route("/admin/api/orders")
@admin_required
def admin_list_orders():
    """
    Returns recent orders joined with resource titles, newest first.
    Supports ?status=paid / pending / failed filtering.
    """
    query = Order.query.join(Resource, Order.resource_id == Resource.id)

    status = request.args.get("status")
    if status:
        query = query.filter(Order.status == status)

    orders = query.order_by(Order.created_at.desc()).limit(200).all()

    result = []
    for o in orders:
        resource = Resource.query.get(o.resource_id)
        result.append({
            "order_ref": o.order_ref,
            "resource_title": resource.title if resource else "(deleted resource)",
            "phone_number": o.phone_number,
            "amount_kes": o.amount_kes,
            "status": o.status,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "paid_at": o.paid_at.isoformat() if o.paid_at else None,
        })

    paid_total = sum(o.amount_kes for o in orders if o.status == "paid")
    return jsonify({"orders": result, "paid_total_kes": paid_total})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_phone(raw):
    """
    Accepts 07xxxxxxxx, 01xxxxxxxx, 254 7xxxxxxxx, 254 1xxxxxxxx,
    +254 7xxxxxxxx, +254 1xxxxxxxx and returns the 2547xxxxxxxx /
    2541xxxxxxxx format IntaSend expects, or None if the prefix
    doesn't match a valid Kenyan mobile range (7 or 1 after the
    country/trunk code — covers Safaricom, Airtel, Telkom).
    """
    digits = "".join(ch for ch in raw if ch.isdigit())

    if digits.startswith("254") and len(digits) == 12:
        core = digits[3:]
    elif digits.startswith("0") and len(digits) == 10:
        core = digits[1:]
    elif len(digits) == 9:
        core = digits
    else:
        return None

    if core[0] in ("7", "1"):
        return "254" + core
    return None


# ---------------------------------------------------------------------------
# CLI helper: seed a couple of sample resources for local testing
# ---------------------------------------------------------------------------

@app.cli.command("seed")
def seed():
    """Run with: flask --app app seed"""
    db.create_all()
    if Resource.query.count() == 0:
        sample = Resource(
            title="Grade 6 Mathematics — End Term 2",
            level="Grade 6",
            subject="Mathematics",
            resource_type="Past Paper",
            description="Full end-of-term past paper with marking scheme.",
            page_count=12,
            file_path="sample_resource.pdf",
            price_kes=RESOURCE_PRICE_KES,
        )
        db.session.add(sample)
        db.session.commit()
        print("Seeded 1 sample resource.")
    else:
        print("Resources already exist, skipping seed.")


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(debug=True)
