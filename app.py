import os
import json
from datetime import datetime
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
EXPORT_FOLDER = os.path.join(BASE_DIR, "exports")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EXPORT_FOLDER, exist_ok=True)

ALLOWED_EXT = {"csv", "xlsx", "xls"}

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["EXPORT_FOLDER"] = EXPORT_FOLDER
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = "replace-this-with-a-secure-random-secret"

db = SQLAlchemy(app)


# --------------------
# DB Models
# --------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    config_json = db.Column(db.Text, nullable=False)  # stores chart configs & filters
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# --------------------
# Helpers
# --------------------
def allowed_file(filename):
    ext = filename.rsplit(".", 1)[-1].lower()
    return "." in filename and ext in ALLOWED_EXT


def read_table(filepath):
    ext = filepath.rsplit(".", 1)[-1].lower()
    if ext in ("xls", "xlsx"):
        df = pd.read_excel(filepath)
    else:
        df = pd.read_csv(filepath)
    return df


# --------------------
# Routes: Auth (simple)
# --------------------
@app.route("/register", methods=["POST"])
def register():
    data = request.json or {}
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "username exists"}), 400
    u = User(username=username, password_hash=generate_password_hash(password))
    db.session.add(u)
    db.session.commit()
    session["user_id"] = u.id
    session["username"] = u.username
    return jsonify({"ok": True, "username": u.username})


@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    u = User.query.filter_by(username=username).first()
    if not u or not check_password_hash(u.password_hash, password):
        return jsonify({"error": "invalid credentials"}), 400
    session["user_id"] = u.id
    session["username"] = u.username
    return jsonify({"ok": True, "username": u.username})


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


# --------------------
# Serve frontend
# --------------------
@app.route("/")
def index():
    return render_template("index.html")


# --------------------
# Upload endpoint (drag-drop or file input)
# --------------------
@app.route("/api/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "no file provided"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "empty filename"}), 400
    if not allowed_file(f.filename):
        return jsonify({"error": "unsupported file type"}), 400
    filename = secure_filename(f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{f.filename}")
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    f.save(path)
    # quick read to return preview columns & first few rows
    try:
        df = read_table(path)
        cols = df.columns.tolist()
        preview = df.head(8).fillna("").to_dict(orient="records")
        return jsonify({"ok": True, "file": filename, "columns": cols, "preview": preview})
    except Exception as e:
        return jsonify({"error": f"unable to read file: {e}"}), 400


# --------------------
# Get columns for file
# --------------------
@app.route("/api/columns", methods=["POST"])
def get_columns():
    data = request.json or {}
    filename = data.get("file")
    if not filename:
        return jsonify({"error": "file required"}), 400
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    if not os.path.exists(path):
        return jsonify({"error": "file not found"}), 404
    df = read_table(path)
    return jsonify({"columns": df.columns.tolist(), "rows": len(df)})


# --------------------
# Generate filtered chart arrays (applies simple filters before returning arrays)
# POST json: {file, xcol, ycol, filters:{col:{type:'range'/'text', min, max, text}} }
# --------------------
@app.route("/api/generate_chart", methods=["POST"])
def generate_chart():
    data = request.json or {}
    filename = data.get("file")
    xcol = data.get("xcol")
    ycol = data.get("ycol")
    filters = data.get("filters", {})
    if not filename or not xcol or not ycol:
        return jsonify({"error": "file,xcol,ycol required"}), 400
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    if not os.path.exists(path):
        return jsonify({"error": "file not found"}), 404
    df = read_table(path)
    # Apply filters
    try:
        for col, fconf in filters.items():
            if col not in df.columns:
                continue
            if fconf.get("type") == "range":
                mn = fconf.get("min")
                mx = fconf.get("max")
                if mn is not None:
                    df = df[df[col].astype(float) >= float(mn)]
                if mx is not None:
                    df = df[df[col].astype(float) <= float(mx)]
            elif fconf.get("type") == "text":
                txt = fconf.get("text", "")
                if txt:
                    df = df[df[col].astype(str).str.contains(txt, case=False, na=False)]
    except Exception as e:
        # If filter failed due to conversion, skip filter but warn
        return jsonify({"error": f"filtering error: {e}"}), 400
    # Prepare arrays
    xvals = df[xcol].astype(str).tolist()
    yvals = df[ycol].tolist()
    return jsonify({"x": xvals, "y": yvals, "rows": len(df)})


# --------------------
# Save project: stores name, owner, file_path, and config_json
# POST json: {name, file, config}
# --------------------
@app.route("/api/save_project", methods=["POST"])
def save_project():
    if "user_id" not in session:
        return jsonify({"error": "authentication required"}), 401
    data = request.json or {}
    name = data.get("name", "Untitled")
    filename = data.get("file")
    config = data.get("config", {})
    if not filename:
        return jsonify({"error": "file required"}), 400
    # verify file exists
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    if not os.path.exists(path):
        return jsonify({"error": "file not found"}), 404
    p = Project(owner_id=session["user_id"], name=name, file_path=filename, config_json=json.dumps(config))
    db.session.add(p)
    db.session.commit()
    return jsonify({"ok": True, "project_id": p.id})


# --------------------
# List projects for user
# --------------------
@app.route("/api/list_projects", methods=["GET"])
def list_projects():
    if "user_id" not in session:
        return jsonify({"error": "authentication required"}), 401
    projs = Project.query.filter_by(owner_id=session["user_id"]).order_by(Project.created_at.desc()).all()
    out = []
    for p in projs:
        out.append({"id": p.id, "name": p.name, "file": p.file_path, "created_at": p.created_at.isoformat()})
    return jsonify({"projects": out})


# --------------------
# Load project
# --------------------
@app.route("/api/load_project/<int:proj_id>", methods=["GET"])
def load_project(proj_id):
    if "user_id" not in session:
        return jsonify({"error": "authentication required"}), 401
    p = Project.query.get(proj_id)
    if not p or p.owner_id != session["user_id"]:
        return jsonify({"error": "project not found or access denied"}), 404
    return jsonify({
        "id": p.id,
        "name": p.name,
        "file": p.file_path,
        "config": json.loads(p.config_json),
        "created_at": p.created_at.isoformat()
    })


# --------------------
# Save exported chart PNG (POST with JSON: {filename, dataURL})
# --------------------
@app.route("/api/save_png", methods=["POST"])
def save_png():
    data = request.json or {}
    name = data.get("name", f"chart_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.png")
    dataurl = data.get("dataURL")
    if not dataurl:
        return jsonify({"error": "dataURL required"}), 400
    # dataURL looks like "data:image/png;base64,AAAA..."
    header, b64 = dataurl.split(",", 1)
    import base64
    raw = base64.b64decode(b64)
    safe_name = secure_filename(name)
    path = os.path.join(app.config["EXPORT_FOLDER"], safe_name)
    with open(path, "wb") as f:
        f.write(raw)
    # return a path that can be retrieved by /exports/<filename>
    return jsonify({"ok": True, "url": f"/exports/{safe_name}"})


@app.route("/exports/<path:filename>")
def serve_export(filename):
    return send_from_directory(app.config["EXPORT_FOLDER"], filename, as_attachment=False)


# --------------------
# Utility: serve uploaded files (read-only) - caution: in prod use proper auth & storage
# --------------------
@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=False)


# --------------------
# Bootstrap DB & run
# --------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000)
